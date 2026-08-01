-- =====================================================================
-- 0011_payroll.sql  —  PAYROLL / COMPENSATION domain source-of-truth
-- =====================================================================
--
-- Employee-pay money (NOT the customer POS payment path). Builds on
-- 0004_hr.sql (employees + employees.pay_type / employees.pay_rate, which
-- already exist there and are NOT re-added here) and 0005_scheduling.sql
-- (shifts = worked hours + monthly_working_hours = the monthly-salary
-- divisor, both OWNED there and only READ by this domain) and 0006_tasks.sql
-- (checklist_run_tasks + task_instances = the bonus origins). All cross-table
-- references are SOFT (plain INTEGER columns, no cross-migration FKs) so the
-- runner stays dialect-portable and each migration is independently additive.
--
-- Covers:
--   * pay rules    — payment_rules: per-employee/day overtime bonus rules;
--   * one-time pay — employee_bonuses: ad-hoc monthly bonuses;
--   * calendar     — scheduled_payments (+ payment_occurrence_overrides,
--                    payment_installments, payment_balances): the plan-side
--                    payment calendar (recurring/one-off, split, partial);
--   * bonus ledger — bonus_task_log: the crediting sink for the tasks-domain
--                    bonus hook (one LIVE payout row per finisher per run task).
--
-- DIALECT PORTABILITY (runs verbatim on sqlite in tests AND postgres in prod
-- via be/migrate.py — mirrors 0002..0010):
--   * unqualified table names        (no CREATE SCHEMA / no `employees_stg.` prefix)
--   * INTEGER PRIMARY KEY; surrogate ids allocated app-side (crud _next_id)
--   * INTEGER 0/1 for booleans       (active, is_recurring, is_approximate,
--                                      link_to_timesheets, paid, voided)
--   * TEXT for JSON columns          (recurrence — parsed/dumped in crud)
--   * TEXT "YYYY-MM-DD" for dates    (start_date, end_date, due_date, ...)
--   * TEXT for timestamps            (ISO-8601, written from Python — no now())
--   * NUMERIC for money              (amount, pay_rate — bound as text via CAST)
--   * no ON CONFLICT / no ANY(...)   (upserts are select-then-insert; the
--                                      double-pay guard is a select-then-insert
--                                      over a partial unique index)
--
-- VENUE SCOPING (per docs/architecture/database-architecture.md target):
--   * compensation ties to the employee and is COMPANY-level; every table
--     carries venue_id (defaults 'default') for uniformity + a future
--     venue-tag, but is not filtered by it yet (mirrors 0004_hr.sql).
--
-- DEFERRED (noted, NOT built here):
--   * Telegram approval on scheduled payments  -> Notifier seam;
--   * accountant export / email of payroll      -> Notifier + Storage seams;
--   * the nightly auto-close / scheduler job     -> a cron/worker concern;
--   * exact IL rounding edge cases beyond the PayrollProfile.
-- =====================================================================

-- ── payment_rules — per-employee/day overtime bonus rules ────────────
CREATE TABLE IF NOT EXISTS payment_rules (
    id            INTEGER PRIMARY KEY,
    venue_id      TEXT NOT NULL DEFAULT 'default',
    employee_id   INTEGER,                                   -- soft ref -> employees.id; NULL = all
    day_of_week   INTEGER CHECK (day_of_week IS NULL OR (day_of_week >= 0 AND day_of_week <= 6)),
    after_minutes INTEGER NOT NULL DEFAULT 0 CHECK (after_minutes >= 0),
    bonus_percent INTEGER NOT NULL DEFAULT 0 CHECK (bonus_percent >= 0),
    description   TEXT,
    active        INTEGER NOT NULL DEFAULT 1,
    created_at    TEXT
);

CREATE INDEX IF NOT EXISTS idx_payment_rules_employee ON payment_rules (employee_id);

-- ── employee_bonuses — ad-hoc one-time monthly bonuses ───────────────
CREATE TABLE IF NOT EXISTS employee_bonuses (
    id          INTEGER PRIMARY KEY,
    venue_id    TEXT NOT NULL DEFAULT 'default',
    employee_id INTEGER NOT NULL,                            -- soft ref -> employees.id
    year        INTEGER NOT NULL,
    month       INTEGER NOT NULL CHECK (month >= 1 AND month <= 12),
    amount      NUMERIC NOT NULL,
    comment     TEXT,
    created_at  TEXT
);

CREATE INDEX IF NOT EXISTS idx_employee_bonuses_emp_ym
    ON employee_bonuses (employee_id, year, month);

-- ── scheduled_payments — payment-calendar definitions ────────────────
CREATE TABLE IF NOT EXISTS scheduled_payments (
    id                 INTEGER PRIMARY KEY,
    venue_id           TEXT NOT NULL DEFAULT 'default',
    name               TEXT NOT NULL,
    category           TEXT NOT NULL DEFAULT 'other' CHECK (category IN (
                           'tax', 'rent', 'salary', 'utility',
                           'loan', 'insurance', 'subscription', 'other')),
    payment_type       TEXT NOT NULL DEFAULT 'planned' CHECK (payment_type IN (
                           'mandatory', 'planned', 'forecast')),
    amount             NUMERIC NOT NULL DEFAULT 0 CHECK (amount >= 0),
    currency           TEXT NOT NULL DEFAULT 'ILS',
    is_recurring       INTEGER NOT NULL DEFAULT 0,
    recurrence         TEXT,                                 -- JSON-as-TEXT; parsed in crud
    start_date         TEXT NOT NULL,
    end_date           TEXT,
    status             TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'archived')),
    is_approximate     INTEGER NOT NULL DEFAULT 0,
    link_to_timesheets INTEGER NOT NULL DEFAULT 0,
    notes              TEXT,
    created_by         TEXT,
    created_at         TEXT,
    updated_at         TEXT
);

CREATE INDEX IF NOT EXISTS idx_scheduled_payments_status_start
    ON scheduled_payments (status, start_date);

-- ── payment_occurrence_overrides — per-(def,due_date) override ───────
CREATE TABLE IF NOT EXISTS payment_occurrence_overrides (
    id                   INTEGER PRIMARY KEY,
    venue_id             TEXT NOT NULL DEFAULT 'default',
    scheduled_payment_id INTEGER NOT NULL,                   -- soft ref -> scheduled_payments.id
    due_date             TEXT NOT NULL,
    status               TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'paid', 'skipped')),
    amount_override      NUMERIC,
    paid_at              TEXT,
    paid_amount          NUMERIC,
    paid_method          TEXT,
    notes                TEXT,
    updated_by           TEXT,
    updated_at           TEXT,
    UNIQUE (scheduled_payment_id, due_date)
);

CREATE INDEX IF NOT EXISTS idx_pay_overrides_due
    ON payment_occurrence_overrides (due_date);

-- ── payment_installments — split one occurrence into 2-4 dated parts ─
CREATE TABLE IF NOT EXISTS payment_installments (
    id                   INTEGER PRIMARY KEY,
    venue_id             TEXT NOT NULL DEFAULT 'default',
    scheduled_payment_id INTEGER NOT NULL,                   -- soft ref -> scheduled_payments.id
    source_due_date      TEXT NOT NULL,
    seq                  INTEGER NOT NULL,
    total_count          INTEGER NOT NULL,
    due_date             TEXT NOT NULL,
    amount               NUMERIC NOT NULL DEFAULT 0 CHECK (amount >= 0),
    status               TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'paid')),
    paid_at              TEXT,
    paid_amount          NUMERIC,
    paid_method          TEXT,
    proof_url            TEXT,
    notes                TEXT,
    created_by           TEXT,
    created_at           TEXT,
    updated_at           TEXT,
    UNIQUE (scheduled_payment_id, source_due_date, seq)
);

CREATE INDEX IF NOT EXISTS idx_payment_installments_due_date
    ON payment_installments (due_date);

-- ── payment_balances — partial-payment remainder chain ──────────────
CREATE TABLE IF NOT EXISTS payment_balances (
    id           INTEGER PRIMARY KEY,
    venue_id     TEXT NOT NULL DEFAULT 'default',
    origin_kind  TEXT NOT NULL,                              -- 'scheduled'|'installment'|'balance'|...
    origin_ref   TEXT,
    name         TEXT NOT NULL,
    category     TEXT,
    payment_type TEXT NOT NULL DEFAULT 'planned',
    amount       NUMERIC NOT NULL DEFAULT 0 CHECK (amount >= 0),
    currency     TEXT NOT NULL DEFAULT 'ILS',
    due_date     TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'paid')),
    paid_at      TEXT,
    paid_amount  NUMERIC,
    paid_method  TEXT,
    proof_url    TEXT,
    notes        TEXT,
    created_by   TEXT,
    created_at   TEXT,
    updated_at   TEXT
);

CREATE INDEX IF NOT EXISTS idx_payment_balances_due_date
    ON payment_balances (due_date);

-- ── bonus_task_log — task-bonus crediting ledger (tasks hook sink) ───
-- One LIVE row per finisher per run task; voided rows survive for audit and
-- re-completion after a reopen inserts fresh live rows. The partial unique
-- index makes double-pay structurally impossible without destroying history.
CREATE TABLE IF NOT EXISTS bonus_task_log (
    id               INTEGER PRIMARY KEY,
    venue_id         TEXT NOT NULL DEFAULT 'default',
    run_task_id      INTEGER,                                -- soft ref -> checklist_run_tasks.id
    task_instance_id INTEGER,                                -- soft ref -> task_instances.id (v1 legacy)
    employee_id      INTEGER NOT NULL,                       -- soft ref -> employees.id
    task_title       TEXT,
    bonus_amount     NUMERIC NOT NULL,
    bonus_currency   TEXT NOT NULL DEFAULT 'ILS',
    completed_at     TEXT,
    paid             INTEGER NOT NULL DEFAULT 0,
    paid_at          TEXT,
    voided           INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_btl_employee ON bonus_task_log (employee_id);
CREATE INDEX IF NOT EXISTS idx_btl_paid ON bonus_task_log (paid);

-- Exactly one LIVE payout per (run_task_id, employee_id); voided rows excluded.
CREATE UNIQUE INDEX IF NOT EXISTS uq_bonus_log_run_task_emp
    ON bonus_task_log (run_task_id, employee_id) WHERE voided = 0;
