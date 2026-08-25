-- Immutable, content-addressed asset catalogs for reproducible generation batches,
-- plus the durable coordinator record for building a library outside a Run.

BEGIN;

CREATE TABLE catalog_snapshots (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  workspace_id uuid NOT NULL REFERENCES workspaces(id) ON DELETE RESTRICT,
  library_ids uuid[] NOT NULL CHECK (cardinality(library_ids) > 0),
  content_hash char(64) NOT NULL CHECK (content_hash ~ '^[a-f0-9]{64}$'),
  item_count integer NOT NULL CHECK (item_count >= 0),
  created_by uuid REFERENCES users(id) ON DELETE SET NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (workspace_id, id),
  UNIQUE (workspace_id, content_hash)
);

-- These keys let each item prove that its frozen file and analysis both belong
-- to the same asset, rather than merely existing somewhere in the workspace.
CREATE UNIQUE INDEX asset_files_workspace_asset_id_id_uq
  ON asset_files (workspace_id, asset_id, id);
CREATE UNIQUE INDEX asset_analyses_workspace_asset_id_id_uq
  ON asset_analyses (workspace_id, asset_id, id);

CREATE TABLE catalog_snapshot_items (
  snapshot_id uuid NOT NULL,
  workspace_id uuid NOT NULL REFERENCES workspaces(id) ON DELETE RESTRICT,
  ordinal integer NOT NULL CHECK (ordinal >= 0),
  library_id uuid NOT NULL,
  asset_id uuid NOT NULL,
  asset_file_id uuid NOT NULL,
  analysis_id uuid NOT NULL,
  content_hash char(64) NOT NULL CHECK (content_hash ~ '^[a-f0-9]{64}$'),
  PRIMARY KEY (snapshot_id, ordinal),
  FOREIGN KEY (workspace_id, snapshot_id)
    REFERENCES catalog_snapshots(workspace_id, id) ON DELETE RESTRICT,
  FOREIGN KEY (workspace_id, library_id, asset_id)
    REFERENCES assets(workspace_id, library_id, id) ON DELETE RESTRICT,
  FOREIGN KEY (workspace_id, asset_id, asset_file_id)
    REFERENCES asset_files(workspace_id, asset_id, id) ON DELETE RESTRICT,
  FOREIGN KEY (workspace_id, asset_id, analysis_id)
    REFERENCES asset_analyses(workspace_id, asset_id, id) ON DELETE RESTRICT,
  UNIQUE (workspace_id, snapshot_id, asset_id),
  UNIQUE (workspace_id, snapshot_id, asset_file_id),
  UNIQUE (workspace_id, snapshot_id, analysis_id)
);

CREATE INDEX catalog_snapshot_items_library_idx
  ON catalog_snapshot_items (workspace_id, snapshot_id, library_id, ordinal);

ALTER TABLE generation_batches
  ADD COLUMN catalog_snapshot_id uuid;
ALTER TABLE generation_batches
  ADD CONSTRAINT generation_batches_catalog_snapshot_fk
  FOREIGN KEY (workspace_id, catalog_snapshot_id)
  REFERENCES catalog_snapshots(workspace_id, id) ON DELETE RESTRICT;
CREATE INDEX generation_batches_catalog_snapshot_idx
  ON generation_batches (workspace_id, catalog_snapshot_id)
  WHERE catalog_snapshot_id IS NOT NULL;

CREATE TABLE library_build_jobs (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  workspace_id uuid NOT NULL REFERENCES workspaces(id) ON DELETE RESTRICT,
  library_id uuid NOT NULL,
  status text NOT NULL DEFAULT 'queued'
    CHECK (status IN (
      'queued', 'running', 'completed', 'completed_with_errors', 'failed', 'cancelled'
    )),
  stage text NOT NULL DEFAULT 'discover'
    CHECK (stage IN ('discover', 'transfer', 'analyze', 'index')),
  spec jsonb NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(spec) = 'object'),
  progress jsonb NOT NULL DEFAULT
    '{"discovered":0,"transferred":0,"analyzed":0,"indexed":0,"failed":0,"asset_ids":[]}'::jsonb
    CHECK (
      jsonb_typeof(progress) = 'object'
      AND jsonb_typeof(progress->'asset_ids') = 'array'
    ),
  error jsonb CHECK (error IS NULL OR jsonb_typeof(error) = 'object'),
  idempotency_key text NOT NULL CHECK (btrim(idempotency_key) <> ''),
  request_hash char(64) NOT NULL CHECK (request_hash ~ '^[a-f0-9]{64}$'),
  revision bigint NOT NULL DEFAULT 1 CHECK (revision > 0),
  created_by uuid REFERENCES users(id) ON DELETE SET NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  started_at timestamptz,
  completed_at timestamptz,
  updated_at timestamptz NOT NULL DEFAULT now(),
  FOREIGN KEY (workspace_id, library_id)
    REFERENCES asset_libraries(workspace_id, id) ON DELETE RESTRICT,
  UNIQUE (workspace_id, id),
  UNIQUE (workspace_id, idempotency_key),
  CONSTRAINT library_build_jobs_completion_shape CHECK (
    (status IN ('completed', 'completed_with_errors', 'failed', 'cancelled')) =
      (completed_at IS NOT NULL)
  )
);

CREATE INDEX library_build_jobs_claim_idx
  ON library_build_jobs (workspace_id, status, created_at, id)
  WHERE status IN ('queued', 'running');
CREATE INDEX library_build_jobs_library_created_idx
  ON library_build_jobs (workspace_id, library_id, created_at DESC, id DESC);

CREATE OR REPLACE FUNCTION app.protect_catalog_snapshot()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  RAISE EXCEPTION 'Catalog snapshots are immutable'
    USING ERRCODE = '55000';
END;
$$;

CREATE TRIGGER catalog_snapshots_immutable
  BEFORE UPDATE OR DELETE ON catalog_snapshots
  FOR EACH ROW EXECUTE FUNCTION app.protect_catalog_snapshot();
CREATE TRIGGER catalog_snapshot_items_immutable
  BEFORE UPDATE OR DELETE ON catalog_snapshot_items
  FOR EACH ROW EXECUTE FUNCTION app.protect_catalog_snapshot();

ALTER TABLE catalog_snapshots ENABLE ROW LEVEL SECURITY;
ALTER TABLE catalog_snapshot_items ENABLE ROW LEVEL SECURITY;
ALTER TABLE library_build_jobs ENABLE ROW LEVEL SECURITY;
CREATE POLICY catalog_snapshots_tenant_access ON catalog_snapshots
  USING (app.can_access_workspace(workspace_id))
  WITH CHECK (app.can_write_workspace(workspace_id));
CREATE POLICY catalog_snapshot_items_tenant_access ON catalog_snapshot_items
  USING (app.can_access_workspace(workspace_id))
  WITH CHECK (app.can_write_workspace(workspace_id));
CREATE POLICY library_build_jobs_tenant_access ON library_build_jobs
  USING (app.can_access_workspace(workspace_id))
  WITH CHECK (app.can_write_workspace(workspace_id));

-- Match the current single-workspace control-plane posture.
ALTER TABLE catalog_snapshots DISABLE ROW LEVEL SECURITY;
ALTER TABLE catalog_snapshot_items DISABLE ROW LEVEL SECURITY;
ALTER TABLE library_build_jobs DISABLE ROW LEVEL SECURITY;

COMMIT;
