-- Dedicated Full-AI control plane. It shares the durable scheduler Run but has
-- its own typed request snapshot and paid-provider operation ledger.

BEGIN;

CREATE TABLE full_ai_runs (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  workspace_id uuid NOT NULL REFERENCES workspaces(id) ON DELETE RESTRICT,
  underlying_run_id uuid NOT NULL,
  status text NOT NULL DEFAULT 'queued'
    CHECK (status IN (
      'queued', 'planning', 'generating', 'assembling', 'quality_check',
      'succeeded', 'failed', 'cancelled', 'reconciliation_required'
    )),
  provider_name text NOT NULL CHECK (btrim(provider_name) <> ''),
  model_id text NOT NULL CHECK (btrim(model_id) <> ''),
  spec jsonb NOT NULL CHECK (jsonb_typeof(spec) = 'object'),
  plan jsonb NOT NULL CHECK (jsonb_typeof(plan) = 'object'),
  quote jsonb NOT NULL CHECK (jsonb_typeof(quote) = 'object'),
  billing_status text NOT NULL DEFAULT 'not_started'
    CHECK (billing_status IN (
      'not_started', 'submitting', 'submitted', 'submit_unknown', 'settled', 'failed'
    )),
  authorized_amount_minor bigint NOT NULL CHECK (authorized_amount_minor >= 0),
  incurred_amount_minor bigint NOT NULL DEFAULT 0 CHECK (incurred_amount_minor >= 0),
  requires_reconciliation boolean NOT NULL DEFAULT false,
  idempotency_key text NOT NULL CHECK (char_length(idempotency_key) BETWEEN 8 AND 255),
  request_hash char(64) NOT NULL CHECK (request_hash ~ '^[a-f0-9]{64}$'),
  estimate_fingerprint char(64) NOT NULL
    CHECK (estimate_fingerprint ~ '^[a-f0-9]{64}$'),
  created_by uuid REFERENCES users(id) ON DELETE SET NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  FOREIGN KEY (workspace_id, underlying_run_id)
    REFERENCES runs(workspace_id, id) ON DELETE RESTRICT,
  UNIQUE (workspace_id, id),
  UNIQUE (workspace_id, underlying_run_id),
  UNIQUE (workspace_id, idempotency_key),
  CONSTRAINT full_ai_runs_reconciliation_shape CHECK (
    requires_reconciliation = (billing_status = 'submit_unknown')
  ),
  CONSTRAINT full_ai_runs_cost_bounds CHECK (
    incurred_amount_minor <= authorized_amount_minor
  )
);

CREATE TABLE full_ai_paid_operations (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  workspace_id uuid NOT NULL REFERENCES workspaces(id) ON DELETE RESTRICT,
  full_ai_run_id uuid NOT NULL,
  operation_key text NOT NULL CHECK (btrim(operation_key) <> ''),
  scene_key text NOT NULL CHECK (btrim(scene_key) <> ''),
  variant_index integer NOT NULL CHECK (variant_index BETWEEN 0 AND 2),
  request_hash char(64) NOT NULL CHECK (request_hash ~ '^[a-f0-9]{64}$'),
  provider_name text NOT NULL CHECK (btrim(provider_name) <> ''),
  model_id text NOT NULL CHECK (btrim(model_id) <> ''),
  provider_idempotency_key text NOT NULL CHECK (btrim(provider_idempotency_key) <> ''),
  status text NOT NULL DEFAULT 'reserved'
    CHECK (status IN (
      'reserved', 'submitting', 'submitted', 'submit_unknown',
      'succeeded', 'failed', 'cancelled'
    )),
  provider_request_id text,
  authorized_amount_minor bigint NOT NULL CHECK (authorized_amount_minor >= 0),
  incurred_amount_minor bigint NOT NULL DEFAULT 0 CHECK (incurred_amount_minor >= 0),
  lease_owner text,
  lease_token uuid,
  lease_expires_at timestamptz,
  reconciliation_attempts integer NOT NULL DEFAULT 0
    CHECK (reconciliation_attempts >= 0),
  last_error jsonb,
  result jsonb,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  reconciled_at timestamptz,
  FOREIGN KEY (workspace_id, full_ai_run_id)
    REFERENCES full_ai_runs(workspace_id, id) ON DELETE RESTRICT,
  UNIQUE (workspace_id, id),
  UNIQUE (workspace_id, full_ai_run_id, operation_key),
  UNIQUE (workspace_id, full_ai_run_id, scene_key, variant_index),
  UNIQUE (provider_name, provider_idempotency_key),
  CONSTRAINT full_ai_paid_operations_task_shape CHECK (
    status NOT IN ('submitted', 'succeeded') OR provider_request_id IS NOT NULL
  ),
  CONSTRAINT full_ai_paid_operations_lease_shape CHECK (
    (status = 'submitting'
      AND lease_owner IS NOT NULL AND lease_token IS NOT NULL AND lease_expires_at IS NOT NULL)
    OR
    (status <> 'submitting'
      AND lease_owner IS NULL AND lease_token IS NULL AND lease_expires_at IS NULL)
  ),
  CONSTRAINT full_ai_paid_operations_cost_bounds CHECK (
    incurred_amount_minor <= authorized_amount_minor
  ),
  CONSTRAINT full_ai_paid_operations_result_shape CHECK (
    CASE WHEN status = 'succeeded' THEN
      result IS NOT NULL
      AND jsonb_typeof(result) = 'object'
      AND pg_column_size(result) <= 1048576
      AND result ?& ARRAY[
        'schema_version', 'verification_status', 'output_content_hash',
        'output_byte_size', 'output_media_type', 'output_filename',
        'accepted_artifact', 'verification_evidence'
      ]
      AND result->>'schema_version' = '1.0.0'
      AND result->>'verification_status' IN ('accepted', 'rejected')
      AND result->>'output_content_hash' ~ '^[a-f0-9]{64}$'
      AND result->>'output_byte_size' ~ '^[1-9][0-9]*$'
      AND btrim(result->>'output_media_type') <> ''
      AND btrim(result->>'output_filename') <> ''
      AND result ? 'accepted_artifact'
      AND jsonb_typeof(result->'accepted_artifact') IN ('null', 'object')
      AND (
        result->>'verification_status' = 'accepted'
        OR jsonb_typeof(result->'accepted_artifact') = 'null'
      )
      AND jsonb_typeof(result->'verification_evidence') = 'object'
      AND result->'verification_evidence' <> '{}'::jsonb
      AND result->'verification_evidence' ? 'accepted'
      AND result->'verification_evidence'->'accepted' IN ('true'::jsonb, 'false'::jsonb)
      AND (
        (result->>'verification_status' = 'accepted'
          AND result->'verification_evidence'->'accepted' = 'true'::jsonb)
        OR
        (result->>'verification_status' = 'rejected'
          AND result->'verification_evidence'->'accepted' = 'false'::jsonb)
      )
      AND CASE
        WHEN jsonb_typeof(result->'accepted_artifact') = 'object' THEN
          result->'accepted_artifact' ?& ARRAY[
            'schema_version', 'id', 'workspace_id', 'ownership_type', 'run_id',
            'step_id', 'kind', 'media_type', 'object_key', 'byte_size',
            'content_hash', 'filename', 'created_at', 'expires_at'
          ]
          AND result->'accepted_artifact'->>'content_hash' = result->>'output_content_hash'
          AND result->'accepted_artifact'->>'byte_size' = result->>'output_byte_size'
          AND result->'accepted_artifact'->>'media_type' = result->>'output_media_type'
          AND result->'accepted_artifact'->>'filename' = result->>'output_filename'
        ELSE jsonb_typeof(result->'accepted_artifact') = 'null'
      END
    ELSE result IS NULL
    END
  )
);

CREATE INDEX full_ai_runs_workspace_created_idx
  ON full_ai_runs (workspace_id, created_at DESC, id DESC);
CREATE INDEX full_ai_paid_operations_recovery_idx
  ON full_ai_paid_operations (status, lease_expires_at, updated_at)
  WHERE status IN ('submitting', 'submitted', 'submit_unknown');

CREATE OR REPLACE FUNCTION app.validate_full_ai_paid_operation_transition()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.workspace_id <> OLD.workspace_id
     OR NEW.full_ai_run_id <> OLD.full_ai_run_id
     OR NEW.operation_key <> OLD.operation_key
     OR NEW.scene_key <> OLD.scene_key
     OR NEW.variant_index <> OLD.variant_index
     OR NEW.request_hash <> OLD.request_hash
     OR NEW.provider_idempotency_key <> OLD.provider_idempotency_key
     OR NEW.provider_name <> OLD.provider_name
     OR NEW.model_id <> OLD.model_id
     OR NEW.authorized_amount_minor <> OLD.authorized_amount_minor THEN
    RAISE EXCEPTION 'Full-AI paid operation identity is immutable'
      USING ERRCODE = '23514';
  END IF;

  IF OLD.provider_request_id IS NOT NULL
     AND NEW.provider_request_id IS DISTINCT FROM OLD.provider_request_id THEN
    RAISE EXCEPTION 'Full-AI Provider task identity is immutable once known'
      USING ERRCODE = '23514';
  END IF;

  IF NEW.incurred_amount_minor < OLD.incurred_amount_minor
     OR NEW.reconciliation_attempts < OLD.reconciliation_attempts THEN
    RAISE EXCEPTION 'Full-AI cost and reconciliation counters are monotonic'
      USING ERRCODE = '23514';
  END IF;

  IF OLD.status IN ('succeeded', 'failed', 'cancelled')
     AND (
       NEW.status <> OLD.status
       OR NEW.incurred_amount_minor <> OLD.incurred_amount_minor
       OR NEW.provider_request_id IS DISTINCT FROM OLD.provider_request_id
       OR NEW.result IS DISTINCT FROM OLD.result
     ) THEN
    RAISE EXCEPTION 'Full-AI paid operation terminal state is immutable'
      USING ERRCODE = '23514';
  END IF;

  IF OLD.result IS NOT NULL AND NEW.result IS DISTINCT FROM OLD.result THEN
    RAISE EXCEPTION 'Full-AI paid operation result checkpoint is immutable'
      USING ERRCODE = '23514';
  END IF;

  IF OLD.status = 'submit_unknown'
     AND NEW.status IN ('reserved', 'submitting') THEN
    RAISE EXCEPTION 'submit_unknown must be reconciled and cannot be resubmitted'
      USING ERRCODE = '23514';
  END IF;

  -- A documented Provider rejection (for example HTTP 429 before task
  -- acceptance) is the only safe path back to reserved. An expired submit
  -- lease is not proof that POST was rejected and must become submit_unknown.
  IF OLD.status = 'submitting' AND NEW.status = 'reserved' AND NOT (
    OLD.provider_request_id IS NULL
    AND NEW.provider_request_id IS NULL
    AND NEW.incurred_amount_minor = 0
    AND COALESCE(
      NEW.last_error @> '{"definite_rejection": true}'::jsonb,
      false
    )
  ) THEN
    RAISE EXCEPTION 'submitting can be released only after a definite Provider rejection'
      USING ERRCODE = '23514';
  END IF;

  IF NEW.status <> OLD.status AND NOT (
    (OLD.status = 'reserved' AND NEW.status IN ('submitting', 'failed', 'cancelled'))
    OR (OLD.status = 'submitting' AND NEW.status IN (
      'reserved', 'submitted', 'submit_unknown', 'failed', 'cancelled'
    ))
    OR (OLD.status = 'submitted' AND NEW.status IN (
      'succeeded', 'submit_unknown', 'failed', 'cancelled'
    ))
    OR (OLD.status = 'submit_unknown' AND NEW.status IN (
      'submitted', 'succeeded', 'failed', 'cancelled'
    ))
  ) THEN
    RAISE EXCEPTION 'Invalid Full-AI paid operation state transition'
      USING ERRCODE = '23514';
  END IF;

  RETURN NEW;
END;
$$;

CREATE OR REPLACE FUNCTION app.prevent_full_ai_paid_operation_delete()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  RAISE EXCEPTION 'Full-AI paid operation audit records cannot be deleted'
    USING ERRCODE = '23514';
END;
$$;

CREATE OR REPLACE FUNCTION app.validate_full_ai_paid_operation_budget()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
  run_authorized bigint;
  run_candidate_count integer;
  run_provider text;
  run_model text;
  existing_count integer;
  existing_authorized bigint;
  existing_incurred bigint;
BEGIN
  SELECT authorized_amount_minor, (plan->>'candidate_count')::integer,
         provider_name, model_id
    INTO run_authorized, run_candidate_count, run_provider, run_model
  FROM full_ai_runs
  WHERE workspace_id = NEW.workspace_id AND id = NEW.full_ai_run_id
  FOR UPDATE;

  IF run_authorized IS NULL THEN
    RAISE EXCEPTION 'Full-AI paid operation has no parent Run'
      USING ERRCODE = '23503';
  END IF;
  IF NEW.provider_name <> run_provider OR NEW.model_id <> run_model THEN
    RAISE EXCEPTION 'Full-AI paid operation Provider differs from the frozen Run'
      USING ERRCODE = '23514';
  END IF;

  SELECT count(*)::integer,
         COALESCE(sum(authorized_amount_minor), 0)::bigint,
         COALESCE(sum(incurred_amount_minor), 0)::bigint
    INTO existing_count, existing_authorized, existing_incurred
  FROM full_ai_paid_operations
  WHERE workspace_id = NEW.workspace_id
    AND full_ai_run_id = NEW.full_ai_run_id
    AND id <> NEW.id;

  IF existing_count >= run_candidate_count THEN
    RAISE EXCEPTION 'Full-AI candidate count exceeds the frozen plan'
      USING ERRCODE = '23514';
  END IF;
  IF existing_authorized + NEW.authorized_amount_minor > run_authorized
     OR existing_incurred + NEW.incurred_amount_minor > run_authorized THEN
    RAISE EXCEPTION 'Full-AI paid operation exceeds the Run budget'
      USING ERRCODE = '23514';
  END IF;
  RETURN NEW;
END;
$$;

CREATE OR REPLACE FUNCTION app.aggregate_full_ai_run_billing()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
  total_incurred bigint;
  operation_count integer;
  expected_candidate_count integer;
  has_submit_unknown boolean;
  has_submitting boolean;
  has_submitted boolean;
  has_failed boolean;
  all_succeeded boolean;
  aggregate_billing_status text;
BEGIN
  SELECT count(*)::integer,
         COALESCE(sum(incurred_amount_minor), 0)::bigint,
         COALESCE(bool_or(status = 'submit_unknown'), false),
         COALESCE(bool_or(status = 'submitting'), false),
         COALESCE(bool_or(status IN ('submitted', 'succeeded')), false),
         COALESCE(bool_or(status IN ('failed', 'cancelled')), false),
         COALESCE(bool_and(status = 'succeeded'), false)
    INTO operation_count, total_incurred, has_submit_unknown, has_submitting,
         has_submitted, has_failed, all_succeeded
  FROM full_ai_paid_operations
  WHERE workspace_id = NEW.workspace_id
    AND full_ai_run_id = NEW.full_ai_run_id;

  SELECT (plan->>'candidate_count')::integer
    INTO expected_candidate_count
  FROM full_ai_runs
  WHERE workspace_id = NEW.workspace_id AND id = NEW.full_ai_run_id
  FOR UPDATE;

  aggregate_billing_status := CASE
    WHEN has_submit_unknown THEN 'submit_unknown'
    WHEN has_failed THEN 'failed'
    WHEN all_succeeded AND operation_count = expected_candidate_count THEN 'settled'
    WHEN has_submitting THEN 'submitting'
    WHEN has_submitted THEN 'submitted'
    ELSE 'not_started'
  END;

  UPDATE full_ai_runs
  SET billing_status = aggregate_billing_status,
      incurred_amount_minor = total_incurred,
      requires_reconciliation = has_submit_unknown,
      status = CASE
        WHEN has_submit_unknown THEN 'reconciliation_required'
        WHEN has_failed THEN 'failed'
        WHEN status = 'reconciliation_required' THEN 'generating'
        ELSE status
      END,
      updated_at = now()
  WHERE workspace_id = NEW.workspace_id AND id = NEW.full_ai_run_id;
  RETURN NEW;
END;
$$;

CREATE TRIGGER full_ai_paid_operations_validate_transition
  BEFORE UPDATE ON full_ai_paid_operations
  FOR EACH ROW EXECUTE FUNCTION app.validate_full_ai_paid_operation_transition();
CREATE TRIGGER full_ai_paid_operations_validate_budget
  BEFORE INSERT OR UPDATE ON full_ai_paid_operations
  FOR EACH ROW EXECUTE FUNCTION app.validate_full_ai_paid_operation_budget();
CREATE TRIGGER full_ai_paid_operations_prevent_delete
  BEFORE DELETE ON full_ai_paid_operations
  FOR EACH ROW EXECUTE FUNCTION app.prevent_full_ai_paid_operation_delete();
CREATE TRIGGER full_ai_paid_operations_aggregate_billing
  AFTER INSERT OR UPDATE ON full_ai_paid_operations
  FOR EACH ROW EXECUTE FUNCTION app.aggregate_full_ai_run_billing();

ALTER TABLE full_ai_runs ENABLE ROW LEVEL SECURITY;
ALTER TABLE full_ai_paid_operations ENABLE ROW LEVEL SECURITY;
CREATE POLICY full_ai_runs_tenant_access ON full_ai_runs
  USING (app.can_access_workspace(workspace_id))
  WITH CHECK (app.can_write_workspace(workspace_id));
CREATE POLICY full_ai_paid_operations_tenant_access ON full_ai_paid_operations
  USING (app.can_access_workspace(workspace_id))
  WITH CHECK (app.can_write_workspace(workspace_id));

-- Match the current single-workspace control-plane posture.
ALTER TABLE full_ai_runs DISABLE ROW LEVEL SECURITY;
ALTER TABLE full_ai_paid_operations DISABLE ROW LEVEL SECURITY;

COMMIT;
