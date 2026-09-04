-- Durable, workspace-scoped pilot evidence for application and customer validation.
-- One mutable record belongs to one webpage-video Run; revision provides optimistic
-- concurrency and idempotency_keys protects write replays.

BEGIN;

CREATE TABLE webpage_video_pilot_feedback (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  workspace_id uuid NOT NULL REFERENCES workspaces(id) ON DELETE RESTRICT,
  webpage_video_run_id uuid NOT NULL,
  customer_segment text NOT NULL
    CHECK (char_length(customer_segment) BETWEEN 1 AND 120),
  baseline_minutes integer NOT NULL CHECK (baseline_minutes BETWEEN 1 AND 10080),
  assisted_minutes integer NOT NULL CHECK (assisted_minutes BETWEEN 1 AND 10080),
  revision_count integer NOT NULL CHECK (revision_count BETWEEN 0 AND 100),
  outcome text NOT NULL CHECK (outcome IN ('evaluating', 'adopted', 'rejected')),
  satisfaction_score integer CHECK (satisfaction_score BETWEEN 1 AND 5),
  willingness_to_pay_hkd integer CHECK (willingness_to_pay_hkd BETWEEN 0 AND 1000000),
  notes text CHECK (notes IS NULL OR char_length(notes) <= 2000),
  revision bigint NOT NULL DEFAULT 1 CHECK (revision > 0),
  created_by uuid REFERENCES users(id) ON DELETE SET NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  FOREIGN KEY (workspace_id, webpage_video_run_id)
    REFERENCES webpage_video_runs(workspace_id, id) ON DELETE RESTRICT,
  UNIQUE (workspace_id, id),
  UNIQUE (workspace_id, webpage_video_run_id)
);

CREATE INDEX webpage_video_pilot_feedback_workspace_updated_idx
  ON webpage_video_pilot_feedback (workspace_id, updated_at DESC, id DESC);

ALTER TABLE webpage_video_pilot_feedback ENABLE ROW LEVEL SECURITY;
CREATE POLICY webpage_video_pilot_feedback_tenant_access
  ON webpage_video_pilot_feedback
  USING (app.can_access_workspace(workspace_id))
  WITH CHECK (app.can_write_workspace(workspace_id));

-- Match the current single-workspace control-plane posture while keeping the
-- future tenant policy explicit and testable.
ALTER TABLE webpage_video_pilot_feedback DISABLE ROW LEVEL SECURITY;

COMMIT;
