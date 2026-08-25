-- Admit the dedicated browser-capture namespace without weakening tenant binding.
--
-- The capture Worker deliberately stores objects below browser-capture/ so its
-- S3 credentials can be restricted independently. The original constraint
-- predates that namespace and only accepted workspaces/... keys.

BEGIN;

ALTER TABLE artifacts
  DROP CONSTRAINT artifacts_tenant_key;

ALTER TABLE artifacts
  ADD CONSTRAINT artifacts_tenant_key CHECK (
    (
      object_key LIKE
        'workspaces/' || workspace_id::text || '/runs/' || run_id::text || '/artifacts/%'
      OR object_key LIKE
        'browser-capture/workspaces/' || workspace_id::text || '/runs/' || run_id::text || '/artifacts/%'
    )
    AND object_key !~ '(^|/)\.\.(/|$)'
  );

COMMIT;
