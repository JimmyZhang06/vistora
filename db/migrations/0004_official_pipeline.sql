-- Allow workspace Runs to consume immutable, public system-owned Skill and Pipeline
-- versions without copying official assets into every workspace.

BEGIN;

ALTER TABLE runs
  DROP CONSTRAINT IF EXISTS runs_workspace_id_skill_version_id_fkey,
  DROP CONSTRAINT IF EXISTS runs_workspace_id_pipeline_version_id_fkey;

DO $do$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conrelid = 'runs'::regclass AND conname = 'runs_skill_version_id_fkey'
  ) THEN
    ALTER TABLE runs ADD CONSTRAINT runs_skill_version_id_fkey
      FOREIGN KEY (skill_version_id) REFERENCES skill_versions(id) ON DELETE RESTRICT;
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conrelid = 'runs'::regclass AND conname = 'runs_pipeline_version_id_fkey'
  ) THEN
    ALTER TABLE runs ADD CONSTRAINT runs_pipeline_version_id_fkey
      FOREIGN KEY (pipeline_version_id) REFERENCES pipeline_versions(id) ON DELETE RESTRICT;
  END IF;
END
$do$;

CREATE OR REPLACE FUNCTION app.validate_run_versions() RETURNS trigger
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
    RAISE EXCEPTION 'run skill version must be a published workspace or public system version'
      USING ERRCODE = '23514';
  END IF;

  IF NOT EXISTS (
    SELECT 1
    FROM pipeline_versions v
    JOIN pipelines p ON p.workspace_id = v.workspace_id AND p.id = v.pipeline_id
    JOIN workspaces w ON w.id = v.workspace_id
    WHERE v.id = NEW.pipeline_version_id
      AND v.state = 'published'
      AND (
        v.workspace_id = NEW.workspace_id
        OR (
          p.visibility = 'public_readonly'
          AND w.kind = 'system'
        )
      )
  ) THEN
    RAISE EXCEPTION 'run pipeline version must be a published workspace or public system version'
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

COMMIT;
