-- =====================================================================
-- 0009_analytics.sql  —  Analytics (sales pipeline) source-of-truth
-- =====================================================================
--
-- The analytics domain's authoritative store: ingested POS/export sales
-- (raw + processed), the dish-reference catalog used to enrich each line
-- (clean name + a generic component breakdown), the menu-items mirror,
-- AI new-dish suggestions, an ingestion audit log, and a processing-run
-- log (ingest | reprocess | menu_sync | chat) that also records the
-- text-to-SQL chat queries for auditability.
--
-- DIALECT PORTABILITY (this file runs verbatim on sqlite in tests AND
-- postgres in prod via be/migrate.py — no dialect branching, mirrors
-- 0003_inventory.sql):
--   * unqualified table names          (NO CREATE SCHEMA / no `analytics.` prefix — one DB)
--   * INTEGER PRIMARY KEY; surrogate ids allocated app-side (crud _next_id)
--   * TEXT for JSON columns            (components, payload, params, proposed_components — parsed in Python)
--   * INTEGER 0/1 for booleans         (menu_items.active)
--   * NUMERIC for money/quantity       (bound as text, wrapped in a numeric CAST)
--   * TEXT for timestamps/dates        (ISO-8601, written from Python; no now()/DEFAULT now()/CAST ::)
--   * pre-computed order_dow / order_hour_int  (0=Sun..6=Sat / 0-23), so the
--       heatmap needs no EXTRACT/AT TIME ZONE — the timezone is applied ONCE at
--       ingest (crud), keeping every read query portable
--   * row_hash UNIQUE stays the idempotency key (re-upload => skip)
--   * no ON CONFLICT / no ANY(...)     (upserts are select-then-insert/update in crud;
--                                        set membership uses an expanding IN bind)
--
-- VENUE SCOPING (per database-architecture.md target):
--   * venue-level (partitioned by venue_id): analytics_sales, analytics_sales_raw,
--       analytics_ingestions, analytics_processing_runs, analytics_menu_suggestions
--   * company-level catalog (venue_id carried for uniformity): analytics_menu,
--       analytics_menu_items
--
-- GENERIC (no cuisine/brand/locale specifics): the per-line breakdown is a generic
-- "components" JSON array of {"name": <str>, "count": <int>}; there is no seeded
-- reference data and no hardcoded "system" noise dish-type (it is a caller param).
--
-- CROSS-DOMAIN LINK: analytics_sales.raw_id is a SOFT reference to
-- analytics_sales_raw.id (no hard FK — matches the live soft-reference reality).
-- =====================================================================

-- --- raw ingested rows (venue-level) ---------------------------------------
-- The verbatim source line (generic column subset). Reprocess rebuilds the
-- processed table from these without a re-upload.
CREATE TABLE IF NOT EXISTS analytics_sales_raw (
    id              INTEGER PRIMARY KEY,            -- app-allocated surrogate (portable)
    venue_id        TEXT    NOT NULL DEFAULT 'default',
    order_id        INTEGER,
    order_date      TEXT,                            -- ISO 'YYYY-MM-DD'
    order_ts        TEXT,                            -- ISO-8601 datetime (may be null)
    order_dow       INTEGER,                         -- 0=Sun..6=Sat (computed at ingest)
    order_hour_int  INTEGER,                         -- 0-23 (computed at ingest)
    dish_type       TEXT,                            -- category
    dish_name       TEXT,                            -- POS line name
    menu_price      NUMERIC,                         -- catalog/list unit price
    discount        NUMERIC,
    sale_price      NUMERIC,                         -- amount actually charged for the line
    qty             NUMERIC,
    waiter_name     TEXT,
    diners          INTEGER,
    cost            NUMERIC,
    cost_sum        NUMERIC,
    components      TEXT    NOT NULL DEFAULT '[]',    -- JSON [{"name","count"}], if the source carried one
    source          TEXT    NOT NULL DEFAULT 'manual',-- 'email' | 'manual' | 'backfill'
    row_hash        TEXT,
    ingestion_id    INTEGER,
    ingested_at     TEXT    NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_analytics_sales_raw_hash
    ON analytics_sales_raw (row_hash);
CREATE INDEX IF NOT EXISTS idx_analytics_sales_raw_ingestion
    ON analytics_sales_raw (ingestion_id);

-- --- processed sales rows (venue-level) ------------------------------------
CREATE TABLE IF NOT EXISTS analytics_sales (
    id              INTEGER PRIMARY KEY,            -- app-allocated surrogate (portable)
    venue_id        TEXT    NOT NULL DEFAULT 'default',
    order_id        INTEGER,
    order_date      TEXT,                            -- ISO 'YYYY-MM-DD'
    order_ts        TEXT,                            -- ISO-8601 datetime (may be null)
    order_dow       INTEGER,                         -- 0=Sun..6=Sat (portable heatmap key)
    order_hour_int  INTEGER,                         -- 0-23
    dish_type       TEXT,
    dish_name       TEXT,
    menu_price      NUMERIC,
    discount        NUMERIC,
    sale_price      NUMERIC,
    qty             NUMERIC,
    waiter_name     TEXT,
    diners          INTEGER,
    cost            NUMERIC,
    cost_sum        NUMERIC,
    components      TEXT    NOT NULL DEFAULT '[]',    -- JSON [{"name","count"}] from the catalog join
    raw_id          INTEGER,                          -- soft ref to analytics_sales_raw.id (null = backfill)
    source          TEXT    NOT NULL DEFAULT 'manual',
    row_hash        TEXT,
    ingested_at     TEXT    NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_analytics_sales_hash
    ON analytics_sales (row_hash);
CREATE INDEX IF NOT EXISTS idx_analytics_sales_date
    ON analytics_sales (venue_id, order_date);
CREATE INDEX IF NOT EXISTS idx_analytics_sales_dish_type
    ON analytics_sales (venue_id, dish_type);
CREATE INDEX IF NOT EXISTS idx_analytics_sales_dish_name
    ON analytics_sales (venue_id, dish_name);

-- --- dish reference catalog (company-level) --------------------------------
-- Maps a POS line name + list price to a clean display name and a generic
-- component breakdown, used to enrich processed rows at ingest/reprocess.
CREATE TABLE IF NOT EXISTS analytics_menu (
    venue_id     TEXT    NOT NULL DEFAULT 'default',
    pos_name     TEXT    NOT NULL,                   -- source line name
    price        NUMERIC NOT NULL,
    clean_name   TEXT    NOT NULL DEFAULT '',        -- clean display label
    components   TEXT    NOT NULL DEFAULT '[]',      -- JSON [{"name","count"}]
    created_at   TEXT    NOT NULL,
    updated_at   TEXT    NOT NULL,
    PRIMARY KEY (venue_id, pos_name, price)
);

-- --- menu-items mirror (company-level) -------------------------------------
-- A read mirror of the FE menu catalog (source of clean names + categories).
CREATE TABLE IF NOT EXISTS analytics_menu_items (
    id          TEXT    PRIMARY KEY,                 -- external document id
    venue_id    TEXT    NOT NULL DEFAULT 'default',
    name        TEXT,
    category    TEXT,
    price       NUMERIC,
    active      INTEGER NOT NULL DEFAULT 1,          -- 0/1
    payload     TEXT    NOT NULL DEFAULT '{}',       -- JSON full doc
    synced_at   TEXT    NOT NULL
);

-- --- AI new-dish suggestions (venue-level) ---------------------------------
CREATE TABLE IF NOT EXISTS analytics_menu_suggestions (
    id                  INTEGER PRIMARY KEY,          -- app-allocated surrogate
    venue_id            TEXT    NOT NULL DEFAULT 'default',
    dish_name           TEXT    NOT NULL,
    dish_type           TEXT,
    price               NUMERIC,
    sample_count        INTEGER NOT NULL DEFAULT 0,
    first_seen          TEXT,
    last_seen           TEXT,
    proposed_name       TEXT,
    proposed_category   TEXT,
    proposed_components  TEXT   NOT NULL DEFAULT '[]',-- JSON [{"name","count"}]
    confidence          TEXT,
    note                TEXT,
    status              TEXT    NOT NULL DEFAULT 'open',-- open|approved|dismissed
    resolved_by         TEXT,
    resolved_at         TEXT,
    created_at          TEXT    NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_analytics_menu_suggestions_dish
    ON analytics_menu_suggestions (venue_id, dish_name, price);
CREATE INDEX IF NOT EXISTS idx_analytics_menu_suggestions_status
    ON analytics_menu_suggestions (venue_id, status);

-- --- ingestion audit log (venue-level) -------------------------------------
CREATE TABLE IF NOT EXISTS analytics_ingestions (
    id             INTEGER PRIMARY KEY,              -- app-allocated surrogate
    venue_id       TEXT    NOT NULL DEFAULT 'default',
    source         TEXT    NOT NULL,
    filename       TEXT,
    rows_inserted  INTEGER NOT NULL DEFAULT 0,
    rows_skipped   INTEGER NOT NULL DEFAULT 0,
    date_min       TEXT,
    date_max       TEXT,
    status         TEXT    NOT NULL DEFAULT 'success',-- 'success' | 'error'
    error          TEXT,
    file_path      TEXT,                              -- Storage key of the archived upload
    created_at     TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_analytics_ingestions_created
    ON analytics_ingestions (venue_id, created_at);

-- --- processing-run log (venue-level) --------------------------------------
-- One row per processing action (ingest | reprocess | menu_sync | chat). Stores
-- the SQL used so the text-to-SQL chat is auditable and rejected queries are visible.
CREATE TABLE IF NOT EXISTS analytics_processing_runs (
    id            INTEGER PRIMARY KEY,               -- app-allocated surrogate
    venue_id      TEXT    NOT NULL DEFAULT 'default',
    kind          TEXT    NOT NULL,                  -- ingest|reprocess|menu_sync|chat
    params        TEXT    NOT NULL DEFAULT '{}',     -- JSON
    sql_text      TEXT,
    rows_affected INTEGER,
    status        TEXT    NOT NULL DEFAULT 'success',-- success|error|rejected|ok
    error         TEXT,
    actor         TEXT,                              -- admin uid/email (verified)
    created_at    TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_analytics_processing_runs_created
    ON analytics_processing_runs (venue_id, created_at);
