-- Index for dashboard queries that filter/sort on ingested_at + type.
-- Without this, the UI gateway's "recent events" query scans the full table.

CREATE INDEX IF NOT EXISTS events_ingested_at_idx ON events (ingested_at DESC);
CREATE INDEX IF NOT EXISTS events_ingested_type_idx ON events (ingested_at DESC, type);
