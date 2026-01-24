-- Schedules table (control plane for time-trigger service)

CREATE TABLE IF NOT EXISTS schedules (
  id            bigserial PRIMARY KEY,
  name          text        NOT NULL UNIQUE,
  enabled       boolean     NOT NULL DEFAULT true,
  kind          text        NOT NULL, -- 'cron' | 'interval' | 'once'
  timezone      text        NOT NULL,
  spec          text        NOT NULL, -- cron: "min hour day month dow" (5 fields)
  mqtt_topic    text        NOT NULL,
  event_type    text        NOT NULL,
  data          jsonb       NOT NULL DEFAULT '{}'::jsonb,
  created_at    timestamptz NOT NULL DEFAULT now(),
  updated_at    timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS schedules_enabled_idx ON schedules (enabled);
CREATE INDEX IF NOT EXISTS schedules_updated_at_idx ON schedules (updated_at DESC);

