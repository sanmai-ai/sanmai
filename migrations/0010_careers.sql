-- =====================================================================
-- 0010_careers.sql  —  CAREERS domain source-of-truth
-- =====================================================================
--
-- The careers domain's authoritative store: company-wide job POSITIONS that
-- are shown on a PUBLIC listing, and job APPLICATIONS the public submits
-- against them (optionally attaching a CV/resume file), which admins review
-- and triage.
--
-- REPLACES PROD FIRESTORE (this is a change of record):
--   The live careers feature is 100% Firestore-backed with NO backend —
--   collections `careers_positions` (public read / authed write) and
--   `careers_applications` (public create / authed read+update+delete),
--   guarded by Firestore security rules. Core models the same data as
--   PORTABLE Postgres tables so authorization, validation, and PII handling
--   move server-side (the public write path stops trusting the client).
--
-- NET-NEW vs live:
--   * CV/resume upload+download — the live apply form is text-only. Core adds
--     a `cv_key` object-store reference (bytes go through the Storage seam);
--     admin-only proxy-stream download.
--
-- DIALECT PORTABILITY (runs verbatim on sqlite in tests AND postgres in prod
-- via be/migrate.py — mirrors 0002..0009):
--   * unqualified table names        (no CREATE SCHEMA / no `*_stg.` prefix)
--   * INTEGER PRIMARY KEY; surrogate ids allocated app-side (crud _next_id)
--   * INTEGER 0/1 for booleans       (is_active, citizenship, english)
--   * TEXT for timestamps            (ISO-8601, written from Python — no now())
--   * department/work_mode/status kept as TEXT CHECK constraints
--   * string arrays (responsibilities/requirements) stored as JSON-as-TEXT
--     (portable) — serialized/parsed in crud, no postgres-only jsonb
--   * no ON CONFLICT / no ANY(...)   (soft refs, additive)
--
-- VENUE SCOPING:
--   * positions + applications carry a venue_id tag (defaults 'default') so
--     the domain is venue_id-ready, but careers is treated as COMPANY-level
--     (the public list + admin lists span venues); an application inherits its
--     position's scope at write time.
--
-- PII / SECURITY (the spine — enforced in the router):
--   * EVERYTHING on an application is PII (full_name/email/phone/city/street/
--     experience/start_date/flags + the CV file). PUBLIC endpoints never read
--     any of it: the public list returns POSITIONS only (active, display
--     fields), and the public apply is WRITE-ONLY (echoes just the new id).
--   * all application reads + the CV download are admin-only (require_admin
--     "careers").
--
-- DEFERRED (noted, NOT built here):
--   * the new-application notification transport — routed through the Notifier
--     seam (NoopNotifier by default); no email is sent from core.
--   * a distributed/production rate-limiter backend for the public endpoints
--     (core ships an in-process guard only; Cloud Run scales horizontally so a
--     shared limiter — Redis / API-gateway — is required in prod).
--   * any bilingual (he/en) notification subject/body specifics.
-- =====================================================================

-- ── careers_positions (company-level, venue-tagged) ───────────────────────
CREATE TABLE IF NOT EXISTS careers_positions (
    id                    INTEGER PRIMARY KEY,           -- app-allocated surrogate
    venue_id              TEXT    NOT NULL DEFAULT 'default',
    department            TEXT    NOT NULL DEFAULT 'service'
        CHECK (department IN ('kitchen', 'service', 'bar', 'management')),
    work_mode             TEXT    NOT NULL DEFAULT 'fulltime'
        CHECK (work_mode IN ('fulltime', 'parttime', 'shift')),
    title_en              TEXT    NOT NULL DEFAULT '',
    title_he              TEXT    NOT NULL DEFAULT '',
    location_en           TEXT    NOT NULL DEFAULT '',
    location_he           TEXT    NOT NULL DEFAULT '',
    salary_en             TEXT    NOT NULL DEFAULT '',
    salary_he             TEXT    NOT NULL DEFAULT '',
    summary_en            TEXT    NOT NULL DEFAULT '',
    summary_he            TEXT    NOT NULL DEFAULT '',
    responsibilities_en   TEXT    NOT NULL DEFAULT '[]',  -- JSON-as-TEXT string[]
    responsibilities_he   TEXT    NOT NULL DEFAULT '[]',  -- JSON-as-TEXT string[]
    requirements_en       TEXT    NOT NULL DEFAULT '[]',  -- JSON-as-TEXT string[]
    requirements_he       TEXT    NOT NULL DEFAULT '[]',  -- JSON-as-TEXT string[]
    sort_order            INTEGER NOT NULL DEFAULT 100,
    is_active             INTEGER NOT NULL DEFAULT 1,      -- 0/1
    created_at            TEXT    NOT NULL,
    updated_at            TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_careers_positions_active_order
    ON careers_positions (is_active, sort_order);

-- ── careers_applications (public-submitted; ALL columns are PII) ───────────
CREATE TABLE IF NOT EXISTS careers_applications (
    id                  INTEGER PRIMARY KEY,              -- app-allocated surrogate
    venue_id            TEXT    NOT NULL DEFAULT 'default',
    position_id         INTEGER,                          -- soft ref -> careers_positions.id
    position_title_en   TEXT    NOT NULL DEFAULT '',
    position_title_he   TEXT    NOT NULL DEFAULT '',
    full_name           TEXT    NOT NULL DEFAULT '',
    email               TEXT    NOT NULL DEFAULT '',
    phone               TEXT    NOT NULL DEFAULT '',
    city                TEXT    NOT NULL DEFAULT '',
    street              TEXT    NOT NULL DEFAULT '',
    experience          TEXT    NOT NULL DEFAULT '',
    start_date          TEXT    NOT NULL DEFAULT '',
    citizenship         INTEGER NOT NULL DEFAULT 0,       -- 0/1
    english             INTEGER NOT NULL DEFAULT 0,       -- 0/1
    lang                TEXT    NOT NULL DEFAULT 'en',
    cv_key              TEXT,                             -- object-store key (nullable)
    status              TEXT    NOT NULL DEFAULT 'new'
        CHECK (status IN ('new', 'reviewed', 'accepted', 'rejected')),
    created_at          TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_careers_applications_status_created
    ON careers_applications (status, created_at);
CREATE INDEX IF NOT EXISTS idx_careers_applications_position
    ON careers_applications (position_id);
