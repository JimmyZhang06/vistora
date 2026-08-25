-- Converge databases created by the two pre-release 0022 variants.
-- Existing databases already have the active-run index; fresh databases get it here.

BEGIN;

CREATE INDEX IF NOT EXISTS webpage_video_runs_active_idx
  ON webpage_video_runs (workspace_id, status, updated_at)
  WHERE status IN (
    'queued', 'validating', 'capturing', 'awaiting_review',
    'rendering', 'quality_check'
  );

COMMENT ON COLUMN webpage_video_runs.status IS
  'Creation-side control-plane state; runs.status and run_steps are authoritative after dispatch.';

COMMIT;
