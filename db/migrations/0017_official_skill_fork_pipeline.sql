-- Let a workspace-owned Skill draft keep the default Pipeline reference copied
-- from a public system Skill during a fork. Existence is enforced by a global
-- foreign key; the trigger prevents references to private foreign workspaces.

BEGIN;

DO $skill_version_pipeline_preflight$
DECLARE
  invalid_count bigint;
BEGIN
  SELECT count(*) INTO invalid_count
  FROM skill_versions v
  LEFT JOIN pipeline_versions pv
    ON pv.id = v.default_pipeline_version_id
  LEFT JOIN pipelines p
    ON p.workspace_id = pv.workspace_id AND p.id = pv.pipeline_id
  LEFT JOIN workspaces w
    ON w.id = pv.workspace_id
  WHERE v.default_pipeline_version_id IS NOT NULL
    AND (
      pv.id IS NULL
      OR p.id IS NULL
      OR w.id IS NULL
      OR NOT (
        pv.workspace_id = v.workspace_id
        OR (
          w.kind = 'system'
          AND p.visibility = 'public_readonly'
          AND p.status = 'active'
          AND pv.state = 'published'
        )
      )
    );

  IF invalid_count > 0 THEN
    RAISE EXCEPTION
      'official Skill fork migration refused: % Skill versions reference an unavailable default Pipeline version',
      invalid_count
      USING ERRCODE = '23514',
            HINT = 'Use a Pipeline version in the Skill workspace or an active published public system Pipeline version, then retry the migration.';
  END IF;
END
$skill_version_pipeline_preflight$;

ALTER TABLE skill_versions
  DROP CONSTRAINT IF EXISTS skill_versions_workspace_id_default_pipeline_version_id_fkey,
  ADD CONSTRAINT skill_versions_default_pipeline_version_id_fkey
    FOREIGN KEY (default_pipeline_version_id)
    REFERENCES pipeline_versions(id) ON DELETE RESTRICT;

CREATE OR REPLACE FUNCTION app.validate_skill_version_default_pipeline() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.default_pipeline_version_id IS NULL THEN
    RETURN NEW;
  END IF;

  IF NOT EXISTS (
    SELECT 1
    FROM pipeline_versions pv
    JOIN pipelines p
      ON p.workspace_id = pv.workspace_id AND p.id = pv.pipeline_id
    JOIN workspaces w
      ON w.id = pv.workspace_id
    WHERE pv.id = NEW.default_pipeline_version_id
      AND (
        pv.workspace_id = NEW.workspace_id
        OR (
          w.kind = 'system'
          AND p.visibility = 'public_readonly'
          AND p.status = 'active'
          AND pv.state = 'published'
        )
      )
  ) THEN
    RAISE EXCEPTION 'Skill default Pipeline version is not available to this workspace'
      USING ERRCODE = '23514',
            CONSTRAINT = 'skill_versions_default_pipeline_policy';
  END IF;

  RETURN NEW;
END
$$;

CREATE TRIGGER skill_versions_validate_default_pipeline
  BEFORE INSERT OR UPDATE OF workspace_id, default_pipeline_version_id
  ON skill_versions
  FOR EACH ROW EXECUTE FUNCTION app.validate_skill_version_default_pipeline();

COMMIT;
