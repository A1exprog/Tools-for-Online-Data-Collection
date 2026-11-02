-- Схема PostgreSQL для no-code загрузки (n8n). Выполняется один раз:
--   docker compose exec postgres psql -U lab6 -d lab6 -f /schema/schema_postgres.sql
CREATE TABLE IF NOT EXISTS locations (
    location_id    TEXT PRIMARY KEY CHECK (location_id ~ '^[a-z0-9]+(-[a-z0-9]+)*$'),
    city           TEXT NOT NULL CHECK (length(city) >= 2),
    latitude       DOUBLE PRECISION NOT NULL CHECK (latitude BETWEEN -90 AND 90),
    longitude      DOUBLE PRECISION NOT NULL CHECK (longitude BETWEEN -180 AND 180),
    source         TEXT NOT NULL,
    source_url     TEXT NOT NULL,
    parser_version TEXT NOT NULL,
    collected_at   TIMESTAMPTZ NOT NULL,
    updated_at     TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS observations (
    observation_id       TEXT PRIMARY KEY,
    location_id          TEXT NOT NULL REFERENCES locations (location_id),
    observation_time_utc TIMESTAMPTZ NOT NULL,
    day_code             CHAR(4) NOT NULL,
    temperature_2m_max   NUMERIC(4, 1) NOT NULL CHECK (temperature_2m_max BETWEEN -90 AND 60),
    temperature_2m_min   NUMERIC(4, 1) NOT NULL CHECK (temperature_2m_min BETWEEN -90 AND 60),
    precipitation_sum    NUMERIC(5, 1) CHECK (precipitation_sum IS NULL OR precipitation_sum BETWEEN 0 AND 500),
    source               TEXT NOT NULL,
    source_url           TEXT NOT NULL,
    parser_version       TEXT NOT NULL,
    collected_at         TIMESTAMPTZ NOT NULL,
    updated_at           TIMESTAMPTZ NOT NULL,
    CHECK (temperature_2m_max >= temperature_2m_min),
    UNIQUE (location_id, observation_time_utc)
);
CREATE INDEX IF NOT EXISTS idx_obs_time ON observations (observation_time_utc);

CREATE TABLE IF NOT EXISTS rejects (
    id           BIGSERIAL PRIMARY KEY,
    source       TEXT NOT NULL,
    reason       TEXT NOT NULL,
    synthetic    TEXT,
    payload      JSONB NOT NULL,
    payload_hash TEXT GENERATED ALWAYS AS (md5(payload::text)) STORED,
    created_at   TIMESTAMPTZ NOT NULL,
    UNIQUE (source, payload_hash)
);
