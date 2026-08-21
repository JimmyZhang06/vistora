-- Formal asset content-management state, optimistic concurrency, jobs and usage.
-- This migration never promotes quarantined media and never deletes object records.

ALTER TABLE assets
  ADD COLUMN IF NOT EXISTS revision integer NOT NULL DEFAULT 1,
  ADD COLUMN IF NOT EXISTS analysis_status text NOT NULL DEFAULT 'pending',
  ADD COLUMN IF NOT EXISTS deleted_at timestamptz;

ALTER TABLE assets DROP CONSTRAINT IF EXISTS assets_revision_check;
ALTER TABLE assets ADD CONSTRAINT assets_revision_check CHECK (revision > 0);
ALTER TABLE assets DROP CONSTRAINT IF EXISTS assets_analysis_status_check;
ALTER TABLE assets ADD CONSTRAINT assets_analysis_status_check
  CHECK (analysis_status IN ('pending', 'running', 'completed', 'failed'));

UPDATE assets SET status='disabled' WHERE status='archived';
ALTER TABLE assets DROP CONSTRAINT IF EXISTS assets_status_check;
ALTER TABLE assets ADD CONSTRAINT assets_status_check
  CHECK (status IN ('processing', 'quarantined', 'ready', 'disabled', 'deleted'));

UPDATE assets a
SET analysis_status = CASE
  WHEN EXISTS (
    SELECT 1 FROM asset_analyses aa
    WHERE aa.workspace_id=a.workspace_id AND aa.asset_id=a.id AND aa.status='completed'
  ) THEN 'completed'
  WHEN EXISTS (
    SELECT 1 FROM asset_analyses aa
    WHERE aa.workspace_id=a.workspace_id AND aa.asset_id=a.id AND aa.status='failed'
  ) THEN 'failed'
  ELSE 'pending'
END;

-- Fail closed: legacy ready rows that do not satisfy every gate become quarantined.
UPDATE assets a
SET status='quarantined', revision=revision+1, updated_at=now()
WHERE a.status='ready' AND (
  a.copyright_status NOT IN ('owned', 'licensed', 'public_domain')
  OR NOT EXISTS (
    SELECT 1 FROM asset_files af
    WHERE af.workspace_id=a.workspace_id AND af.asset_id=a.id
      AND af.deleted_at IS NULL AND af.scan_status='clean'
  )
  OR a.analysis_status <> 'completed'
);

UPDATE assets SET deleted_at=COALESCE(deleted_at, updated_at) WHERE status='deleted';
ALTER TABLE assets DROP CONSTRAINT IF EXISTS assets_deleted_at_shape_check;
ALTER TABLE assets ADD CONSTRAINT assets_deleted_at_shape_check CHECK (
  (status='deleted' AND deleted_at IS NOT NULL)
  OR (status<>'deleted' AND deleted_at IS NULL)
);

CREATE INDEX IF NOT EXISTS assets_library_management_idx
  ON assets (workspace_id, library_id, updated_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS assets_filter_idx
  ON assets (workspace_id, library_id, status, kind, copyright_status, analysis_status);
CREATE INDEX IF NOT EXISTS assets_text_search_idx
  ON assets USING gin (to_tsvector('simple', title || ' ' || description));

ALTER TABLE asset_sources
  ADD COLUMN IF NOT EXISTS evidence_type text NOT NULL DEFAULT 'provenance',
  ADD COLUMN IF NOT EXISTS verified_at timestamptz,
  ADD COLUMN IF NOT EXISTS verified_by uuid REFERENCES users(id) ON DELETE SET NULL;

CREATE TABLE IF NOT EXISTS asset_analysis_jobs (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  workspace_id uuid NOT NULL REFERENCES workspaces(id) ON DELETE RESTRICT,
  asset_id uuid NOT NULL,
  kind text NOT NULL DEFAULT 'reanalysis' CHECK (kind IN ('initial_analysis', 'reanalysis')),
  status text NOT NULL DEFAULT 'queued'
    CHECK (status IN ('queued', 'running', 'succeeded', 'failed', 'cancelled')),
  reason text NOT NULL,
  requested_by uuid REFERENCES users(id) ON DELETE SET NULL,
  queue_job_id text,
  error jsonb,
  created_at timestamptz NOT NULL DEFAULT now(),
  started_at timestamptz,
  completed_at timestamptz,
  updated_at timestamptz NOT NULL DEFAULT now(),
  FOREIGN KEY (workspace_id, asset_id)
    REFERENCES assets(workspace_id, id) ON DELETE RESTRICT,
  UNIQUE (workspace_id, id)
);
CREATE INDEX IF NOT EXISTS asset_analysis_jobs_asset_idx
  ON asset_analysis_jobs (workspace_id, asset_id, created_at DESC, id DESC);

CREATE TABLE IF NOT EXISTS asset_usage_records (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  workspace_id uuid NOT NULL REFERENCES workspaces(id) ON DELETE RESTRICT,
  asset_id uuid NOT NULL,
  run_id uuid,
  step_id uuid,
  usage_type text NOT NULL,
  details jsonb NOT NULL DEFAULT '{}'::jsonb,
  occurred_at timestamptz NOT NULL DEFAULT now(),
  FOREIGN KEY (workspace_id, asset_id)
    REFERENCES assets(workspace_id, id) ON DELETE RESTRICT,
  FOREIGN KEY (run_id) REFERENCES runs(id) ON DELETE SET NULL,
  FOREIGN KEY (step_id) REFERENCES run_steps(id) ON DELETE SET NULL,
  UNIQUE (workspace_id, id)
);
CREATE INDEX IF NOT EXISTS asset_usage_records_asset_idx
  ON asset_usage_records (workspace_id, asset_id, occurred_at DESC, id DESC);

ALTER TABLE asset_analysis_jobs ENABLE ROW LEVEL SECURITY;
ALTER TABLE asset_usage_records ENABLE ROW LEVEL SECURITY;
CREATE POLICY asset_analysis_jobs_tenant_access ON asset_analysis_jobs
  USING (app.can_access_workspace(workspace_id))
  WITH CHECK (app.can_write_workspace(workspace_id));
CREATE POLICY asset_usage_records_tenant_access ON asset_usage_records
  USING (app.can_access_workspace(workspace_id))
  WITH CHECK (app.can_write_workspace(workspace_id));

-- Match the single-workspace phase used by the other v2 control-plane tables.
ALTER TABLE asset_analysis_jobs DISABLE ROW LEVEL SECURITY;
ALTER TABLE asset_usage_records DISABLE ROW LEVEL SECURITY;
