-- Preserve the declarative review gate separately from a review requested by
-- a step result.  Without this distinction, a dynamic review that is sent
-- back for changes remains review-required forever, even after a clean retry.
ALTER TABLE run_steps
  ADD COLUMN IF NOT EXISTS static_review_required boolean NOT NULL DEFAULT false;

UPDATE run_steps rs
SET static_review_required = COALESCE((
  SELECT (node.value ->> 'review_gate')::boolean
  FROM runs r
  JOIN pipeline_versions pv ON pv.id = r.pipeline_version_id
  CROSS JOIN LATERAL jsonb_array_elements(pv.graph -> 'nodes') AS node(value)
  WHERE r.workspace_id = rs.workspace_id
    AND r.id = rs.run_id
    AND node.value ->> 'key' = rs.step_key
  LIMIT 1
), false);
