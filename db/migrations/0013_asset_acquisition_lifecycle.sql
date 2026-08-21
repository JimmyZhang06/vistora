-- Unify the control API and analysis worker asset lifecycle.
-- 0009 introduced disabled/deleted, while 0010 accidentally replaced those
-- states when it added awaiting_review. Automatic acquisition needs all of
-- them: analyse -> await/reject -> soft-delete -> optionally restore.

ALTER TABLE assets DROP CONSTRAINT IF EXISTS assets_status_check;
ALTER TABLE assets ADD CONSTRAINT assets_status_check
  CHECK (status IN (
    'processing',
    'quarantined',
    'awaiting_review',
    'ready',
    'disabled',
    'deleted',
    'archived'
  ));
