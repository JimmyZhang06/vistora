-- Durable subtitle/ASR timelines and cut-safety evidence for semantic video segments.

CREATE TABLE IF NOT EXISTS asset_transcripts (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  workspace_id uuid NOT NULL REFERENCES workspaces(id) ON DELETE RESTRICT,
  asset_id uuid NOT NULL,
  analysis_id uuid NOT NULL,
  source text NOT NULL CHECK (source IN ('embedded_subtitle', 'asr', 'aligned')),
  language text,
  provider text,
  model text,
  status text NOT NULL CHECK (status IN ('available', 'unavailable', 'failed')),
  full_text text NOT NULL DEFAULT '',
  confidence numeric(4,3) CHECK (confidence IS NULL OR confidence BETWEEN 0 AND 1),
  word_count integer NOT NULL DEFAULT 0 CHECK (word_count >= 0),
  raw_result jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now(),
  FOREIGN KEY (workspace_id, asset_id)
    REFERENCES assets(workspace_id, id) ON DELETE CASCADE,
  FOREIGN KEY (workspace_id, analysis_id)
    REFERENCES asset_analyses(workspace_id, id) ON DELETE CASCADE,
  UNIQUE (workspace_id, id),
  UNIQUE (workspace_id, analysis_id, source)
);

CREATE TABLE IF NOT EXISTS asset_transcript_cues (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  workspace_id uuid NOT NULL REFERENCES workspaces(id) ON DELETE RESTRICT,
  asset_id uuid NOT NULL,
  transcript_id uuid NOT NULL,
  ordinal integer NOT NULL CHECK (ordinal >= 0),
  start_ms bigint NOT NULL CHECK (start_ms >= 0),
  end_ms bigint NOT NULL CHECK (end_ms > start_ms),
  text text NOT NULL,
  confidence numeric(4,3) CHECK (confidence IS NULL OR confidence BETWEEN 0 AND 1),
  source text NOT NULL CHECK (source IN ('embedded_subtitle', 'asr', 'aligned', 'temporal')),
  created_at timestamptz NOT NULL DEFAULT now(),
  FOREIGN KEY (workspace_id, asset_id)
    REFERENCES assets(workspace_id, id) ON DELETE CASCADE,
  FOREIGN KEY (workspace_id, transcript_id)
    REFERENCES asset_transcripts(workspace_id, id) ON DELETE CASCADE,
  UNIQUE (workspace_id, id),
  UNIQUE (workspace_id, transcript_id, ordinal)
);

CREATE TABLE IF NOT EXISTS asset_shot_boundaries (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  workspace_id uuid NOT NULL REFERENCES workspaces(id) ON DELETE RESTRICT,
  asset_id uuid NOT NULL,
  analysis_id uuid NOT NULL,
  timestamp_ms bigint NOT NULL CHECK (timestamp_ms >= 0),
  score numeric(4,3) NOT NULL CHECK (score BETWEEN 0 AND 1),
  reasons text[] NOT NULL DEFAULT '{}',
  cut_safe boolean NOT NULL DEFAULT false,
  semantic_complete boolean NOT NULL DEFAULT false,
  created_at timestamptz NOT NULL DEFAULT now(),
  FOREIGN KEY (workspace_id, asset_id)
    REFERENCES assets(workspace_id, id) ON DELETE CASCADE,
  FOREIGN KEY (workspace_id, analysis_id)
    REFERENCES asset_analyses(workspace_id, id) ON DELETE CASCADE,
  UNIQUE (workspace_id, id),
  UNIQUE (workspace_id, analysis_id, timestamp_ms)
);

ALTER TABLE asset_segments
  ADD COLUMN IF NOT EXISTS transcript text NOT NULL DEFAULT '',
  ADD COLUMN IF NOT EXISTS boundary_score numeric(4,3),
  ADD COLUMN IF NOT EXISTS boundary_reasons text[] NOT NULL DEFAULT '{}',
  ADD COLUMN IF NOT EXISTS cut_safe boolean NOT NULL DEFAULT false,
  ADD COLUMN IF NOT EXISTS semantic_complete boolean NOT NULL DEFAULT false;

ALTER TABLE asset_segments DROP CONSTRAINT IF EXISTS asset_segments_boundary_score_check;
ALTER TABLE asset_segments ADD CONSTRAINT asset_segments_boundary_score_check
  CHECK (boundary_score IS NULL OR boundary_score BETWEEN 0 AND 1);

CREATE INDEX IF NOT EXISTS asset_transcripts_asset_idx
  ON asset_transcripts (workspace_id, asset_id, created_at DESC);
CREATE INDEX IF NOT EXISTS asset_transcript_cues_time_idx
  ON asset_transcript_cues (workspace_id, asset_id, start_ms, end_ms);
CREATE INDEX IF NOT EXISTS asset_shot_boundaries_time_idx
  ON asset_shot_boundaries (workspace_id, asset_id, timestamp_ms);
CREATE INDEX IF NOT EXISTS asset_segments_cut_safe_idx
  ON asset_segments (workspace_id, asset_id, cut_safe, semantic_complete)
  WHERE cut_safe;

ALTER TABLE asset_transcripts ENABLE ROW LEVEL SECURITY;
ALTER TABLE asset_transcript_cues ENABLE ROW LEVEL SECURITY;
ALTER TABLE asset_shot_boundaries ENABLE ROW LEVEL SECURITY;

CREATE POLICY asset_transcripts_tenant_access ON asset_transcripts
  USING (app.can_access_workspace(workspace_id))
  WITH CHECK (app.can_write_workspace(workspace_id));
CREATE POLICY asset_transcript_cues_tenant_access ON asset_transcript_cues
  USING (app.can_access_workspace(workspace_id))
  WITH CHECK (app.can_write_workspace(workspace_id));
CREATE POLICY asset_shot_boundaries_tenant_access ON asset_shot_boundaries
  USING (app.can_access_workspace(workspace_id))
  WITH CHECK (app.can_write_workspace(workspace_id));

-- Single-workspace phase: retain policies but defer enforcement with the rest of v3.
ALTER TABLE asset_transcripts DISABLE ROW LEVEL SECURITY;
ALTER TABLE asset_transcript_cues DISABLE ROW LEVEL SECURITY;
ALTER TABLE asset_shot_boundaries DISABLE ROW LEVEL SECURITY;
