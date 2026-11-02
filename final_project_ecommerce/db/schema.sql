-- =====================================================================
--  Схема хранилища (SQLite). Применяется идемпотентно при каждом запуске.
--  Сущности: products (товары двух источников), reviews (отзывы DummyJSON),
--  product_groups (справочник связи источников), product_history (история
--  цен/наличия, SCD2-подобная), runs (журнал прогонов).
-- =====================================================================
PRAGMA foreign_keys = ON;

-- ---------- Справочник товарных групп (связь HTML <-> API) ----------
CREATE TABLE IF NOT EXISTS product_groups (
    product_group  TEXT PRIMARY KEY,
    title_ru       TEXT NOT NULL
);

INSERT OR IGNORE INTO product_groups (product_group, title_ru) VALUES
    ('tops', 'Верх: футболки, худи, майки, рубашки'),
    ('bottoms', 'Низ: брюки, шорты, леггинсы'),
    ('outerwear', 'Верхняя одежда: куртки'),
    ('bags', 'Сумки и рюкзаки'),
    ('watches', 'Часы'),
    ('fitness', 'Спорт и фитнес-аксессуары'),
    ('shoes', 'Обувь'),
    ('dresses', 'Платья'),
    ('other', 'Прочее / нет пары в другом источнике');

-- ---------- Товары ----------
CREATE TABLE IF NOT EXISTS products (
    business_key       TEXT PRIMARY KEY,                       -- 'scrapingcourse:<slug>' | 'dummyjson:<id>'
    source             TEXT NOT NULL CHECK (source IN ('scrapingcourse.com', 'dummyjson.com')),
    source_id          TEXT NOT NULL,
    title              TEXT NOT NULL CHECK (length(title) >= 2),
    brand              TEXT,
    category_raw       TEXT,
    product_group      TEXT NOT NULL REFERENCES product_groups(product_group),
    price_usd          REAL NOT NULL CHECK (price_usd > 0 AND price_usd <= 100000),
    regular_price_usd  REAL CHECK (regular_price_usd IS NULL OR regular_price_usd >= price_usd - 0.005),
    discount_pct       REAL CHECK (discount_pct IS NULL OR discount_pct BETWEEN 0 AND 100),
    rating             REAL CHECK (rating IS NULL OR rating BETWEEN 0 AND 5),
    review_count       INTEGER NOT NULL DEFAULT 0 CHECK (review_count >= 0),
    stock_qty          INTEGER CHECK (stock_qty IS NULL OR stock_qty >= 0),
    in_stock           INTEGER NOT NULL CHECK (in_stock IN (0, 1)),
    sku                TEXT,
    url                TEXT,
    extra_json         TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(extra_json)),  -- аналог JSONB
    content_hash       TEXT NOT NULL,
    -- служебные поля
    collected_at       TEXT NOT NULL,
    first_seen_at      TEXT NOT NULL,
    updated_at         TEXT NOT NULL,
    parser_version     TEXT NOT NULL,
    last_run_id        TEXT NOT NULL,
    UNIQUE (source, source_id)
);
CREATE INDEX IF NOT EXISTS ix_products_source ON products(source);
CREATE INDEX IF NOT EXISTS ix_products_group  ON products(product_group, source);
CREATE INDEX IF NOT EXISTS ix_products_price  ON products(price_usd);

-- ---------- Отзывы (вторая сущность, связь N:1 с products) ----------
CREATE TABLE IF NOT EXISTS reviews (
    review_key      TEXT PRIMARY KEY,                          -- '<product_key>:<hash>'
    product_key     TEXT NOT NULL REFERENCES products(business_key) ON DELETE CASCADE,
    source          TEXT NOT NULL CHECK (source = 'dummyjson.com'),
    rating          INTEGER NOT NULL CHECK (rating BETWEEN 1 AND 5),
    comment         TEXT NOT NULL CHECK (length(comment) >= 1),
    review_date     TEXT NOT NULL,
    content_hash    TEXT NOT NULL,
    collected_at    TEXT NOT NULL,
    first_seen_at   TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    parser_version  TEXT NOT NULL,
    last_run_id     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_reviews_product ON reviews(product_key);
CREATE INDEX IF NOT EXISTS ix_reviews_rating  ON reviews(rating);

-- ---------- История изменений цены и наличия (SCD2-подобная) ----------
CREATE TABLE IF NOT EXISTS product_history (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    business_key  TEXT NOT NULL REFERENCES products(business_key) ON DELETE CASCADE,
    price_usd     REAL NOT NULL,
    discount_pct  REAL,
    in_stock      INTEGER NOT NULL,
    stock_qty     INTEGER,
    valid_from    TEXT NOT NULL,
    valid_to      TEXT,                                         -- NULL = актуальная версия
    run_id        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_history_key ON product_history(business_key, valid_to);

CREATE TRIGGER IF NOT EXISTS trg_products_insert_history
AFTER INSERT ON products
BEGIN
    INSERT INTO product_history (business_key, price_usd, discount_pct, in_stock, stock_qty, valid_from, run_id)
    VALUES (NEW.business_key, NEW.price_usd, NEW.discount_pct, NEW.in_stock, NEW.stock_qty, NEW.updated_at, NEW.last_run_id);
END;

CREATE TRIGGER IF NOT EXISTS trg_products_update_history
AFTER UPDATE OF price_usd, discount_pct, in_stock, stock_qty ON products
WHEN OLD.price_usd IS NOT NEW.price_usd
  OR OLD.discount_pct IS NOT NEW.discount_pct
  OR OLD.in_stock IS NOT NEW.in_stock
  OR OLD.stock_qty IS NOT NEW.stock_qty
BEGIN
    UPDATE product_history SET valid_to = NEW.updated_at
     WHERE business_key = NEW.business_key AND valid_to IS NULL;
    INSERT INTO product_history (business_key, price_usd, discount_pct, in_stock, stock_qty, valid_from, run_id)
    VALUES (NEW.business_key, NEW.price_usd, NEW.discount_pct, NEW.in_stock, NEW.stock_qty, NEW.updated_at, NEW.last_run_id);
END;

-- ---------- Журнал прогонов (сравнение с предыдущим прогоном) ----------
CREATE TABLE IF NOT EXISTS runs (
    run_id            TEXT PRIMARY KEY,
    started_at        TEXT NOT NULL,
    finished_at       TEXT,
    mode              TEXT NOT NULL,
    quality_status    TEXT,
    products_clean    INTEGER,
    reviews_clean     INTEGER,
    inserted          INTEGER,
    updated           INTEGER,
    unchanged         INTEGER,
    requests_total    INTEGER,
    error_rate        REAL,
    exit_code         INTEGER
);

-- =====================================================================
--  Слой аналитики (mart): представления. Материализуются в mart/*.csv и
--  mart/*.parquet только при статусе качества GREEN/YELLOW.
-- =====================================================================
DROP VIEW IF EXISTS v_group_comparison;
CREATE VIEW v_group_comparison AS
SELECT p.product_group,
       g.title_ru,
       p.source,
       COUNT(*)                                   AS n_products,
       ROUND(AVG(p.price_usd), 2)                 AS avg_price,
       ROUND(MIN(p.price_usd), 2)                 AS min_price,
       ROUND(MAX(p.price_usd), 2)                 AS max_price,
       ROUND(AVG(COALESCE(p.discount_pct, 0)), 2) AS avg_discount_pct,
       ROUND(100.0 * AVG(p.in_stock), 1)          AS in_stock_pct
FROM products p JOIN product_groups g USING (product_group)
GROUP BY p.product_group, g.title_ru, p.source;

DROP VIEW IF EXISTS v_overlap_groups;
CREATE VIEW v_overlap_groups AS
SELECT product_group,
       SUM(CASE WHEN source = 'scrapingcourse.com' THEN n_products END) AS n_html,
       SUM(CASE WHEN source = 'dummyjson.com'      THEN n_products END) AS n_api,
       MAX(CASE WHEN source = 'scrapingcourse.com' THEN avg_price END)  AS avg_price_html,
       MAX(CASE WHEN source = 'dummyjson.com'      THEN avg_price END)  AS avg_price_api,
       ROUND(MAX(CASE WHEN source = 'dummyjson.com' THEN avg_price END)
           / MAX(CASE WHEN source = 'scrapingcourse.com' THEN avg_price END), 2) AS price_ratio_api_to_html
FROM v_group_comparison
WHERE product_group <> 'other'
GROUP BY product_group
HAVING n_html > 0 AND n_api > 0;

DROP VIEW IF EXISTS v_price_rating;
CREATE VIEW v_price_rating AS
SELECT CASE
         WHEN price_usd < 10    THEN '1) < $10'
         WHEN price_usd < 50    THEN '2) $10–50'
         WHEN price_usd < 200   THEN '3) $50–200'
         WHEN price_usd < 1000  THEN '4) $200–1000'
         ELSE                        '5) ≥ $1000'
       END                                AS price_band,
       COUNT(*)                           AS n_products,
       ROUND(AVG(rating), 3)              AS avg_rating,
       ROUND(AVG(discount_pct), 2)        AS avg_discount_pct,
       ROUND(100.0 * AVG(in_stock), 1)    AS in_stock_pct
FROM products
WHERE source = 'dummyjson.com'
GROUP BY price_band;

DROP VIEW IF EXISTS v_review_consistency;
CREATE VIEW v_review_consistency AS
SELECT p.business_key,
       p.title,
       p.category_raw,
       p.product_group,
       p.price_usd,
       p.discount_pct,
       p.rating                     AS product_rating,
       COUNT(r.review_key)          AS n_reviews,
       ROUND(AVG(r.rating), 3)      AS avg_review_rating,
       ROUND(p.rating - AVG(r.rating), 3) AS rating_gap
FROM products p
LEFT JOIN reviews r ON r.product_key = p.business_key
WHERE p.source = 'dummyjson.com'
GROUP BY p.business_key;

DROP VIEW IF EXISTS v_source_overview;
CREATE VIEW v_source_overview AS
SELECT source,
       COUNT(*)                          AS n_products,
       COUNT(DISTINCT product_group)     AS n_groups,
       ROUND(AVG(price_usd), 2)          AS avg_price,
       ROUND(100.0 * AVG(in_stock), 1)   AS in_stock_pct,
       ROUND(100.0 * AVG(CASE WHEN discount_pct > 0 THEN 1 ELSE 0 END), 1) AS discounted_pct,
       MAX(updated_at)                   AS last_updated_at
FROM products
GROUP BY source;
