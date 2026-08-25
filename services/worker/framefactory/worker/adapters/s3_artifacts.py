"""S3-compatible immutable artifact publication for provider outputs."""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol
from uuid import NAMESPACE_URL, uuid5

from framefactory.runtime import PermanentStepError, RetryableStepError
from framefactory.steps import ArtifactRef, StepContext
from framefactory.worker.config import ObjectStorageSettings
from framefactory.worker.providers import ProviderArtifact

logger = logging.getLogger(__name__)


class ArtifactRecorder(Protocol):
    def record_artifact(self, artifact: ArtifactRef, *, bucket: str) -> None: ...


class S3ArtifactStorage:
    def __init__(
        self,
        settings: ObjectStorageSettings,
        *,
        recorder: ArtifactRecorder,
        client: Any | None = None,
    ) -> None:
        self.settings = settings
        self.recorder = recorder
        self.client = client or self._client(settings)

    def healthcheck(self) -> None:
        try:
            self.client.head_bucket(Bucket=self.settings.bucket)
        except Exception as exc:
            raise RuntimeError("configured artifact bucket is unavailable") from exc

    def publish(
        self,
        context: StepContext,
        artifact: ProviderArtifact,
        *,
        attempt_scoped: bool = False,
    ) -> ArtifactRef:
        digest = hashlib.sha256(artifact.data).hexdigest()
        attempt_identity = f":attempt:{context.attempt}" if attempt_scoped else ""
        artifact_id = uuid5(
            NAMESPACE_URL,
            f"framefactory-artifact:{context.workspace_id}:{context.run_id}:"
            f"{context.step_id}:{artifact.kind}:{artifact.filename}{attempt_identity}:{digest}",
        )
        key = self._key(
            f"workspaces/{context.workspace_id}/runs/{context.run_id}/artifacts/"
            f"{artifact_id}/{artifact.filename}"
        )
        metadata = {
            "sha256": digest,
            "workspace-id": context.workspace_id,
            "run-id": context.run_id,
        }
        object_created = False
        try:
            self.client.put_object(
                Bucket=self.settings.bucket,
                Key=key,
                Body=artifact.data,
                ContentType=artifact.media_type,
                Metadata=metadata,
                IfNoneMatch="*",
            )
            object_created = True
        except Exception as exc:
            response = getattr(exc, "response", {})
            code = str(response.get("Error", {}).get("Code", ""))
            status = response.get("ResponseMetadata", {}).get("HTTPStatusCode")
            if (
                code in {"PreconditionFailed", "ConditionalRequestConflict"}
                or status == 412
            ):
                self._verify_existing(key, digest)
            elif status in {408, 409, 425, 429} or (
                isinstance(status, int) and status >= 500
            ):
                raise RetryableStepError(
                    "artifact object storage is temporarily unavailable"
                ) from exc
            else:
                raise PermanentStepError(
                    "artifact publication was rejected by object storage"
                ) from exc

        reference = ArtifactRef(
            id=str(artifact_id),
            workspace_id=context.workspace_id,
            run_id=context.run_id,
            # ArtifactRef is a runtime-domain value, so retain the stable
            # worker step identifier here.  The PostgreSQL adapter alone owns
            # the mapping to run_steps.id; pre-mapping at this boundary caused
            # it to hash the UUID a second time and violate the artifact FK.
            step_id=context.step_id,
            kind=artifact.kind,
            media_type=artifact.media_type,
            object_key=key,
            byte_size=len(artifact.data),
            content_hash=digest,
            filename=artifact.filename,
            created_at=datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        )
        try:
            self.recorder.record_artifact(reference, bucket=self.settings.bucket)
        except Exception:
            # S3 and PostgreSQL cannot share a transaction. Delete only when
            # this call proved that it created the object; never delete a
            # pre-existing immutable object that may already have a durable row.
            if object_created:
                self._delete_unrecorded(key)
            raise
        return reference

    def read_json(self, artifact: ArtifactRef) -> Mapping[str, Any]:
        data = self.read_bytes(artifact)
        try:
            value = json.loads(data)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise PermanentStepError("input artifact is not valid JSON") from exc
        if not isinstance(value, Mapping):
            raise PermanentStepError("input artifact JSON must be an object")
        return value

    def read_bytes(self, artifact: ArtifactRef) -> bytes:
        try:
            response = self.client.get_object(
                Bucket=self.settings.bucket,
                Key=artifact.object_key,
            )
            data = response["Body"].read()
        except Exception as exc:
            raise RetryableStepError(
                "input artifact could not be read from object storage"
            ) from exc
        if hashlib.sha256(data).hexdigest() != artifact.content_hash:
            raise PermanentStepError(
                "input artifact content hash does not match its durable reference"
            )
        return data

    def materialize(self, artifact: ArtifactRef, destination: Path) -> None:
        """Stream a potentially large artifact to disk and verify its digest."""
        body: Any | None = None
        digest = hashlib.sha256()
        byte_size = 0
        try:
            response = self.client.get_object(
                Bucket=self.settings.bucket,
                Key=artifact.object_key,
            )
            body = response["Body"]
            destination.parent.mkdir(parents=True, exist_ok=True)
            with destination.open("wb") as output:
                while True:
                    chunk = body.read(8 * 1024 * 1024)
                    if not chunk:
                        break
                    output.write(chunk)
                    digest.update(chunk)
                    byte_size += len(chunk)
        except Exception as exc:
            destination.unlink(missing_ok=True)
            raise RetryableStepError(
                "input artifact could not be materialized from object storage"
            ) from exc
        finally:
            close = getattr(body, "close", None)
            if callable(close):
                close()
        if (
            byte_size != artifact.byte_size
            or digest.hexdigest() != artifact.content_hash
        ):
            destination.unlink(missing_ok=True)
            raise PermanentStepError(
                "input artifact content hash does not match its durable reference"
            )

    def publish_copy(
        self,
        context: StepContext,
        *,
        kind: str,
        filename: str,
        media_type: str,
        source_bucket: str,
        source_key: str,
        content_hash: str,
        byte_size: int,
    ) -> ArtifactRef:
        """Publish an existing immutable S3 object without routing bytes through Worker."""
        artifact_id = uuid5(
            NAMESPACE_URL,
            f"framefactory-artifact:{context.workspace_id}:{context.run_id}:"
            f"{context.step_id}:{kind}:{filename}:{content_hash}",
        )
        key = self._key(
            f"workspaces/{context.workspace_id}/runs/{context.run_id}/artifacts/"
            f"{artifact_id}/{filename}"
        )
        metadata = {
            "sha256": content_hash,
            "workspace-id": context.workspace_id,
            "run-id": context.run_id,
        }
        try:
            self.client.copy_object(
                Bucket=self.settings.bucket,
                Key=key,
                CopySource={"Bucket": source_bucket, "Key": source_key},
                ContentType=media_type,
                Metadata=metadata,
                MetadataDirective="REPLACE",
            )
            existing = self.client.head_object(Bucket=self.settings.bucket, Key=key)
            if existing.get("Metadata", {}).get("sha256") != content_hash:
                raise PermanentStepError(
                    "immutable artifact key contains different content"
                )
            if int(existing.get("ContentLength", -1)) != byte_size:
                raise PermanentStepError(
                    "immutable artifact key contains a different byte size"
                )
        except PermanentStepError:
            raise
        except Exception as exc:
            raise RetryableStepError(
                "catalog asset could not be copied in object storage"
            ) from exc

        reference = ArtifactRef(
            id=str(artifact_id),
            workspace_id=context.workspace_id,
            run_id=context.run_id,
            step_id=context.step_id,
            kind=kind,
            media_type=media_type,
            object_key=key,
            byte_size=byte_size,
            content_hash=content_hash,
            filename=filename,
            created_at=datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        )
        self.recorder.record_artifact(reference, bucket=self.settings.bucket)
        return reference

    def _verify_existing(self, key: str, digest: str) -> None:
        try:
            head = self.client.head_object(Bucket=self.settings.bucket, Key=key)
        except Exception as exc:
            raise RetryableStepError("existing artifact could not be verified") from exc
        if head.get("Metadata", {}).get("sha256") != digest:
            raise PermanentStepError(
                "immutable artifact key contains different content"
            )

    def _delete_unrecorded(self, key: str) -> None:
        try:
            self.client.delete_object(Bucket=self.settings.bucket, Key=key)
        except Exception:
            logger.warning(
                "could not remove artifact object after durable record failure",
                extra={"bucket": self.settings.bucket, "object_key": key},
                exc_info=True,
            )

    def _key(self, relative: str) -> str:
        prefix = self.settings.key_prefix
        return f"{prefix}/{relative}" if prefix else relative

    @staticmethod
    def _client(settings: ObjectStorageSettings) -> Any:
        try:
            import boto3
            from botocore.config import Config
        except ImportError as exc:  # pragma: no cover - packaging error
            raise RuntimeError("S3 artifact publication requires boto3") from exc
        return boto3.client(
            "s3",
            endpoint_url=settings.endpoint_url,
            region_name=settings.region,
            aws_access_key_id=settings.access_key_id,
            aws_secret_access_key=settings.secret_access_key,
            config=Config(signature_version="s3v4", s3={"addressing_style": "path"}),
        )
