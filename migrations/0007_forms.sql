-- =====================================================================
-- 0007_forms.sql  —  FORMS / COURSES / FLOWS / ASSIGNMENTS domain
-- =====================================================================
--
-- The employee onboarding + signed-document domain's authoritative store.
-- Builds on 0004_hr.sql (employees) via SOFT references (plain INTEGER
-- columns, no cross-table FKs) so the runner stays dialect-portable and each
-- migration is independently additive.
--
-- Covers, in one domain package:
--   * forms      — forms_templates (versioned, bilingual form definitions) +
--                  form_responses (a staff member's draft/submitted answers,
--                  optionally signed and rendered to a legal PDF artifact);
--   * courses    — courses (+ course_sections + course_items) versioned like
--                  forms, plus course_progress (per-employee completion set);
--   * flows      — flows (+ flow_items): an ordered onboarding bundle of forms
--                  and courses (parallel / sequential unlock);
--   * assignments— assignments (+ assignment_progress): a flow OR a standalone
--                  form/course handed to an employee, with per-item progress.
--
-- DIALECT PORTABILITY (runs verbatim on sqlite in tests AND postgres in prod
-- via be/migrate.py — mirrors 0002..0006):
--   * unqualified table names        (no CREATE SCHEMA / no `employees_stg.`)
--   * TEXT PRIMARY KEY; ids are app-allocated UUID strings (crud _new_id) —
--     forms are UUID-keyed and cross-referenced polymorphically (flow_items,
--     assignment_progress), so TEXT ids port cleanly to both engines and keep
--     the polymorphic refs stable (no gen_random_uuid()/pgcrypto)
--   * INTEGER 0/1 for booleans       (bilingual)
--   * TEXT for JSON columns          (fields, answers, payload,
--                                      completed_item_ids — parsed/dumped in crud)
--   * TEXT ISO-8601 for timestamps   (created_at, submitted_at, due_at, … —
--                                      written from Python, no now()); date math
--                                      (per-item due_at, overdue) is done in Python
--   * TEXT for the client IP         (submitted_ip — no INET type)
--   * INTEGER for version / position / *_due_days
--   * no ON CONFLICT / no ANY(...) / no TEXT[] — the completed-item set is a JSON
--     TEXT list de-duped in Python; polymorphic existence + cascade deletes are
--     enforced in crud (no cross-table FK / ON DELETE CASCADE)
--
-- VENUE SCOPING (per docs/architecture/database-architecture.md target):
--   * company-level definitions (venue_id, defaults 'default'): forms_templates,
--     courses, course_sections, course_items, flows, flow_items;
--   * venue-scoped execution records (venue_id, defaults 'default'):
--     form_responses, course_progress, assignments; assignment_progress inherits
--     its venue via the parent assignment (no own column, mirrors run children).
--
-- SECURITY NOTE (fixes a live hole): the signed-document / PII reads
-- (form_responses list + PDF) are gated on require_admin("hr") in the router,
-- NOT unauthenticated — see be/app/domains/forms/router.py. Staff-self reads are
-- ownership-checked against the VERIFIED token identity (never a query-param
-- email/phone).
--
-- DEFERRED (noted, NOT built here):
--   * real WeasyPrint PDF rendering — seamed behind forms.render.PdfRenderer
--     (Null default stores answers+signature, no bytes); pdf_gcs_path/pdf_sha256
--     stay NULL until a real renderer is bound;
--   * Telegram/notify on nudge — last_nudged_at is recorded, no notifier;
--   * bilingual legal logic beyond generic i18n passthrough (he/en columns kept).
-- =====================================================================

-- ── forms_templates (company-level) ───────────────────────────────────────
CREATE TABLE IF NOT EXISTS forms_templates (
    id                TEXT    PRIMARY KEY,                 -- app-allocated uuid string
    venue_id          TEXT    NOT NULL DEFAULT 'default',
    name_he           TEXT    NOT NULL DEFAULT 'Untitled form',
    name_en           TEXT    NOT NULL DEFAULT 'Untitled form',
    description_he    TEXT,
    description_en    TEXT,
    fields            TEXT    NOT NULL DEFAULT '[]',       -- JSON array of field defs
    binding_language  TEXT    NOT NULL DEFAULT 'none'
                          CHECK (binding_language IN ('he', 'en', 'none')),
    bilingual         INTEGER NOT NULL DEFAULT 0,          -- 0/1
    status            TEXT    NOT NULL DEFAULT 'draft'
                          CHECK (status IN ('draft', 'published', 'archived')),
    version           INTEGER NOT NULL DEFAULT 1,
    parent_id         TEXT,                                -- groups a version chain
    created_by        INTEGER,                             -- soft ref -> employees.id
    created_at        TEXT    NOT NULL,
    published_at      TEXT
);

CREATE INDEX IF NOT EXISTS idx_forms_templates_parent ON forms_templates (parent_id);
CREATE INDEX IF NOT EXISTS idx_forms_templates_status ON forms_templates (status);

-- ── form_responses (venue-scoped, employee-tied) ──────────────────────────
CREATE TABLE IF NOT EXISTS form_responses (
    id                          TEXT    PRIMARY KEY,
    venue_id                    TEXT    NOT NULL DEFAULT 'default',
    template_id                 TEXT    NOT NULL,          -- soft ref -> forms_templates.id
    template_version            INTEGER NOT NULL,          -- SNAPSHOT of the version filled
    employee_id                 INTEGER NOT NULL,          -- soft ref -> employees.id
    assignment_progress_id      TEXT,                      -- soft ref -> assignment_progress.id
    answers                     TEXT    NOT NULL DEFAULT '{}',  -- JSON object
    status                      TEXT    NOT NULL DEFAULT 'draft'
                                    CHECK (status IN ('draft', 'submitted')),
    signature_base64            TEXT,
    binding_language            TEXT,
    binding_language_confirmed  TEXT,
    pdf_gcs_path                TEXT,                      -- storage key (NULL until rendered)
    pdf_sha256                  TEXT,
    submitted_at                TEXT,
    submitted_ip                TEXT,                      -- client IP (no INET type)
    created_at                  TEXT    NOT NULL,
    updated_at                  TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_form_responses_employee_status
    ON form_responses (employee_id, status);
CREATE INDEX IF NOT EXISTS idx_form_responses_template
    ON form_responses (template_id);
CREATE INDEX IF NOT EXISTS idx_form_responses_progress
    ON form_responses (assignment_progress_id);

-- ── courses (company-level) ───────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS courses (
    id              TEXT    PRIMARY KEY,
    venue_id        TEXT    NOT NULL DEFAULT 'default',
    name_he         TEXT    NOT NULL DEFAULT 'Untitled course',
    name_en         TEXT    NOT NULL DEFAULT 'Untitled course',
    description_he  TEXT,
    description_en  TEXT,
    status          TEXT    NOT NULL DEFAULT 'draft'
                        CHECK (status IN ('draft', 'published', 'archived')),
    version         INTEGER NOT NULL DEFAULT 1,
    parent_id       TEXT,
    created_by      INTEGER,                               -- soft ref -> employees.id
    created_at      TEXT    NOT NULL,
    published_at    TEXT
);

CREATE INDEX IF NOT EXISTS idx_courses_parent ON courses (parent_id);
CREATE INDEX IF NOT EXISTS idx_courses_status ON courses (status);

-- ── course_sections (company-level) ───────────────────────────────────────
CREATE TABLE IF NOT EXISTS course_sections (
    id          TEXT    PRIMARY KEY,
    course_id   TEXT    NOT NULL,                          -- soft ref -> courses.id
    position    INTEGER NOT NULL DEFAULT 0,
    title_he    TEXT    NOT NULL,
    title_en    TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_course_sections_course
    ON course_sections (course_id, position);

-- ── course_items (company-level) ──────────────────────────────────────────
-- payload JSON shape is type-dependent:
--   text  -> {body_he, body_en}   image -> {gcs_path, alt_he, alt_en}
--   video -> {url, provider}      quiz  -> {question_he, question_en, options, correct_index}
CREATE TABLE IF NOT EXISTS course_items (
    id          TEXT    PRIMARY KEY,
    section_id  TEXT    NOT NULL,                          -- soft ref -> course_sections.id
    position    INTEGER NOT NULL DEFAULT 0,
    type        TEXT    NOT NULL
                    CHECK (type IN ('text', 'image', 'video', 'quiz')),
    payload     TEXT    NOT NULL DEFAULT '{}'             -- JSON object
);

CREATE INDEX IF NOT EXISTS idx_course_items_section
    ON course_items (section_id, position);

-- ── course_progress (venue-scoped, employee-tied) ─────────────────────────
CREATE TABLE IF NOT EXISTS course_progress (
    id                  TEXT    PRIMARY KEY,
    venue_id            TEXT    NOT NULL DEFAULT 'default',
    course_id           TEXT    NOT NULL,                  -- soft ref -> courses.id
    course_version      INTEGER NOT NULL,
    employee_id         INTEGER NOT NULL,                  -- soft ref -> employees.id
    last_item_id        TEXT,
    completed_item_ids  TEXT    NOT NULL DEFAULT '[]',     -- JSON array of item ids
    started_at          TEXT    NOT NULL,
    completed_at        TEXT
);

CREATE INDEX IF NOT EXISTS idx_course_progress_employee_course
    ON course_progress (employee_id, course_id);

-- ── flows (company-level) ─────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS flows (
    id                TEXT    PRIMARY KEY,
    venue_id          TEXT    NOT NULL DEFAULT 'default',
    name_he           TEXT    NOT NULL DEFAULT 'Untitled flow',
    name_en           TEXT    NOT NULL DEFAULT 'Untitled flow',
    ordering          TEXT    NOT NULL DEFAULT 'sequential'
                          CHECK (ordering IN ('parallel', 'sequential')),
    default_due_days  INTEGER,
    status            TEXT    NOT NULL DEFAULT 'draft'
                          CHECK (status IN ('draft', 'published', 'archived')),
    created_by        INTEGER,                             -- soft ref -> employees.id
    created_at        TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_flows_status ON flows (status);

-- ── flow_items (company-level) ────────────────────────────────────────────
-- item_id is polymorphic (forms_templates.id OR courses.id per item_type); no
-- FK — crud enforces existence at write time.
CREATE TABLE IF NOT EXISTS flow_items (
    id                 TEXT    PRIMARY KEY,
    flow_id            TEXT    NOT NULL,                   -- soft ref -> flows.id
    position           INTEGER NOT NULL DEFAULT 0,
    item_type          TEXT    NOT NULL
                           CHECK (item_type IN ('form', 'course')),
    item_id            TEXT    NOT NULL,                   -- soft polymorphic ref
    due_days_override  INTEGER
);

CREATE INDEX IF NOT EXISTS idx_flow_items_flow
    ON flow_items (flow_id, position);

-- ── assignments (venue-scoped, employee-tied) ─────────────────────────────
-- source='flow' uses flow_id; source='standalone' uses exactly one of
-- form_id / course_id (exclusivity enforced in crud).
CREATE TABLE IF NOT EXISTS assignments (
    id             TEXT    PRIMARY KEY,
    venue_id       TEXT    NOT NULL DEFAULT 'default',
    employee_id    INTEGER NOT NULL,                       -- soft ref -> employees.id
    source         TEXT    NOT NULL
                       CHECK (source IN ('flow', 'standalone')),
    flow_id        TEXT,                                   -- soft ref -> flows.id
    form_id        TEXT,                                   -- soft ref -> forms_templates.id
    course_id      TEXT,                                   -- soft ref -> courses.id
    assigned_by    INTEGER,                                -- soft ref -> employees.id
    assigned_at    TEXT    NOT NULL,
    due_at         TEXT,
    last_nudged_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_assignments_employee_due
    ON assignments (employee_id, due_at);
CREATE INDEX IF NOT EXISTS idx_assignments_flow ON assignments (flow_id);

-- ── assignment_progress (child of assignments) ────────────────────────────
-- One row per (assignment, item). Flow assignments: one per flow_item;
-- standalone: a single row pointing at the form/course directly. due_at is a
-- snapshot computed at creation time (default_due_days / due_days_override).
CREATE TABLE IF NOT EXISTS assignment_progress (
    id                  TEXT    PRIMARY KEY,
    assignment_id       TEXT    NOT NULL,                  -- soft ref -> assignments.id
    flow_item_id        TEXT,                              -- soft ref -> flow_items.id
    form_response_id    TEXT,                              -- soft ref -> form_responses.id
    course_progress_id  TEXT,                              -- soft ref -> course_progress.id
    status              TEXT    NOT NULL DEFAULT 'locked'
                            CHECK (status IN ('locked', 'available', 'in_progress', 'completed')),
    due_at              TEXT,
    started_at          TEXT,
    completed_at        TEXT
);

CREATE INDEX IF NOT EXISTS idx_assignment_progress_assignment
    ON assignment_progress (assignment_id);
CREATE INDEX IF NOT EXISTS idx_assignment_progress_status
    ON assignment_progress (status, due_at);
