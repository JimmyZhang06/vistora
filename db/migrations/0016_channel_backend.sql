-- Upgrade the reserved Channel skeleton into the durable publishing-channel model.
-- Platform credentials remain outside channels: a Channel stores only a scoped
-- platform_connections reference, whose secret_ref points at an external secret store.

BEGIN;

-- The old skeleton allowed incomplete defaults. Do not silently invent or
-- discard business choices during the upgrade: abort before changing the
-- schema so an operator can repair the affected rows and safely retry the
-- checksum-managed, transactional migration.
DO $channel_defaults_preflight$
DECLARE
  incomplete_count bigint;
  invalid_skill_count bigint;
  invalid_pipeline_count bigint;
BEGIN
  SELECT count(*) INTO incomplete_count
  FROM channel_defaults
  WHERE skill_version_id IS NULL OR pipeline_version_id IS NULL;

  IF incomplete_count > 0 THEN
    RAISE EXCEPTION
      'channel backend migration refused: % channel_defaults rows have incomplete version defaults',
      incomplete_count
      USING ERRCODE = '23514',
            HINT = 'Populate both skill_version_id and pipeline_version_id with published versions, then retry the migration.';
  END IF;

  SELECT count(*) INTO invalid_skill_count
  FROM channel_defaults d
  LEFT JOIN skill_versions v
    ON v.id = d.skill_version_id
  LEFT JOIN skills s
    ON s.workspace_id = v.workspace_id AND s.id = v.skill_id
  LEFT JOIN workspaces w
    ON w.id = v.workspace_id
  WHERE v.id IS NULL
     OR s.id IS NULL
     OR w.id IS NULL
     OR v.state <> 'published'
     OR NOT (
       v.workspace_id = d.workspace_id
       OR (
         v.ownership_type = 'system'
         AND s.ownership_type = 'system'
         AND s.visibility = 'public_readonly'
         AND w.kind = 'system'
       )
     );

  IF invalid_skill_count > 0 THEN
    RAISE EXCEPTION
      'channel backend migration refused: % channel_defaults rows reference an unavailable Skill version',
      invalid_skill_count
      USING ERRCODE = '23514',
            HINT = 'Use a published version in the Channel workspace or a published public system Skill version, then retry the migration.';
  END IF;

  SELECT count(*) INTO invalid_pipeline_count
  FROM channel_defaults d
  LEFT JOIN pipeline_versions v
    ON v.id = d.pipeline_version_id
  LEFT JOIN pipelines p
    ON p.workspace_id = v.workspace_id AND p.id = v.pipeline_id
  LEFT JOIN workspaces w
    ON w.id = v.workspace_id
  WHERE v.id IS NULL
     OR p.id IS NULL
     OR w.id IS NULL
     OR v.state <> 'published'
     OR p.status <> 'active'
     OR NOT (
       v.workspace_id = d.workspace_id
       OR (
         p.visibility = 'public_readonly'
         AND w.kind = 'system'
       )
     );

  IF invalid_pipeline_count > 0 THEN
    RAISE EXCEPTION
      'channel backend migration refused: % channel_defaults rows reference an unavailable Pipeline version',
      invalid_pipeline_count
      USING ERRCODE = '23514',
            HINT = 'Use an active published version in the Channel workspace or an active published public system Pipeline version, then retry the migration.';
  END IF;
END
$channel_defaults_preflight$;

CREATE TYPE channel_status AS ENUM ('active', 'paused', 'archived');

CREATE TABLE platform_connections (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  workspace_id uuid NOT NULL REFERENCES workspaces(id) ON DELETE RESTRICT,
  platform text NOT NULL CHECK (btrim(platform) <> ''),
  name text NOT NULL CHECK (btrim(name) <> ''),
  secret_ref text NOT NULL CHECK (btrim(secret_ref) <> ''),
  status channel_status NOT NULL DEFAULT 'active',
  created_by uuid REFERENCES users(id) ON DELETE SET NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (workspace_id, id),
  UNIQUE (workspace_id, id, platform),
  UNIQUE (workspace_id, platform, name)
);

CREATE TRIGGER platform_connections_set_updated_at
  BEFORE UPDATE ON platform_connections
  FOR EACH ROW EXECUTE FUNCTION app.set_updated_at();

ALTER TABLE channels RENAME COLUMN external_ref TO handle;
ALTER TABLE channels RENAME COLUMN brand_config TO brand_profile;
ALTER TABLE channels
  DROP CONSTRAINT IF EXISTS channels_workspace_id_platform_external_ref_key;

ALTER TABLE channels
  ALTER COLUMN status DROP DEFAULT,
  ADD COLUMN schema_version text NOT NULL DEFAULT '1.0.0',
  ADD COLUMN ownership_type ownership_type NOT NULL DEFAULT 'workspace',
  ADD COLUMN description text NOT NULL DEFAULT '',
  ADD COLUMN platform_connection_id uuid,
  ADD COLUMN revision integer NOT NULL DEFAULT 1 CHECK (revision > 0);

ALTER TABLE channels
  ALTER COLUMN status TYPE channel_status
  USING (
    CASE status::text
      WHEN 'draft' THEN 'paused'
      ELSE status::text
    END
  )::channel_status,
  ALTER COLUMN status SET DEFAULT 'active',
  ADD CONSTRAINT channels_platform_connection_fk
    FOREIGN KEY (workspace_id, platform_connection_id, platform)
    REFERENCES platform_connections(workspace_id, id, platform) ON DELETE RESTRICT,
  ADD CONSTRAINT channels_platform_connection_requires_platform
    CHECK (platform_connection_id IS NULL OR platform IS NOT NULL),
  ADD CONSTRAINT channels_name_not_blank CHECK (btrim(name) <> ''),
  ADD CONSTRAINT channels_slug_format CHECK (slug ~ '^[a-z0-9]+(?:-[a-z0-9]+)*$'),
  ADD CONSTRAINT channels_platform_not_blank CHECK (platform IS NULL OR btrim(platform) <> ''),
  ADD CONSTRAINT channels_handle_not_blank CHECK (handle IS NULL OR btrim(handle) <> '');

CREATE UNIQUE INDEX channels_workspace_platform_handle_uq
  ON channels (workspace_id, platform, handle)
  WHERE platform IS NOT NULL AND handle IS NOT NULL;

CREATE TABLE channel_default_asset_libraries (
  channel_id uuid NOT NULL,
  workspace_id uuid NOT NULL REFERENCES workspaces(id) ON DELETE RESTRICT,
  asset_library_id uuid NOT NULL,
  ordinal integer NOT NULL CHECK (ordinal >= 0),
  PRIMARY KEY (channel_id, asset_library_id),
  UNIQUE (channel_id, ordinal),
  FOREIGN KEY (workspace_id, channel_id)
    REFERENCES channels(workspace_id, id) ON DELETE CASCADE,
  FOREIGN KEY (workspace_id, asset_library_id)
    REFERENCES asset_libraries(workspace_id, id) ON DELETE RESTRICT
);

INSERT INTO channel_default_asset_libraries (
  channel_id, workspace_id, asset_library_id, ordinal
)
SELECT channel_id, workspace_id, asset_library_id, 0
FROM channel_defaults
WHERE asset_library_id IS NOT NULL;

ALTER TABLE channel_defaults
  DROP CONSTRAINT IF EXISTS channel_defaults_workspace_id_skill_version_id_fkey,
  DROP CONSTRAINT IF EXISTS channel_defaults_workspace_id_pipeline_version_id_fkey,
  DROP COLUMN asset_library_id,
  DROP COLUMN qc_overrides,
  ALTER COLUMN skill_version_id SET NOT NULL,
  ALTER COLUMN pipeline_version_id SET NOT NULL,
  ADD CONSTRAINT channel_defaults_skill_version_id_fkey
    FOREIGN KEY (skill_version_id) REFERENCES skill_versions(id) ON DELETE RESTRICT,
  ADD CONSTRAINT channel_defaults_pipeline_version_id_fkey
    FOREIGN KEY (pipeline_version_id) REFERENCES pipeline_versions(id) ON DELETE RESTRICT;

-- A Channel may use versions owned by its workspace or immutable public
-- system versions. The global foreign keys prove existence; this trigger owns
-- the workspace/visibility/publication policy and prevents direct SQL writes
-- from bypassing the API's validation.
CREATE OR REPLACE FUNCTION app.validate_channel_default_versions() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  IF NOT EXISTS (
    SELECT 1
    FROM skill_versions v
    JOIN skills s ON s.workspace_id = v.workspace_id AND s.id = v.skill_id
    JOIN workspaces w ON w.id = v.workspace_id
    WHERE v.id = NEW.skill_version_id
      AND v.state = 'published'
      AND (
        v.workspace_id = NEW.workspace_id
        OR (
          v.ownership_type = 'system'
          AND s.ownership_type = 'system'
          AND s.visibility = 'public_readonly'
          AND w.kind = 'system'
        )
      )
  ) THEN
    RAISE EXCEPTION 'channel default Skill version is not available to this workspace'
      USING ERRCODE = '23514',
            CONSTRAINT = 'channel_defaults_skill_version_policy';
  END IF;

  IF NOT EXISTS (
    SELECT 1
    FROM pipeline_versions v
    JOIN pipelines p ON p.workspace_id = v.workspace_id AND p.id = v.pipeline_id
    JOIN workspaces w ON w.id = v.workspace_id
    WHERE v.id = NEW.pipeline_version_id
      AND v.state = 'published'
      AND p.status = 'active'
      AND (
        v.workspace_id = NEW.workspace_id
        OR (
          p.visibility = 'public_readonly'
          AND w.kind = 'system'
        )
      )
  ) THEN
    RAISE EXCEPTION 'channel default Pipeline version is not available to this workspace'
      USING ERRCODE = '23514',
            CONSTRAINT = 'channel_defaults_pipeline_version_policy';
  END IF;

  RETURN NEW;
END
$$;

CREATE TRIGGER channel_defaults_validate_versions
  BEFORE INSERT OR UPDATE OF workspace_id, skill_version_id, pipeline_version_id
  ON channel_defaults
  FOR EACH ROW EXECUTE FUNCTION app.validate_channel_default_versions();

COMMIT;
