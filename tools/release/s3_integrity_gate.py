"""Cross-check available PostgreSQL artifact rows against S3-compatible objects."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from postgres_restore_gate import _pg_environment, _psql
from release_gate_common import GateBlocked, GateFailure, gate_main, require_env


def _aws(arguments: list[str]) -> dict[str, Any]:
    command = ["aws"]
    endpoint = os.environ.get("FF_RELEASE_S3_ENDPOINT_URL", "").strip()
    if endpoint:
        command.extend(["--endpoint-url", endpoint])
    region = os.environ.get("FF_RELEASE_S3_REGION", "").strip()
    if region:
        command.extend(["--region", region])
    command.extend(arguments)
    try:
        result = subprocess.run(command, text=True, capture_output=True, timeout=300, check=False)
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as exc:
        raise GateBlocked(f"cannot execute AWS CLI: {exc}") from exc
    if result.returncode:
        raise GateFailure(f"AWS CLI failed ({result.returncode}): {result.stderr[-1000:].strip()}")
    try:
        value = json.loads(result.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise GateFailure("AWS CLI did not return valid JSON") from exc
    if not isinstance(value, dict):
        raise GateFailure("AWS CLI response must be a JSON object")
    return value


def _artifact_rows() -> list[dict[str, Any]]:
    database_env, _ = _pg_environment(require_env("FF_RELEASE_DATABASE_URL"))
    sql = (
        "SELECT COALESCE(json_agg(row_to_json(a)), '[]'::json)::text FROM ("
        "SELECT id::text, workspace_id::text, run_id::text, bucket, object_key, "
        "byte_size, content_hash, status::text FROM artifacts "
        "WHERE status = 'available' ORDER BY id) a"
    )
    try:
        rows = json.loads(_psql(database_env, sql))
    except json.JSONDecodeError as exc:
        raise GateFailure("psql artifact query did not return valid JSON") from exc
    if not isinstance(rows, list):
        raise GateFailure("artifact query must return a JSON array")
    return rows


def run_gate() -> dict[str, Any]:
    rows = _artifact_rows()
    if not rows:
        raise GateBlocked("database has no available artifacts to verify")
    expected_bucket = os.environ.get("FF_RELEASE_S3_BUCKET", "").strip()
    verified = 0
    total_bytes = 0
    with tempfile.TemporaryDirectory(prefix="framefactory-s3-check-") as directory:
        for row in rows:
            bucket = row.get("bucket")
            key = row.get("object_key")
            expected_size = row.get("byte_size")
            expected_hash = row.get("content_hash")
            if not all(
                [isinstance(bucket, str), isinstance(key, str), isinstance(expected_size, int),
                 isinstance(expected_hash, str) and len(expected_hash) == 64]
            ):
                raise GateFailure(f"artifact {row.get('id')} lacks complete storage integrity metadata")
            if expected_bucket and bucket != expected_bucket:
                raise GateFailure(f"artifact {row.get('id')} references unexpected bucket {bucket!r}")
            workspace_prefix = f"workspaces/{row.get('workspace_id')}/runs/{row.get('run_id')}/artifacts/"
            if not key.startswith(workspace_prefix) or "/../" in f"/{key}/":
                raise GateFailure(f"artifact {row.get('id')} violates the tenant object-key boundary")

            head = _aws(["s3api", "head-object", "--bucket", bucket, "--key", key, "--output", "json"])
            if head.get("ContentLength") != expected_size:
                raise GateFailure(
                    f"artifact {row.get('id')} size mismatch: DB={expected_size}, S3={head.get('ContentLength')}"
                )
            target = Path(directory) / str(row["id"])
            _aws(
                [
                    "s3api", "get-object", "--bucket", bucket, "--key", key,
                    str(target), "--output", "json",
                ]
            )
            digest = hashlib.sha256(target.read_bytes()).hexdigest()
            if digest != expected_hash:
                raise GateFailure(
                    f"artifact {row.get('id')} SHA-256 mismatch: DB={expected_hash}, object={digest}"
                )
            verified += 1
            total_bytes += expected_size
    return {"objects": verified, "bytes": total_bytes, "content": "sha256-verified"}


if __name__ == "__main__":
    gate_main(run_gate)
