-- =====================================================================
-- 0005_scheduling.sql  —  SCHEDULING + TIMESHEET + TIME-OFF domain
-- =====================================================================
--
-- The scheduling domain's authoritative store. Builds on 0004_hr.sql
-- (employees, admin_users, job_roles) via SOFT references (plain INTEGER
-- columns, no cross-table FKs) so the runner stays dialect-portable and each
-- migration is independently additive.
--
-- Covers, in one domain package:
--   * scheduling  — weekly schedule containers, per-day scheduled shifts,
--                   recurring ("stable") shift templates, shift-change requests,
--                   employee weekly availability;
--   * timesheet   — clock/worked-shift records, per-month confirmations,
--                   timesheet corrections (reuse shift_change_requests);
--   * time-off    — vacation/sick requests;
--   * config      — monthly working-hours (payroll hourly-rate derivation).
--
-- DIALECT PORTABILITY (runs verbatim on sqlite in tests AND postgres in prod via
-- be/migrate.py — mirrors 0002_menu.sql / 0003_inventory.sql / 0004_hr.sql):
--   * unqualified table names        (no CREATE SCHEMA / no `employees_stg.` prefix)
--   * INTEGER PRIMARY KEY; surrogate ids allocated app-side (crud _next_id)
--   * INTEGER 0/1 for booleans       (is_available, is_active, confirmed, …)
--   * TEXT "HH:MM" for times         (avoids the asyncpg TIME-binding gotcha;
--                                      durations are computed in Python)
--   * TEXT "YYYY-MM-DD" for dates    (week_start, shift_date, date_from/to, …)
--   * TEXT for timestamps            (ISO-8601, written from Python — no now())
--   * NUMERIC for hours              (bound as text via a numeric CAST)
--   * no ON CONFLICT / no ANY(...)   (upserts are select-then-insert/update in
--                                      crud; id lists use an expanding IN bind)
--
-- VENUE SCOPING (per docs/architecture/database-architecture.md target):
--   * venue-scoped: schedules, scheduled_shifts, recurring_shifts, shifts,
--     time_off_requests (via the schedule/employee), monthly_working_hours;
--   * employee-tied (venue carried through the employee): employee_availability,
--     timesheet_confirmations, shift_change_requests.
--   Every venue_id defaults 'default' (single-venue-friendly).
--
-- DEFERRED (noted, not built here): payroll rules (payment_rules / bonuses /
-- installments / balances) and the overtime engine; the Firestore clock-in/out
-- QR flow (clock_sessions -> staff-app/clock domain) that WRITES `shifts` in
-- prod; Telegram approval notifications (wire later via a Notifier adapter).
-- =====================================================================

-- --- schedules (weekly containers, venue-scoped) ---------------------------
-- One row per (venue, week). week_start is always a Sunday (ISO "YYYY-MM-DD").
CREATE TABLE IF NOT EXISTS schedules (
    id          INTEGER PRIMARY KEY,               -- app-allocated surrogate
    venue_id    TEXT    NOT NULL DEFAULT 'default',
    week_start  TEXT    NOT NULL,                  -- ISO "YYYY-MM-DD" (a Sunday)
    created_at  TEXT    NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_schedules_week
    ON schedules (venue_id, week_start);

-- --- scheduled_shifts (per-day assignments, venue-scoped) ------------------
CREATE TABLE IF NOT EXISTS scheduled_shifts (
    id                    INTEGER PRIMARY KEY,
    venue_id              TEXT    NOT NULL DEFAULT 'default',
    schedule_id           INTEGER NOT NULL,        -- soft ref -> schedules.id
    employee_id           INTEGER NOT NULL,        -- soft ref -> employees.id
    role_id               INTEGER NOT NULL,        -- soft ref -> job_roles.id
    shift_date            TEXT    NOT NULL,         -- ISO "YYYY-MM-DD"
    start_time            TEXT    NOT NULL,         -- "HH:MM"
    end_time              TEXT    NOT NULL,         -- "HH:MM"
    -- draft | pending | approved | declined | cancelled (a 'cancelled' row is a
    -- one-week recurring tombstone; see the recurring-merge in crud).
    status                TEXT    NOT NULL DEFAULT 'draft',
    notes                 TEXT,
    edited_after_approval INTEGER NOT NULL DEFAULT 0,   -- 0/1
    created_at            TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_sshifts_schedule ON scheduled_shifts (schedule_id);
CREATE INDEX IF NOT EXISTS idx_sshifts_employee ON scheduled_shifts (employee_id);
CREATE INDEX IF NOT EXISTS idx_sshifts_date     ON scheduled_shifts (shift_date);

-- --- recurring_shifts ("stable" templates, venue-scoped) -------------------
CREATE TABLE IF NOT EXISTS recurring_shifts (
    id           INTEGER PRIMARY KEY,
    venue_id     TEXT    NOT NULL DEFAULT 'default',
    employee_id  INTEGER NOT NULL,                 -- soft ref -> employees.id
    day_of_week  INTEGER NOT NULL,                 -- 0=Sun .. 6=Sat
    role_id      INTEGER NOT NULL,                 -- soft ref -> job_roles.id
    start_time   TEXT    NOT NULL,                 -- "HH:MM"
    end_time     TEXT    NOT NULL,                 -- "HH:MM"
    is_active    INTEGER NOT NULL DEFAULT 1,       -- 0/1
    start_date   TEXT,                             -- ISO date; NULL = no lower bound
    created_at   TEXT    NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_recurring_shift
    ON recurring_shifts (employee_id, day_of_week, role_id);

-- --- shift_change_requests (schedule change + timesheet correction) --------
-- Overloaded by request_type: 'schedule_change' (scheduled_shift_id OR
-- recurring_shift_id + target_date) and 'timesheet_correction' (shift_id).
-- Status flow: pending_admin -> approved | rejected.
CREATE TABLE IF NOT EXISTS shift_change_requests (
    id                   INTEGER PRIMARY KEY,
    venue_id             TEXT    NOT NULL DEFAULT 'default',
    employee_id          INTEGER NOT NULL,         -- soft ref -> employees.id
    request_type         TEXT    NOT NULL DEFAULT 'schedule_change',
    scheduled_shift_id   INTEGER,                  -- soft ref -> scheduled_shifts.id
    recurring_shift_id   INTEGER,                  -- soft ref -> recurring_shifts.id
    target_date          TEXT,                     -- ISO date (recurring change)
    shift_id             INTEGER,                  -- soft ref -> shifts.id (correction)
    requested_start_time TEXT,                     -- "HH:MM"
    requested_end_time   TEXT,                     -- "HH:MM"
    original_start_time  TEXT,                     -- "HH:MM" snapshot at creation
    original_end_time    TEXT,                     -- "HH:MM" snapshot at creation
    reason               TEXT,                     -- schedule-change reason
    correction_note      TEXT,                     -- timesheet-correction note
    status               TEXT    NOT NULL DEFAULT 'pending_admin',
    admin_response_at    TEXT,                     -- ISO timestamp
    admin_response_by    INTEGER,                  -- soft ref -> admin_users.id
    admin_response_note  TEXT,                     -- reject reason shown to staff
    created_at           TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_scr_employee ON shift_change_requests (employee_id);
CREATE INDEX IF NOT EXISTS idx_scr_status   ON shift_change_requests (status);
CREATE INDEX IF NOT EXISTS idx_scr_shift    ON shift_change_requests (scheduled_shift_id);
CREATE INDEX IF NOT EXISTS idx_scr_tsshift  ON shift_change_requests (shift_id);

-- --- employee_availability (employee-tied) ---------------------------------
CREATE TABLE IF NOT EXISTS employee_availability (
    id            INTEGER PRIMARY KEY,
    employee_id   INTEGER NOT NULL,                -- soft ref -> employees.id
    day_of_week   INTEGER NOT NULL,               -- 0=Sun .. 6=Sat
    is_available  INTEGER NOT NULL DEFAULT 1,      -- 0/1
    note          TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_availability
    ON employee_availability (employee_id, day_of_week);

-- --- shifts (clock/worked records, venue-scoped) ---------------------------
-- The QR clock-in/out flow that WRITES these in prod is DEFERRED (staff-app/
-- clock domain). The timesheet endpoints here read them and the report-hours
-- path creates one directly. end_time NULL = an open (unfinished) shift.
CREATE TABLE IF NOT EXISTS shifts (
    id                  INTEGER PRIMARY KEY,
    venue_id            TEXT    NOT NULL DEFAULT 'default',
    employee_id         INTEGER NOT NULL,          -- soft ref -> employees.id
    role_id             INTEGER,                   -- soft ref -> job_roles.id
    shift_date          TEXT    NOT NULL,          -- ISO "YYYY-MM-DD"
    start_time          TEXT    NOT NULL,          -- "HH:MM"
    end_time            TEXT,                      -- "HH:MM" or NULL (open)
    confirmed           INTEGER NOT NULL DEFAULT 0,    -- 0/1
    auto_close_ack      INTEGER NOT NULL DEFAULT 0,    -- 0/1
    source              TEXT    NOT NULL DEFAULT 'clock',  -- clock|auto_closed|forgot_clockin
    scheduled_shift_id  INTEGER,                   -- soft ref -> scheduled_shifts.id
    recurring_shift_id  INTEGER,                   -- soft ref -> recurring_shifts.id
    admin_comment       TEXT,
    created_at          TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_shifts_employee ON shifts (employee_id);
CREATE INDEX IF NOT EXISTS idx_shifts_date     ON shifts (shift_date);

-- --- timesheet_confirmations (audit-only; month state is DERIVED) ----------
CREATE TABLE IF NOT EXISTS timesheet_confirmations (
    id            INTEGER PRIMARY KEY,
    employee_id   INTEGER NOT NULL,                -- soft ref -> employees.id
    year          INTEGER NOT NULL,
    month         INTEGER NOT NULL,                -- 1..12
    confirmed_at  TEXT    NOT NULL                 -- ISO timestamp
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_tc_employee_month
    ON timesheet_confirmations (employee_id, year, month);

-- --- time_off_requests (employee-tied, venue carried) ----------------------
CREATE TABLE IF NOT EXISTS time_off_requests (
    id                 INTEGER PRIMARY KEY,
    venue_id           TEXT    NOT NULL DEFAULT 'default',
    employee_id        INTEGER NOT NULL,           -- soft ref -> employees.id
    type               TEXT    NOT NULL DEFAULT 'vacation',  -- vacation | sick
    date_from          TEXT    NOT NULL,           -- ISO "YYYY-MM-DD"
    date_to            TEXT    NOT NULL,           -- ISO "YYYY-MM-DD"
    note               TEXT,
    document_url       TEXT,
    status             TEXT    NOT NULL DEFAULT 'pending',  -- pending|approved|rejected
    admin_response_at  TEXT,                        -- ISO timestamp
    admin_response_by  INTEGER,                     -- soft ref -> admin_users.id
    admin_response_note TEXT,
    created_at         TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_tor_employee ON time_off_requests (employee_id);
CREATE INDEX IF NOT EXISTS idx_tor_dates    ON time_off_requests (date_from, date_to);

-- --- monthly_working_hours (venue-scoped config) ---------------------------
CREATE TABLE IF NOT EXISTS monthly_working_hours (
    id        INTEGER PRIMARY KEY,
    venue_id  TEXT    NOT NULL DEFAULT 'default',
    year      INTEGER NOT NULL,
    month     INTEGER NOT NULL,                    -- 1..12
    hours     NUMERIC NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_mwh_venue_month
    ON monthly_working_hours (venue_id, year, month);
