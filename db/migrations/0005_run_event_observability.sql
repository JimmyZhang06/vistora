-- Durable, idempotent event stream metadata used by both the API and worker.
-- A run row is locked before sequence allocation, so concurrent writers cannot
-- allocate the same sequence. The deduplication key closes recovery/replay gaps.

ALTER TABLE run_events
  ADD COLUMN IF NOT EXISTS deduplication_key text,
  ADD COLUMN IF NOT EXISTS correlation_id uuid,
  ADD COLUMN IF NOT EXISTS causation_id uuid;

UPDATE run_events
SET deduplication_key = COALESCE(deduplication_key, 'legacy:' || id::text),
    correlation_id = COALESCE(correlation_id, run_id)
WHERE deduplication_key IS NULL OR correlation_id IS NULL;

ALTER TABLE run_events
  ALTER COLUMN deduplication_key SET NOT NULL,
  ALTER COLUMN correlation_id SET NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS run_events_deduplication_idx
  ON run_events (workspace_id, run_id, deduplication_key);

CREATE INDEX IF NOT EXISTS run_events_correlation_idx
  ON run_events (workspace_id, correlation_id, sequence);
