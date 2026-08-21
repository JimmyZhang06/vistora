# FrameFactory database

`migrations/0001_initial.sql` is the PostgreSQL 15+ schema migration. Apply it with
`psql -v ON_ERROR_STOP=1 -f db/migrations/0001_initial.sql`.

`seeds/official-seed.example.json` defines the neutral import envelope for
official resources. It intentionally contains no user, account, credential,
business-specific resource, or fixed UUID. Importers must resolve the target
system workspace by configuration and allocate identifiers at import time.

The seed resource arrays use stable slugs plus positive version numbers as
in-file references. Importers allocate UUIDs, resolve those natural references,
validate each version against `packages/contracts`, and import the entire
manifest in one transaction. Official and user resources use the same tables;
system ownership is represented by `workspaces.kind = 'system'`.

The current runtime deliberately operates in one configured personal workspace.
`workspace_id` remains mandatory on business resources as a forward-compatible
ownership boundary, but the initial release does not enable PostgreSQL RLS,
workspace switching, membership management, or cross-workspace administration.

The control API resolves its configured workspace through a context provider.
Future multi-workspace deployments can set transaction context without changing
resource contracts:

```sql
SET LOCAL app.user_id = '<authenticated user UUID>';
SET LOCAL app.workspace_id = '<authorized workspace UUID>';
```

`app.system_actor` is reserved for trusted internal transactions and must never
be exposed as a client-controlled setting. RLS, membership authorization, and
workspace selection require a later opt-in migration and must not be inferred
from the presence of `workspace_id` alone. Queue consumers claim
`run_steps`/`outbox_events` with `FOR UPDATE SKIP LOCKED` and write a lease token
and expiry in the same transaction.
