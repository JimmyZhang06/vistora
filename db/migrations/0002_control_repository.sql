-- Persist every field exposed by the v1 control-plane repository contract.
-- This migration is intentionally additive so existing phase-one data remains valid.

BEGIN;

ALTER TABLE skills
  ADD COLUMN schema_version text NOT NULL DEFAULT '1.0.0',
  ADD COLUMN revision integer NOT NULL DEFAULT 1 CHECK (revision > 0);

ALTER TABLE skill_versions
  ADD COLUMN revision integer NOT NULL DEFAULT 1 CHECK (revision > 0),
  ADD COLUMN test_topics jsonb NOT NULL DEFAULT '[]'::jsonb,
  ADD COLUMN release_notes text NOT NULL DEFAULT '';

ALTER TABLE runs
  ADD COLUMN schema_version text NOT NULL DEFAULT '1.0.0',
  ADD COLUMN ownership_type ownership_type NOT NULL DEFAULT 'workspace',
  ADD COLUMN idempotency_key text;
CREATE UNIQUE INDEX runs_workspace_idempotency_uq
  ON runs (workspace_id, idempotency_key)
  WHERE idempotency_key IS NOT NULL;

ALTER TABLE skill_evaluations
  DROP CONSTRAINT skill_evaluations_status_check,
  ADD COLUMN schema_version text NOT NULL DEFAULT '1.0.0',
  ADD COLUMN ownership_type ownership_type NOT NULL DEFAULT 'workspace',
  ADD COLUMN skill_id uuid,
  ADD COLUMN left_version_id uuid,
  ADD COLUMN right_version_id uuid,
  ADD COLUMN topic text,
  ADD COLUMN inputs jsonb NOT NULL DEFAULT '{}'::jsonb,
  ADD COLUMN evaluator text,
  ADD COLUMN error jsonb,
  ADD COLUMN started_at timestamptz,
  ADD COLUMN finished_at timestamptz,
  ADD COLUMN updated_at timestamptz NOT NULL DEFAULT now(),
  ADD CONSTRAINT skill_evaluations_status_check
    CHECK (status IN ('queued', 'running', 'passed', 'succeeded', 'failed', 'cancelled')),
  ADD CONSTRAINT skill_evaluations_skill_fk
    FOREIGN KEY (workspace_id, skill_id) REFERENCES skills(workspace_id, id) ON DELETE RESTRICT,
  ADD CONSTRAINT skill_evaluations_left_version_fk
    FOREIGN KEY (workspace_id, left_version_id)
    REFERENCES skill_versions(workspace_id, id) ON DELETE RESTRICT,
  ADD CONSTRAINT skill_evaluations_right_version_fk
    FOREIGN KEY (workspace_id, right_version_id)
    REFERENCES skill_versions(workspace_id, id) ON DELETE RESTRICT;

-- Rows created before the HTTP comparison API existed remain readable, while new
-- repository writes must provide the complete comparison identity.
ALTER TABLE skill_evaluations ADD CONSTRAINT skill_evaluations_comparison_shape CHECK (
  evaluation_kind <> 'version_comparison'
  OR (
    skill_id IS NOT NULL
    AND left_version_id IS NOT NULL
    AND right_version_id IS NOT NULL
    AND topic IS NOT NULL
    AND evaluator IS NOT NULL
  )
);

CREATE INDEX skill_evaluations_skill_idx
  ON skill_evaluations (workspace_id, skill_id, created_at DESC);

CREATE TRIGGER skill_evaluations_set_updated_at
  BEFORE UPDATE ON skill_evaluations
  FOR EACH ROW EXECUTE FUNCTION app.set_updated_at();

COMMIT;
