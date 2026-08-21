-- Public media acquisition may legitimately reject several candidates for a
-- watermark, embedded credits, unsafe content or an unusable download. Give
-- the automatic media-selection node enough recovery rounds to replace those
-- candidates without requiring a person to restart the Run.

DROP TRIGGER IF EXISTS pipeline_versions_immutable ON pipeline_versions;

UPDATE pipeline_versions
SET graph = jsonb_set(graph, '{nodes,3,maximum_attempts}', '6'::jsonb, false),
    content_hash = '87ab3ff535a8e56a745f46170ecd0d8ff23201f09b98328a1fc09598c07e8801'
WHERE id = '30b37ab7-9cf7-5d26-ac3e-6d66880b536c'
  AND content_hash = '160d6eff263856f9f1e437d44d5f8c2f340353a7f3b323b18640587f0b36b5ae';

CREATE TRIGGER pipeline_versions_immutable
  BEFORE UPDATE OR DELETE ON pipeline_versions
  FOR EACH ROW EXECUTE FUNCTION app.protect_version();
