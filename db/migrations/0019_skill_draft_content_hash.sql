-- A follow-up draft starts as an exact copy of an immutable published version.
-- Its content hash must therefore be allowed to match until the user edits it.
-- Keep lookup performance without treating the hash as version identity.

BEGIN;

ALTER TABLE skill_versions
  DROP CONSTRAINT skill_versions_skill_id_content_hash_key;

CREATE INDEX skill_versions_skill_id_content_hash_idx
  ON skill_versions (skill_id, content_hash);

COMMIT;
