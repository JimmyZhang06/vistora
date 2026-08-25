-- Dedicated webpage-video control plane. Browser capture attempts are immutable
-- evidence; the durable scheduler Run remains the execution substrate.

BEGIN;

CREATE TABLE webpage_video_runs (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  workspace_id uuid NOT NULL REFERENCES workspaces(id) ON DELETE RESTRICT,
  underlying_run_id uuid NOT NULL,
  status text NOT NULL DEFAULT 'queued'
    CHECK (status IN (
      'queued', 'validating', 'capturing', 'awaiting_review',
      'rendering', 'quality_check', 'succeeded', 'failed', 'cancelled'
    )),
  requested_url text NOT NULL
    CHECK (char_length(requested_url) BETWEEN 9 AND 2048),
  normalized_url text NOT NULL
    CHECK (
      char_length(normalized_url) BETWEEN 9 AND 2048
      AND normalized_url ~ '^https://'
    ),
  spec jsonb NOT NULL
    CHECK (jsonb_typeof(spec) = 'object' AND pg_column_size(spec) <= 65536),
  revision bigint NOT NULL DEFAULT 1 CHECK (revision > 0),
  idempotency_key text NOT NULL
    CHECK (char_length(idempotency_key) BETWEEN 8 AND 255),
  request_hash char(64) NOT NULL CHECK (request_hash ~ '^[a-f0-9]{64}$'),
  created_by uuid REFERENCES users(id) ON DELETE SET NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  FOREIGN KEY (workspace_id, underlying_run_id)
    REFERENCES runs(workspace_id, id) ON DELETE RESTRICT,
  UNIQUE (workspace_id, id),
  UNIQUE (workspace_id, underlying_run_id),
  UNIQUE (workspace_id, idempotency_key)
);

COMMENT ON COLUMN webpage_video_runs.status IS
  'Creation-side control-plane state; runs.status and run_steps are authoritative after dispatch.';

CREATE TABLE webpage_capture_attempts (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  workspace_id uuid NOT NULL REFERENCES workspaces(id) ON DELETE RESTRICT,
  webpage_video_run_id uuid NOT NULL,
  underlying_run_id uuid NOT NULL,
  screenshot_step_id uuid NOT NULL,
  attempt_number integer NOT NULL CHECK (attempt_number > 0),
  capture_revision bigint NOT NULL CHECK (capture_revision > 0),
  outcome text NOT NULL CHECK (outcome IN ('captured', 'failed')),
  requested_url text NOT NULL
    CHECK (char_length(requested_url) BETWEEN 9 AND 2048),
  final_url text,
  viewport_width integer NOT NULL CHECK (viewport_width BETWEEN 320 AND 4096),
  viewport_height integer NOT NULL CHECK (viewport_height BETWEEN 320 AND 4096),
  full_page boolean NOT NULL DEFAULT false CHECK (full_page = false),
  artifact_id uuid,
  sha256 char(64),
  media_type text,
  metadata jsonb NOT NULL DEFAULT '{}'::jsonb
    CHECK (jsonb_typeof(metadata) = 'object' AND pg_column_size(metadata) <= 1048576),
  error jsonb,
  captured_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  FOREIGN KEY (workspace_id, webpage_video_run_id)
    REFERENCES webpage_video_runs(workspace_id, id) ON DELETE RESTRICT,
  FOREIGN KEY (workspace_id, underlying_run_id)
    REFERENCES runs(workspace_id, id) ON DELETE RESTRICT,
  FOREIGN KEY (workspace_id, underlying_run_id, screenshot_step_id)
    REFERENCES run_steps(workspace_id, run_id, id) ON DELETE RESTRICT,
  FOREIGN KEY (workspace_id, underlying_run_id, artifact_id)
    REFERENCES artifacts(workspace_id, run_id, id) ON DELETE RESTRICT,
  UNIQUE (workspace_id, id),
  UNIQUE (workspace_id, webpage_video_run_id, id),
  UNIQUE (workspace_id, webpage_video_run_id, attempt_number),
  UNIQUE (workspace_id, artifact_id),
  CONSTRAINT webpage_capture_attempt_run_binding CHECK (
    webpage_video_run_id IS NOT NULL AND underlying_run_id IS NOT NULL
  ),
  CONSTRAINT webpage_capture_attempt_result_shape CHECK (
    CASE WHEN outcome = 'captured' THEN
      artifact_id IS NOT NULL
      AND sha256 IS NOT NULL
      AND sha256 ~ '^[a-f0-9]{64}$'
      AND media_type IS NOT NULL
      AND media_type IN ('image/png', 'image/jpeg', 'image/webp')
      AND final_url IS NOT NULL
      AND final_url ~ '^https://'
      AND captured_at IS NOT NULL
      AND error IS NULL
    ELSE
      artifact_id IS NULL
      AND sha256 IS NULL
      AND media_type IS NULL
      AND captured_at IS NULL
      AND error IS NOT NULL
      AND jsonb_typeof(error) = 'object'
    END
  )
);

CREATE INDEX webpage_video_runs_workspace_created_idx
  ON webpage_video_runs (workspace_id, created_at DESC, id DESC);
CREATE INDEX webpage_capture_attempts_history_idx
  ON webpage_capture_attempts (
    workspace_id, webpage_video_run_id, attempt_number DESC, created_at DESC
  );

CREATE OR REPLACE FUNCTION app.prevent_webpage_capture_attempt_mutation()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
  RAISE EXCEPTION 'webpage_capture_attempts are append-only';
END;
$$;

CREATE TRIGGER webpage_capture_attempts_prevent_update
  BEFORE UPDATE ON webpage_capture_attempts
  FOR EACH ROW EXECUTE FUNCTION app.prevent_webpage_capture_attempt_mutation();
CREATE TRIGGER webpage_capture_attempts_prevent_delete
  BEFORE DELETE ON webpage_capture_attempts
  FOR EACH ROW EXECUTE FUNCTION app.prevent_webpage_capture_attempt_mutation();

ALTER TABLE webpage_video_runs ENABLE ROW LEVEL SECURITY;
ALTER TABLE webpage_capture_attempts ENABLE ROW LEVEL SECURITY;
CREATE POLICY webpage_video_runs_tenant_access ON webpage_video_runs
  USING (app.can_access_workspace(workspace_id))
  WITH CHECK (app.can_write_workspace(workspace_id));
CREATE POLICY webpage_capture_attempts_tenant_access ON webpage_capture_attempts
  USING (app.can_access_workspace(workspace_id))
  WITH CHECK (app.can_write_workspace(workspace_id));

-- Match the current single-workspace control-plane posture.
ALTER TABLE webpage_video_runs DISABLE ROW LEVEL SECURITY;
ALTER TABLE webpage_capture_attempts DISABLE ROW LEVEL SECURITY;

COMMIT;
