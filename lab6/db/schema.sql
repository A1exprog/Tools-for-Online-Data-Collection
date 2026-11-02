-- Хранилище погодных наблюдений (SQLite). Все даты — TEXT в ISO 8601 с явным смещением +00:00.
PRAGMA foreign_keys = ON;

-- Справочник локаций (источник: HTML time-in.ru)
CREATE TABLE IF NOT EXISTS locations (
    location_id    TEXT PRIMARY KEY                         -- бизнес-ключ: ASCII-slug города
                   CHECK (location_id GLOB '[a-z0-9]*' AND location_id NOT GLOB '*[^a-z0-9-]*'),
    city           TEXT NOT NULL CHECK (length(city) >= 2),
    latitude       REAL NOT NULL CHECK (latitude  BETWEEN -90  AND 90),
    longitude      REAL NOT NULL CHECK (longitude BETWEEN -180 AND 180),
    source         TEXT NOT NULL,
    source_url     TEXT NOT NULL,
    parser_version TEXT NOT NULL,
    collected_at   TEXT NOT NULL CHECK (collected_at LIKE '%+00:00'),
    updated_at     TEXT NOT NULL CHECK (updated_at  LIKE '%+00:00'),
    UNIQUE (latitude, longitude, city)
);

-- Суточные наблюдения (источник: Open-Meteo API)
CREATE TABLE IF NOT EXISTS observations (
    observation_id       TEXT PRIMARY KEY,                  -- бизнес-ключ: location_id + '_' + дата
    location_id          TEXT NOT NULL REFERENCES locations(location_id),
    observation_time_utc TEXT NOT NULL CHECK (observation_time_utc LIKE '%+00:00'),
    day_code             TEXT NOT NULL CHECK (length(day_code) = 4),   -- 'MMDD', с ведущим нулём
    temperature_2m_max   REAL NOT NULL CHECK (temperature_2m_max > -90 AND temperature_2m_max <= 60),
    temperature_2m_min   REAL NOT NULL CHECK (temperature_2m_min > -90 AND temperature_2m_min <= 60),
    precipitation_sum    REAL CHECK (precipitation_sum IS NULL OR precipitation_sum BETWEEN 0 AND 500),
    source               TEXT NOT NULL,
    source_url           TEXT NOT NULL,
    parser_version       TEXT NOT NULL,
    collected_at         TEXT NOT NULL CHECK (collected_at LIKE '%+00:00'),
    updated_at           TEXT NOT NULL CHECK (updated_at  LIKE '%+00:00'),
    CHECK (temperature_2m_max >= temperature_2m_min),
    UNIQUE (location_id, observation_time_utc)              -- одна запись на город и сутки
);
-- под запросы mart: динамика по дням и выборки по периоду
CREATE INDEX IF NOT EXISTS idx_obs_time ON observations (observation_time_utc);

-- История изменений, SCD Type 2: версия действует с valid_from до valid_to (NULL = актуальная)
CREATE TABLE IF NOT EXISTS observations_history (
    observation_id     TEXT NOT NULL,
    valid_from         TEXT NOT NULL CHECK (valid_from LIKE '%+00:00'),
    valid_to           TEXT CHECK (valid_to IS NULL OR valid_to LIKE '%+00:00'),
    is_current         INTEGER NOT NULL CHECK (is_current IN (0, 1)),
    temperature_2m_max REAL NOT NULL,
    temperature_2m_min REAL NOT NULL,
    precipitation_sum  REAL,
    collected_at       TEXT NOT NULL,
    PRIMARY KEY (observation_id, valid_from),
    CHECK (valid_to IS NULL OR valid_to > valid_from)
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_hist_current ON observations_history (observation_id) WHERE is_current = 1;

-- Версия создаётся триггерами, поэтому история ведётся при любой загрузке, в том числе пакетной
CREATE TRIGGER IF NOT EXISTS trg_obs_insert AFTER INSERT ON observations
BEGIN
    INSERT INTO observations_history VALUES
        (NEW.observation_id, NEW.updated_at, NULL, 1,
         NEW.temperature_2m_max, NEW.temperature_2m_min, NEW.precipitation_sum, NEW.collected_at);
END;

CREATE TRIGGER IF NOT EXISTS trg_obs_update
AFTER UPDATE OF temperature_2m_max, temperature_2m_min, precipitation_sum ON observations
WHEN OLD.temperature_2m_max IS NOT NEW.temperature_2m_max
  OR OLD.temperature_2m_min IS NOT NEW.temperature_2m_min
  OR OLD.precipitation_sum  IS NOT NEW.precipitation_sum
BEGIN
    UPDATE observations_history SET valid_to = NEW.updated_at, is_current = 0
     WHERE observation_id = NEW.observation_id AND is_current = 1;
    INSERT INTO observations_history VALUES
        (NEW.observation_id, NEW.updated_at, NULL, 1,
         NEW.temperature_2m_max, NEW.temperature_2m_min, NEW.precipitation_sum, NEW.collected_at);
END;

-- Отбракованные записи: payload целиком + шаг, поле, причина
CREATE TABLE IF NOT EXISTS rejects (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    source       TEXT NOT NULL,
    step         TEXT NOT NULL,
    field        TEXT,
    reason       TEXT NOT NULL,
    synthetic    TEXT,                                      -- вид синтетической порчи, если есть
    payload      TEXT NOT NULL CHECK (json_valid(payload)),
    payload_hash TEXT NOT NULL,
    loaded_at    TEXT NOT NULL CHECK (loaded_at LIKE '%+00:00'),
    UNIQUE (payload_hash, step)                             -- повторная загрузка не плодит дубли
);
CREATE INDEX IF NOT EXISTS idx_rejects_reason ON rejects (source, step);

-- Журнал прогонов загрузки и качества
CREATE TABLE IF NOT EXISTS load_runs (
    run_at      TEXT PRIMARY KEY,
    label       TEXT NOT NULL,
    rows_in     INTEGER NOT NULL,
    inserted    INTEGER NOT NULL,
    updated     INTEGER NOT NULL,
    unchanged   INTEGER NOT NULL,
    rows_total  INTEGER NOT NULL,
    history_versions INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS quality_runs (
    run_at      TEXT PRIMARY KEY,
    raw_rows    INTEGER NOT NULL,
    clean_rows  INTEGER NOT NULL,
    verdict     TEXT NOT NULL CHECK (verdict IN ('green', 'yellow', 'red')),
    metrics     TEXT NOT NULL CHECK (json_valid(metrics))
);

-- Аналитические представления (mart)
CREATE VIEW IF NOT EXISTS v_city_stats AS
SELECT o.location_id, l.city, l.latitude,
       COUNT(*)                                   AS days,
       ROUND(AVG(o.temperature_2m_max), 1)        AS avg_tmax,
       ROUND(AVG(o.temperature_2m_min), 1)        AS avg_tmin,
       ROUND(SUM(o.precipitation_sum), 1)         AS precip_total,
       SUM(o.precipitation_sum > 0)               AS rainy_days
FROM observations o JOIN locations l USING (location_id)
GROUP BY o.location_id;

CREATE VIEW IF NOT EXISTS v_daily_dynamics AS
SELECT substr(observation_time_utc, 1, 10)        AS day,
       COUNT(*)                                   AS cities,
       ROUND(AVG(temperature_2m_max), 2)          AS avg_tmax,
       ROUND(AVG(temperature_2m_min), 2)          AS avg_tmin,
       ROUND(AVG(precipitation_sum), 2)           AS avg_precip
FROM observations GROUP BY day ORDER BY day;

CREATE VIEW IF NOT EXISTS v_top_warmest AS
SELECT * FROM v_city_stats ORDER BY avg_tmax DESC LIMIT 10;
