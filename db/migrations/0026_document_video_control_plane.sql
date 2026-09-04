BEGIN;

CREATE TABLE document_sources (
  id uuid PRIMARY KEY,
  workspace_id uuid NOT NULL REFERENCES workspaces(id) ON DELETE RESTRICT,
  filename text NOT NULL CHECK (
    length(filename) BETWEEN 1 AND 255
    AND filename !~ '[/\\]'
    AND lower(filename) LIKE '%.pdf'
  ),
  media_type text NOT NULL CHECK (media_type = 'application/pdf'),
  byte_size bigint NOT NULL CHECK (byte_size BETWEEN 1 AND 209715200),
  content_hash text NOT NULL CHECK (content_hash ~ '^[a-f0-9]{64}$'),
  storage_provider text NOT NULL CHECK (storage_provider = 's3'),
  bucket text NOT NULL CHECK (length(bucket) BETWEEN 1 AND 255),
  object_key text NOT NULL,
  rights_confirmed boolean NOT NULL CHECK (rights_confirmed),
  status text NOT NULL CHECK (status IN ('uploading', 'uploaded', 'rejected')),
  validation jsonb NOT NULL DEFAULT '{"state":"pending_worker_inspection"}'::jsonb,
  revision integer NOT NULL DEFAULT 1 CHECK (revision > 0),
  created_by uuid REFERENCES users(id) ON DELETE SET NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  uploaded_at timestamptz,
  updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (workspace_id, id),
  UNIQUE (workspace_id, object_key),
  CONSTRAINT document_sources_tenant_key CHECK (
    object_key LIKE 'workspaces/' || workspace_id::text || '/%'
    AND object_key !~ '(^|/)\.\.(/|$)'
  ),
  CONSTRAINT document_sources_upload_state CHECK (
    (status = 'uploading' AND uploaded_at IS NULL)
    OR (status IN ('uploaded', 'rejected') AND uploaded_at IS NOT NULL)
  )
);

CREATE INDEX document_sources_workspace_created_idx
  ON document_sources (workspace_id, created_at DESC, id DESC);

ALTER TABLE document_sources ENABLE ROW LEVEL SECURITY;
CREATE POLICY document_sources_workspace_isolation ON document_sources
  USING (app.can_access_workspace(workspace_id))
  WITH CHECK (app.can_write_workspace(workspace_id));

COMMIT;
