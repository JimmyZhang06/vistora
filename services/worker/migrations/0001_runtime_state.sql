BEGIN;

ALTER TABLE runs
  ADD COLUMN IF NOT EXISTS worker_revision bigint NOT NULL DEFAULT 0,
  ADD COLUMN IF NOT EXISTS worker_error jsonb;

ALTER TABLE run_steps
  ADD COLUMN IF NOT EXISTS worker_step_id text,
  ADD COLUMN IF NOT EXISTS dependencies text[] NOT NULL DEFAULT '{}',
  ADD COLUMN IF NOT EXISTS retry_policy jsonb NOT NULL DEFAULT '{"base_delay_seconds":1,"max_delay_seconds":300,"multiplier":2}',
  ADD COLUMN IF NOT EXISTS output_artifacts jsonb NOT NULL DEFAULT '[]',
  ADD COLUMN IF NOT EXISTS review_required boolean NOT NULL DEFAULT false,
  ADD COLUMN IF NOT EXISTS review jsonb,
  ADD COLUMN IF NOT EXISTS cancellation_requested_at timestamptz,
  ADD COLUMN IF NOT EXISTS worker_revision bigint NOT NULL DEFAULT 0;

CREATE UNIQUE INDEX IF NOT EXISTS run_steps_workspace_worker_id_uq
  ON run_steps (workspace_id, worker_step_id);

CREATE INDEX IF NOT EXISTS run_steps_worker_recovery_idx
  ON run_steps (status, available_at, lease_expires_at)
  WHERE worker_step_id IS NOT NULL
    AND status IN ('queued', 'retrying', 'running');

COMMIT;
