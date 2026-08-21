# FrameFactory Control API

This package is the second-generation HTTP control plane for FrameFactory. It currently runs as a
single-user, single-workspace service while keeping the context and repository seams needed for a
future deployment model.

## Current scope

- `GET /healthz` and `GET /v1/context`
- account profile and creation-preference reads/writes with `ETag` concurrency
- session inventory/revocation and hashed API key creation/list/revocation
- Skill create, list, read, optimistic-concurrency replace, and delete
- editable SkillVersion drafts, validation, RFC 8785 content hashing, and immutable publication
- forking any ready/published Skill version, including ordinary `ownership_type=system` catalog data
- queryable deterministic local Skill test executions
- idempotent reference-based Run creation plus list/read; clients never submit snapshot hashes
- asset-library upload plus controlled public-link import from YouTube, Bilibili, and RedNote
- contract-shaped errors and cursor pagination

The API validates persisted Skill, SkillVersion, and Run resources against
`packages/contracts/schemas/v1`. It never branches on an official Skill ID. Official catalog and
user-created resources use the same resource schemas, repository, and execution references.

## Single-workspace behavior

`WorkspaceContextProvider` is the forward-compatible context seam. The active
`DefaultWorkspaceContextProvider` always returns the installation's default user and workspace.
`X-Workspace-Id` is optional and ignored; it cannot select another tenant.

Browser clients may call the API directly. Development permits localhost origins on any port;
deployments configure `FRAMEFACTORY_CORS_ALLOW_ORIGINS` (comma-separated) and optionally
`FRAMEFACTORY_CORS_ALLOW_ORIGIN_REGEX`. `ETag`, `Location`, and `X-Request-Id` are exposed.

Create/update request models are deliberately distinct from persisted resources. The server owns
IDs, workspace/ownership metadata, lifecycle state, revisions, timestamps, content hashes, and Run
composition snapshots. Draft writes use the numeric `ETag` in `If-Match`; a stale write returns
`412 REVISION_CONFLICT`. Create/action requests use `Idempotency-Key`; reuse with a different body
returns `409 IDEMPOTENCY_KEY_REUSED`.

The fallback IDs are deterministic development IDs derived with UUID v5, not product accounts or
an account list. A production installation must set `FRAMEFACTORY_DEFAULT_USER_ID` and
`FRAMEFACTORY_DEFAULT_WORKSPACE_ID`, or replace the provider with a persisted first-run bootstrap.
It can also set `FRAMEFACTORY_DEFAULT_WORKSPACE_NAME`,
`FRAMEFACTORY_DEFAULT_USER_EMAIL`, and `FRAMEFACTORY_DEFAULT_USER_DISPLAY_NAME`. Startup creates
missing account preferences but never overwrites profile edits made by the user.

The development app discovers `packages/seeds/official-skills/v1/manifest.json`
and loads that validated catalog into the same repository used for user Skills.
Packaged deployments set `FRAMEFACTORY_OFFICIAL_SEED_MANIFEST` to the copied
manifest path; absence of a catalog never enables a hidden built-in fallback.

Workspace CRUD, members, invitations, tenant selection, RLS policy, and cross-workspace APIs are
not enabled. The corresponding paths/header in the shared OpenAPI file are reserved contract
surface, not current product behavior.

## Run locally

The default development profile uses the in-process repository and does not require external
services. It is intentionally non-durable and is suitable only for tests and local UI work.

```powershell
python -m pip install -e ".\apps\api[dev]"
python -m uvicorn framefactory_api.main:app --reload --port 8200
```

To exercise durable persistence locally, start the infrastructure stack, copy
`apps/api/.env.example` into your preferred secret/env loader, then set the repository backend to
`postgresql` and enable Redis and object storage:

```powershell
docker compose -f deploy/docker-compose.persistence.yml up -d
$env:FRAMEFACTORY_REPOSITORY_BACKEND = "postgresql"
$env:FRAMEFACTORY_DATABASE_URL = "postgresql://framefactory:framefactory-local-only@localhost:5432/framefactory"
$env:FRAMEFACTORY_REDIS_ENABLED = "true"
$env:FRAMEFACTORY_REDIS_URL = "redis://localhost:6379/0"
$env:FRAMEFACTORY_OBJECT_STORAGE_ENABLED = "true"
$env:FRAMEFACTORY_S3_BUCKET = "framefactory"
$env:FRAMEFACTORY_S3_ENDPOINT_URL = "http://localhost:9000"
$env:FRAMEFACTORY_S3_ACCESS_KEY_ID = "framefactory"
$env:FRAMEFACTORY_S3_SECRET_ACCESS_KEY = "framefactory-local-only"
$env:FRAMEFACTORY_S3_ADDRESSING_STYLE = "path"
$env:FRAMEFACTORY_S3_VERIFY_TLS = "false"
python -m uvicorn framefactory_api.main:app --port 8200
```

The Compose stack runs a one-shot `migrate` service after PostgreSQL becomes healthy. For a
host-managed database, run the same upgrade gate before starting API or Worker processes:

```powershell
$env:FRAMEFACTORY_DATABASE_URL = "postgresql://framefactory:change-me@localhost:5432/framefactory"
python -m framefactory_api.migrate --project-root .
```

The runner applies `db/migrations/*.sql` followed by `services/worker/migrations/*.sql`, records
their SHA-256 checksums in `schema_migrations`, and serializes deploys with a PostgreSQL advisory
lock. Never edit an applied SQL file: checksum drift intentionally blocks startup; add a new
migration instead. A pre-ledger installation is baselined only when the runner can prove the
expected tables and columns already exist. A partial legacy schema fails closed for manual review.

`FRAMEFACTORY_DATABASE_URL`, `FRAMEFACTORY_REDIS_URL`, and the `FRAMEFACTORY_S3_*` variables in
`.env.example` match the host ports and development credentials in that Compose file. If the API
runs in the same Compose network, replace `localhost` with service names `postgres`, `redis`, and
`minio` respectively.

Run checks from `apps/api`:

```powershell
python -m pytest
python -m ruff check .
```

## Import a public video URL

`POST /v1/asset-imports` accepts a user-supplied public YouTube, Bilibili, or Xiaohongshu link
after explicit usage-rights confirmation. The API downloads without browser cookies, rejects
playlists/live/private/paid/DRM access, limits a file to 500 MB and one hour, verifies SHA-256,
streams it into S3/R2, and stores platform/source/author/license/retrieval provenance with the
ready asset. YouTube and Bilibili use the pinned `yt-dlp` runtime dependency. Xiaohongshu links
are resolved through `https://rednote-downloader.online/api/check`; this is an undocumented
third-party boundary and therefore fails closed if its response shape or availability changes.

This endpoint imports an explicit URL; it does not automatically search arbitrary platform
results when a scene has no match. Search-based acquisition needs a separate provider policy so
licensing, attribution, ranking, quotas, and duplicate control remain auditable.

## Rebuild and re-index source assets

The v2 importer reads media files themselves and never trusts v1 catalog/tag JSON. It rejects
repository-wide scans and generated output roots, validates media with FFprobe, stores each unique
file under a SHA-256 content address in S3/R2, and records source provenance separately. Unknown
rights stay quarantined and are not eligible for automatic rendering.

```powershell
$env:FRAMEFACTORY_DATABASE_URL = "postgresql://framefactory:framefactory-local-only@localhost:5432/framefactory_v2"
$env:FRAMEFACTORY_S3_BUCKET = "framefactory-v2"
$env:FRAMEFACTORY_S3_ENDPOINT_URL = "http://localhost:9000"
$env:FRAMEFACTORY_S3_ACCESS_KEY_ID = "framefactory"
$env:FRAMEFACTORY_S3_SECRET_ACCESS_KEY = "framefactory-local-only"
$env:FRAMEFACTORY_S3_ADDRESSING_STYLE = "path"
$env:FRAMEFACTORY_S3_VERIFY_TLS = "false"
python -m framefactory_api.asset_ingest `
  --source ..\src\material-library `
  --library-slug reindexed-source-media `
  --copyright-status unknown
```

Add `--tag --limit 5` for a cost-bounded visual-analysis pilot. Visual tagging requires
`FRAMEFACTORY_VISION_API_KEY` (or its `_FILE` form); every analysis is versioned, videos receive
time-bounded scene segments, and provider output is normalized before persistence. Run a pilot and
review its accuracy before expanding the limit. Do not mark an asset `owned`, `licensed`, or
`public_domain`, or its file scan `clean`, without the corresponding evidence and safety check.

## Production persistence boundary

`ControlRepository` is the application port. `InMemoryControlRepository` is intentionally limited
to local UI integration and tests: it loses state on restart and its lock only protects one Python
process. `FRAMEFACTORY_ENV=production` refuses to start with that backend and defaults
`FRAMEFACTORY_REPOSITORY_BACKEND` to `postgresql`; a production installation must provide
`FRAMEFACTORY_DATABASE_URL`.

The PostgreSQL adapter provides:

- unique `(workspace_id, slug)` and `(skill_id, version)` constraints;
- a transaction for fork creation and version publication;
- a durable idempotency table with request fingerprints and response resource IDs;
- optimistic concurrency/version checks for mutable Skill metadata and drafts;
- an outbox record in the same transaction as Run creation for worker dispatch.
- durable profile/preferences revisions, session revocation, and API key metadata. API key
  plaintext is returned only by the create response; PostgreSQL stores only its SHA-256 hash.

Redis and S3/R2 remain independently configurable because API-only development does not require
them. Set `FRAMEFACTORY_REDIS_ENABLED=true` and `FRAMEFACTORY_OBJECT_STORAGE_ENABLED=true` in a
worker-capable deployment. Startup verifies every enabled dependency, `/healthz` rechecks it, and
lifespan shutdown closes owned database and Redis connections. Bucket creation is disabled by
default in production-style configuration; use `FRAMEFACTORY_S3_CREATE_BUCKET=true` only for
controlled bootstrap environments such as the local MinIO stack.

The stage-one Skill test evaluator is explicitly `local_deterministic_v1`: it verifies and compares
stored declarative policies without invoking a model or network. Its resource already exposes
queued/running/succeeded/failed states so a worker-backed evaluator can replace it later.

Published Pipeline versions are persisted and validated alongside Skills. Run creation resolves the
selected SkillVersion and PipelineVersion from the repository, permits only the current workspace or
published system seeds, and records their real content hashes in the immutable composition snapshot.
Rendering itself remains a Worker provider capability and fails closed until a production render
plugin is configured.

Authentication should be added by supplying another `WorkspaceContextProvider`; repository and
service methods already require a resolved context. This boundary does not imply that multi-tenant
product features are currently supported. Creating an API key currently manages credentials but
does not by itself enable request authentication. Two-factor authentication is deliberately
reported as unavailable instead of exposing a non-functional setting.
