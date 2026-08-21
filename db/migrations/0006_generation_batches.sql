-- Durable batch control plane. Every item owns a normal Run so execution,
-- recovery, review and artifact semantics remain on the existing Run model.

CREATE TABLE IF NOT EXISTS generation_batches (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  workspace_id uuid NOT NULL REFERENCES workspaces(id) ON DELETE RESTRICT,
  name text NOT NULL CHECK (char_length(name) BETWEEN 1 AND 160),
  composition_snapshot jsonb NOT NULL,
  requested_by uuid REFERENCES users(id) ON DELETE SET NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (workspace_id, id)
);

CREATE TABLE IF NOT EXISTS generation_batch_items (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  workspace_id uuid NOT NULL REFERENCES workspaces(id) ON DELETE RESTRICT,
  batch_id uuid NOT NULL,
  run_id uuid NOT NULL,
  ordinal integer NOT NULL CHECK (ordinal >= 0),
  label text NOT NULL CHECK (char_length(label) BETWEEN 1 AND 300),
  input_snapshot jsonb NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  FOREIGN KEY (workspace_id, batch_id)
    REFERENCES generation_batches(workspace_id, id) ON DELETE RESTRICT,
  FOREIGN KEY (workspace_id, run_id)
    REFERENCES runs(workspace_id, id) ON DELETE RESTRICT,
  UNIQUE (workspace_id, id),
  UNIQUE (batch_id, ordinal),
  UNIQUE (workspace_id, run_id)
);

CREATE INDEX IF NOT EXISTS generation_batches_workspace_created_idx
  ON generation_batches (workspace_id, created_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS generation_batch_items_batch_ordinal_idx
  ON generation_batch_items (workspace_id, batch_id, ordinal);
