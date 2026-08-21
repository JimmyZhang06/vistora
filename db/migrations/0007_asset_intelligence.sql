-- Versioned asset ingestion and semantic analysis. Source media is immutable;
-- analyses can be replaced without rewriting provenance or file identity.

CREATE EXTENSION IF NOT EXISTS pg_trgm;

CREATE TABLE IF NOT EXISTS asset_ingestion_jobs (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  workspace_id uuid NOT NULL REFERENCES workspaces(id) ON DELETE RESTRICT,
  library_id uuid NOT NULL,
  source_root text NOT NULL,
  status text NOT NULL DEFAULT 'pending'
    CHECK (status IN ('pending', 'running', 'completed', 'completed_with_errors', 'failed')),
  discovered_count integer NOT NULL DEFAULT 0 CHECK (discovered_count >= 0),
  imported_count integer NOT NULL DEFAULT 0 CHECK (imported_count >= 0),
  deduplicated_count integer NOT NULL DEFAULT 0 CHECK (deduplicated_count >= 0),
  tagged_count integer NOT NULL DEFAULT 0 CHECK (tagged_count >= 0),
  rejected_count integer NOT NULL DEFAULT 0 CHECK (rejected_count >= 0),
  error jsonb,
  requested_by uuid REFERENCES users(id) ON DELETE SET NULL,
  started_at timestamptz,
  completed_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  FOREIGN KEY (workspace_id, library_id)
    REFERENCES asset_libraries(workspace_id, id) ON DELETE RESTRICT,
  UNIQUE (workspace_id, id)
);

CREATE INDEX IF NOT EXISTS asset_ingestion_jobs_status_idx
  ON asset_ingestion_jobs (workspace_id, status, created_at DESC);

CREATE TABLE IF NOT EXISTS asset_analyses (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  workspace_id uuid NOT NULL REFERENCES workspaces(id) ON DELETE RESTRICT,
  asset_id uuid NOT NULL,
  analysis_version integer NOT NULL CHECK (analysis_version > 0),
  provider text NOT NULL,
  model text NOT NULL,
  status text NOT NULL DEFAULT 'completed'
    CHECK (status IN ('pending', 'completed', 'failed', 'superseded')),
  summary text NOT NULL DEFAULT '',
  language text,
  people text[] NOT NULL DEFAULT '{}',
  organizations text[] NOT NULL DEFAULT '{}',
  locations text[] NOT NULL DEFAULT '{}',
  eras text[] NOT NULL DEFAULT '{}',
  scene_types text[] NOT NULL DEFAULT '{}',
  actions text[] NOT NULL DEFAULT '{}',
  moods text[] NOT NULL DEFAULT '{}',
  visual_styles text[] NOT NULL DEFAULT '{}',
  keywords text[] NOT NULL DEFAULT '{}',
  has_embedded_text boolean NOT NULL DEFAULT false,
  has_watermark boolean NOT NULL DEFAULT false,
  safety jsonb NOT NULL DEFAULT '{}'::jsonb,
  quality jsonb NOT NULL DEFAULT '{}'::jsonb,
  confidence numeric(4,3) CHECK (confidence IS NULL OR confidence BETWEEN 0 AND 1),
  search_text text NOT NULL DEFAULT '',
  raw_result jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now(),
  FOREIGN KEY (workspace_id, asset_id)
    REFERENCES assets(workspace_id, id) ON DELETE CASCADE,
  UNIQUE (workspace_id, id),
  UNIQUE (workspace_id, asset_id, analysis_version)
);

CREATE INDEX IF NOT EXISTS asset_analyses_asset_version_idx
  ON asset_analyses (workspace_id, asset_id, analysis_version DESC);
CREATE INDEX IF NOT EXISTS asset_analyses_search_idx
  ON asset_analyses USING gin (search_text gin_trgm_ops);
CREATE INDEX IF NOT EXISTS asset_analyses_keywords_idx
  ON asset_analyses USING gin (keywords);
CREATE INDEX IF NOT EXISTS asset_analyses_people_idx
  ON asset_analyses USING gin (people);

CREATE TABLE IF NOT EXISTS asset_segments (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  workspace_id uuid NOT NULL REFERENCES workspaces(id) ON DELETE RESTRICT,
  asset_id uuid NOT NULL,
  analysis_id uuid NOT NULL,
  ordinal integer NOT NULL CHECK (ordinal >= 0),
  start_ms bigint NOT NULL CHECK (start_ms >= 0),
  end_ms bigint NOT NULL CHECK (end_ms > start_ms),
  description text NOT NULL,
  people text[] NOT NULL DEFAULT '{}',
  locations text[] NOT NULL DEFAULT '{}',
  keywords text[] NOT NULL DEFAULT '{}',
  scene_type text,
  action text,
  era text,
  mood text,
  visual_style text,
  shot_type text,
  confidence numeric(4,3) CHECK (confidence IS NULL OR confidence BETWEEN 0 AND 1),
  representative_frame_key text,
  search_text text NOT NULL DEFAULT '',
  created_at timestamptz NOT NULL DEFAULT now(),
  FOREIGN KEY (workspace_id, asset_id)
    REFERENCES assets(workspace_id, id) ON DELETE CASCADE,
  FOREIGN KEY (workspace_id, analysis_id)
    REFERENCES asset_analyses(workspace_id, id) ON DELETE CASCADE,
  UNIQUE (workspace_id, id),
  UNIQUE (workspace_id, analysis_id, ordinal)
);

CREATE INDEX IF NOT EXISTS asset_segments_time_idx
  ON asset_segments (workspace_id, asset_id, start_ms, end_ms);
CREATE INDEX IF NOT EXISTS asset_segments_search_idx
  ON asset_segments USING gin (search_text gin_trgm_ops);
CREATE INDEX IF NOT EXISTS asset_segments_keywords_idx
  ON asset_segments USING gin (keywords);

ALTER TABLE asset_tags
  ADD COLUMN IF NOT EXISTS analysis_id uuid,
  ADD COLUMN IF NOT EXISTS confidence numeric(4,3),
  ADD COLUMN IF NOT EXISTS source text NOT NULL DEFAULT 'manual';

ALTER TABLE asset_tags
  DROP CONSTRAINT IF EXISTS asset_tags_confidence_check;
ALTER TABLE asset_tags
  ADD CONSTRAINT asset_tags_confidence_check
  CHECK (confidence IS NULL OR confidence BETWEEN 0 AND 1);
ALTER TABLE asset_tags
  DROP CONSTRAINT IF EXISTS asset_tags_analysis_fk;
ALTER TABLE asset_tags
  ADD CONSTRAINT asset_tags_analysis_fk
  FOREIGN KEY (workspace_id, analysis_id)
  REFERENCES asset_analyses(workspace_id, id) ON DELETE SET NULL;

CREATE UNIQUE INDEX IF NOT EXISTS asset_files_workspace_content_hash_uq
  ON asset_files (workspace_id, content_hash)
  WHERE deleted_at IS NULL;

ALTER TABLE asset_ingestion_jobs ENABLE ROW LEVEL SECURITY;
ALTER TABLE asset_analyses ENABLE ROW LEVEL SECURITY;
ALTER TABLE asset_segments ENABLE ROW LEVEL SECURITY;

CREATE POLICY asset_ingestion_jobs_tenant_access ON asset_ingestion_jobs
  USING (app.can_access_workspace(workspace_id))
  WITH CHECK (app.can_write_workspace(workspace_id));
CREATE POLICY asset_analyses_tenant_access ON asset_analyses
  USING (app.can_access_workspace(workspace_id))
  WITH CHECK (app.can_write_workspace(workspace_id));
CREATE POLICY asset_segments_tenant_access ON asset_segments
  USING (app.can_access_workspace(workspace_id))
  WITH CHECK (app.can_write_workspace(workspace_id));

-- Single-workspace phase: retain policy definitions but leave enforcement off,
-- matching the v2 control-plane tables until workspace selection is enabled.
ALTER TABLE asset_ingestion_jobs DISABLE ROW LEVEL SECURITY;
ALTER TABLE asset_analyses DISABLE ROW LEVEL SECURITY;
ALTER TABLE asset_segments DISABLE ROW LEVEL SECURITY;
