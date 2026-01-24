-- Core event log table (Timescale hypertable)

CREATE TABLE IF NOT EXISTS events (
  ts           timestamptz NOT NULL,
  ingested_at  timestamptz NOT NULL DEFAULT now(),
  topic        text        NOT NULL,
  source       text        NULL,
  type         text        NULL,
  id           text        NULL,
  trace_id     text        NULL,
  payload      jsonb       NOT NULL
);

SELECT create_hypertable('events', 'ts', if_not_exists => TRUE);

CREATE INDEX IF NOT EXISTS events_topic_ts_idx ON events (topic, ts DESC);
CREATE INDEX IF NOT EXISTS events_type_ts_idx  ON events (type, ts DESC);
CREATE INDEX IF NOT EXISTS events_trace_ts_idx ON events (trace_id, ts DESC);

