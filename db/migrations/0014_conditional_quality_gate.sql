-- A successful decoded A/V quality check may complete automatically. The
-- capability still requests review whenever it finds a concrete risk; keeping
-- a static review gate here would stop every unattended production even when
-- the report has verdict=pass and an empty risk list.

-- This is a one-time correction to the unreleased official v1 seed. Temporarily
-- remove only its version-protection trigger inside the migration transaction;
-- user-created and unrelated official versions remain untouched by the guarded
-- UPDATE below, and immutability is restored immediately afterwards.
DROP TRIGGER IF EXISTS pipeline_versions_immutable ON pipeline_versions;

UPDATE pipeline_versions
SET graph = jsonb_set(graph, '{nodes,5,review_gate}', 'false'::jsonb, false),
    content_hash = '160d6eff263856f9f1e437d44d5f8c2f340353a7f3b323b18640587f0b36b5ae'
WHERE id = '30b37ab7-9cf7-5d26-ac3e-6d66880b536c'
  AND content_hash = '3b54dc8a227beab1bc60b6fe7a9081bbcec0ee6bb07de7815711e1471ba24b1f';

CREATE TRIGGER pipeline_versions_immutable
  BEFORE UPDATE OR DELETE ON pipeline_versions
  FOR EACH ROW EXECUTE FUNCTION app.protect_version();
