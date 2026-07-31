-- =====================================================================
-- 0003_inventory.sql  —  Inventory / Suppliers / Purchasing source-of-truth
-- =====================================================================
--
-- The inventory domain's authoritative store: item DEFINITIONS, per-venue STOCK
-- levels, suppliers + supplier_items, purchase orders + items, supplier monthly
-- payments, and the order-import learned-alias memory. The document-store read
-- copy (purchase-order / stock-transfer mirrors) is DEFERRED behind a Null
-- projection seam, so nothing here touches a projection.
--
-- DIALECT PORTABILITY (this file runs verbatim on sqlite in tests AND postgres in
-- prod via be/migrate.py — no dialect branching, mirrors 0002_menu.sql):
--   * unqualified table names        (no CREATE SCHEMA / no `inventory_stg.` prefix)
--   * INTEGER PRIMARY KEY / composite PKs; surrogate ids allocated app-side (crud)
--   * TEXT for JSON list columns     (allowed_locations, categories — parsed in Python)
--   * INTEGER 0/1 for booleans       (is_active, tax_inclusive, … ; compared `= 1`)
--   * NUMERIC for money/quantity     (bound as text, wrapped in a numeric CAST)
--   * TEXT for timestamps/dates      (ISO-8601, written from Python; no now()/CAST ::)
--   * TEXT "HH:MM" for order_cutoff_time  (avoids the asyncpg TIME-binding gotcha)
--   * no ON CONFLICT / no ANY(...)   (upserts are select-then-insert/update in crud;
--                                      set membership uses an expanding IN bind)
--
-- VENUE SCOPING (per database-architecture.md target):
--   * company-level (venue_id carried for uniformity, defaults 'default'):
--       inventory_items, suppliers, supplier_items, order_import_aliases
--   * venue-level (partitioned by venue_id):
--       stock, purchase_orders, purchase_items, supplier_monthly_payments
--
-- CROSS-DOMAIN LINK: menu_item_recipes.inventory_sku (0002) and
-- order_import_aliases.inventory_sku are SOFT string references to
-- inventory_items.sku — no hard cross-table FK (matches the live soft-reference
-- reality and keeps each domain independently migratable).
-- =====================================================================

-- --- item definitions (company-level) --------------------------------------
CREATE TABLE IF NOT EXISTS inventory_items (
    sku                TEXT    PRIMARY KEY,          -- stable slug/id (client- or slug-allocated)
    venue_id           TEXT    NOT NULL DEFAULT 'default',
    name               TEXT    NOT NULL DEFAULT '',
    category           TEXT    NOT NULL DEFAULT '',
    unit               TEXT    NOT NULL DEFAULT 'units',
    allowed_locations  TEXT    NOT NULL DEFAULT '[]',  -- JSON array of location codes
    reorder_point      NUMERIC,
    max_threshold      NUMERIC,
    comment            TEXT,
    is_active          INTEGER NOT NULL DEFAULT 1,     -- 0/1
    created_at         TEXT    NOT NULL,
    updated_at         TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_inventory_items_active
    ON inventory_items (is_active, name);

-- --- per-venue stock levels (venue-level) ----------------------------------
CREATE TABLE IF NOT EXISTS stock (
    inventory_sku   TEXT    NOT NULL,
    location        TEXT    NOT NULL,                 -- storage|kitchen|walk_in|foh
    venue_id        TEXT    NOT NULL DEFAULT 'default',
    quantity        NUMERIC NOT NULL DEFAULT 0,
    unit            TEXT    NOT NULL DEFAULT 'units',
    last_counted_at TEXT,
    updated_at      TEXT    NOT NULL,
    PRIMARY KEY (inventory_sku, location, venue_id)
);

-- --- suppliers (company-level) ---------------------------------------------
CREATE TABLE IF NOT EXISTS suppliers (
    supplier_id                 TEXT    PRIMARY KEY,
    venue_id                    TEXT    NOT NULL DEFAULT 'default',
    name                        TEXT    NOT NULL DEFAULT '',
    contact_person              TEXT,
    payment_type                TEXT,
    site                        TEXT,
    chat_link                   TEXT,
    comment                     TEXT,
    categories                  TEXT    NOT NULL DEFAULT '[]',  -- JSON array
    approx_delivery_time_info   TEXT,
    is_active                   INTEGER NOT NULL DEFAULT 1,
    min_order_amount            NUMERIC,
    payment_day                 INTEGER,                        -- opt-in monthly billing day
    order_cutoff_time           TEXT,                           -- "HH:MM" or NULL
    delivery_lead_days          INTEGER,
    delivery_working_days_only  INTEGER NOT NULL DEFAULT 1,
    created_at                  TEXT    NOT NULL,
    updated_at                  TEXT    NOT NULL
);

-- --- supplier-specific item facts (company-level) --------------------------
CREATE TABLE IF NOT EXISTS supplier_items (
    supplier_id         TEXT    NOT NULL,
    inventory_sku       TEXT    NOT NULL,
    venue_id            TEXT    NOT NULL DEFAULT 'default',
    supplier_sku        TEXT,
    pack_name           TEXT,
    pack_size           NUMERIC,
    price_per_pack      NUMERIC,
    tax_inclusive           INTEGER NOT NULL DEFAULT 0,             -- tax-inclusive flag (generic)
    is_primary_supplier INTEGER NOT NULL DEFAULT 0,
    is_on_stop          INTEGER NOT NULL DEFAULT 0,
    link                TEXT,
    comment             TEXT,
    created_at          TEXT    NOT NULL,
    updated_at          TEXT    NOT NULL,
    PRIMARY KEY (supplier_id, inventory_sku)
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_supplier_items_supplier_sku
    ON supplier_items (supplier_id, supplier_sku);

-- --- purchase orders (venue-level) -----------------------------------------
CREATE TABLE IF NOT EXISTS purchase_orders (
    order_id               TEXT    PRIMARY KEY,
    venue_id               TEXT    NOT NULL DEFAULT 'default',
    date                   TEXT,
    supplier_id            TEXT,
    supplier_name          TEXT,
    status                 TEXT    NOT NULL DEFAULT 'placed',   -- placed|received
    expected_delivery_date TEXT,
    expected_total_amount  NUMERIC,
    invoice_id             TEXT,
    notes                  TEXT,
    created_at             TEXT    NOT NULL,
    placed_at              TEXT,
    received_at            TEXT
);

CREATE INDEX IF NOT EXISTS idx_purchase_orders_status
    ON purchase_orders (venue_id, status);

-- --- purchase order line items (venue-level) -------------------------------
CREATE TABLE IF NOT EXISTS purchase_items (
    id                INTEGER PRIMARY KEY,            -- app-allocated surrogate (portable)
    venue_id          TEXT    NOT NULL DEFAULT 'default',
    order_id          TEXT    NOT NULL,
    inventory_sku     TEXT    NOT NULL,
    date              TEXT,
    packs             INTEGER NOT NULL DEFAULT 0,
    pack_size         NUMERIC,
    pack_price        NUMERIC,
    supplier_id       TEXT,
    supplier_sku      TEXT,
    is_received       INTEGER NOT NULL DEFAULT 0,
    expected_quantity NUMERIC,                        -- derived packs*pack_size (computed in crud)
    created_at        TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_purchase_items_order
    ON purchase_items (order_id);

CREATE INDEX IF NOT EXISTS idx_purchase_items_sku
    ON purchase_items (inventory_sku);

-- --- supplier monthly payments (venue-level, financial) --------------------
CREATE TABLE IF NOT EXISTS supplier_monthly_payments (
    id           INTEGER PRIMARY KEY,                 -- app-allocated surrogate (portable)
    venue_id     TEXT    NOT NULL DEFAULT 'default',
    supplier_id  TEXT    NOT NULL,
    period_year  INTEGER NOT NULL,
    period_month INTEGER NOT NULL,
    status       TEXT    NOT NULL DEFAULT 'pending',  -- pending|paid
    paid_at      TEXT,
    paid_amount  NUMERIC,
    paid_method  TEXT,
    proof_url    TEXT,
    notes        TEXT,
    created_at   TEXT    NOT NULL,
    updated_at   TEXT    NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_supplier_monthly_payments_period
    ON supplier_monthly_payments (venue_id, supplier_id, period_year, period_month);

-- --- order-import learned aliases (company-level) --------------------------
-- Soft link: inventory_sku references inventory_items.sku (validated in crud, no FK).
CREATE TABLE IF NOT EXISTS order_import_aliases (
    supplier_id   TEXT NOT NULL,
    raw_name_norm TEXT NOT NULL,
    venue_id      TEXT NOT NULL DEFAULT 'default',
    raw_name      TEXT NOT NULL DEFAULT '',
    inventory_sku TEXT NOT NULL,
    created_by    TEXT,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    PRIMARY KEY (supplier_id, raw_name_norm)
);
