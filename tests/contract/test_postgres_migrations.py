"""Static PostgreSQL architecture checks; no database server is required."""

from __future__ import annotations

import re


REQUIRED_TABLES = {
    "users",
    "workspaces",
    "workspace_members",
    "sessions",
    "channels",
    "channel_defaults",
    "skills",
    "skill_versions",
    "skill_evaluations",
    "asset_libraries",
    "assets",
    "asset_files",
    "asset_sources",
    "asset_tags",
    "voice_profiles",
    "render_presets",
    "render_preset_versions",
    "pipelines",
    "pipeline_versions",
    "runs",
    "run_steps",
    "run_events",
    "artifacts",
    "review_actions",
    "api_keys",
    "idempotency_keys",
    "webhooks",
    "audit_logs",
    "usage_records",
    "outbox_events",
}

TENANT_ROOT_TABLES = {
    "channels",
    "skills",
    "asset_libraries",
    "voice_profiles",
    "render_presets",
    "pipelines",
    "runs",
    "api_keys",
    "webhooks",
}


def _table_body(sql: str, table: str) -> str:
    match = re.search(
        rf"create\s+table\s+(?:if\s+not\s+exists\s+)?(?:public\.)?\"?{re.escape(table)}\"?\s*\((.*?)\)\s*;",
        sql,
        flags=re.I | re.S,
    )
    assert match, f"cannot parse CREATE TABLE {table}"
    return re.sub(r"\s+", " ", match.group(1)).lower()


def _has_index_covering(sql: str, table: str, required_columns: set[str]) -> bool:
    expressions = re.findall(
        rf"create\s+(?:unique\s+)?index\s+[^;]+?\s+on\s+(?:public\.)?\"?{re.escape(table)}\"?\s*(?:using\s+\w+\s*)?\((.*?)\)",
        sql,
        flags=re.I | re.S,
    )
    return any(required_columns <= set(re.findall(r"\b[a-z_][a-z0-9_]*\b", expr.lower())) for expr in expressions)


def _has_rls_policy(sql: str, table: str) -> bool:
    explicit_rls = re.search(
        rf"alter\s+table\s+(?:public\.)?{re.escape(table)}\s+enable\s+row\s+level\s+security",
        sql,
    )
    explicit_policy = re.search(
        rf"create\s+policy\s+[^;]+\s+on\s+(?:public\.)?{re.escape(table)}\b", sql
    )
    if explicit_rls and explicit_policy:
        return True

    for table_list, loop_body in re.findall(
        r"foreach\s+\w+\s+in\s+array\s+array\[(.*?)\]\s+loop(.*?)end\s+loop",
        sql,
        flags=re.S,
    ):
        if (
            f"'{table}'" in table_list
            and "enable row level security" in loop_body
            and "create policy" in loop_body
        ):
            return True
    return False


def test_initial_migration_creates_complete_domain_skeleton(created_tables: set[str]) -> None:
    assert REQUIRED_TABLES <= created_tables, f"missing PostgreSQL tables: {sorted(REQUIRED_TABLES - created_tables)}"


def test_domain_tables_have_primary_keys_and_foreign_keys(
    normalized_sql: str, created_tables: set[str]
) -> None:
    for table in REQUIRED_TABLES & created_tables:
        body = _table_body(normalized_sql, table)
        assert "primary key" in body, f"{table} needs a primary key"

    required_references = {
        "workspace_members": {"workspaces", "users"},
        "skills": {"workspaces", "users"},
        "skill_versions": {"skills", "users"},
        "runs": {"workspaces", "skill_versions", "pipeline_versions"},
        "run_steps": {"runs"},
        "artifacts": {"runs"},
        "idempotency_keys": {"workspaces"},
        "audit_logs": {"workspaces"},
        "outbox_events": {"workspaces"},
    }
    for table, targets in required_references.items():
        body = _table_body(normalized_sql, table)
        found = set(re.findall(r"references\s+(?:public\.)?\"?([a-z_][a-z0-9_]*)", body))
        assert targets <= found, f"{table} missing foreign keys to {sorted(targets - found)}"


def test_tenant_roots_have_non_null_workspace_ownership_and_rls(normalized_sql: str) -> None:
    workspace_body = _table_body(normalized_sql, "workspaces")
    assert re.search(r"kind\s*=\s*'system'\s+and\s+owner_user_id\s+is\s+null", workspace_body)
    assert re.search(r"kind\s*<>\s*'system'\s+and\s+owner_user_id\s+is\s+not\s+null", workspace_body)

    for table in TENANT_ROOT_TABLES:
        body = _table_body(normalized_sql, table)
        assert re.search(r"workspace_id\s+[^,;]*not\s+null", body), f"{table}.workspace_id must be NOT NULL"
        assert re.search(r"workspace_id\s+[^,;]*references\s+(?:public\.)?workspaces", body), (
            f"{table}.workspace_id must reference workspaces"
        )
        assert re.search(rf"alter\s+table\s+(?:public\.)?{table}\s+enable\s+row\s+level\s+security", normalized_sql), (
            f"{table} must enable RLS"
        )
        assert re.search(rf"create\s+policy\s+[^;]+\s+on\s+(?:public\.)?{table}\b", normalized_sql), (
            f"{table} needs an explicit workspace authorization policy"
        )


def test_single_workspace_release_keeps_reserved_rls_disabled(normalized_sql: str) -> None:
    disable_blocks = [
        (table_list, loop_body)
        for table_list, loop_body in re.findall(
            r"foreach\s+\w+\s+in\s+array\s+array\[(.*?)\]\s+loop(.*?)end\s+loop",
            normalized_sql,
            flags=re.S,
        )
        if "disable row level security" in loop_body
    ]
    assert disable_blocks, "single-workspace bootstrap must disable reserved RLS policies"
    disabled_tables = {
        table
        for table_list, _ in disable_blocks
        for table in re.findall(r"'([a-z_][a-z0-9_]*)'", table_list)
    }
    assert REQUIRED_TABLES <= disabled_tables


def test_uniqueness_scopes_versions_membership_and_idempotency(normalized_sql: str) -> None:
    expectations = {
        "workspace_members": {"workspace_id", "user_id"},
        "skills": {"workspace_id", "slug"},
        "skill_versions": {"skill_id", "version"},
        "render_preset_versions": {"render_preset_id", "version"},
        "pipeline_versions": {"pipeline_id", "version"},
        "idempotency_keys": {"workspace_id", "key"},
    }
    for table, columns in expectations.items():
        body = _table_body(normalized_sql, table)
        table_unique = [
            set(re.findall(r"\b[a-z_][a-z0-9_]*\b", group))
            for group in re.findall(r"(?:unique|primary\s+key)\s*\(([^)]+)\)", body)
        ]
        assert columns in table_unique or _has_index_covering(normalized_sql, table, columns), (
            f"{table} needs a unique constraint/index on {sorted(columns)}"
        )


def test_version_rows_are_immutable_and_content_addressed(normalized_sql: str) -> None:
    for table in ("skill_versions", "render_preset_versions", "pipeline_versions"):
        assert re.search(
            rf"create\s+trigger\s+[^;]+before\s+(?:update|update\s+or\s+delete|delete\s+or\s+update)[^;]+on\s+(?:public\.)?{table}\b",
            normalized_sql,
        ), f"{table} needs an immutability trigger"

    body = _table_body(normalized_sql, "skill_versions")
    assert re.search(r"content_hash\s+[^,;]*not\s+null", body)
    assert "check" in body and "content_hash" in body, "skill_versions.content_hash needs a digest-format CHECK"


def test_skill_version_columns_match_the_declarative_json_contract(normalized_sql: str) -> None:
    skill_body = _table_body(normalized_sql, "skills")
    skill_columns = set(re.findall(r"\b[a-z_][a-z0-9_]*\b", skill_body))
    assert {"ownership_type", "publisher_type", "publisher_name"} <= skill_columns

    body = _table_body(normalized_sql, "skill_versions")
    columns = set(re.findall(r"\b[a-z_][a-z0-9_]*\b", body))
    expected = {
        "input_schema",
        "research_policy",
        "writing_policy",
        "visual_policy",
        "asset_policy",
        "qc_policy",
        "ownership_type",
        "execution_kind",
        "capability_requirements",
        "output_contract",
        "content_hash",
    }
    assert expected <= columns, f"skill_versions diverges from JSON Schema: {sorted(expected - columns)}"
    assert re.search(
        r"version\s+text\s+not\s+null\s+check\s*\(\s*version\s*~",
        body,
    ), "skill_versions.version must persist the SemVer contract"


def test_channel_backend_migration_separates_connections_and_ordered_defaults(
    normalized_sql: str,
) -> None:
    assert "create type channel_status as enum ('active', 'paused', 'archived')" in normalized_sql
    assert "create table platform_connections" in normalized_sql
    assert "secret_ref text not null" in _table_body(normalized_sql, "platform_connections")
    assert "create table channel_default_asset_libraries" in normalized_sql
    assert re.search(r"alter\s+table\s+channels[^;]+add\s+column\s+revision", normalized_sql)
    assert "platform_connection_id" in normalized_sql
    assert "foreign key (workspace_id, platform_connection_id, platform)" in normalized_sql
    assert "platform_connection_id is null or platform is not null" in normalized_sql
    assert _has_index_covering(
        normalized_sql, "channels", {"workspace_id", "platform", "handle"}
    )
    assert re.search(
        r"alter\s+column\s+skill_version_id\s+set\s+not\s+null", normalized_sql
    )
    assert re.search(
        r"alter\s+column\s+pipeline_version_id\s+set\s+not\s+null", normalized_sql
    )
    assert "skill_version_id is null or pipeline_version_id is null" in normalized_sql
    assert "channel backend migration refused" in normalized_sql
    assert (
        "drop constraint if exists "
        "channel_defaults_workspace_id_skill_version_id_fkey" in normalized_sql
    )
    assert (
        "drop constraint if exists "
        "channel_defaults_workspace_id_pipeline_version_id_fkey" in normalized_sql
    )
    assert "foreign key (skill_version_id) references skill_versions(id)" in normalized_sql
    assert "foreign key (pipeline_version_id) references pipeline_versions(id)" in normalized_sql
    assert "create or replace function app.validate_channel_default_versions()" in normalized_sql
    assert "create trigger channel_defaults_validate_versions" in normalized_sql
    assert "v.state = 'published'" in normalized_sql
    assert "p.status = 'active'" in normalized_sql
    assert "s.visibility = 'public_readonly'" in normalized_sql
    assert "p.visibility = 'public_readonly'" in normalized_sql
    assert "w.kind = 'system'" in normalized_sql

    initial_channel_body = _table_body(normalized_sql, "channels")
    assert not {"secret", "token", "password", "credential"}.intersection(
        set(re.findall(r"\b[a-z_][a-z0-9_]*\b", initial_channel_body))
    )


def test_official_skill_forks_can_keep_a_public_system_pipeline(
    normalized_sql: str,
) -> None:
    assert (
        "drop constraint if exists "
        "skill_versions_workspace_id_default_pipeline_version_id_fkey"
        in normalized_sql
    )
    assert (
        "foreign key (default_pipeline_version_id) "
        "references pipeline_versions(id)" in normalized_sql
    )
    assert (
        "create or replace function app.validate_skill_version_default_pipeline()"
        in normalized_sql
    )
    assert "create trigger skill_versions_validate_default_pipeline" in normalized_sql
    assert "pv.workspace_id = new.workspace_id" in normalized_sql
    assert "p.visibility = 'public_readonly'" in normalized_sql
    assert "p.status = 'active'" in normalized_sql
    assert "pv.state = 'published'" in normalized_sql
    assert "skill pipeline constraint audit refused" in normalized_sql
    assert "cardinality(c.conkey) = 2" in normalized_sql
    assert "skill_versions_validate_default_pipeline" in normalized_sql


def test_follow_up_skill_drafts_can_start_from_identical_content(
    normalized_sql: str,
) -> None:
    assert "drop constraint skill_versions_skill_id_content_hash_key" in normalized_sql
    assert "create index skill_versions_skill_id_content_hash_idx" in normalized_sql
    assert "on skill_versions (skill_id, content_hash)" in normalized_sql


def test_full_ai_paid_operations_are_durable_and_never_resubmit_unknown_charges(
    normalized_sql: str,
) -> None:
    run_body = _table_body(normalized_sql, "full_ai_runs")
    operation_body = _table_body(normalized_sql, "full_ai_paid_operations")
    assert {
        "underlying_run_id",
        "request_hash",
        "estimate_fingerprint",
        "authorized_amount_minor",
        "requires_reconciliation",
    } <= set(re.findall(r"\b[a-z_][a-z0-9_]*\b", run_body))
    assert {
        "operation_key",
        "scene_key",
        "variant_index",
        "provider_idempotency_key",
        "provider_request_id",
        "result",
        "submit_unknown",
        "reconciliation_attempts",
    } <= set(re.findall(r"\b[a-z_][a-z0-9_]*\b", operation_body))
    assert "unique (workspace_id, full_ai_run_id, operation_key)" in operation_body
    assert "unique (workspace_id, full_ai_run_id, scene_key, variant_index)" in operation_body
    assert _has_rls_policy(normalized_sql, "full_ai_runs")
    assert _has_rls_policy(normalized_sql, "full_ai_paid_operations")
    assert "submit_unknown must be reconciled and cannot be resubmitted" in normalized_sql
    assert "full_ai_paid_operations_validate_transition" in normalized_sql
    assert "full_ai_paid_operations_validate_budget" in normalized_sql
    assert "full_ai_paid_operations_aggregate_billing" in normalized_sql
    assert "full_ai_paid_operations_prevent_delete" in normalized_sql
    assert "full-ai candidate count exceeds the frozen plan" in normalized_sql
    assert "full-ai paid operation exceeds the run budget" in normalized_sql
    assert "definite_rejection" in normalized_sql
    assert "submitting can be released only after a definite provider rejection" in normalized_sql
    assert "full-ai cost and reconciliation counters are monotonic" in normalized_sql
    assert "status = 'reconciliation_required'" in normalized_sql
    assert "full_ai_paid_operations_result_shape" in normalized_sql
    assert "verification_status" in normalized_sql
    assert "output_content_hash" in normalized_sql
    assert "accepted_artifact" in normalized_sql
    assert "verification_evidence" in normalized_sql
    assert "pg_column_size(result) <= 1048576" in normalized_sql
    assert "full-ai paid operation result checkpoint is immutable" in normalized_sql


def test_recovery_queues_have_claim_and_retry_indexes(normalized_sql: str) -> None:
    assert _has_index_covering(normalized_sql, "run_steps", {"status", "available_at"}), (
        "run_steps needs a status/available_at recovery index"
    )
    run_step_body = _table_body(normalized_sql, "run_steps")
    assert {"attempt_count", "max_attempts", "lease_expires_at"} <= set(
        re.findall(r"\b[a-z_][a-z0-9_]*\b", run_step_body)
    ), "run_steps must persist retry counts and an expiring worker lease"
    assert _has_index_covering(normalized_sql, "outbox_events", {"status", "available_at"}), (
        "outbox_events needs a dispatch/recovery index"
    )


def test_artifact_tenant_key_accepts_only_standard_or_browser_capture_namespace(
    normalized_sql: str,
) -> None:
    assert "drop constraint artifacts_tenant_key" in normalized_sql
    assert (
        "'workspaces/' || workspace_id::text || '/runs/' || run_id::text || '/artifacts/%'"
        in normalized_sql
    )
    assert (
        "'browser-capture/workspaces/' || workspace_id::text || '/runs/' || "
        "run_id::text || '/artifacts/%'" in normalized_sql
    )
    assert r"object_key !~ '(^|/)\.\.(/|$)'" in normalized_sql


def test_audit_and_outbox_are_workspace_scoped_and_append_only(normalized_sql: str) -> None:
    for table in ("audit_logs", "outbox_events"):
        body = _table_body(normalized_sql, table)
        assert re.search(r"workspace_id\s+[^,;]*not\s+null", body)
        assert _has_rls_policy(normalized_sql, table), f"{table} needs RLS and a tenant policy"
    assert re.search(r"create\s+trigger\s+[^;]+on\s+(?:public\.)?audit_logs\b", normalized_sql), (
        "audit_logs needs an append-only protection trigger"
    )
