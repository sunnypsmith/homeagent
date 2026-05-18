-- Retention: auto-drop chunks older than 30 days.
SELECT add_retention_policy('events', INTERVAL '30 days', if_not_exists => true);

-- Partial index for dashboard error queries so they hit an index
-- instead of doing a sequential scan with ILIKE on every poll.
CREATE INDEX IF NOT EXISTS events_error_ingested_idx
  ON events (ingested_at DESC)
  WHERE type LIKE '%.failed%'
     OR type LIKE '%.error%'
     OR type LIKE '%.exception%';
