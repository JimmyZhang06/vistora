BEGIN;

ALTER TABLE document_sources
  DROP CONSTRAINT document_sources_upload_state,
  DROP CONSTRAINT document_sources_status_check;

ALTER TABLE document_sources
  ADD COLUMN upload_expires_at timestamptz,
  ADD COLUMN retention_until timestamptz,
  ADD COLUMN legal_hold boolean NOT NULL DEFAULT false,
  ADD COLUMN legal_hold_reason text,
  ADD COLUMN legal_hold_set_by uuid REFERENCES users(id) ON DELETE SET NULL,
  ADD COLUMN legal_hold_set_at timestamptz,
  ADD COLUMN deletion_requested_at timestamptz,
  ADD COLUMN purged_at timestamptz,
  ADD CONSTRAINT document_sources_status_check CHECK (
    status IN ('uploading', 'uploaded', 'rejected', 'deletion_pending', 'purging', 'purged')
  ),
  ADD CONSTRAINT document_sources_upload_state CHECK (
    (status = 'uploading' AND uploaded_at IS NULL)
    OR (status <> 'uploading' AND uploaded_at IS NOT NULL)
  ),
  ADD CONSTRAINT document_sources_legal_hold_shape CHECK (
    (NOT legal_hold AND legal_hold_reason IS NULL AND legal_hold_set_by IS NULL
      AND legal_hold_set_at IS NULL)
    OR (legal_hold AND legal_hold_reason IS NOT NULL
      AND length(btrim(legal_hold_reason)) BETWEEN 1 AND 1000
      AND legal_hold_set_at IS NOT NULL)
  ),
  ADD CONSTRAINT document_sources_purge_shape CHECK (
    (status = 'purged') = (purged_at IS NOT NULL)
  );

-- Existing presigned PUTs can be valid for up to seven days.  Preserve a
-- conservative replay fence when upgrading rows created before the expiry was
-- persisted; new rows store the exact grant expiry.
UPDATE document_sources
SET upload_expires_at = created_at + interval '7 days'
WHERE upload_expires_at IS NULL;
ALTER TABLE document_sources ALTER COLUMN upload_expires_at SET NOT NULL;

CREATE TABLE document_purge_requests (
  id uuid PRIMARY KEY,
  workspace_id uuid NOT NULL REFERENCES workspaces(id) ON DELETE RESTRICT,
  source_id uuid NOT NULL,
  status text NOT NULL CHECK (
    status IN ('queued', 'purging', 'retrying', 'blocked', 'failed', 'succeeded')
  ),
  reason text NOT NULL CHECK (length(btrim(reason)) BETWEEN 1 AND 1000),
  delete_derived boolean NOT NULL DEFAULT true CHECK (delete_derived),
  idempotency_key text NOT NULL CHECK (length(idempotency_key) BETWEEN 1 AND 512),
  request_fingerprint char(64) NOT NULL CHECK (request_fingerprint ~ '^[a-f0-9]{64}$'),
  requested_by uuid REFERENCES users(id) ON DELETE SET NULL,
  attempt_count integer NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
  max_attempts integer NOT NULL DEFAULT 8 CHECK (max_attempts BETWEEN 1 AND 100),
  next_attempt_at timestamptz NOT NULL DEFAULT now(),
  lease_owner text,
  lease_token uuid,
  lease_expires_at timestamptz,
  last_error jsonb,
  source_object_deleted_at timestamptz,
  derived_objects_deleted_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  started_at timestamptz,
  completed_at timestamptz,
  updated_at timestamptz NOT NULL DEFAULT now(),
  FOREIGN KEY (workspace_id, source_id)
    REFERENCES document_sources(workspace_id, id) ON DELETE RESTRICT,
  UNIQUE (workspace_id, id),
  UNIQUE (workspace_id, idempotency_key),
  CONSTRAINT document_purge_requests_lease_shape CHECK (
    (status = 'purging' AND lease_owner IS NOT NULL AND lease_token IS NOT NULL
      AND lease_expires_at IS NOT NULL)
    OR (status <> 'purging' AND lease_owner IS NULL AND lease_token IS NULL
      AND lease_expires_at IS NULL)
  ),
  CONSTRAINT document_purge_requests_completion_shape CHECK (
    (status IN ('failed', 'succeeded') AND completed_at IS NOT NULL)
    OR (status NOT IN ('failed', 'succeeded') AND completed_at IS NULL)
  )
);

CREATE INDEX document_purge_requests_claim_idx
  ON document_purge_requests (next_attempt_at, created_at, id)
  WHERE status IN ('queued', 'retrying');
CREATE INDEX document_purge_requests_source_idx
  ON document_purge_requests (workspace_id, source_id, created_at DESC);

ALTER TABLE document_purge_requests ENABLE ROW LEVEL SECURITY;
CREATE POLICY document_purge_requests_workspace_isolation ON document_purge_requests
  USING (app.can_access_workspace(workspace_id))
  WITH CHECK (app.can_write_workspace(workspace_id));

COMMIT;
