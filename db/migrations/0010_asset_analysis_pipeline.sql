-- Recoverable, review-gated analysis for already-ingested assets.
-- Source objects remain immutable and assets remain quarantined until the
-- final publish transaction has stored a complete analysis.

ALTER TABLE assets DROP CONSTRAINT IF EXISTS assets_status_check;
ALTER TABLE assets ADD CONSTRAINT assets_status_check
  CHECK (status IN ('processing', 'ready', 'quarantined', 'awaiting_review', 'archived'));

CREATE TABLE asset_analysis_batches (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  workspace_id uuid NOT NULL REFERENCES workspaces(id) ON DELETE RESTRICT,
  idempotency_key text NOT NULL,
  status text NOT NULL DEFAULT 'pending'
    CHECK (status IN ('pending', 'running', 'cancellation_requested', 'completed',
                      'completed_with_errors', 'cancelled')),
  dry_run boolean NOT NULL DEFAULT true,
  requested_limit integer NOT NULL CHECK (requested_limit BETWEEN 1 AND 100),
  rate_limit_per_minute integer NOT NULL CHECK (rate_limit_per_minute BETWEEN 1 AND 600),
  pipeline_version text NOT NULL,
  configuration jsonb NOT NULL DEFAULT '{}'::jsonb,
  total_count integer NOT NULL DEFAULT 0 CHECK (total_count >= 0),
  pending_count integer NOT NULL DEFAULT 0 CHECK (pending_count >= 0),
  running_count integer NOT NULL DEFAULT 0 CHECK (running_count >= 0),
  awaiting_review_count integer NOT NULL DEFAULT 0 CHECK (awaiting_review_count >= 0),
  ready_count integer NOT NULL DEFAULT 0 CHECK (ready_count >= 0),
  failed_count integer NOT NULL DEFAULT 0 CHECK (failed_count >= 0),
  cancelled_count integer NOT NULL DEFAULT 0 CHECK (cancelled_count >= 0),
  cancel_requested_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  started_at timestamptz,
  completed_at timestamptz,
  updated_at timestamptz NOT NULL DEFAULT now(),
  revision bigint NOT NULL DEFAULT 0,
  UNIQUE (workspace_id, id),
  UNIQUE (workspace_id, idempotency_key)
);

CREATE TABLE asset_analysis_items (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  workspace_id uuid NOT NULL REFERENCES workspaces(id) ON DELETE RESTRICT,
  batch_id uuid NOT NULL,
  asset_id uuid NOT NULL,
  source_content_hash text NOT NULL CHECK (source_content_hash ~ '^[a-f0-9]{64}$'),
  pipeline_version text NOT NULL,
  status text NOT NULL DEFAULT 'pending'
    CHECK (status IN ('pending', 'running', 'retry_wait', 'awaiting_review',
                      'ready', 'failed', 'cancelled')),
  current_stage text NOT NULL DEFAULT 'file_detection',
  completed_stages text[] NOT NULL DEFAULT '{}',
  checkpoints jsonb NOT NULL DEFAULT '{}'::jsonb,
  review_reasons text[] NOT NULL DEFAULT '{}',
  attempts integer NOT NULL DEFAULT 0 CHECK (attempts >= 0),
  max_attempts integer NOT NULL DEFAULT 3 CHECK (max_attempts BETWEEN 1 AND 20),
  failure_code text,
  failure_reason text,
  retryable boolean NOT NULL DEFAULT false,
  available_at timestamptz NOT NULL DEFAULT now(),
  lease_owner text,
  lease_expires_at timestamptz,
  started_at timestamptz,
  completed_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  revision bigint NOT NULL DEFAULT 0,
  FOREIGN KEY (workspace_id, batch_id)
    REFERENCES asset_analysis_batches(workspace_id, id) ON DELETE CASCADE,
  FOREIGN KEY (workspace_id, asset_id)
    REFERENCES assets(workspace_id, id) ON DELETE RESTRICT,
  UNIQUE (workspace_id, id),
  UNIQUE (workspace_id, batch_id, asset_id)
);

CREATE INDEX asset_analysis_items_claim_idx
  ON asset_analysis_items (batch_id, status, available_at, created_at)
  WHERE status IN ('pending', 'retry_wait', 'running');
CREATE UNIQUE INDEX asset_analysis_items_active_source_uq
  ON asset_analysis_items (workspace_id, asset_id, pipeline_version, source_content_hash)
  WHERE status IN ('pending', 'running', 'retry_wait', 'awaiting_review');

CREATE TABLE asset_review_actions (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  workspace_id uuid NOT NULL REFERENCES workspaces(id) ON DELETE RESTRICT,
  item_id uuid NOT NULL,
  asset_id uuid NOT NULL,
  decision text NOT NULL CHECK (decision IN ('approve', 'reject', 'request_changes')),
  actor_id uuid REFERENCES users(id) ON DELETE SET NULL,
  comment text NOT NULL DEFAULT '',
  previous_status text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  FOREIGN KEY (workspace_id, item_id)
    REFERENCES asset_analysis_items(workspace_id, id) ON DELETE RESTRICT,
  FOREIGN KEY (workspace_id, asset_id)
    REFERENCES assets(workspace_id, id) ON DELETE RESTRICT,
  UNIQUE (workspace_id, id)
);

ALTER TABLE asset_analysis_batches ENABLE ROW LEVEL SECURITY;
ALTER TABLE asset_analysis_items ENABLE ROW LEVEL SECURITY;
ALTER TABLE asset_review_actions ENABLE ROW LEVEL SECURITY;
CREATE POLICY asset_analysis_batches_tenant_access ON asset_analysis_batches
  USING (app.can_access_workspace(workspace_id))
  WITH CHECK (app.can_write_workspace(workspace_id));
CREATE POLICY asset_analysis_items_tenant_access ON asset_analysis_items
  USING (app.can_access_workspace(workspace_id))
  WITH CHECK (app.can_write_workspace(workspace_id));
CREATE POLICY asset_review_actions_tenant_access ON asset_review_actions
  USING (app.can_access_workspace(workspace_id))
  WITH CHECK (app.can_write_workspace(workspace_id));

-- Match the current single-workspace control-plane posture.
ALTER TABLE asset_analysis_batches DISABLE ROW LEVEL SECURITY;
ALTER TABLE asset_analysis_items DISABLE ROW LEVEL SECURITY;
ALTER TABLE asset_review_actions DISABLE ROW LEVEL SECURITY;
