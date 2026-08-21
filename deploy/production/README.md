# Production deployment

This directory is the hardened self-hosted baseline for the FrameFactory v3 API, Worker,
PostgreSQL, Redis, and S3-compatible storage. The browser app is deployed separately and must use
an HTTPS `NEXT_PUBLIC_FRAMEFACTORY_API_URL`.

## Required boundary

The API container binds to `127.0.0.1` by design. Put an HTTPS reverse proxy and an identity-aware
gateway in front of it; do not expose the control API directly. API-key records in account settings
are management data and are not a substitute for request authentication.

Create one secret file per value, copy `.env.example` to a machine-local `.env`, and set:

- stable UUIDs for `FF_DEFAULT_USER_ID` and `FF_DEFAULT_WORKSPACE_ID`;
- the exact browser origin in `FF_CORS_ALLOW_ORIGINS`;
- PostgreSQL and Redis password/URL files;
- S3 access-key and secret-key files.

The database URL and Redis URL files must contain complete URLs. Percent-encode reserved
characters in passwords. Secret files and `.env` must not be committed.

## Deploy

```powershell
docker compose --env-file deploy/production/.env `
  -f deploy/production/compose.yml config --quiet
docker compose --env-file deploy/production/.env `
  -f deploy/production/compose.yml build --pull
docker compose --env-file deploy/production/.env `
  -f deploy/production/compose.yml up -d
```

The one-shot `migrate` service owns schema changes. API and Worker start only after its
checksum-verified migrations finish successfully. Never edit an applied SQL migration.

## Release gates

Before accepting a release, run unit/build checks followed by the fail-closed gates under
`tools/release/`:

```powershell
python tools/release/security_gate.py
python tools/release/api_e2e_gate.py
python tools/release/recovery_gate.py
python tools/release/postgres_restore_gate.py
python tools/release/s3_integrity_gate.py
```

These commands intentionally return `BLOCKED` when their target URLs, fixture payloads, isolated
restore database, AWS CLI credentials, or external provider capabilities are absent. A blocked gate
is not a pass. Use dedicated non-production restore targets; the recovery gate deliberately stops
and restarts the configured Worker service.

## Backup and rollback

- Back up PostgreSQL with `pg_dump --format=custom` and retain the matching release/migration set.
- Back up the object bucket with versioning or immutable replication enabled.
- Verify each backup by restoring into an empty disposable database and comparing tables and row
  counts with `postgres_restore_gate.py`.
- Roll back application images only when the retained database schema is compatible. Database
  rollback is restore-based; migrations are forward-only.

Provider credentials, reverse-proxy certificates, DNS, identity policy, monitoring, alerting, and
off-host backup retention are installation-specific and are therefore not embedded in this repo.
