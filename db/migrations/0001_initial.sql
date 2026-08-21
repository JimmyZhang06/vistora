-- FrameFactory PostgreSQL 15+ initial schema.
-- This migration is transactional and deliberately contains no tenant data.

BEGIN;

CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE SCHEMA IF NOT EXISTS app;

CREATE TYPE workspace_kind AS ENUM ('personal', 'team', 'system');
CREATE TYPE workspace_role AS ENUM ('owner', 'admin', 'editor', 'reviewer', 'viewer');
CREATE TYPE member_status AS ENUM ('invited', 'active', 'suspended');
CREATE TYPE ownership_type AS ENUM ('workspace', 'system');
CREATE TYPE publisher_type AS ENUM ('user', 'system');
CREATE TYPE resource_visibility AS ENUM ('private', 'workspace', 'public_readonly');
CREATE TYPE resource_status AS ENUM ('draft', 'active', 'archived');
CREATE TYPE version_state AS ENUM
  ('draft', 'validating', 'ready', 'published', 'deprecated', 'rejected');
CREATE TYPE run_status AS ENUM
  ('queued', 'running', 'awaiting_review', 'succeeded', 'failed', 'cancelled');
CREATE TYPE step_status AS ENUM
  ('queued', 'running', 'awaiting_review', 'retrying', 'succeeded', 'failed', 'cancelled');
CREATE TYPE artifact_status AS ENUM ('pending', 'available', 'quarantined', 'deleted');
CREATE TYPE delivery_status AS ENUM ('pending', 'processing', 'delivered', 'retrying', 'failed', 'cancelled');

CREATE FUNCTION app.current_user_id() RETURNS uuid
LANGUAGE sql STABLE PARALLEL SAFE
AS $$ SELECT NULLIF(current_setting('app.user_id', true), '')::uuid $$;

CREATE FUNCTION app.current_workspace_id() RETURNS uuid
LANGUAGE sql STABLE PARALLEL SAFE
AS $$ SELECT NULLIF(current_setting('app.workspace_id', true), '')::uuid $$;

CREATE FUNCTION app.is_system_actor() RETURNS boolean
LANGUAGE sql STABLE PARALLEL SAFE
AS $$ SELECT COALESCE(NULLIF(current_setting('app.system_actor', true), '')::boolean, false) $$;

CREATE TABLE users (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  email text NOT NULL,
  display_name text NOT NULL,
  password_hash text,
  email_verified_at timestamptz,
  disabled_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT users_email_not_blank CHECK (btrim(email) <> '')
);
CREATE UNIQUE INDEX users_email_lower_uq ON users (lower(email));

CREATE TABLE workspaces (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  kind workspace_kind NOT NULL DEFAULT 'team',
  slug text NOT NULL,
  name text NOT NULL,
  owner_user_id uuid REFERENCES users(id) ON DELETE RESTRICT,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  archived_at timestamptz,
  CONSTRAINT workspaces_slug_format CHECK (slug ~ '^[a-z0-9][a-z0-9-]{1,62}[a-z0-9]$'),
  CONSTRAINT workspaces_system_owner CHECK (
    (kind = 'system' AND owner_user_id IS NULL)
    OR (kind <> 'system' AND owner_user_id IS NOT NULL)
  ),
  UNIQUE (slug),
  UNIQUE (id, kind)
);

CREATE TABLE workspace_members (
  workspace_id uuid NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
  user_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  role workspace_role NOT NULL,
  status member_status NOT NULL DEFAULT 'active',
  invited_by uuid REFERENCES users(id) ON DELETE SET NULL,
  joined_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (workspace_id, user_id)
);
CREATE INDEX workspace_members_user_idx ON workspace_members (user_id, status);
CREATE UNIQUE INDEX workspace_members_workspace_user_uq
  ON workspace_members (workspace_id, user_id);

CREATE FUNCTION app.can_access_workspace(target_workspace_id uuid) RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public, pg_temp
AS $$
  SELECT app.is_system_actor() OR (
    target_workspace_id = app.current_workspace_id()
    AND EXISTS (
      SELECT 1 FROM workspace_members m
      WHERE m.workspace_id = target_workspace_id
        AND m.user_id = app.current_user_id()
        AND m.status = 'active'
    )
  )
$$;

CREATE FUNCTION app.can_write_workspace(target_workspace_id uuid) RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public, pg_temp
AS $$
  SELECT app.is_system_actor() OR (
    target_workspace_id = app.current_workspace_id()
    AND EXISTS (
      SELECT 1 FROM workspace_members m
      WHERE m.workspace_id = target_workspace_id
        AND m.user_id = app.current_user_id()
        AND m.status = 'active'
        AND m.role IN ('owner', 'admin', 'editor')
    )
  )
$$;

CREATE TABLE sessions (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  workspace_id uuid NOT NULL,
  user_id uuid NOT NULL,
  token_hash text NOT NULL UNIQUE,
  ip_hash text,
  user_agent text,
  created_at timestamptz NOT NULL DEFAULT now(),
  expires_at timestamptz NOT NULL,
  last_seen_at timestamptz NOT NULL DEFAULT now(),
  revoked_at timestamptz,
  FOREIGN KEY (workspace_id, user_id)
    REFERENCES workspace_members(workspace_id, user_id) ON DELETE CASCADE,
  CONSTRAINT sessions_expiry CHECK (expires_at > created_at)
);
CREATE INDEX sessions_user_active_idx ON sessions (user_id, expires_at) WHERE revoked_at IS NULL;

-- Pipeline identity and immutable versions are shared by official and user-owned data.
CREATE TABLE pipelines (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  workspace_id uuid NOT NULL REFERENCES workspaces(id) ON DELETE RESTRICT,
  name text NOT NULL,
  slug text NOT NULL,
  description text NOT NULL DEFAULT '',
  visibility resource_visibility NOT NULL DEFAULT 'private',
  status resource_status NOT NULL DEFAULT 'draft',
  current_version_id uuid,
  created_by uuid REFERENCES users(id) ON DELETE SET NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (workspace_id, id),
  UNIQUE (workspace_id, slug)
);

CREATE TABLE pipeline_versions (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  workspace_id uuid NOT NULL REFERENCES workspaces(id) ON DELETE RESTRICT,
  pipeline_id uuid NOT NULL,
  version integer NOT NULL CHECK (version > 0),
  schema_version text NOT NULL,
  state version_state NOT NULL DEFAULT 'draft',
  graph jsonb NOT NULL,
  capability_requirements jsonb NOT NULL DEFAULT '[]'::jsonb,
  output_contract jsonb NOT NULL DEFAULT '{}'::jsonb,
  content_hash text NOT NULL CHECK (content_hash ~ '^[a-f0-9]{64}$'),
  created_by uuid REFERENCES users(id) ON DELETE SET NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  published_at timestamptz,
  FOREIGN KEY (workspace_id, pipeline_id)
    REFERENCES pipelines(workspace_id, id) ON DELETE RESTRICT,
  UNIQUE (workspace_id, id),
  UNIQUE (workspace_id, pipeline_id, id),
  UNIQUE (pipeline_id, version),
  UNIQUE (workspace_id, pipeline_id, version),
  UNIQUE (pipeline_id, content_hash),
  CONSTRAINT pipeline_versions_published_at CHECK ((state IN ('published', 'deprecated')) = (published_at IS NOT NULL))
);
ALTER TABLE pipelines ADD CONSTRAINT pipelines_current_version_fk
  FOREIGN KEY (workspace_id, id, current_version_id)
  REFERENCES pipeline_versions(workspace_id, pipeline_id, id) ON DELETE RESTRICT
  DEFERRABLE INITIALLY DEFERRED;

CREATE TABLE skills (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  workspace_id uuid NOT NULL REFERENCES workspaces(id) ON DELETE RESTRICT,
  ownership_type ownership_type NOT NULL DEFAULT 'workspace',
  publisher_type publisher_type NOT NULL DEFAULT 'user',
  publisher_name text NOT NULL,
  name text NOT NULL,
  slug text NOT NULL,
  description text NOT NULL DEFAULT '',
  visibility resource_visibility NOT NULL DEFAULT 'private',
  status resource_status NOT NULL DEFAULT 'draft',
  current_version_id uuid,
  forked_from_skill_id uuid REFERENCES skills(id) ON DELETE SET NULL,
  created_by uuid REFERENCES users(id) ON DELETE SET NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT skills_publisher_ownership CHECK (
    (ownership_type = 'system' AND publisher_type = 'system')
    OR (ownership_type = 'workspace' AND publisher_type = 'user')
  ),
  UNIQUE (workspace_id, id),
  UNIQUE (workspace_id, slug)
);

CREATE TABLE skill_versions (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  workspace_id uuid NOT NULL REFERENCES workspaces(id) ON DELETE RESTRICT,
  ownership_type ownership_type NOT NULL DEFAULT 'workspace',
  skill_id uuid NOT NULL,
  version text NOT NULL CHECK (
    version ~ '^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$'
  ),
  schema_version text NOT NULL,
  state version_state NOT NULL DEFAULT 'draft',
  execution_kind text NOT NULL DEFAULT 'declarative'
    CHECK (execution_kind = 'declarative'),
  input_schema jsonb NOT NULL,
  research_policy jsonb NOT NULL,
  writing_policy jsonb NOT NULL,
  visual_policy jsonb NOT NULL,
  asset_policy jsonb NOT NULL,
  qc_policy jsonb NOT NULL,
  output_contract jsonb NOT NULL,
  capability_requirements jsonb NOT NULL DEFAULT '[]'::jsonb,
  default_pipeline_version_id uuid,
  content_hash text NOT NULL CHECK (content_hash ~ '^[a-f0-9]{64}$'),
  created_by uuid REFERENCES users(id) ON DELETE SET NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  published_at timestamptz,
  FOREIGN KEY (workspace_id, skill_id)
    REFERENCES skills(workspace_id, id) ON DELETE RESTRICT,
  FOREIGN KEY (workspace_id, default_pipeline_version_id)
    REFERENCES pipeline_versions(workspace_id, id) ON DELETE RESTRICT,
  UNIQUE (workspace_id, id),
  UNIQUE (workspace_id, skill_id, id),
  UNIQUE (skill_id, version),
  UNIQUE (workspace_id, skill_id, version),
  UNIQUE (skill_id, content_hash),
  CONSTRAINT skill_versions_published_at CHECK ((state IN ('published', 'deprecated')) = (published_at IS NOT NULL))
);
ALTER TABLE skills ADD CONSTRAINT skills_current_version_fk
  FOREIGN KEY (workspace_id, id, current_version_id)
  REFERENCES skill_versions(workspace_id, skill_id, id) ON DELETE RESTRICT
  DEFERRABLE INITIALLY DEFERRED;

CREATE TABLE skill_evaluations (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  workspace_id uuid NOT NULL REFERENCES workspaces(id) ON DELETE RESTRICT,
  skill_version_id uuid NOT NULL,
  evaluation_kind text NOT NULL,
  status text NOT NULL CHECK (status IN ('queued', 'running', 'passed', 'failed', 'cancelled')),
  input_snapshot jsonb NOT NULL,
  result jsonb,
  cost_microunits bigint NOT NULL DEFAULT 0 CHECK (cost_microunits >= 0),
  created_by uuid REFERENCES users(id) ON DELETE SET NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  completed_at timestamptz,
  FOREIGN KEY (workspace_id, skill_version_id)
    REFERENCES skill_versions(workspace_id, id) ON DELETE RESTRICT,
  UNIQUE (workspace_id, id)
);
CREATE INDEX skill_evaluations_version_idx ON skill_evaluations (workspace_id, skill_version_id, created_at DESC);

CREATE TABLE asset_libraries (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  workspace_id uuid NOT NULL REFERENCES workspaces(id) ON DELETE RESTRICT,
  name text NOT NULL,
  slug text NOT NULL,
  description text NOT NULL DEFAULT '',
  visibility resource_visibility NOT NULL DEFAULT 'private',
  status resource_status NOT NULL DEFAULT 'active',
  created_by uuid REFERENCES users(id) ON DELETE SET NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (workspace_id, id),
  UNIQUE (workspace_id, slug)
);

CREATE TABLE assets (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  workspace_id uuid NOT NULL REFERENCES workspaces(id) ON DELETE RESTRICT,
  library_id uuid NOT NULL,
  kind text NOT NULL CHECK (kind IN ('image', 'video', 'audio', 'document', 'text', 'other')),
  title text NOT NULL,
  description text NOT NULL DEFAULT '',
  metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
  copyright_status text NOT NULL DEFAULT 'unknown'
    CHECK (copyright_status IN ('unknown', 'owned', 'licensed', 'public_domain', 'restricted')),
  status text NOT NULL DEFAULT 'processing'
    CHECK (status IN ('processing', 'ready', 'quarantined', 'archived')),
  created_by uuid REFERENCES users(id) ON DELETE SET NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  FOREIGN KEY (workspace_id, library_id)
    REFERENCES asset_libraries(workspace_id, id) ON DELETE RESTRICT,
  UNIQUE (workspace_id, id),
  UNIQUE (workspace_id, library_id, id)
);
CREATE INDEX assets_library_status_idx ON assets (workspace_id, library_id, status, created_at DESC);

CREATE TABLE asset_files (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  workspace_id uuid NOT NULL REFERENCES workspaces(id) ON DELETE RESTRICT,
  asset_id uuid NOT NULL,
  storage_provider text NOT NULL,
  bucket text NOT NULL,
  object_key text NOT NULL,
  original_filename text,
  media_type text NOT NULL,
  byte_size bigint NOT NULL CHECK (byte_size >= 0),
  content_hash text NOT NULL CHECK (content_hash ~ '^[a-f0-9]{64}$'),
  scan_status text NOT NULL DEFAULT 'pending'
    CHECK (scan_status IN ('pending', 'clean', 'rejected', 'failed')),
  width integer CHECK (width IS NULL OR width > 0),
  height integer CHECK (height IS NULL OR height > 0),
  duration_ms bigint CHECK (duration_ms IS NULL OR duration_ms >= 0),
  created_at timestamptz NOT NULL DEFAULT now(),
  deleted_at timestamptz,
  FOREIGN KEY (workspace_id, asset_id)
    REFERENCES assets(workspace_id, id) ON DELETE RESTRICT,
  UNIQUE (workspace_id, id),
  UNIQUE (workspace_id, bucket, object_key),
  UNIQUE (workspace_id, asset_id, content_hash),
  CONSTRAINT asset_files_tenant_key CHECK (
    object_key LIKE 'workspaces/' || workspace_id::text || '/%'
    AND object_key !~ '(^|/)\.\.(/|$)'
  )
);

CREATE TABLE asset_sources (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  workspace_id uuid NOT NULL REFERENCES workspaces(id) ON DELETE RESTRICT,
  asset_id uuid NOT NULL,
  source_type text NOT NULL CHECK (source_type IN ('upload', 'website', 'api', 'connector', 'generated')),
  locator text,
  provider text,
  attribution text,
  license text,
  captured_at timestamptz,
  metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now(),
  FOREIGN KEY (workspace_id, asset_id)
    REFERENCES assets(workspace_id, id) ON DELETE CASCADE,
  UNIQUE (workspace_id, id)
);
CREATE INDEX asset_sources_asset_idx ON asset_sources (workspace_id, asset_id);

CREATE TABLE tags (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  workspace_id uuid NOT NULL REFERENCES workspaces(id) ON DELETE RESTRICT,
  name text NOT NULL,
  slug text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (workspace_id, id),
  UNIQUE (workspace_id, slug)
);

CREATE TABLE asset_tags (
  workspace_id uuid NOT NULL REFERENCES workspaces(id) ON DELETE RESTRICT,
  asset_id uuid NOT NULL,
  tag_id uuid NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  FOREIGN KEY (workspace_id, asset_id)
    REFERENCES assets(workspace_id, id) ON DELETE CASCADE,
  FOREIGN KEY (workspace_id, tag_id)
    REFERENCES tags(workspace_id, id) ON DELETE CASCADE,
  PRIMARY KEY (workspace_id, asset_id, tag_id)
);

CREATE TABLE voice_profiles (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  workspace_id uuid NOT NULL REFERENCES workspaces(id) ON DELETE RESTRICT,
  name text NOT NULL,
  slug text NOT NULL,
  provider text NOT NULL,
  provider_voice_ref text NOT NULL,
  secret_ref text,
  configuration jsonb NOT NULL DEFAULT '{}'::jsonb,
  status resource_status NOT NULL DEFAULT 'active',
  created_by uuid REFERENCES users(id) ON DELETE SET NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (workspace_id, id),
  UNIQUE (workspace_id, slug)
);

CREATE TABLE render_presets (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  workspace_id uuid NOT NULL REFERENCES workspaces(id) ON DELETE RESTRICT,
  name text NOT NULL,
  slug text NOT NULL,
  description text NOT NULL DEFAULT '',
  visibility resource_visibility NOT NULL DEFAULT 'private',
  status resource_status NOT NULL DEFAULT 'draft',
  current_version_id uuid,
  created_by uuid REFERENCES users(id) ON DELETE SET NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (workspace_id, id),
  UNIQUE (workspace_id, slug)
);

CREATE TABLE render_preset_versions (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  workspace_id uuid NOT NULL REFERENCES workspaces(id) ON DELETE RESTRICT,
  render_preset_id uuid NOT NULL,
  version integer NOT NULL CHECK (version > 0),
  schema_version text NOT NULL,
  state version_state NOT NULL DEFAULT 'draft',
  specification jsonb NOT NULL,
  capability_requirements jsonb NOT NULL DEFAULT '[]'::jsonb,
  content_hash text NOT NULL CHECK (content_hash ~ '^[a-f0-9]{64}$'),
  created_by uuid REFERENCES users(id) ON DELETE SET NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  published_at timestamptz,
  FOREIGN KEY (workspace_id, render_preset_id)
    REFERENCES render_presets(workspace_id, id) ON DELETE RESTRICT,
  UNIQUE (workspace_id, id),
  UNIQUE (workspace_id, render_preset_id, id),
  UNIQUE (render_preset_id, version),
  UNIQUE (workspace_id, render_preset_id, version),
  UNIQUE (render_preset_id, content_hash),
  CONSTRAINT render_preset_versions_published_at CHECK ((state IN ('published', 'deprecated')) = (published_at IS NOT NULL))
);
ALTER TABLE render_presets ADD CONSTRAINT render_presets_current_version_fk
  FOREIGN KEY (workspace_id, id, current_version_id)
  REFERENCES render_preset_versions(workspace_id, render_preset_id, id) ON DELETE RESTRICT
  DEFERRABLE INITIALLY DEFERRED;

CREATE TABLE channels (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  workspace_id uuid NOT NULL REFERENCES workspaces(id) ON DELETE RESTRICT,
  name text NOT NULL,
  slug text NOT NULL,
  platform text,
  external_ref text,
  brand_config jsonb NOT NULL DEFAULT '{}'::jsonb,
  status resource_status NOT NULL DEFAULT 'active',
  created_by uuid REFERENCES users(id) ON DELETE SET NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (workspace_id, id),
  UNIQUE (workspace_id, slug),
  UNIQUE NULLS NOT DISTINCT (workspace_id, platform, external_ref)
);

CREATE TABLE channel_defaults (
  channel_id uuid PRIMARY KEY,
  workspace_id uuid NOT NULL REFERENCES workspaces(id) ON DELETE RESTRICT,
  skill_version_id uuid,
  asset_library_id uuid,
  voice_profile_id uuid,
  render_preset_version_id uuid,
  pipeline_version_id uuid,
  qc_overrides jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  FOREIGN KEY (workspace_id, channel_id)
    REFERENCES channels(workspace_id, id) ON DELETE CASCADE,
  FOREIGN KEY (workspace_id, skill_version_id)
    REFERENCES skill_versions(workspace_id, id) ON DELETE RESTRICT,
  FOREIGN KEY (workspace_id, asset_library_id)
    REFERENCES asset_libraries(workspace_id, id) ON DELETE RESTRICT,
  FOREIGN KEY (workspace_id, voice_profile_id)
    REFERENCES voice_profiles(workspace_id, id) ON DELETE RESTRICT,
  FOREIGN KEY (workspace_id, render_preset_version_id)
    REFERENCES render_preset_versions(workspace_id, id) ON DELETE RESTRICT,
  FOREIGN KEY (workspace_id, pipeline_version_id)
    REFERENCES pipeline_versions(workspace_id, id) ON DELETE RESTRICT,
  UNIQUE (workspace_id, channel_id)
);

CREATE TABLE runs (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  workspace_id uuid NOT NULL REFERENCES workspaces(id) ON DELETE RESTRICT,
  channel_id uuid,
  skill_version_id uuid NOT NULL,
  asset_library_id uuid,
  voice_profile_id uuid,
  render_preset_version_id uuid,
  pipeline_version_id uuid NOT NULL,
  status run_status NOT NULL DEFAULT 'queued',
  input_snapshot jsonb NOT NULL,
  composition_snapshot jsonb NOT NULL,
  capability_snapshot jsonb NOT NULL DEFAULT '[]'::jsonb,
  priority smallint NOT NULL DEFAULT 0 CHECK (priority BETWEEN -100 AND 100),
  requested_by uuid REFERENCES users(id) ON DELETE SET NULL,
  cancel_requested_at timestamptz,
  started_at timestamptz,
  completed_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  FOREIGN KEY (workspace_id, channel_id)
    REFERENCES channels(workspace_id, id) ON DELETE RESTRICT,
  FOREIGN KEY (workspace_id, skill_version_id)
    REFERENCES skill_versions(workspace_id, id) ON DELETE RESTRICT,
  FOREIGN KEY (workspace_id, asset_library_id)
    REFERENCES asset_libraries(workspace_id, id) ON DELETE RESTRICT,
  FOREIGN KEY (workspace_id, voice_profile_id)
    REFERENCES voice_profiles(workspace_id, id) ON DELETE RESTRICT,
  FOREIGN KEY (workspace_id, render_preset_version_id)
    REFERENCES render_preset_versions(workspace_id, id) ON DELETE RESTRICT,
  FOREIGN KEY (workspace_id, pipeline_version_id)
    REFERENCES pipeline_versions(workspace_id, id) ON DELETE RESTRICT,
  UNIQUE (workspace_id, id),
  CONSTRAINT runs_completion CHECK (
    (status IN ('succeeded', 'failed', 'cancelled')) = (completed_at IS NOT NULL)
  )
);
CREATE INDEX runs_workspace_status_idx ON runs (workspace_id, status, created_at DESC);
CREATE INDEX runs_active_idx ON runs (workspace_id, priority DESC, created_at)
  WHERE status IN ('queued', 'running', 'awaiting_review');

CREATE TABLE run_steps (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  workspace_id uuid NOT NULL REFERENCES workspaces(id) ON DELETE RESTRICT,
  run_id uuid NOT NULL,
  step_key text NOT NULL,
  step_type text NOT NULL,
  status step_status NOT NULL DEFAULT 'queued',
  queue_name text NOT NULL DEFAULT 'default',
  required_capabilities text[] NOT NULL DEFAULT '{}',
  input_snapshot jsonb NOT NULL,
  output_summary jsonb,
  error jsonb,
  priority smallint NOT NULL DEFAULT 0 CHECK (priority BETWEEN -100 AND 100),
  available_at timestamptz NOT NULL DEFAULT now(),
  attempt_count integer NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
  max_attempts integer NOT NULL DEFAULT 3 CHECK (max_attempts > 0),
  lease_owner text,
  lease_token uuid,
  lease_expires_at timestamptz,
  heartbeat_at timestamptz,
  started_at timestamptz,
  completed_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  FOREIGN KEY (workspace_id, run_id)
    REFERENCES runs(workspace_id, id) ON DELETE RESTRICT,
  UNIQUE (workspace_id, id),
  UNIQUE (workspace_id, run_id, id),
  UNIQUE (run_id, step_key),
  CONSTRAINT run_steps_attempt_limit CHECK (attempt_count <= max_attempts),
  CONSTRAINT run_steps_lease_shape CHECK (
    (lease_owner IS NULL AND lease_token IS NULL AND lease_expires_at IS NULL)
    OR (lease_owner IS NOT NULL AND lease_token IS NOT NULL AND lease_expires_at IS NOT NULL)
  ),
  CONSTRAINT run_steps_running_lease CHECK (status <> 'running' OR lease_expires_at IS NOT NULL),
  CONSTRAINT run_steps_completion CHECK (
    (status IN ('succeeded', 'failed', 'cancelled')) = (completed_at IS NOT NULL)
  )
);
CREATE INDEX run_steps_claim_idx
  ON run_steps (status, available_at, priority DESC, created_at)
  WHERE status IN ('queued', 'retrying');
CREATE INDEX run_steps_recovery_idx
  ON run_steps (lease_expires_at)
  WHERE status = 'running';
CREATE INDEX run_steps_run_idx ON run_steps (workspace_id, run_id, created_at);

CREATE TABLE run_events (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  workspace_id uuid NOT NULL REFERENCES workspaces(id) ON DELETE RESTRICT,
  run_id uuid NOT NULL,
  step_id uuid,
  sequence bigint NOT NULL CHECK (sequence > 0),
  event_type text NOT NULL,
  schema_version text NOT NULL,
  payload jsonb NOT NULL,
  actor_type text NOT NULL CHECK (actor_type IN ('user', 'worker', 'system')),
  actor_id text,
  occurred_at timestamptz NOT NULL DEFAULT now(),
  FOREIGN KEY (workspace_id, run_id)
    REFERENCES runs(workspace_id, id) ON DELETE RESTRICT,
  FOREIGN KEY (workspace_id, run_id, step_id)
    REFERENCES run_steps(workspace_id, run_id, id) ON DELETE RESTRICT,
  UNIQUE (workspace_id, id),
  UNIQUE (run_id, sequence)
);
CREATE INDEX run_events_stream_idx ON run_events (workspace_id, run_id, sequence);

CREATE TABLE artifacts (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  workspace_id uuid NOT NULL REFERENCES workspaces(id) ON DELETE RESTRICT,
  run_id uuid NOT NULL,
  step_id uuid,
  kind text NOT NULL,
  status artifact_status NOT NULL DEFAULT 'pending',
  schema_version text,
  media_type text NOT NULL,
  storage_provider text NOT NULL,
  bucket text NOT NULL,
  object_key text NOT NULL,
  content_hash text CHECK (content_hash IS NULL OR content_hash ~ '^[a-f0-9]{64}$'),
  byte_size bigint CHECK (byte_size IS NULL OR byte_size >= 0),
  metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now(),
  deleted_at timestamptz,
  FOREIGN KEY (workspace_id, run_id)
    REFERENCES runs(workspace_id, id) ON DELETE RESTRICT,
  FOREIGN KEY (workspace_id, run_id, step_id)
    REFERENCES run_steps(workspace_id, run_id, id) ON DELETE RESTRICT,
  UNIQUE (workspace_id, id),
  UNIQUE (workspace_id, run_id, id),
  UNIQUE (workspace_id, bucket, object_key),
  CONSTRAINT artifacts_tenant_key CHECK (
    object_key LIKE 'workspaces/' || workspace_id::text || '/runs/' || run_id::text || '/artifacts/%'
    AND object_key !~ '(^|/)\.\.(/|$)'
  )
);
CREATE INDEX artifacts_run_idx ON artifacts (workspace_id, run_id, kind, created_at);

CREATE TABLE review_actions (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  workspace_id uuid NOT NULL REFERENCES workspaces(id) ON DELETE RESTRICT,
  run_id uuid NOT NULL,
  step_id uuid,
  artifact_id uuid,
  action text NOT NULL CHECK (action IN ('requested', 'approved', 'rejected', 'changes_requested', 'commented')),
  comment text,
  details jsonb NOT NULL DEFAULT '{}'::jsonb,
  actor_user_id uuid REFERENCES users(id) ON DELETE SET NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  FOREIGN KEY (workspace_id, run_id)
    REFERENCES runs(workspace_id, id) ON DELETE RESTRICT,
  FOREIGN KEY (workspace_id, run_id, step_id)
    REFERENCES run_steps(workspace_id, run_id, id) ON DELETE RESTRICT,
  FOREIGN KEY (workspace_id, run_id, artifact_id)
    REFERENCES artifacts(workspace_id, run_id, id) ON DELETE RESTRICT,
  UNIQUE (workspace_id, id)
);
CREATE INDEX review_actions_run_idx ON review_actions (workspace_id, run_id, created_at DESC);

CREATE TABLE api_keys (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  workspace_id uuid NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
  name text NOT NULL,
  key_prefix text NOT NULL,
  key_hash text NOT NULL,
  scopes text[] NOT NULL,
  created_by uuid REFERENCES users(id) ON DELETE SET NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  expires_at timestamptz,
  last_used_at timestamptz,
  revoked_at timestamptz,
  UNIQUE (workspace_id, id),
  UNIQUE (key_hash),
  UNIQUE (workspace_id, key_prefix)
);
CREATE INDEX api_keys_active_idx ON api_keys (workspace_id, key_prefix) WHERE revoked_at IS NULL;

CREATE TABLE idempotency_keys (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  workspace_id uuid NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
  key text NOT NULL,
  request_method text NOT NULL,
  request_path text NOT NULL,
  request_hash text NOT NULL CHECK (request_hash ~ '^[a-f0-9]{64}$'),
  status text NOT NULL DEFAULT 'processing' CHECK (status IN ('processing', 'completed', 'failed')),
  response_status integer CHECK (response_status IS NULL OR response_status BETWEEN 100 AND 599),
  response_body jsonb,
  resource_type text,
  resource_id uuid,
  locked_until timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  expires_at timestamptz NOT NULL,
  UNIQUE (workspace_id, id),
  UNIQUE (workspace_id, key),
  CONSTRAINT idempotency_response_shape CHECK (
    (status = 'processing' AND response_status IS NULL)
    OR (status <> 'processing' AND response_status IS NOT NULL)
  )
);
CREATE INDEX idempotency_expiry_idx ON idempotency_keys (expires_at);

CREATE TABLE webhooks (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  workspace_id uuid NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
  name text NOT NULL,
  url text NOT NULL CHECK (url ~ '^https://'),
  secret_ref text NOT NULL,
  event_types text[] NOT NULL,
  status text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'disabled')),
  created_by uuid REFERENCES users(id) ON DELETE SET NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (workspace_id, id),
  UNIQUE (workspace_id, name)
);

CREATE TABLE webhook_deliveries (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  workspace_id uuid NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
  webhook_id uuid NOT NULL,
  outbox_event_id uuid,
  status delivery_status NOT NULL DEFAULT 'pending',
  attempt_count integer NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
  max_attempts integer NOT NULL DEFAULT 8 CHECK (max_attempts > 0),
  next_attempt_at timestamptz NOT NULL DEFAULT now(),
  last_response_status integer,
  last_error text,
  delivered_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  FOREIGN KEY (workspace_id, webhook_id)
    REFERENCES webhooks(workspace_id, id) ON DELETE CASCADE,
  UNIQUE (workspace_id, id),
  UNIQUE NULLS NOT DISTINCT (webhook_id, outbox_event_id),
  CONSTRAINT webhook_delivery_attempt_limit CHECK (attempt_count <= max_attempts)
);
CREATE INDEX webhook_deliveries_claim_idx
  ON webhook_deliveries (status, next_attempt_at)
  WHERE status IN ('pending', 'retrying');

CREATE TABLE audit_logs (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  workspace_id uuid NOT NULL REFERENCES workspaces(id) ON DELETE RESTRICT,
  actor_type text NOT NULL CHECK (actor_type IN ('user', 'api_key', 'worker', 'system')),
  actor_id text,
  action text NOT NULL,
  resource_type text NOT NULL,
  resource_id uuid,
  request_id text,
  ip_hash text,
  before_data jsonb,
  after_data jsonb,
  metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
  occurred_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (workspace_id, id)
);
CREATE INDEX audit_logs_resource_idx ON audit_logs (workspace_id, resource_type, resource_id, occurred_at DESC);
CREATE INDEX audit_logs_actor_idx ON audit_logs (workspace_id, actor_type, actor_id, occurred_at DESC);

CREATE TABLE usage_records (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  workspace_id uuid NOT NULL REFERENCES workspaces(id) ON DELETE RESTRICT,
  run_id uuid,
  step_id uuid,
  meter text NOT NULL,
  quantity numeric(20, 6) NOT NULL CHECK (quantity >= 0),
  unit text NOT NULL,
  cost_microunits bigint NOT NULL DEFAULT 0 CHECK (cost_microunits >= 0),
  provider text,
  source_type text NOT NULL,
  source_id text NOT NULL,
  recorded_at timestamptz NOT NULL DEFAULT now(),
  metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
  FOREIGN KEY (workspace_id, run_id)
    REFERENCES runs(workspace_id, id) ON DELETE RESTRICT,
  FOREIGN KEY (workspace_id, run_id, step_id)
    REFERENCES run_steps(workspace_id, run_id, id) ON DELETE RESTRICT,
  UNIQUE (workspace_id, id),
  UNIQUE (workspace_id, meter, source_type, source_id)
);
CREATE INDEX usage_records_time_idx ON usage_records (workspace_id, recorded_at DESC);

CREATE TABLE outbox_events (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  workspace_id uuid NOT NULL REFERENCES workspaces(id) ON DELETE RESTRICT,
  aggregate_type text NOT NULL,
  aggregate_id uuid NOT NULL,
  event_type text NOT NULL,
  schema_version text NOT NULL,
  payload jsonb NOT NULL,
  status delivery_status NOT NULL DEFAULT 'pending',
  available_at timestamptz NOT NULL DEFAULT now(),
  attempt_count integer NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
  max_attempts integer NOT NULL DEFAULT 12 CHECK (max_attempts > 0),
  lease_owner text,
  lease_token uuid,
  lease_expires_at timestamptz,
  last_error text,
  created_at timestamptz NOT NULL DEFAULT now(),
  delivered_at timestamptz,
  UNIQUE (workspace_id, id),
  UNIQUE (workspace_id, aggregate_type, aggregate_id, event_type, id),
  CONSTRAINT outbox_attempt_limit CHECK (attempt_count <= max_attempts),
  CONSTRAINT outbox_lease_shape CHECK (
    (lease_owner IS NULL AND lease_token IS NULL AND lease_expires_at IS NULL)
    OR (lease_owner IS NOT NULL AND lease_token IS NOT NULL AND lease_expires_at IS NOT NULL)
  )
);
CREATE INDEX outbox_events_claim_idx
  ON outbox_events (status, available_at, created_at)
  WHERE status IN ('pending', 'retrying');
CREATE INDEX outbox_events_recovery_idx
  ON outbox_events (lease_expires_at)
  WHERE status = 'processing';

ALTER TABLE webhook_deliveries ADD CONSTRAINT webhook_deliveries_outbox_fk
  FOREIGN KEY (workspace_id, outbox_event_id)
  REFERENCES outbox_events(workspace_id, id) ON DELETE RESTRICT;

CREATE FUNCTION app.set_updated_at() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  NEW.updated_at := now();
  RETURN NEW;
END
$$;

DO $do$
DECLARE table_name text;
BEGIN
  FOREACH table_name IN ARRAY ARRAY[
    'users', 'workspaces', 'workspace_members', 'pipelines', 'skills',
    'asset_libraries', 'assets', 'voice_profiles', 'render_presets',
    'channels', 'channel_defaults', 'runs', 'run_steps', 'webhooks'
  ] LOOP
    EXECUTE format(
      'CREATE TRIGGER %I_set_updated_at BEFORE UPDATE ON %I '
      'FOR EACH ROW EXECUTE FUNCTION app.set_updated_at()',
      table_name, table_name
    );
  END LOOP;
END
$do$;

CREATE FUNCTION app.protect_version() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE is_referenced boolean := false;
BEGIN
  IF TG_TABLE_NAME = 'skill_versions' THEN
    SELECT EXISTS (SELECT 1 FROM runs WHERE skill_version_id = OLD.id) INTO is_referenced;
  ELSIF TG_TABLE_NAME = 'render_preset_versions' THEN
    SELECT EXISTS (SELECT 1 FROM runs WHERE render_preset_version_id = OLD.id) INTO is_referenced;
  ELSIF TG_TABLE_NAME = 'pipeline_versions' THEN
    SELECT EXISTS (SELECT 1 FROM runs WHERE pipeline_version_id = OLD.id) INTO is_referenced;
  END IF;

  IF TG_OP = 'DELETE' THEN
    IF OLD.state IN ('published', 'deprecated') OR is_referenced THEN
      RAISE EXCEPTION '% % is immutable', TG_TABLE_NAME, OLD.id USING ERRCODE = '55000';
    END IF;
    RETURN OLD;
  END IF;

  IF OLD.state IN ('published', 'deprecated') OR is_referenced THEN
    IF OLD.state = 'published'
       AND NEW.state = 'deprecated'
       AND (to_jsonb(NEW) - 'state') = (to_jsonb(OLD) - 'state') THEN
      RETURN NEW;
    END IF;
    RAISE EXCEPTION '% % is immutable; create a new version', TG_TABLE_NAME, OLD.id
      USING ERRCODE = '55000';
  END IF;
  RETURN NEW;
END
$$;

CREATE TRIGGER skill_versions_immutable
  BEFORE UPDATE OR DELETE ON skill_versions
  FOR EACH ROW EXECUTE FUNCTION app.protect_version();
CREATE TRIGGER render_preset_versions_immutable
  BEFORE UPDATE OR DELETE ON render_preset_versions
  FOR EACH ROW EXECUTE FUNCTION app.protect_version();
CREATE TRIGGER pipeline_versions_immutable
  BEFORE UPDATE OR DELETE ON pipeline_versions
  FOR EACH ROW EXECUTE FUNCTION app.protect_version();

CREATE FUNCTION app.validate_run_versions() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM skill_versions v
    WHERE v.id = NEW.skill_version_id
      AND v.workspace_id = NEW.workspace_id
      AND v.state = 'published'
  ) THEN
    RAISE EXCEPTION 'run skill version must be a published version in the same workspace'
      USING ERRCODE = '23514';
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM pipeline_versions v
    WHERE v.id = NEW.pipeline_version_id
      AND v.workspace_id = NEW.workspace_id
      AND v.state = 'published'
  ) THEN
    RAISE EXCEPTION 'run pipeline version must be a published version in the same workspace'
      USING ERRCODE = '23514';
  END IF;
  IF NEW.render_preset_version_id IS NOT NULL AND NOT EXISTS (
    SELECT 1 FROM render_preset_versions v
    WHERE v.id = NEW.render_preset_version_id
      AND v.workspace_id = NEW.workspace_id
      AND v.state = 'published'
  ) THEN
    RAISE EXCEPTION 'run render preset version must be a published version in the same workspace'
      USING ERRCODE = '23514';
  END IF;
  RETURN NEW;
END
$$;
CREATE TRIGGER runs_validate_versions
  BEFORE INSERT OR UPDATE OF workspace_id, skill_version_id, render_preset_version_id, pipeline_version_id
  ON runs FOR EACH ROW EXECUTE FUNCTION app.validate_run_versions();

CREATE FUNCTION app.forbid_mutation() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  RAISE EXCEPTION '% is append-only', TG_TABLE_NAME USING ERRCODE = '55000';
END
$$;
CREATE TRIGGER audit_logs_append_only
  BEFORE UPDATE OR DELETE ON audit_logs
  FOR EACH ROW EXECUTE FUNCTION app.forbid_mutation();
CREATE TRIGGER run_events_append_only
  BEFORE UPDATE OR DELETE ON run_events
  FOR EACH ROW EXECUTE FUNCTION app.forbid_mutation();
CREATE TRIGGER review_actions_append_only
  BEFORE UPDATE OR DELETE ON review_actions
  FOR EACH ROW EXECUTE FUNCTION app.forbid_mutation();
CREATE TRIGGER usage_records_append_only
  BEFORE UPDATE OR DELETE ON usage_records
  FOR EACH ROW EXECUTE FUNCTION app.forbid_mutation();

-- Identity policies. Service/worker database roles should have BYPASSRLS; tenant
-- requests must set app.user_id and app.workspace_id at transaction scope.
ALTER TABLE users ENABLE ROW LEVEL SECURITY;
CREATE POLICY users_self_select ON users FOR SELECT
  USING (id = app.current_user_id() OR app.is_system_actor());
CREATE POLICY users_self_update ON users FOR UPDATE
  USING (id = app.current_user_id() OR app.is_system_actor())
  WITH CHECK (id = app.current_user_id() OR app.is_system_actor());

ALTER TABLE workspaces ENABLE ROW LEVEL SECURITY;
CREATE POLICY workspaces_member_select ON workspaces FOR SELECT
  USING (app.can_access_workspace(id));
CREATE POLICY workspaces_member_update ON workspaces FOR UPDATE
  USING (app.can_write_workspace(id)) WITH CHECK (app.can_write_workspace(id));
CREATE POLICY workspaces_system_insert ON workspaces FOR INSERT
  WITH CHECK (app.is_system_actor());

ALTER TABLE workspace_members ENABLE ROW LEVEL SECURITY;
CREATE POLICY workspace_members_select ON workspace_members FOR SELECT
  USING (app.can_access_workspace(workspace_id));
CREATE POLICY workspace_members_write ON workspace_members FOR ALL
  USING (app.can_write_workspace(workspace_id))
  WITH CHECK (app.can_write_workspace(workspace_id));

ALTER TABLE sessions ENABLE ROW LEVEL SECURITY;
CREATE POLICY sessions_owner ON sessions FOR ALL
  USING (
    app.is_system_actor()
    OR (user_id = app.current_user_id() AND app.can_access_workspace(workspace_id))
  )
  WITH CHECK (
    app.is_system_actor()
    OR (user_id = app.current_user_id() AND app.can_access_workspace(workspace_id))
  );

-- Keep root-table RLS declarations explicit so ownership remains obvious during
-- review; the loop below applies the same policy shape to dependent tables.
ALTER TABLE channels ENABLE ROW LEVEL SECURITY;
CREATE POLICY channels_root_authorization ON channels
  USING (app.can_access_workspace(workspace_id))
  WITH CHECK (app.can_write_workspace(workspace_id));
ALTER TABLE skills ENABLE ROW LEVEL SECURITY;
CREATE POLICY skills_root_authorization ON skills
  USING (app.can_access_workspace(workspace_id))
  WITH CHECK (app.can_write_workspace(workspace_id));
ALTER TABLE asset_libraries ENABLE ROW LEVEL SECURITY;
CREATE POLICY asset_libraries_root_authorization ON asset_libraries
  USING (app.can_access_workspace(workspace_id))
  WITH CHECK (app.can_write_workspace(workspace_id));
ALTER TABLE voice_profiles ENABLE ROW LEVEL SECURITY;
CREATE POLICY voice_profiles_root_authorization ON voice_profiles
  USING (app.can_access_workspace(workspace_id))
  WITH CHECK (app.can_write_workspace(workspace_id));
ALTER TABLE render_presets ENABLE ROW LEVEL SECURITY;
CREATE POLICY render_presets_root_authorization ON render_presets
  USING (app.can_access_workspace(workspace_id))
  WITH CHECK (app.can_write_workspace(workspace_id));
ALTER TABLE pipelines ENABLE ROW LEVEL SECURITY;
CREATE POLICY pipelines_root_authorization ON pipelines
  USING (app.can_access_workspace(workspace_id))
  WITH CHECK (app.can_write_workspace(workspace_id));
ALTER TABLE runs ENABLE ROW LEVEL SECURITY;
CREATE POLICY runs_root_authorization ON runs
  USING (app.can_access_workspace(workspace_id))
  WITH CHECK (app.can_write_workspace(workspace_id));
ALTER TABLE api_keys ENABLE ROW LEVEL SECURITY;
CREATE POLICY api_keys_root_authorization ON api_keys
  USING (app.can_access_workspace(workspace_id))
  WITH CHECK (app.can_write_workspace(workspace_id));
ALTER TABLE webhooks ENABLE ROW LEVEL SECURITY;
CREATE POLICY webhooks_root_authorization ON webhooks
  USING (app.can_access_workspace(workspace_id))
  WITH CHECK (app.can_write_workspace(workspace_id));
ALTER TABLE audit_logs ENABLE ROW LEVEL SECURITY;
ALTER TABLE outbox_events ENABLE ROW LEVEL SECURITY;

DO $do$
DECLARE table_name text;
BEGIN
  FOREACH table_name IN ARRAY ARRAY[
    'channels', 'channel_defaults', 'pipelines', 'pipeline_versions',
    'skills', 'skill_versions', 'skill_evaluations',
    'asset_libraries', 'assets', 'asset_files', 'asset_sources', 'tags', 'asset_tags',
    'voice_profiles', 'render_presets', 'render_preset_versions',
    'runs', 'run_steps', 'run_events', 'artifacts', 'review_actions',
    'api_keys', 'idempotency_keys', 'webhooks', 'webhook_deliveries',
    'audit_logs', 'usage_records', 'outbox_events'
  ] LOOP
    EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', table_name);
    EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', table_name);
    EXECUTE format(
      'CREATE POLICY %I_tenant_select ON %I FOR SELECT USING (app.can_access_workspace(workspace_id))',
      table_name, table_name
    );
    EXECUTE format(
      'CREATE POLICY %I_tenant_insert ON %I FOR INSERT WITH CHECK (app.can_write_workspace(workspace_id))',
      table_name, table_name
    );
    EXECUTE format(
      'CREATE POLICY %I_tenant_update ON %I FOR UPDATE '
      'USING (app.can_write_workspace(workspace_id)) WITH CHECK (app.can_write_workspace(workspace_id))',
      table_name, table_name
    );
    EXECUTE format(
      'CREATE POLICY %I_tenant_delete ON %I FOR DELETE USING (app.can_write_workspace(workspace_id))',
      table_name, table_name
    );
  END LOOP;
END
$do$;

-- System-owned, public-readonly resources remain in the same tables and API.
CREATE POLICY skills_official_read ON skills FOR SELECT USING (
  visibility = 'public_readonly'
  AND EXISTS (SELECT 1 FROM workspaces w WHERE w.id = workspace_id AND w.kind = 'system')
);
CREATE POLICY skill_versions_official_read ON skill_versions FOR SELECT USING (
  EXISTS (
    SELECT 1 FROM skills s JOIN workspaces w ON w.id = s.workspace_id
    WHERE s.id = skill_id AND s.workspace_id = workspace_id
      AND s.visibility = 'public_readonly' AND w.kind = 'system'
  )
);
CREATE POLICY render_presets_official_read ON render_presets FOR SELECT USING (
  visibility = 'public_readonly'
  AND EXISTS (SELECT 1 FROM workspaces w WHERE w.id = workspace_id AND w.kind = 'system')
);
CREATE POLICY render_preset_versions_official_read ON render_preset_versions FOR SELECT USING (
  EXISTS (
    SELECT 1 FROM render_presets r JOIN workspaces w ON w.id = r.workspace_id
    WHERE r.id = render_preset_id AND r.workspace_id = workspace_id
      AND r.visibility = 'public_readonly' AND w.kind = 'system'
  )
);
CREATE POLICY pipelines_official_read ON pipelines FOR SELECT USING (
  visibility = 'public_readonly'
  AND EXISTS (SELECT 1 FROM workspaces w WHERE w.id = workspace_id AND w.kind = 'system')
);
CREATE POLICY pipeline_versions_official_read ON pipeline_versions FOR SELECT USING (
  EXISTS (
    SELECT 1 FROM pipelines p JOIN workspaces w ON w.id = p.workspace_id
    WHERE p.id = pipeline_id AND p.workspace_id = workspace_id
      AND p.visibility = 'public_readonly' AND w.kind = 'system'
  )
);

-- Phase 1 ships as a single configured personal workspace. Keep the ownership
-- columns and dormant policies as a forward-compatible interface, but do not
-- activate row-level multi-workspace enforcement until a later opt-in migration
-- also introduces workspace selection, membership administration, and isolation
-- tests for the production repository.
DO $do$
DECLARE table_name text;
BEGIN
  FOREACH table_name IN ARRAY ARRAY[
    'users', 'workspaces', 'workspace_members', 'sessions',
    'channels', 'channel_defaults', 'pipelines', 'pipeline_versions',
    'skills', 'skill_versions', 'skill_evaluations',
    'asset_libraries', 'assets', 'asset_files', 'asset_sources', 'tags', 'asset_tags',
    'voice_profiles', 'render_presets', 'render_preset_versions',
    'runs', 'run_steps', 'run_events', 'artifacts', 'review_actions',
    'api_keys', 'idempotency_keys', 'webhooks', 'webhook_deliveries',
    'audit_logs', 'usage_records', 'outbox_events'
  ] LOOP
    EXECUTE format('ALTER TABLE %I DISABLE ROW LEVEL SECURITY', table_name);
  END LOOP;
END
$do$;

COMMIT;
