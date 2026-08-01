-- =====================================================================
-- 0012_clock.sql  —  STAFF CLOCK-IN/OUT (QR session) domain
-- =====================================================================
--
-- Fills the seam explicitly deferred by 0005_scheduling.sql: the staff
-- clock-in/out flow that WRITES `shifts`. In prod this was a Firestore
-- `clock_sessions` doc used only as a realtime handoff between a station
-- iPad (mints a QR) and the staff phone (scans it to clock in/out). Here it
-- is reimplemented as a PORTABLE POSTGRES table — no Firestore — consistent
-- with how every other Firestore surface has been deferred behind a seam.
--
-- REALTIME -> POLL: the station no longer opens a Firestore onSnapshot
-- listener; it polls a status endpoint. The session row is short-lived (a
-- 60s TTL computed in Python, since there is no now() in portable SQL) and
-- expiry is evaluated lazily on read.
--
-- SESSION LIFECYCLE:  pending --redeem--> used --shift-written--> completed
--                     pending --TTL/force-close--> expired
--   * pending    : freshly minted by the station; the QR payload = `token`.
--   * used       : atomically claimed by exactly one staff redeem
--                  (UPDATE ... WHERE status='pending'); single-use guard.
--   * completed  : the shift row has been written; carries the snapshot the
--                  station poll displays (kind + employee_name + shift_id).
--   * expired    : past `expires_at` (lazy flip on read) or force-closed.
--
-- DIALECT PORTABILITY (runs verbatim on sqlite in tests AND postgres in prod
-- via be/migrate.py — mirrors 0002..0011):
--   * unqualified table name; INTEGER PRIMARY KEY allocated app-side (_next_id)
--   * TEXT for timestamps (ISO-8601, written from Python — no now())
--   * TTL computed in Python (expires_at = created_at + 60s at create time)
--   * no ON CONFLICT / no ANY(...) / no now()
--
-- VENUE SCOPING: clock sessions are venue-scoped (venue_id, default 'default').
-- A staff redeem is rejected unless the employee's venue matches the session's,
-- and the resulting `shifts` row inherits that venue_id. The employee is always
-- resolved from the VERIFIED token — never a query param — so a member clocks
-- only THEMSELVES.
--
-- DEFERRED (noted, not built here): realtime push (poll instead of Firestore
-- listeners); the physical kiosk-device specifics; and Telegram clock
-- notifications (wire later behind a Notifier adapter). The scheduling
-- extras that hung off the live /staff/clock (approve-next-week gate,
-- forgot-clock-in prompt, auto-close of a stale open shift, task-instance
-- generation, last-shift-of-month) are out of scope for this seam.
-- =====================================================================

-- --- clock_sessions (short-TTL QR handoff, venue-scoped) -------------------
CREATE TABLE IF NOT EXISTS clock_sessions (
    id             INTEGER PRIMARY KEY,               -- app-allocated surrogate
    token          TEXT    NOT NULL,                  -- opaque QR payload (secrets)
    venue_id       TEXT    NOT NULL DEFAULT 'default',
    -- pending | used | completed | expired
    status         TEXT    NOT NULL DEFAULT 'pending',
    employee_id    INTEGER,                           -- soft ref -> employees.id (NULL until redeemed)
    kind           TEXT,                              -- clock_in | clock_out (resolved at redeem)
    shift_id       INTEGER,                           -- soft ref -> shifts.id (written at redeem)
    employee_name  TEXT,                              -- snapshot for the station poll display
    created_at     TEXT    NOT NULL,                  -- ISO-8601 (from Python)
    expires_at     TEXT    NOT NULL,                  -- ISO-8601 = created_at + TTL (from Python)
    redeemed_at    TEXT                               -- ISO-8601 when it reached 'completed'
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_clock_sessions_token
    ON clock_sessions (token);

CREATE INDEX IF NOT EXISTS idx_clock_sessions_status
    ON clock_sessions (status);

CREATE INDEX IF NOT EXISTS idx_clock_sessions_venue
    ON clock_sessions (venue_id);
