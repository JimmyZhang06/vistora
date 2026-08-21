# FrameFactory contracts

Contract version `1.0.0` is defined with JSON Schema 2020-12 in `schemas/v1` and
OpenAPI 3.1 in `openapi/v1.yaml`.

Persisted resource schemas and client command schemas are separate. In particular,
`skill-create`, `skill-version-create`, `skill-version-patch`, `channel-write`, `run-create`, and
`skill-test-execution-create` omit all server-generated identity, ownership, lifecycle, revision,
hash, and timestamp fields. The canonical OpenAPI uses those command schemas for request bodies and
the resource schemas for responses.

Every business resource keeps a workspace ownership field, while the current
runtime resolves one configured personal workspace and does not expose workspace
switching or membership management. `X-Workspace-Id` is therefore optional and
reserved for a future mode. Official and user-published skills use the same
`Skill` and `SkillVersion` schemas and the same `/v1/skills` API;
`ownership_type` and `publisher_type` are data, not execution branches.

Worker steps persist an immutable, content-addressed `input_snapshot` before
delivery. Retries and restart recovery reuse that snapshot and the step
`idempotency_key`; outputs are referenced as `Artifact` resources rather than
embedded mutable paths. The worker-facing Queue, RunStore, and ObjectStore ports
are intentionally infrastructure-neutral so production adapters can target
Redis, PostgreSQL, and S3 while tests use deterministic in-memory implementations.

`SkillVersion` is deliberately non-executable. Its closed schema supports only
declarative input, research, writing, visual, asset, quality, capability, and
output policies. It has no command, module, handler, script, callback, or local
path field. External research references are HTTPS URLs and stored assets are
referenced by UUID.

## SkillVersion content hash

`content_hash` is the lowercase hexadecimal SHA-256 digest of the UTF-8 bytes of
an [RFC 8785 JSON Canonicalization Scheme (JCS)](https://www.rfc-editor.org/rfc/rfc8785)
object containing exactly these nine keys and their values:

1. `input_schema`
2. `research_policy`
3. `writing_policy`
4. `visual_policy`
5. `asset_policy`
6. `qc_policy`
7. `capability_requirements`
8. `output_contract`
9. `default_pipeline_version_id`

The hash domain excludes identity and lifecycle metadata, including
`schema_version`, `id`, `workspace_id`, `ownership_type`, `skill_id`, `version`,
`state`, `execution_kind`, `content_hash` itself, `created_by`, `created_at`, and
`published_at`. The server computes the digest once when persisting an immutable
version; clients and workers may recompute it to verify the snapshot.

All schemas have stable canonical `$id` values rooted at
`https://schemas.framefactory.dev/v1/`. Consumers should register the files in
`schemas/v1` under those identifiers before validation. Each resource schema
contains at least one valid example fixture; examples are illustrative contract
data, not production seed data.
