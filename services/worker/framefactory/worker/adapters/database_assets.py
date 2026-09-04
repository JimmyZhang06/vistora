"""Scene-level selection from the durable, rights-aware v3 asset catalog."""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import re
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import psycopg
from psycopg.rows import dict_row

from framefactory.runtime import PermanentStepError, RetryableStepError
from framefactory.steps import ArtifactRef, StepContext, StepResult
from framefactory.worker.asset_acquisition_plan import plan_acquisition_slots
from framefactory.worker.config import AssetLibrarySettings
from framefactory.worker.providers import ArtifactStorage, ProviderArtifact
from framefactory.worker.semantic_text import ordered_visual_beats


@dataclass(frozen=True, slots=True)
class AssetMatch:
    asset_id: str
    title: str
    bucket: str
    object_key: str
    media_type: str
    content_hash: str
    byte_size: int
    original_filename: str
    start_ms: int
    end_ms: int
    description: str
    labels: tuple[str, ...]
    score: float
    transcript: str = ""
    cut_safe: bool = False
    semantic_complete: bool = False


SearchAssets = Callable[[str, tuple[str, ...], str, int, float], Sequence[AssetMatch]]
SegmentKey = tuple[str, int, int]


@dataclass(frozen=True, slots=True)
class AssetAcquisitionResult:
    imported_count: int
    unresolved_queries: tuple[str, ...]
    provider_errors: tuple[Mapping[str, Any], ...] = ()
    asset_ids: tuple[str, ...] = ()


class AssetAcquirer(Protocol):
    async def acquire(
        self,
        *,
        workspace_id: str,
        run_id: str | None,
        step_id: str | None,
        library_id: str,
        queries: tuple[str, ...],
        sources: tuple[str, ...],
        max_assets: int,
        copyright_status: str,
        idempotency_scope: str | None = None,
    ) -> AssetAcquisitionResult: ...


class ControlApiAssetAcquirer:
    """Small fail-closed client for the control plane acquisition endpoint."""

    def __init__(self, base_url: str, *, timeout_seconds: float = 600.0) -> None:
        self._url = base_url.rstrip("/") + "/v1/asset-acquisitions"
        self._timeout_seconds = timeout_seconds

    async def acquire(
        self,
        *,
        workspace_id: str,
        run_id: str | None,
        step_id: str | None,
        library_id: str,
        queries: tuple[str, ...],
        sources: tuple[str, ...],
        max_assets: int,
        copyright_status: str,
        idempotency_scope: str | None = None,
    ) -> AssetAcquisitionResult:
        payload = {
            "library_id": library_id,
            "queries": list(queries),
            "sources": list(sources),
            "max_assets": max_assets,
            "copyright_status": copyright_status,
            "rights_confirmed": True,
        }
        if run_id is not None:
            payload["run_id"] = run_id
        if step_id is not None:
            payload["step_id"] = step_id
        if idempotency_scope is not None and (
            not idempotency_scope.strip() or len(idempotency_scope) > 255
        ):
            raise ValueError("asset acquisition idempotency scope is invalid")
        digest_input: Mapping[str, Any] = payload
        if idempotency_scope is not None:
            # The scope separates independent durable jobs without leaking a
            # worker-only coordination key into the public request contract.
            digest_input = {
                "payload": payload,
                "idempotency_scope": idempotency_scope,
            }
        digest = hashlib.sha256(
            json.dumps(digest_input, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()[:32]
        request = urllib.request.Request(
            self._url,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "Idempotency-Key": f"worker-assets-{digest}",
                "X-Workspace-Id": workspace_id,
            },
            method="POST",
        )
        try:
            body = await asyncio.to_thread(self._send, request)
            value = json.loads(body)
        except (TimeoutError, urllib.error.URLError, OSError) as exc:
            raise RetryableStepError(
                "automatic asset acquisition is temporarily unavailable",
                retry_after_seconds=30,
            ) from exc
        except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
            raise PermanentStepError("automatic asset acquisition returned an invalid response") from exc
        if not isinstance(value, Mapping):
            raise PermanentStepError("automatic asset acquisition returned an invalid response")
        unresolved = value.get("unresolved_queries", [])
        errors = value.get("provider_errors", [])
        imported_assets = value.get("imported_assets", [])
        return AssetAcquisitionResult(
            imported_count=max(0, int(value.get("imported_count", 0))),
            unresolved_queries=tuple(str(item) for item in unresolved if str(item).strip()),
            provider_errors=tuple(item for item in errors if isinstance(item, Mapping)),
            asset_ids=tuple(
                dict.fromkeys(
                    str(item.get("id") or "").strip()
                    for item in imported_assets
                    if isinstance(item, Mapping) and str(item.get("id") or "").strip()
                )
            ),
        )

    def _send(self, request: urllib.request.Request) -> bytes:
        try:
            with urllib.request.urlopen(request, timeout=self._timeout_seconds) as response:
                return response.read(2_097_153)
        except urllib.error.HTTPError as exc:
            if exc.code >= 500 or exc.code in {408, 409, 425, 429}:
                raise RetryableStepError(
                    "automatic asset acquisition is temporarily unavailable",
                    retry_after_seconds=30,
                ) from exc
            raise PermanentStepError("automatic asset acquisition request was rejected") from exc


class PostgresAssetCatalog:
    def __init__(self, database_url: str) -> None:
        self.database_url = database_url

    def search(
        self,
        workspace_id: str,
        library_ids: tuple[str, ...],
        query: str,
        limit: int,
        minimum_similarity: float,
    ) -> tuple[AssetMatch, ...]:
        if not library_ids:
            return ()
        with psycopg.connect(self.database_url, row_factory=dict_row) as connection:
            rows = connection.execute(
                """SELECT a.id AS asset_id, a.title, f.bucket, f.object_key,
                          f.media_type, f.content_hash, f.byte_size,
                          f.original_filename,
                          s.start_ms, s.end_ms, s.description,s.transcript,
                          s.cut_safe,s.semantic_complete,
                          s.people || s.locations || s.keywords ||
                          ARRAY_REMOVE(ARRAY[s.scene_type,s.action,s.era,s.mood,
                                             s.visual_style,s.shot_type],NULL) ||
                          COALESCE(asset_labels.labels,ARRAY[]::text[]) AS labels,
                          similarity(s.search_text, %s)
                            + LEAST(0.64, label_hits.count * 0.32)
                            + CASE WHEN s.cut_safe THEN 0.08 ELSE 0 END
                            + CASE WHEN s.semantic_complete THEN 0.04 ELSE 0 END AS score
                     FROM asset_segments s
                     JOIN asset_analyses aa
                       ON aa.workspace_id=s.workspace_id AND aa.id=s.analysis_id
                     JOIN assets a
                       ON a.workspace_id=s.workspace_id AND a.id=s.asset_id
                     JOIN LATERAL (
                       SELECT af.bucket,af.object_key,af.media_type,af.content_hash,
                              af.byte_size,af.original_filename,af.duration_ms
                         FROM asset_files af
                        WHERE af.workspace_id=a.workspace_id AND af.asset_id=a.id
                          AND af.deleted_at IS NULL AND af.scan_status='clean'
                        ORDER BY af.created_at LIMIT 1
                     ) f ON true
                     LEFT JOIN LATERAL (
                       SELECT array_agg(t.name ORDER BY t.name) AS labels
                         FROM asset_tags at
                         JOIN tags t
                           ON t.workspace_id=at.workspace_id AND t.id=at.tag_id
                        WHERE at.workspace_id=a.workspace_id AND at.asset_id=a.id
                     ) asset_labels ON true
                     CROSS JOIN LATERAL (
                       SELECT count(DISTINCT lower(label))::double precision AS count
                         FROM unnest(
                           s.people || s.locations || s.keywords ||
                           ARRAY_REMOVE(ARRAY[s.scene_type,s.action,s.era,s.mood,
                                              s.visual_style,s.shot_type],NULL) ||
                           COALESCE(asset_labels.labels,ARRAY[]::text[])
                         ) label
                        WHERE char_length(label) >= 2
                          AND %s ILIKE '%%' || label || '%%'
                          AND lower(label) NOT IN (
                            '男性','女性','人物',
                            '特写','近景','中景','远景','全景','镜头',
                            '室内','室外','现代','静止','温暖','微笑','阳光',
                            '对话','交流'
                          )
                     ) label_hits
                    WHERE s.workspace_id=%s AND a.library_id=ANY(%s::uuid[])
                      AND aa.status='completed' AND COALESCE(s.confidence,0) >= 0.5
                      AND (
                        s.start_ms >= 5000
                        OR (
                          s.start_ms=0 AND f.duration_ms <= 10000
                          AND s.cut_safe AND s.semantic_complete
                        )
                      )
                      -- Video segments must contain enough real motion to
                      -- cover a normal beat. A still image is intentionally a
                      -- one-frame source whose display duration is assigned by
                      -- the timeline planner, so applying the same three-second
                      -- source-duration gate would exclude every image.
                      AND (a.kind='image' OR s.end_ms - s.start_ms >= 3000)
                      AND a.kind IN ('image','video')
                      AND a.status='ready'
                      AND a.copyright_status IN ('owned','licensed','public_domain')
                      AND NOT (
                        lower(COALESCE(s.description,'')) ~
                          '(片尾|演职员|制作名单|credits?|credit roll)'
                        OR EXISTS (
                          SELECT 1 FROM unnest(s.keywords) keyword
                           WHERE lower(keyword) IN (
                             '片尾','演职员','制作名单','credits','credit roll'
                           )
                        )
                      )
                      AND similarity(s.search_text, %s)
                            + LEAST(0.64, label_hits.count * 0.32)
                            + CASE WHEN s.cut_safe THEN 0.08 ELSE 0 END
                            + CASE WHEN s.semantic_complete THEN 0.04 ELSE 0 END >= %s
                    ORDER BY score DESC, s.confidence DESC NULLS LAST, a.id, s.ordinal
                    LIMIT %s""",
                (query, query, workspace_id, list(library_ids), query, minimum_similarity, limit),
            ).fetchall()
        return tuple(
            AssetMatch(
                asset_id=str(row["asset_id"]),
                title=str(row["title"]),
                bucket=str(row["bucket"]),
                object_key=str(row["object_key"]),
                media_type=str(row["media_type"]),
                content_hash=str(row["content_hash"]),
                byte_size=int(row["byte_size"]),
                original_filename=str(row["original_filename"] or "source.mp4"),
                start_ms=int(row["start_ms"]),
                end_ms=int(row["end_ms"]),
                description=str(row["description"]),
                labels=tuple(str(item) for item in row["labels"]),
                score=float(row["score"]),
                transcript=str(row["transcript"] or ""),
                cut_safe=bool(row["cut_safe"]),
                semantic_complete=bool(row["semantic_complete"]),
            )
            for row in rows
        )


class DatabaseAssetCapability:
    def __init__(
        self,
        settings: AssetLibrarySettings,
        storage: ArtifactStorage,
        *,
        search_assets: SearchAssets | None = None,
        object_client: Any | None = None,
        acquire_assets: AssetAcquirer | None = None,
    ) -> None:
        self.settings = settings
        self.storage = storage
        self._search = search_assets or PostgresAssetCatalog(settings.database_url).search
        self._acquire = acquire_assets
        self._client = object_client or getattr(storage, "client", None)
        if self._client is None:
            raise ValueError("database asset selection requires an object-storage client")

    async def execute(self, context: StepContext) -> StepResult:
        await context.checkpoint()
        script = dict(self.storage.read_json(_required_artifact(context, "script")))
        scenes = _scene_queries(script)
        requirements = _scene_requirements(script)
        if not scenes:
            raise PermanentStepError("script contains no scene or narration to match")
        library_ids = _asset_library_ids(context.input_snapshot)
        if not library_ids:
            raise PermanentStepError("run snapshot contains no selected asset library")
        selected, usage, missing_scenes = await self._select(
            context, library_ids, scenes, requirements
        )
        acquisition = _asset_acquisition(context.input_snapshot)
        acquired_count = 0
        unresolved_queries: tuple[str, ...] = ()
        if missing_scenes and acquisition["enabled"]:
            if not acquisition["rights_confirmed"]:
                raise PermanentStepError(
                    "automatic asset acquisition requires explicit rights confirmation"
                )
            if self._acquire is None:
                raise RetryableStepError("automatic asset acquisition is not configured")
            result = await self._acquire.acquire(
                workspace_id=context.workspace_id,
                run_id=context.run_id,
                step_id=context.step_id,
                library_id=library_ids[0],
                queries=_acquisition_queries(
                    context.input_snapshot,
                    tuple(missing_scenes),
                    # Search breadth and download count are separate budgets.
                    # We need enough queries to cover distinct story beats even
                    # when only a few files may be imported in one round.
                    limit=max(
                        6,
                        acquisition["max_assets"] * 3,
                        len(missing_scenes),
                    ),
                ),
                sources=acquisition["sources"],
                max_assets=acquisition["max_assets"],
                copyright_status=acquisition["copyright_status"],
            )
            acquired_count = result.imported_count
            unresolved_queries = result.unresolved_queries
            retryable_provider_error = any(
                item.get("retryable") is True for item in result.provider_errors
            )
            if (
                not acquired_count
                and retryable_provider_error
                and context.attempt < context.maximum_attempts
            ):
                raise RetryableStepError(
                    "automatic asset provider is temporarily rate-limited or unavailable",
                    retry_after_seconds=30,
                )
            if acquired_count:
                selected, usage, missing_scenes = await self._select(
                    context, library_ids, scenes, requirements
                )

        if missing_scenes:
            # The first pass is intentionally parked behind the review-capable
            # runtime state while the separately queued asset-analysis jobs run.
            # AssetAnalysisService will issue the audited system request-changes
            # decision after the whole acquisition group settles.  If matching
            # still fails on that new attempt, human intervention is required;
            # otherwise the UI would promise another automatic wake-up for a
            # group whose resume marker has already been consumed.
            auto_resume_pending = (
                acquired_count > 0 and context.attempt < context.maximum_attempts
            )
            if auto_resume_pending:
                action = "auto_resume_after_asset_analysis"
            elif acquired_count:
                action = "review_acquired_assets_then_request_changes"
            else:
                action = "upload_or_tag_assets_then_request_changes"
            return StepResult(
                output_summary={
                    "provider": "database-asset-library",
                    "selected_assets": len(usage),
                    "covered_scenes": len(selected),
                    "missing_scenes": missing_scenes,
                    "unresolved_queries": unresolved_queries,
                    "provider_errors": [dict(item) for item in result.provider_errors]
                    if missing_scenes and acquisition["enabled"]
                    else [],
                    "acquired_assets": acquired_count,
                    "auto_acquisition_enabled": acquisition["enabled"],
                    "blocking_reason": "asset_coverage",
                    "action_required": action,
                    "auto_resume_pending": auto_resume_pending,
                    "rights_status": "verified",
                },
                requires_review=True,
            )

        references: list[ArtifactRef] = []
        manifest_items: list[dict[str, Any]] = []
        published: dict[str, ArtifactRef] = {}
        for index, (scene, match) in enumerate(selected, start=1):
            await context.checkpoint()
            artifact = published.get(match.asset_id)
            if artifact is None:
                suffix = Path(match.original_filename).suffix.lower() or ".mp4"
                filename = f"asset-{len(published) + 1:03d}{suffix}"
                copy = getattr(self.storage, "publish_copy", None)
                if callable(copy):
                    artifact = await asyncio.to_thread(
                        copy,
                        context,
                        kind="asset",
                        filename=filename,
                        media_type=match.media_type,
                        source_bucket=match.bucket,
                        source_key=match.object_key,
                        content_hash=match.content_hash,
                        byte_size=match.byte_size,
                    )
                else:
                    data = await asyncio.to_thread(self._read, match)
                    artifact = self.storage.publish(
                        context,
                        ProviderArtifact("asset", filename, match.media_type, data),
                    )
                published[match.asset_id] = artifact
                references.append(artifact)
            manifest_items.append(
                {
                    "filename": artifact.filename,
                    "artifact_id": artifact.id,
                    "asset_id": match.asset_id,
                    "media_type": match.media_type,
                    "selected_for_scene": scene,
                    "selected_for_narration": requirements.get(scene, {}).get(
                        "narration", ""
                    ),
                    "beat_id": requirements.get(scene, {}).get("id"),
                    "must_match": list(
                        requirements.get(scene, {}).get("must_match", ())
                    ),
                    "description": match.description,
                    "start_ms": match.start_ms,
                    "end_ms": match.end_ms,
                    "labels": list(match.labels),
                    "match_score": round(match.score, 4),
                    "source_transcript": match.transcript,
                    "cut_safe": match.cut_safe,
                    "semantic_complete": match.semantic_complete,
                    "reuse_count": usage[_segment_key(match)],
                    "rights_verified": True,
                }
            )
        manifest = {
            "schema_version": "2.0.0",
            "provider": "database-asset-library",
            "script": script,
            "assets": manifest_items,
            "rights_status": "verified",
        }
        manifest_ref = self.storage.publish(
            context,
            ProviderArtifact(
                "manifest",
                "asset-manifest.json",
                "application/json",
                json.dumps(manifest, ensure_ascii=False, sort_keys=True).encode(),
            ),
        )
        return StepResult(
            artifacts=(*references, manifest_ref),
            output_summary={
                "provider": "database-asset-library",
                "selected_assets": len(references),
                "minimum_match_score": min(match.score for _, match in selected),
                "rights_status": "verified",
                "acquired_assets": acquired_count,
                "auto_acquisition_enabled": acquisition["enabled"],
            },
        )

    async def _select(
        self,
        context: StepContext,
        library_ids: tuple[str, ...],
        scenes: tuple[str, ...],
        requirements: Mapping[str, Mapping[str, Any]],
    ) -> tuple[list[tuple[str, AssetMatch]], dict[SegmentKey, int], list[str]]:
        selected: list[tuple[str, AssetMatch]] = []
        usage: dict[SegmentKey, int] = {}
        description_usage: dict[str, int] = {}
        selected_asset_ids: set[str] = set()
        missing_scenes: list[str] = []
        fallback_query = _fallback_asset_query(context.input_snapshot)
        for scene in scenes:
            await context.checkpoint()
            semantic_query = _semantic_query(scene)
            candidates = self._search(
                context.workspace_id,
                library_ids,
                semantic_query,
                64,
                self.settings.minimum_similarity,
            )
            if not candidates and fallback_query and fallback_query != scene:
                candidates = self._search(
                    context.workspace_id,
                    library_ids,
                    fallback_query,
                    8,
                    self.settings.minimum_similarity,
                )
            candidates = tuple(
                candidate
                for candidate in candidates
                if math.isfinite(candidate.score)
                and candidate.score >= self.settings.minimum_similarity
                and _matches_visual_anchor(scene, candidate)
                and _matches_beat_constraints(requirements.get(scene, {}), candidate)
                and (
                    candidate.asset_id in selected_asset_ids
                    or len(selected_asset_ids) < self.settings.maximum_assets
                )
            )
            match = min(
                candidates,
                key=lambda candidate: (
                    -(
                        candidate.score
                        + _usable_duration_bonus(candidate)
                        - min(0.75, usage.get(_segment_key(candidate), 0) * 0.35)
                        - min(
                            0.7,
                            description_usage.get(_visual_fingerprint(candidate), 0)
                            * 0.45,
                        )
                        - min(0.4, _nearby_usage(candidate, selected) * 0.2)
                    ),
                    usage.get(_segment_key(candidate), 0),
                    candidate.asset_id,
                    candidate.start_ms,
                ),
                default=None,
            )
            if match is None:
                missing_scenes.append(scene)
                continue
            selected.append((scene, match))
            key = _segment_key(match)
            usage[key] = usage.get(key, 0) + 1
            fingerprint = _visual_fingerprint(match)
            description_usage[fingerprint] = description_usage.get(fingerprint, 0) + 1
            selected_asset_ids.add(match.asset_id)

            # Very short but semantically strong source clips otherwise force
            # the renderer to freeze their final frame under a longer sentence.
            # Retain one distinct, still-relevant alternate for that same beat;
            # the timeline planner can then make a real continuity cut.
            if _segment_duration_seconds(match) < 5.5:
                alternate = min(
                    (
                        candidate
                        for candidate in candidates
                        if candidate.asset_id != match.asset_id
                        and candidate.score >= match.score - 0.2
                        and (
                            candidate.asset_id in selected_asset_ids
                            or len(selected_asset_ids) < self.settings.maximum_assets
                        )
                    ),
                    key=lambda candidate: (
                        -(candidate.score + _usable_duration_bonus(candidate)),
                        candidate.asset_id,
                        candidate.start_ms,
                    ),
                    default=None,
                )
                if alternate is not None:
                    selected.append((scene, alternate))
                    alternate_key = _segment_key(alternate)
                    usage[alternate_key] = usage.get(alternate_key, 0) + 1
                    alternate_fingerprint = _visual_fingerprint(alternate)
                    description_usage[alternate_fingerprint] = (
                        description_usage.get(alternate_fingerprint, 0) + 1
                    )
                    selected_asset_ids.add(alternate.asset_id)
        return selected, usage, missing_scenes

    def _read(self, match: AssetMatch) -> bytes:
        try:
            response = self._client.get_object(Bucket=match.bucket, Key=match.object_key)
            data = response["Body"].read()
        except Exception as exc:
            raise RetryableStepError("catalog asset could not be read from object storage") from exc
        if hashlib.sha256(data).hexdigest() != match.content_hash:
            raise PermanentStepError("catalog asset content hash does not match its database record")
        return data


def _scene_queries(script: Mapping[str, Any]) -> tuple[str, ...]:
    raw = script.get("beats") or script.get("scenes") or ()
    if isinstance(raw, Sequence) and not isinstance(raw, (str, bytes)) and any(
        isinstance(item, Mapping) for item in raw
    ):
        raw = sorted(raw, key=_beat_sequence)
    scenes = (
        tuple(
            item
            for item in raw
            if isinstance(item, Mapping)
            or not _TIMING_MARKER.fullmatch(str(item).strip())
        )
        if isinstance(raw, Sequence) and not isinstance(raw, (str, bytes))
        else ()
    )
    narration = str(script.get("narration", "")).strip()
    beats = ordered_visual_beats(scenes, narration)
    if beats:
        return beats
    candidates = (
        str(script.get("title", "")).strip(),
        narration,
    )
    fallback = " ".join(part for part in candidates if part)
    return (fallback,) if fallback else ()


def _scene_requirements(script: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    raw = script.get("beats")
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        return {}
    result: dict[str, dict[str, Any]] = {}
    ordered = sorted(
        (item for item in raw if isinstance(item, Mapping)),
        key=_beat_sequence,
    )
    for item in ordered:
        scene = str(
            item.get("visual_description")
            or item.get("description")
            or item.get("text")
            or ""
        ).strip()
        if not scene:
            continue
        result[scene] = {
            "id": str(item.get("id") or "").strip() or None,
            "narration": str(item.get("narration") or "").strip(),
            "must_match": _constraint_terms(item.get("must_match")),
            "must_not_match": _constraint_terms(item.get("must_not_match")),
        }
    return result


def _beat_sequence(value: object) -> int:
    if not isinstance(value, Mapping) or isinstance(value.get("sequence"), bool):
        return 1_000_000
    try:
        sequence = int(value.get("sequence", 1_000_000))
    except (TypeError, ValueError):
        return 1_000_000
    return sequence if sequence > 0 else 1_000_000


def _constraint_terms(value: Any) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return ()
    return tuple(
        dict.fromkeys(str(item).strip() for item in value if str(item).strip())
    )[:12]


def _matches_beat_constraints(
    requirement: Mapping[str, Any], match: AssetMatch
) -> bool:
    """Apply model-authored, Run-frozen evidence without topic code."""

    haystack = " ".join(
        (match.title, match.description, match.transcript, *match.labels)
    ).casefold()
    required = _constraint_terms(requirement.get("must_match"))
    forbidden = _constraint_terms(requirement.get("must_not_match"))
    return (
        not required or any(term.casefold() in haystack for term in required)
    ) and not any(term.casefold() in haystack for term in forbidden)


def _segment_key(match: AssetMatch) -> SegmentKey:
    return (match.asset_id, match.start_ms, match.end_ms)


def _visual_fingerprint(match: AssetMatch) -> str:
    return " ".join(match.description.lower().split())


def _segment_duration_seconds(match: AssetMatch) -> float:
    return max(0.0, (match.end_ms - match.start_ms) / 1000)


def _usable_duration_bonus(match: AssetMatch) -> float:
    """Prefer enough real motion to cover a normal narration sentence."""

    return min(0.16, max(0.0, _segment_duration_seconds(match) - 5.5) * 0.02)


def _matches_visual_anchor(scene: str, match: AssetMatch) -> bool:
    """Apply generic identity and lexical-evidence gates before ranking.

    The gate intentionally has no topic vocabulary. Years and an explicit
    subject are exact constraints; all other subjects use weighted n-gram
    evidence from the segment description, transcript and analyzed labels.
    """

    segment_haystack = " ".join(
        (
            match.description,
            match.transcript,
            *match.labels,
        )
    ).casefold()
    identity_haystack = f"{match.title.casefold()} {segment_haystack}"

    # DOI suffixes often contain a publication year, but a citation overlay is
    # an editorial instruction rather than evidence that the B-roll itself was
    # captured in that year. Remove identifiers before applying visual year
    # constraints while preserving real years authored in shot descriptions.
    visual_scene = _DOI_IDENTIFIER.sub("", scene)

    # A named subject and explicit year are domain-neutral identity constraints.
    subject = re.match(
        r"(?P<subject>[\u3400-\u9fff]{2,6})(?=在|进行|参加|出席|获得|赢得|夺得)",
        visual_scene,
    )
    if subject and subject.group("subject").casefold() not in identity_haystack:
        return False
    for year in set(re.findall(r"(?:19|20)\d{2}", visual_scene)):
        if year not in identity_haystack:
            return False
    # The database query already applies a calibrated score threshold. Generic
    # code cannot infer that one domain term is more important than another;
    # stronger must-match semantics come from structured Beat fields instead.
    return True


def _nearby_usage(
    candidate: AssetMatch,
    selected: Sequence[tuple[str, AssetMatch]],
) -> int:
    center = (candidate.start_ms + candidate.end_ms) // 2
    return sum(
        1
        for _, existing in selected
        if existing.asset_id == candidate.asset_id
        and abs(center - (existing.start_ms + existing.end_ms) // 2) < 60_000
    )


def _semantic_query(scene: str) -> str:
    """Remove production timing while preserving authored semantic evidence."""

    return _TIMING_PREFIX.sub("", scene).strip()


_TIMING_MARKER = re.compile(
    r"(?:(?:\d{1,2}:)?\d{1,2}(?::|\.)\d{2}(?:\.\d{1,3})?)"
    r"\s*[-–—~至]\s*"
    r"(?:(?:\d{1,2}:)?\d{1,2}(?::|\.)\d{2}(?:\.\d{1,3})?)"
)

_DOI_IDENTIFIER = re.compile(r"\b10\.\d{4,9}/\S+", re.IGNORECASE)

_TIMING_PREFIX = re.compile(
    r"^\s*(?:(?:\d{1,2}:)?\d{1,2}(?::|\.)\d{2}(?:\.\d{1,3})?)"
    r"\s*[-–—~至]\s*"
    r"(?:(?:\d{1,2}:)?\d{1,2}(?::|\.)\d{2}(?:\.\d{1,3})?)\s*[:：]?\s*"
)


def _acquisition_queries(
    snapshot: Mapping[str, Any],
    missing_scenes: tuple[str, ...],
    *,
    limit: int,
) -> tuple[str, ...]:
    """Build one primary query per uncovered story beat."""
    topic = str(snapshot.get("topic", "")).strip()
    slots = plan_acquisition_slots(topic, missing_scenes, limit=limit)
    return tuple(
        dict.fromkeys(slot.query for slot in slots if slot.query.strip())
    )


def _fallback_asset_query(snapshot: Mapping[str, Any]) -> str:
    queries = _acquisition_queries(snapshot, (), limit=1)
    return queries[0] if queries else ""


def _asset_library_ids(snapshot: Mapping[str, Any]) -> tuple[str, ...]:
    framefactory = snapshot.get("_framefactory", {})
    framefactory = framefactory if isinstance(framefactory, Mapping) else {}
    composition = framefactory.get("composition_snapshot", {})
    composition = composition if isinstance(composition, Mapping) else {}
    raw = composition.get("asset_library_ids", [])
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        return ()
    return tuple(dict.fromkeys(str(value).strip() for value in raw if str(value).strip()))


def _asset_acquisition(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    framefactory = snapshot.get("_framefactory", {})
    framefactory = framefactory if isinstance(framefactory, Mapping) else {}
    composition = framefactory.get("composition_snapshot", {})
    composition = composition if isinstance(composition, Mapping) else {}
    production = composition.get("production_settings", {})
    production = production if isinstance(production, Mapping) else {}
    raw = production.get("asset_acquisition", {})
    raw = raw if isinstance(raw, Mapping) else {}
    sources = tuple(
        value
        for value in (str(item) for item in raw.get("sources", ()))
        if value in {"youtube", "bilibili", "wikimedia"}
    )
    try:
        max_assets = max(1, min(6, int(raw.get("max_assets", 3))))
    except (TypeError, ValueError):
        max_assets = 3
    copyright_status = str(raw.get("copyright_status", "licensed"))
    if copyright_status not in {"licensed", "public_domain"}:
        copyright_status = "licensed"
    provider_verified_public_domain = (
        sources == ("wikimedia",) and copyright_status == "public_domain"
    )
    return {
        "enabled": raw.get("enabled") is True,
        "sources": sources or ("wikimedia", "youtube", "bilibili"),
        "max_assets": max_assets,
        "copyright_status": copyright_status,
        "rights_confirmed": (
            raw.get("rights_confirmed") is True
            or provider_verified_public_domain
        ),
        "rights_mode": (
            "provider_verified_public_domain"
            if provider_verified_public_domain
            else "user_attested"
        ),
    }


def _required_artifact(context: StepContext, kind: str) -> ArtifactRef:
    artifact = next((item for item in context.input_artifacts if item.kind == kind), None)
    if artifact is None:
        raise PermanentStepError(f"required {kind} artifact is unavailable")
    return artifact
