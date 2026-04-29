-- Learned intents table for voice-intent-agent fuzzy matching.
-- Uses pg_trgm for similarity-based phrase lookup.

CREATE EXTENSION IF NOT EXISTS pg_trgm;

CREATE TABLE IF NOT EXISTS learned_intents (
  id            bigserial PRIMARY KEY,
  phrase        text        NOT NULL,
  category      text        NOT NULL,
  tool_name     text        NOT NULL DEFAULT '',
  tool_args     jsonb       NOT NULL DEFAULT '{}'::jsonb,
  mqtt_topic    text        NOT NULL DEFAULT '',
  mqtt_payload  jsonb       NOT NULL DEFAULT '{}'::jsonb,
  description   text        NOT NULL DEFAULT '',
  use_count     integer     NOT NULL DEFAULT 0,
  last_used_at  timestamptz,
  created_at    timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS learned_intents_phrase_trgm_idx
  ON learned_intents USING gin (phrase gin_trgm_ops);

CREATE INDEX IF NOT EXISTS learned_intents_category_idx
  ON learned_intents (category);
