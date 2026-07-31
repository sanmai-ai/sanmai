-- =====================================================================
-- 0002_menu.sql  —  Menu domain source-of-truth (Postgres SoT + admin API)
-- =====================================================================
--
-- The menu domain's authoritative store: append-only full-item version history
-- (menu_item_versions) and menu-item -> inventory recipe lines (menu_item_recipes).
-- The Firestore/document-store read copy is DEFERRED (needs a doc-store adapter),
-- so nothing here touches a projection.
--
-- DIALECT PORTABILITY (this file runs verbatim on sqlite in tests AND postgres in
-- prod via be/migrate.py — no dialect branching):
--   * unqualified table names          (no CREATE SCHEMA / no `rest.` prefix)
--   * INTEGER PRIMARY KEY              (surrogate ids allocated app-side; see crud)
--   * TEXT for the JSON snapshot       (parsed in Python; no JSONB / no `::` casts)
--   * INTEGER 0/1 for booleans         (is_current; compared `= 1`, portable)
--   * NUMERIC for quantity             (bound as text, wrapped in a numeric CAST)
--   * created_at as TEXT (ISO-8601)    (written from Python; no now()/CURRENT_TIMESTAMP)
--
-- VENUE SCOPING: both tables carry venue_id (default 'default'). Versions stay
-- company-level today; venue_id makes a future venue publish/override seam additive.
-- =====================================================================

-- --- versioned full-item content history -----------------------------------
CREATE TABLE IF NOT EXISTS menu_item_versions (
    id                INTEGER PRIMARY KEY,        -- app-allocated surrogate (portable)
    venue_id          TEXT    NOT NULL DEFAULT 'default',
    item_id           TEXT    NOT NULL,           -- external menu item id (doc key)
    snapshot          TEXT    NOT NULL DEFAULT '{}',  -- JSON content snapshot (text)
    changed_by        TEXT,                       -- Principal.uid of the editing admin
    changed_by_name   TEXT,
    source_version_id INTEGER,                    -- set when this row is a restore
    is_current        INTEGER NOT NULL DEFAULT 1, -- 0/1; one current row per (item, venue)
    created_at        TEXT    NOT NULL            -- ISO-8601, written from Python
);

CREATE INDEX IF NOT EXISTS idx_menu_item_versions_item
    ON menu_item_versions (item_id, venue_id, id);

-- Single-current invariant: at most one is_current=1 row per (item_id, venue_id).
CREATE UNIQUE INDEX IF NOT EXISTS uq_menu_item_versions_current
    ON menu_item_versions (item_id, venue_id)
    WHERE is_current = 1;

-- --- menu item -> inventory recipe lines -----------------------------------
CREATE TABLE IF NOT EXISTS menu_item_recipes (
    id            INTEGER PRIMARY KEY,            -- app-allocated surrogate (portable)
    venue_id      TEXT    NOT NULL DEFAULT 'default',
    menu_item_id  TEXT    NOT NULL,               -- external menu item id
    option_index  INTEGER NOT NULL DEFAULT -1,    -- -1 = item-level / single-price
    inventory_sku TEXT    NOT NULL,               -- inventory sku soft-link
    location      TEXT    NOT NULL,               -- storage|kitchen|walk_in|foh
    quantity      NUMERIC NOT NULL,               -- deduction per 1 unit sold
    updated_by    TEXT,
    created_at    TEXT    NOT NULL,
    updated_at    TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_menu_item_recipes_lookup
    ON menu_item_recipes (menu_item_id, venue_id, option_index);

CREATE UNIQUE INDEX IF NOT EXISTS uq_menu_item_recipes_line
    ON menu_item_recipes (menu_item_id, venue_id, option_index, inventory_sku, location);
