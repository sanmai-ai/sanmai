-- =====================================================================
-- 0004_hr.sql  —  HR & ACCESS domain source-of-truth (+ RBAC trust anchor)
-- =====================================================================
--
-- The HR/access domain's authoritative store: employees, the admin_users RBAC
-- trust anchor (+ its per-venue access grants), job roles, employee groups and
-- their membership, and manager -> employee assignments.
--
-- admin_users is FOUNDATIONAL: be/app/deps.py require_admin resolves the verified
-- Principal (uid / email claim) against this table to authorize every admin route.
-- Authz is DB-driven and IdentityProvider-agnostic — no reliance on token claims
-- like `admin` (a claim only carries identity, never authority).
--
-- DIALECT PORTABILITY (runs verbatim on sqlite in tests AND postgres in prod via
-- be/migrate.py — no dialect branching, mirrors 0002_menu.sql / 0003_inventory.sql):
--   * unqualified table names        (no CREATE SCHEMA / no `employees_stg.` prefix)
--   * INTEGER PRIMARY KEY / composite PKs; surrogate ids allocated app-side (crud)
--   * TEXT for JSON columns          (allowed_pages — parsed in Python)
--   * INTEGER 0/1 for booleans       (is_active, is_stable_schedule ; compared `= 1`)
--   * NUMERIC for money              (pay_rate — bound as text via a numeric CAST)
--   * TEXT for timestamps/dates      (ISO-8601 / "YYYY-MM-DD", written from Python)
--   * TEXT "HH:MM" for role times    (avoids the asyncpg TIME-binding gotcha)
--   * no ON CONFLICT / no ANY(...)   (upserts are select-then-insert/update in crud;
--                                      set membership uses an expanding IN bind)
--
-- VENUE SCOPING (per docs/architecture/database-architecture.md target):
--   * company-level (venue_id carried for uniformity, defaults 'default'):
--       employees, admin_users, job_roles, employee_groups
--   * the venue-scoping SEAM for admin access is admin_user_venues: an admin with
--     ZERO grant rows is unrestricted (single-venue-friendly default); with grants,
--     require_admin confines the caller to those venues. full_admin bypasses.
--
-- DEFERRED to later domains (noted, not built here): scheduling, timesheets,
-- payroll rules (payment_rules/installments/balances/bonuses), tasks/checklists,
-- forms/courses. pay_type/pay_rate live on the employee row; payroll RULES do not.
-- =====================================================================

-- --- employees (company-level) ---------------------------------------------
CREATE TABLE IF NOT EXISTS employees (
    id                  INTEGER PRIMARY KEY,            -- app-allocated surrogate (portable)
    venue_id            TEXT    NOT NULL DEFAULT 'default',
    name                TEXT    NOT NULL DEFAULT '',
    surname             TEXT    NOT NULL DEFAULT '',
    email               TEXT,                           -- staff-login identity (nullable)
    phone               TEXT,                           -- staff-login identity (nullable)
    is_stable_schedule  INTEGER NOT NULL DEFAULT 0,     -- 0/1
    pay_type            TEXT    NOT NULL DEFAULT 'hourly',   -- 'hourly' | 'monthly'
    pay_rate            NUMERIC,
    birth_date          TEXT,                           -- ISO "YYYY-MM-DD" or NULL
    default_role_id     INTEGER,                        -- soft ref -> job_roles.id
    created_at          TEXT    NOT NULL,
    updated_at          TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_employees_name
    ON employees (name, surname);

CREATE UNIQUE INDEX IF NOT EXISTS uq_employees_email
    ON employees (email) WHERE email IS NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS uq_employees_phone
    ON employees (phone) WHERE phone IS NOT NULL;

-- --- admin_users (company-level, RBAC trust anchor) ------------------------
-- Either uid (stable IdentityProvider subject) OR email may be the match key;
-- require_admin looks the verified Principal up by both.
CREATE TABLE IF NOT EXISTS admin_users (
    id                INTEGER PRIMARY KEY,              -- app-allocated surrogate (portable)
    venue_id          TEXT    NOT NULL DEFAULT 'default',
    uid               TEXT,                             -- IdentityProvider subject (nullable)
    email             TEXT,                             -- login email (nullable)
    role              TEXT    NOT NULL DEFAULT 'full_admin',  -- full_admin | accountant | manager
    allowed_pages     TEXT    NOT NULL DEFAULT '[]',    -- JSON array of page keys
    is_active         INTEGER NOT NULL DEFAULT 1,       -- 0/1
    telegram_user_id  INTEGER,                          -- optional approval-channel id
    created_at        TEXT    NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_admin_users_email
    ON admin_users (email) WHERE email IS NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS uq_admin_users_uid
    ON admin_users (uid) WHERE uid IS NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS uq_admin_users_telegram
    ON admin_users (telegram_user_id) WHERE telegram_user_id IS NOT NULL;

-- --- admin access venue grants (the venue-scoping seam) --------------------
CREATE TABLE IF NOT EXISTS admin_user_venues (
    admin_user_id  INTEGER NOT NULL,
    venue_id       TEXT    NOT NULL,
    PRIMARY KEY (admin_user_id, venue_id)
);

-- --- job roles (company-level) ---------------------------------------------
CREATE TABLE IF NOT EXISTS job_roles (
    id          INTEGER PRIMARY KEY,                    -- app-allocated surrogate (portable)
    venue_id    TEXT    NOT NULL DEFAULT 'default',
    name        TEXT    NOT NULL DEFAULT '',
    start_time  TEXT,                                   -- "HH:MM" or NULL
    end_time    TEXT,                                   -- "HH:MM" or NULL
    color       TEXT    NOT NULL DEFAULT '#60a5fa',
    is_active   INTEGER NOT NULL DEFAULT 1,             -- 0/1
    created_at  TEXT    NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_job_roles_name
    ON job_roles (venue_id, name);

-- --- employee groups (company-level) ---------------------------------------
CREATE TABLE IF NOT EXISTS employee_groups (
    id          INTEGER PRIMARY KEY,                    -- app-allocated surrogate (portable)
    venue_id    TEXT    NOT NULL DEFAULT 'default',
    name        TEXT    NOT NULL DEFAULT '',
    color       TEXT    NOT NULL DEFAULT '#60a5fa',
    is_active   INTEGER NOT NULL DEFAULT 1,             -- 0/1
    created_at  TEXT    NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_employee_groups_name
    ON employee_groups (venue_id, name);

-- --- employee <-> group membership -----------------------------------------
CREATE TABLE IF NOT EXISTS employee_group_members (
    employee_id  INTEGER NOT NULL,
    group_id     INTEGER NOT NULL,
    PRIMARY KEY (employee_id, group_id)
);

CREATE INDEX IF NOT EXISTS idx_egm_employee
    ON employee_group_members (employee_id);

CREATE INDEX IF NOT EXISTS idx_egm_group
    ON employee_group_members (group_id);

-- --- manager -> employee assignments ---------------------------------------
CREATE TABLE IF NOT EXISTS manager_employees (
    manager_id   INTEGER NOT NULL,                      -- -> admin_users.id
    employee_id  INTEGER NOT NULL,                      -- -> employees.id
    PRIMARY KEY (manager_id, employee_id)
);

CREATE INDEX IF NOT EXISTS idx_manager_employees_manager
    ON manager_employees (manager_id);
