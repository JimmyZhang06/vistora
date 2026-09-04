"""Read-only PostgreSQL/MinIO audit for asset, frame, and Run locators.

Exit codes: 0 passed, 1 invariant failed, 2 environment/capability blocked.
The database session is forced read-only. S3 calls are list/head/get only.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import tempfile
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any
from uuid import UUID

from postgres_restore_gate import _pg_environment, _psql
from release_gate_common import GateBlocked, GateFailure, load_json, require_env
from s3_integrity_gate import _aws

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_REPORT = ROOT / "artifacts" / "release" / "asset-integrity-audit.json"
SHA256_RE = re.compile(r"^[a-f0-9]{64}$")
REPRESENTATIVE_FRAME_COLUMNS = {
    "representative_frame_key",
    "representative_frame_bucket",
    "representative_frame_content_hash",
    "representative_frame_byte_size",
    "representative_frame_media_type",
}
REQUIRED_RLS_TABLES = {
    "assets",
    "asset_files",
    "asset_analyses",
    "asset_segments",
    "asset_review_actions",
    "run_asset_snapshots",
}
RUN_SNAPSHOT_COLUMNS = {
    "id",
    "workspace_id",
    "run_id",
    "asset_id",
    "file_id",
    "artifact_id",
    "library_id",
    "bucket",
    "object_key",
    "byte_size",
    "content_hash",
    "media_type",
    "copyright_status",
    "analysis_id",
    "analysis_version",
    "review_id",
    "review_revision",
}


def _json_query(environment: dict[str, str], sql: str) -> Any:
    try:
        return json.loads(_psql(environment, sql))
    except json.JSONDecodeError as exc:
        raise GateFailure("PostgreSQL audit query did not return valid JSON") from exc


def _schema(environment: dict[str, str]) -> dict[str, set[str]]:
    rows = _json_query(
        environment,
        "SELECT COALESCE(json_agg(json_build_object('table',table_name,'column',column_name)),"
        "'[]'::json)::text FROM information_schema.columns "
        "WHERE table_schema='public' ORDER BY table_name,ordinal_position",
    )
    result: dict[str, set[str]] = {}
    for row in rows:
        result.setdefault(str(row["table"]), set()).add(str(row["column"]))
    return result


def _rls_state(environment: dict[str, str]) -> dict[str, dict[str, Any]]:
    rows = _json_query(
        environment,
        "SELECT COALESCE(json_agg(json_build_object('table',c.relname,"
        "'enabled',c.relrowsecurity,'forced',c.relforcerowsecurity,'policies',"
        "COALESCE((SELECT json_agg(json_build_object('qual',p.qual,'with_check',p.with_check)) "
        "FROM pg_policies p WHERE p.schemaname='public' AND p.tablename=c.relname),"
        "'[]'::json))), '[]'::json)::text "
        "FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
        "WHERE n.nspname='public' AND c.relname IN "
        "('assets','asset_files','asset_analyses','asset_segments','asset_review_actions',"
        "'run_asset_snapshots')",
    )
    return {str(row["table"]): dict(row) for row in rows}


def _inventory(
    environment: dict[str, str],
    workspace_id: UUID,
    schema: Mapping[str, set[str]],
    sentinel_asset_ids: set[str],
) -> dict[str, Any]:
    workspace = str(workspace_id)
    review_failure = "TRUE"
    if (
        {"review_revision"}.issubset(schema.get("assets", set()))
        and {
            "workspace_id",
            "asset_id",
            "action",
            "review_revision",
            "analysis_id",
            "file_content_hash",
            "library_id",
            "copyright_status",
            "created_at",
        }.issubset(schema.get("asset_review_actions", set()))
    ):
        review_failure = (
            "NOT EXISTS (SELECT 1 FROM asset_review_actions ar "
            "WHERE ar.workspace_id=a.workspace_id AND ar.asset_id=a.id "
            "AND ar.action='approved' AND ar.review_revision=a.review_revision "
            "AND ar.file_content_hash=f.content_hash "
            "AND ar.analysis_id=(SELECT aa.id FROM asset_analyses aa "
            "WHERE aa.workspace_id=a.workspace_id AND aa.asset_id=a.id "
            "AND aa.status='completed' ORDER BY aa.analysis_version DESC LIMIT 1) "
            "AND NOT EXISTS (SELECT 1 FROM asset_review_actions newer "
            "WHERE newer.workspace_id=ar.workspace_id AND newer.asset_id=ar.asset_id "
            "AND (newer.review_revision>ar.review_revision OR "
            "(newer.review_revision=ar.review_revision AND newer.created_at>ar.created_at))))"
        )
    missing_frames = "0"
    if REPRESENTATIVE_FRAME_COLUMNS.issubset(schema.get("asset_segments", set())):
        missing_frames = (
            "(SELECT count(DISTINCT aa.id) FROM asset_analyses aa JOIN assets va "
            "ON va.workspace_id=aa.workspace_id AND va.id=aa.asset_id "
            f"WHERE aa.workspace_id='{workspace}'::uuid AND aa.status='completed' "
            "AND va.kind IN ('image','video') AND NOT EXISTS (SELECT 1 FROM asset_segments seg "
            "WHERE seg.workspace_id=aa.workspace_id AND seg.analysis_id=aa.id "
            "AND seg.representative_frame_key IS NOT NULL "
            "AND seg.representative_frame_content_hash IS NOT NULL "
            "AND seg.representative_frame_byte_size IS NOT NULL))"
        )
    sentinel_values = ",".join(f"'{value}'::uuid" for value in sorted(sentinel_asset_ids))
    worker_eligible = (
        "(SELECT count(DISTINCT a.id) FROM assets a JOIN asset_files wf "
        "ON wf.workspace_id=a.workspace_id AND wf.asset_id=a.id AND wf.deleted_at IS NULL "
        "WHERE a.workspace_id='" + workspace + "'::uuid "
        f"AND a.id IN ({sentinel_values}) AND a.status='ready' "
        "AND a.copyright_status IN ('owned','licensed','public_domain') "
        "AND wf.scan_status='clean' AND EXISTS (SELECT 1 FROM asset_analyses waa "
        "WHERE waa.workspace_id=a.workspace_id AND waa.asset_id=a.id "
        "AND waa.status='completed' AND EXISTS (SELECT 1 FROM asset_segments was "
        "WHERE was.workspace_id=waa.workspace_id AND was.analysis_id=waa.id)))"
    )
    snapshot_sentinel_references = "0"
    snapshot_semantic_mismatches = "0"
    if RUN_SNAPSHOT_COLUMNS.issubset(schema.get("run_asset_snapshots", set())):
        snapshot_sentinel_references = (
            "(SELECT count(*) FROM run_asset_snapshots rs "
            f"WHERE rs.workspace_id='{workspace}'::uuid AND rs.asset_id IN ({sentinel_values}))"
        )
        snapshot_semantic_mismatches = (
            "(SELECT count(*) FROM run_asset_snapshots rs "
            "LEFT JOIN assets sa ON sa.workspace_id=rs.workspace_id AND sa.id=rs.asset_id "
            "LEFT JOIN asset_files sf ON sf.workspace_id=rs.workspace_id AND sf.id=rs.file_id "
            "LEFT JOIN asset_analyses saa ON saa.workspace_id=rs.workspace_id "
            "AND saa.id=rs.analysis_id LEFT JOIN asset_review_actions sar "
            "ON sar.workspace_id=rs.workspace_id AND sar.id=rs.review_id "
            f"WHERE rs.workspace_id='{workspace}'::uuid AND (sa.id IS NULL OR sf.id IS NULL "
            "OR saa.id IS NULL OR sar.id IS NULL OR rs.library_id<>sar.library_id "
            "OR rs.content_hash<>sf.content_hash OR rs.content_hash<>sar.file_content_hash "
            "OR rs.analysis_id<>sar.analysis_id OR rs.analysis_version<>saa.analysis_version "
            "OR rs.review_revision<>sar.review_revision "
            "OR rs.copyright_status<>sar.copyright_status))"
        )
    counts = _json_query(
        environment,
        f"""SELECT json_build_object(
          'assets', (SELECT count(*) FROM assets a WHERE a.workspace_id='{workspace}'::uuid),
          'ready', (SELECT count(*) FROM assets a WHERE a.workspace_id='{workspace}'::uuid
                    AND a.status='ready'),
          'quarantine', (SELECT count(DISTINCT a.id) FROM assets a
            LEFT JOIN asset_files f ON f.workspace_id=a.workspace_id AND f.asset_id=a.id
                                     AND f.deleted_at IS NULL
            WHERE a.workspace_id='{workspace}'::uuid AND (
              a.status='quarantined' OR a.copyright_status IN ('unknown','restricted')
              OR COALESCE(f.scan_status,'pending') <> 'clean'
              OR NOT EXISTS (SELECT 1 FROM asset_analyses aa
                WHERE aa.workspace_id=a.workspace_id AND aa.asset_id=a.id
                  AND aa.status='completed'))),
          'unsafe_ready', (SELECT count(DISTINCT a.id) FROM assets a
            LEFT JOIN asset_files f ON f.workspace_id=a.workspace_id AND f.asset_id=a.id
                                     AND f.deleted_at IS NULL
            WHERE a.workspace_id='{workspace}'::uuid AND a.status='ready' AND (
              a.copyright_status NOT IN ('owned','licensed','public_domain')
              OR COALESCE(f.scan_status,'pending') <> 'clean'
              OR NOT EXISTS (SELECT 1 FROM asset_analyses aa
                WHERE aa.workspace_id=a.workspace_id AND aa.asset_id=a.id
              AND aa.status='completed') OR {review_failure})),
          'completed_visual_analyses_missing_frames', {missing_frames},
          'worker_eligible_quarantine_sentinels', {worker_eligible},
          'run_snapshot_quarantine_references', {snapshot_sentinel_references},
          'run_snapshot_semantic_mismatches', {snapshot_semantic_mismatches})::text""",
    )

    sentinels = _json_query(
        environment,
        f"""SELECT COALESCE(json_agg(row_to_json(q) ORDER BY asset_id),'[]'::json)::text
        FROM (SELECT DISTINCT ON (a.id) a.id::text AS asset_id,f.id::text AS file_id,
                     f.content_hash
                FROM assets a LEFT JOIN asset_files f
                  ON f.workspace_id=a.workspace_id AND f.asset_id=a.id AND f.deleted_at IS NULL
               WHERE a.workspace_id='{workspace}'::uuid AND (
                 a.status='quarantined' OR a.copyright_status IN ('unknown','restricted')
                 OR COALESCE(f.scan_status,'pending') <> 'clean'
                 OR NOT EXISTS (SELECT 1 FROM asset_analyses aa
                   WHERE aa.workspace_id=a.workspace_id AND aa.asset_id=a.id
                     AND aa.status='completed'))
               ORDER BY a.id,f.created_at) q""",
    )
    locator_queries = [
        f"""SELECT 'asset_file' AS kind,f.id::text AS id,f.bucket,f.object_key,
                   f.byte_size,f.content_hash,f.media_type,a.workspace_id::text AS workspace_id
              FROM asset_files f JOIN assets a
                ON a.workspace_id=f.workspace_id AND a.id=f.asset_id
             WHERE f.workspace_id='{workspace}'::uuid AND f.deleted_at IS NULL""",
        f"""SELECT 'artifact' AS kind,id::text,bucket,object_key,byte_size,content_hash,
                   media_type,workspace_id::text
              FROM artifacts WHERE workspace_id='{workspace}'::uuid
                AND status='available' AND deleted_at IS NULL""",
    ]
    segment_columns = schema.get("asset_segments", set())
    if REPRESENTATIVE_FRAME_COLUMNS.issubset(segment_columns):
        locator_queries.append(
            f"""SELECT 'representative_frame' AS kind,id::text,
                       representative_frame_bucket AS bucket,
                       representative_frame_key AS object_key,
                       representative_frame_byte_size AS byte_size,
                       representative_frame_content_hash AS content_hash,
                       representative_frame_media_type AS media_type,
                       workspace_id::text
                  FROM asset_segments WHERE workspace_id='{workspace}'::uuid
                    AND representative_frame_key IS NOT NULL"""
        )
    snapshot_columns = schema.get("run_asset_snapshots", set())
    if RUN_SNAPSHOT_COLUMNS.issubset(snapshot_columns):
        locator_queries.append(
            f"""SELECT 'run_asset_snapshot' AS kind,id::text,bucket,object_key,byte_size,
                       content_hash,media_type,workspace_id::text
                  FROM run_asset_snapshots WHERE workspace_id='{workspace}'::uuid"""
        )
    union = " UNION ALL ".join(locator_queries)
    locators = _json_query(
        environment,
        "SELECT COALESCE(json_agg(row_to_json(locator) ORDER BY kind,id),'[]'::json)::text "
        f"FROM ({union}) locator",
    )
    if not isinstance(counts, dict) or not isinstance(locators, list):
        raise GateFailure("PostgreSQL asset inventory has an invalid shape")
    return {"counts": counts, "locators": locators, "quarantine_entries": sentinels}


def _list_objects(bucket: str, prefix: str) -> list[dict[str, Any]]:
    objects: list[dict[str, Any]] = []
    token: str | None = None
    while True:
        arguments = [
            "s3api",
            "list-objects-v2",
            "--bucket",
            bucket,
            "--prefix",
            prefix,
            "--output",
            "json",
        ]
        if token:
            arguments.extend(("--continuation-token", token))
        page = _aws(arguments)
        contents = page.get("Contents", [])
        if not isinstance(contents, list):
            raise GateFailure("S3 list-objects-v2 returned an invalid Contents value")
        objects.extend(item for item in contents if isinstance(item, dict))
        if not page.get("IsTruncated"):
            return objects
        token = page.get("NextContinuationToken")
        if not isinstance(token, str) or not token:
            raise GateFailure("S3 truncated listing omitted its continuation token")


def _head_findings(locator: Mapping[str, Any], head: Mapping[str, Any]) -> list[str]:
    findings: list[str] = []
    label = f"{locator.get('kind')}:{locator.get('id')}"
    expected_size = locator.get("byte_size")
    expected_hash = str(locator.get("content_hash") or "")
    expected_workspace = str(locator.get("workspace_id") or "")
    expected_media_type = locator.get("media_type")
    metadata = {str(key).lower(): str(value) for key, value in (head.get("Metadata") or {}).items()}
    if not isinstance(expected_size, int) or head.get("ContentLength") != expected_size:
        findings.append(f"{label}: DB/S3 byte size mismatch")
    if not SHA256_RE.fullmatch(expected_hash) or metadata.get("sha256") != expected_hash:
        findings.append(f"{label}: DB/S3 SHA-256 metadata mismatch")
    if metadata.get("workspace-id") != expected_workspace:
        findings.append(f"{label}: S3 workspace metadata mismatch")
    if expected_media_type and head.get("ContentType") != expected_media_type:
        findings.append(f"{label}: DB/S3 media type mismatch")
    return findings


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _key_token(key: str) -> str:
    return f"key-sha256:{hashlib.sha256(key.encode()).hexdigest()[:16]}"


def evaluate_inventory(
    inventory: Mapping[str, Any],
    *,
    schema: Mapping[str, set[str]],
    expected_quarantine_count: int,
    s3_keys: Iterable[str],
    heads: Mapping[str, Mapping[str, Any]],
    expected_bucket: str | None = None,
    expected_quarantine_entries: Iterable[tuple[str, str, str]] | None = None,
    rls_state: Mapping[str, Mapping[str, bool]] | None = None,
) -> list[str]:
    findings: list[str] = []
    required_schema = {
        "assets": {"review_revision", "quarantine_reason", "deleted_at"},
        "asset_review_actions": {
            "workspace_id",
            "asset_id",
            "action",
            "review_revision",
            "analysis_id",
            "file_content_hash",
            "library_id",
            "copyright_status",
            "created_at",
        },
        "run_asset_snapshots": RUN_SNAPSHOT_COLUMNS,
        "asset_segments": REPRESENTATIVE_FRAME_COLUMNS,
    }
    for table, required_columns in required_schema.items():
        if table not in schema:
            findings.append(f"schema: missing table {table}")
            continue
        missing = required_columns - schema[table]
        if missing:
            findings.append(f"schema: {table} missing columns {sorted(missing)}")

    counts = inventory.get("counts", {})
    if counts.get("quarantine") != expected_quarantine_count:
        findings.append(
            "quarantine inventory mismatch: "
            f"DB={counts.get('quarantine')} expected={expected_quarantine_count}"
        )
    if counts.get("unsafe_ready") != 0:
        findings.append(f"unsafe ready assets detected: {counts.get('unsafe_ready')}")
    if counts.get("completed_visual_analyses_missing_frames") != 0:
        findings.append(
            "completed visual analyses missing representative frames: "
            f"{counts.get('completed_visual_analyses_missing_frames')}"
        )
    if counts.get("worker_eligible_quarantine_sentinels") != 0:
        findings.append(
            "quarantine sentinels eligible for production Worker search: "
            f"{counts.get('worker_eligible_quarantine_sentinels')}"
        )
    if counts.get("run_snapshot_quarantine_references") != 0:
        findings.append(
            "Run snapshots reference quarantine sentinels: "
            f"{counts.get('run_snapshot_quarantine_references')}"
        )
    if counts.get("run_snapshot_semantic_mismatches") != 0:
        findings.append(
            "Run snapshot provenance mismatches: "
            f"{counts.get('run_snapshot_semantic_mismatches')}"
        )
    if expected_quarantine_entries is not None:
        expected_set = set(expected_quarantine_entries)
        actual_set = {
            (
                str(entry.get("asset_id") or ""),
                str(entry.get("file_id") or ""),
                str(entry.get("content_hash") or ""),
            )
            for entry in inventory.get("quarantine_entries", [])
        }
        if actual_set != expected_set:
            findings.append(
                "quarantine sentinel ID/file/hash set mismatch: "
                f"missing={len(expected_set - actual_set)} unexpected={len(actual_set - expected_set)}"
            )
    if rls_state is not None:
        for table in REQUIRED_RLS_TABLES:
            state = rls_state.get(table, {})
            if state.get("enabled") is not True or state.get("forced") is not True:
                findings.append(f"RLS is not enabled and forced for {table}")
                continue
            policies = state.get("policies", [])
            normalized = " ".join(
                str(policy.get(field) or "")
                for policy in policies
                if isinstance(policy, dict)
                for field in ("qual", "with_check")
            ).replace('"', "").replace(" ", "")
            if "can_access_workspace(workspace_id)" not in normalized:
                findings.append(f"RLS policy for {table} lacks workspace-scoped read access")
            if "can_write_workspace(workspace_id)" not in normalized:
                findings.append(f"RLS policy for {table} lacks workspace-scoped write checks")

    locators = inventory.get("locators", [])
    keys = [str(locator.get("object_key") or "") for locator in locators]
    keyed_kinds = [
        (str(locator.get("kind") or ""), str(locator.get("object_key") or ""))
        for locator in locators
        if locator.get("kind") != "run_asset_snapshot"
    ]
    duplicates = sorted(
        f"{kind}:{_key_token(key)}"
        for (kind, key), count in Counter(keyed_kinds).items()
        if key and count > 1
    )
    if duplicates:
        findings.append(f"duplicate DB object locators: {duplicates[:10]}")
    db_keys = {key for key in keys if key}
    object_keys = set(s3_keys)
    missing_objects = sorted(db_keys - object_keys)
    orphan_objects = sorted(object_keys - db_keys)
    if missing_objects:
        findings.append(
            "DB locators missing in object storage: "
            f"{[_key_token(key) for key in missing_objects[:10]]}"
        )
    if orphan_objects:
        findings.append(
            "unreferenced object-storage keys: "
            f"{[_key_token(key) for key in orphan_objects[:10]]}"
        )
    for locator in locators:
        key = str(locator.get("object_key") or "")
        if expected_bucket and locator.get("bucket") != expected_bucket:
            findings.append(
                f"{locator.get('kind')}:{locator.get('id')}: unexpected bucket "
                f"{locator.get('bucket')!r}"
            )
        if key in heads:
            findings.extend(_head_findings(locator, heads[key]))
        elif key in object_keys:
            findings.append(f"{locator.get('kind')}:{locator.get('id')}: object HEAD was not verified")
    return findings


def _quarantine_manifest(path: Path, workspace_id: UUID) -> set[tuple[str, str, str]]:
    value = load_json(str(path))
    if value.get("workspace_id") != str(workspace_id):
        raise GateBlocked("quarantine manifest workspace does not match the audit workspace")
    entries = value.get("entries")
    if not isinstance(entries, list) or len(entries) != 1247:
        raise GateBlocked("quarantine manifest must contain exactly 1247 entries")
    result: set[tuple[str, str, str]] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise GateBlocked("quarantine manifest entries must be objects")
        try:
            asset_id = str(UUID(str(entry.get("asset_id") or "")))
            file_id = str(UUID(str(entry.get("file_id") or "")))
        except ValueError as exc:
            raise GateBlocked("quarantine manifest contains an invalid UUID") from exc
        identity = (asset_id, file_id, str(entry.get("content_hash") or ""))
        if not SHA256_RE.fullmatch(identity[2]):
            raise GateBlocked("quarantine manifest entry lacks asset/file/SHA-256 identity")
        result.add(identity)
    if len(result) != 1247:
        raise GateBlocked("quarantine manifest contains duplicate identities")
    return result


def run_audit(
    *,
    expected_quarantine_count: int,
    quarantine_manifest: Path,
    rehash_all: bool,
) -> dict[str, Any]:
    if os.environ.get("FF_RELEASE_ASSET_AUDIT_QUIESCED") != "1":
        raise GateBlocked(
            "set FF_RELEASE_ASSET_AUDIT_QUIESCED=1 only after the audit workspace is read-only"
        )
    workspace_id = UUID(require_env("FF_RELEASE_ASSET_WORKSPACE_ID"))
    expected_quarantine_entries = _quarantine_manifest(quarantine_manifest, workspace_id)
    database_env, _ = _pg_environment(require_env("FF_RELEASE_DATABASE_URL"))
    prior_options = database_env.get("PGOPTIONS", "").strip()
    database_env["PGOPTIONS"] = (
        f"{prior_options} -c default_transaction_read_only=on -c statement_timeout=60000"
    ).strip()
    schema = _schema(database_env)
    rls_state = _rls_state(database_env)
    inventory = _inventory(
        database_env,
        workspace_id,
        schema,
        {entry[0] for entry in expected_quarantine_entries},
    )
    bucket = require_env("FF_RELEASE_S3_BUCKET")
    prefix = f"workspaces/{workspace_id}/"
    objects = _list_objects(bucket, prefix)
    s3_keys = [str(item.get("Key")) for item in objects if item.get("Key")]
    heads: dict[str, Mapping[str, Any]] = {}
    rehashed = 0
    with tempfile.TemporaryDirectory(prefix="framefactory-asset-audit-") as directory:
        for index, locator in enumerate(inventory["locators"]):
            key = str(locator.get("object_key") or "")
            if not key or key not in s3_keys:
                continue
            heads[key] = _aws(
                ["s3api", "head-object", "--bucket", bucket, "--key", key, "--output", "json"]
            )
            if rehash_all:
                target = Path(directory) / f"object-{index:06d}"
                _aws(
                    [
                        "s3api",
                        "get-object",
                        "--bucket",
                        bucket,
                        "--key",
                        key,
                        str(target),
                        "--output",
                        "json",
                    ]
                )
                digest = _sha256_file(target)
                if digest != locator.get("content_hash"):
                    heads[key] = dict(heads[key], _body_hash_mismatch=True)
                rehashed += 1
    findings = evaluate_inventory(
        inventory,
        schema=schema,
        expected_quarantine_count=expected_quarantine_count,
        s3_keys=s3_keys,
        heads=heads,
        expected_bucket=bucket,
        expected_quarantine_entries=expected_quarantine_entries,
        rls_state=rls_state,
    )
    for key, head in heads.items():
        if head.get("_body_hash_mismatch"):
            findings.append(f"{_key_token(key)}: downloaded object SHA-256 mismatch")
    counts = inventory["counts"]
    return {
        "result": "PASSED" if not findings else "FAILED",
        "workspace_id": str(workspace_id),
        "expected_quarantine_count": expected_quarantine_count,
        "database_counts": counts,
        "db_locator_count": len(inventory["locators"]),
        "distinct_db_object_count": len(
            {str(locator.get("object_key")) for locator in inventory["locators"]}
        ),
        "object_count": len(s3_keys),
        "head_verified": len(heads),
        "body_rehashed": rehashed,
        "quarantine_manifest_entries": len(expected_quarantine_entries),
        "findings": findings,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--expected-quarantine-count",
        type=int,
        default=int(os.environ.get("FF_RELEASE_EXPECTED_QUARANTINED_ASSETS", "1247")),
    )
    parser.add_argument("--rehash-all", action="store_true")
    parser.add_argument(
        "--quarantine-manifest",
        type=Path,
        default=None,
        help="fixed 1247-entry asset/file/hash manifest (required)",
    )
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        manifest = arguments.quarantine_manifest
        if manifest is None:
            manifest = Path(require_env("FF_RELEASE_QUARANTINE_MANIFEST"))
        result = run_audit(
            expected_quarantine_count=arguments.expected_quarantine_count,
            quarantine_manifest=manifest,
            rehash_all=arguments.rehash_all,
        )
        code = 0 if result["result"] == "PASSED" else 1
    except GateBlocked as exc:
        result = {"result": "BLOCKED", "reason": str(exc)}
        code = 2
    except (GateFailure, ValueError) as exc:
        result = {"result": "FAILED", "reason": str(exc)}
        code = 1
    arguments.report.parent.mkdir(parents=True, exist_ok=True)
    arguments.report.write_text(
        json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
