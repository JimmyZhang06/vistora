-- Repair schema drift where a renamed legacy composite foreign key survived
-- 0017, then prove the global Pipeline foreign key and policy trigger exist.

BEGIN;

DO $skill_pipeline_constraint_audit$
DECLARE
  legacy_constraint text;
  workspace_column smallint;
  pipeline_column smallint;
BEGIN
  SELECT attnum INTO workspace_column
  FROM pg_attribute
  WHERE attrelid = 'skill_versions'::regclass
    AND attname = 'workspace_id'
    AND NOT attisdropped;

  SELECT attnum INTO pipeline_column
  FROM pg_attribute
  WHERE attrelid = 'skill_versions'::regclass
    AND attname = 'default_pipeline_version_id'
    AND NOT attisdropped;

  FOR legacy_constraint IN
    SELECT c.conname
    FROM pg_constraint c
    WHERE c.conrelid = 'skill_versions'::regclass
      AND c.confrelid = 'pipeline_versions'::regclass
      AND c.contype = 'f'
      AND cardinality(c.conkey) = 2
      AND c.conkey @> ARRAY[workspace_column, pipeline_column]::smallint[]
  LOOP
    EXECUTE format(
      'ALTER TABLE skill_versions DROP CONSTRAINT %I',
      legacy_constraint
    );
  END LOOP;

  IF NOT EXISTS (
    SELECT 1
    FROM pg_constraint c
    WHERE c.conrelid = 'skill_versions'::regclass
      AND c.confrelid = 'pipeline_versions'::regclass
      AND c.contype = 'f'
      AND c.conkey = ARRAY[pipeline_column]::smallint[]
  ) THEN
    RAISE EXCEPTION
      'Skill Pipeline constraint audit refused: global default Pipeline foreign key is missing'
      USING ERRCODE = '23514';
  END IF;

  IF NOT EXISTS (
    SELECT 1
    FROM pg_trigger t
    WHERE t.tgrelid = 'skill_versions'::regclass
      AND t.tgname = 'skill_versions_validate_default_pipeline'
      AND NOT t.tgisinternal
  ) THEN
    RAISE EXCEPTION
      'Skill Pipeline constraint audit refused: visibility policy trigger is missing'
      USING ERRCODE = '23514';
  END IF;
END
$skill_pipeline_constraint_audit$;

COMMIT;
