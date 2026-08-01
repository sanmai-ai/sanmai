-- =====================================================================
-- 0006_tasks.sql  —  TASKS & CHECKLISTS (v2) domain source-of-truth
-- =====================================================================
--
-- The tasks/checklists domain's authoritative store. Builds on 0004_hr.sql
-- (employees, employee_groups, employee_group_members, job_roles,
-- employees.default_role_id) and 0005_scheduling.sql (scheduled_shifts,
-- recurring_shifts, shifts) via SOFT references (plain INTEGER columns, no
-- cross-table FKs) so the runner stays dialect-portable and each migration is
-- independently additive.
--
-- Covers, in one domain package:
--   * library      — task_templates (+ dependencies): reusable task definitions
--                    with proof type / required-staff / subtasks / bonus attrs;
--   * checklists   — checklists (+ items + assignees): a set of library tasks
--                    targeted at employees / groups / job-roles, with a
--                    manager->admin approval flow and a weekday recurrence;
--   * runs         — checklist_runs (+ run_tasks + run_assignees +
--                    run_task_participants + run_events): the per-day, immutable
--                    execution snapshot staff actually work against;
--   * v1 history   — task_instances: the deprecated v1 daily-instance table,
--                    kept ONLY as a history/FK anchor (no CRUD, no generation).
--
-- DIALECT PORTABILITY (runs verbatim on sqlite in tests AND postgres in prod via
-- be/migrate.py — mirrors 0002..0005):
--   * unqualified table names        (no CREATE SCHEMA / no `employees_stg.` prefix)
--   * INTEGER PRIMARY KEY; surrogate ids allocated app-side (crud _next_id)
--   * INTEGER 0/1 for booleans       (is_active, is_mandatory, is_bonus, …)
--   * TEXT for JSON columns          (photos, subtasks, subtasks_done,
--                                      assigned_*_ids, depends_on_template_ids,
--                                      visible_to_employee_ids, recurrence,
--                                      payload — parsed/dumped in crud)
--   * TEXT "HH:MM" for times         (due_time, window_start/end — avoids the
--                                      asyncpg TIME-binding gotcha; overlap math
--                                      is done in Python)
--   * TEXT "YYYY-MM-DD" for dates    (run_date, task_date)
--   * TEXT for timestamps            (ISO-8601, written from Python — no now())
--   * NUMERIC for money              (bonus_amount — bound as text via a CAST)
--   * no ON CONFLICT / no ANY(...)   (upserts are select-then-insert; id lists
--                                      use an expanding IN bind; the 2-year prune
--                                      does its date math in Python)
--
-- VENUE SCOPING (per docs/architecture/database-architecture.md target):
--   * company-level (no venue_id): task_templates, task_dependencies, checklists,
--     checklist_items, checklist_assignees — the reusable definitions;
--   * venue-scoped (venue_id, defaults 'default'): checklist_runs +
--     ALL run_* children, and task_instances — the per-venue execution records.
--
-- DEFERRED (noted, NOT built here):
--   * restaurant_settings / a generic company_settings|venue_settings key/value
--     config table — NO tasks-domain code reads it (run generation is driven by
--     checklists.due_time/recurrence, not settings); it belongs to a future
--     cross-cutting config domain (portable shape: key TEXT, value TEXT JSON).
--   * bonus_task_log + bonus crediting — a PAYROLL concern. is_bonus/bonus_amount
--     columns and the run-execution "bonus owned" semantics stay here; the actual
--     crediting is a hook in crud.finish_task (wire to payroll + a Notifier later).
--   * v1 /staff/tasks + /tasks/generate-daily endpoints — SUPERSEDED by the v2
--     checklist-run flow. task_instances is retained as history only.
-- =====================================================================

-- ── library: task_templates (company-level) ───────────────────────────────
CREATE TABLE IF NOT EXISTS task_templates (
    id                 INTEGER PRIMARY KEY,               -- app-allocated surrogate
    title              TEXT    NOT NULL DEFAULT 'Untitled task',
    description        TEXT,
    category           TEXT    NOT NULL DEFAULT 'during_shift'
        CHECK (category IN ('opening', 'closing', 'during_shift')),
    estimated_minutes  INTEGER,
    sort_order         INTEGER NOT NULL DEFAULT 0,
    is_active          INTEGER NOT NULL DEFAULT 1,        -- 0/1
    is_bonus           INTEGER NOT NULL DEFAULT 0,        -- 0/1 (attribute, not category)
    bonus_amount       NUMERIC,                           -- payroll crediting DEFERRED
    bonus_currency     TEXT    DEFAULT 'ILS',
    recurrence         TEXT,                              -- v1 leftover, kept
    photos             TEXT    NOT NULL DEFAULT '[]',     -- JSON array of urls
    default_proof_type TEXT    NOT NULL DEFAULT 'check'
        CHECK (default_proof_type IN ('check', 'text', 'photo')),
    required_staff     INTEGER NOT NULL DEFAULT 1 CHECK (required_staff >= 1),
    subtasks           TEXT    NOT NULL DEFAULT '[]',     -- JSON array of strings
    created_at         TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_task_templates_cat ON task_templates (category);

-- ── library: task_dependencies (company-level) ────────────────────────────
CREATE TABLE IF NOT EXISTS task_dependencies (
    task_template_id INTEGER NOT NULL,                    -- soft ref -> task_templates.id
    depends_on_id    INTEGER NOT NULL,                    -- soft ref -> task_templates.id
    PRIMARY KEY (task_template_id, depends_on_id),
    CHECK (task_template_id != depends_on_id)
);

-- ── checklists (company-level) ────────────────────────────────────────────
-- recurrence: JSON array of weekday ints 0=Sun..6=Sat; NULL/[] = every day.
CREATE TABLE IF NOT EXISTS checklists (
    id              INTEGER PRIMARY KEY,
    name            TEXT    NOT NULL DEFAULT 'New checklist',
    group_id        INTEGER,                              -- soft ref -> employee_groups.id (legacy v1 target)
    category        TEXT    NOT NULL DEFAULT 'during_shift'
        CHECK (category IN ('opening', 'closing', 'during_shift')),
    is_active       INTEGER NOT NULL DEFAULT 1,           -- 0/1
    due_time        TEXT,                                 -- "HH:MM" or NULL
    recurrence      TEXT,                                 -- JSON weekday ints; NULL/[] = every day
    auto_generate   INTEGER NOT NULL DEFAULT 0,           -- 0/1
    created_by      TEXT,
    activated_at    TEXT,                                 -- ISO timestamp
    updated_at      TEXT    NOT NULL,                     -- ISO timestamp (R11 stale-save)
    approval_status TEXT    NOT NULL DEFAULT 'approved'
        CHECK (approval_status IN ('approved', 'pending_approval', 'rejected')),
    approval_note   TEXT,
    approved_by     TEXT,
    approved_at     TEXT,
    is_adhoc        INTEGER NOT NULL DEFAULT 0,           -- 0/1 (hidden quick-assign checklist)
    created_at      TEXT    NOT NULL
);

-- ── checklist_items (company-level) ───────────────────────────────────────
-- Item-level overrides + per-item assignee sets. proof_type / required_staff
-- NULL = inherit the template default.
CREATE TABLE IF NOT EXISTS checklist_items (
    id                    INTEGER PRIMARY KEY,
    checklist_id          INTEGER NOT NULL,               -- soft ref -> checklists.id
    task_template_id      INTEGER NOT NULL,               -- soft ref -> task_templates.id
    sort_order            INTEGER NOT NULL DEFAULT 0,
    is_mandatory          INTEGER NOT NULL DEFAULT 1,     -- 0/1 (item-level only, D12)
    proof_type            TEXT
        CHECK (proof_type IS NULL OR proof_type IN ('check', 'text', 'photo')),
    required_staff        INTEGER CHECK (required_staff IS NULL OR required_staff >= 1),
    assigned_employee_ids TEXT NOT NULL DEFAULT '[]',     -- JSON int array
    assigned_group_ids    TEXT NOT NULL DEFAULT '[]',     -- JSON int array
    assigned_role_ids     TEXT NOT NULL DEFAULT '[]'      -- JSON int array (job_roles.id)
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_checklist_items
    ON checklist_items (checklist_id, task_template_id);
CREATE INDEX IF NOT EXISTS idx_ci_checklist ON checklist_items (checklist_id);

-- ── checklist_assignees (company-level) ───────────────────────────────────
-- One row per target. At most one of employee/group/role; both-NULL is allowed
-- only for schedule_scope='scheduled' ("everyone scheduled" targeting).
CREATE TABLE IF NOT EXISTS checklist_assignees (
    id             INTEGER PRIMARY KEY,
    checklist_id   INTEGER NOT NULL,                      -- soft ref -> checklists.id
    employee_id    INTEGER,                               -- soft ref -> employees.id
    group_id       INTEGER,                               -- soft ref -> employee_groups.id
    role_id        INTEGER,                               -- soft ref -> job_roles.id
    schedule_scope TEXT    NOT NULL DEFAULT 'anyone'
        CHECK (schedule_scope IN ('anyone', 'scheduled')),
    window_start   TEXT,                                  -- "HH:MM" or NULL
    window_end     TEXT,                                  -- "HH:MM" or NULL
    created_at     TEXT    NOT NULL,
    CHECK (
        (CASE WHEN employee_id IS NOT NULL THEN 1 ELSE 0 END
         + CASE WHEN group_id IS NOT NULL THEN 1 ELSE 0 END
         + CASE WHEN role_id  IS NOT NULL THEN 1 ELSE 0 END) <= 1
    ),
    CHECK (
        employee_id IS NOT NULL OR group_id IS NOT NULL OR role_id IS NOT NULL
        OR schedule_scope = 'scheduled'
    )
);

CREATE INDEX IF NOT EXISTS idx_ca_checklist ON checklist_assignees (checklist_id);

-- ── checklist_runs (venue-scoped) ─────────────────────────────────────────
CREATE TABLE IF NOT EXISTS checklist_runs (
    id            INTEGER PRIMARY KEY,
    venue_id      TEXT    NOT NULL DEFAULT 'default',
    checklist_id  INTEGER NOT NULL,                       -- soft ref -> checklists.id
    run_date      TEXT    NOT NULL,                       -- ISO "YYYY-MM-DD"
    status        TEXT    NOT NULL DEFAULT 'open'
        CHECK (status IN ('open', 'completed', 'expired')),
    name_snapshot TEXT    NOT NULL,
    category      TEXT,
    due_time      TEXT,                                   -- "HH:MM" or NULL
    generated_by  TEXT,
    created_at    TEXT    NOT NULL,
    completed_at  TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_checklist_runs
    ON checklist_runs (venue_id, checklist_id, run_date);
CREATE INDEX IF NOT EXISTS idx_cr_run_date ON checklist_runs (run_date);

-- ── checklist_run_tasks — immutable content snapshot (venue-scoped) ────────
CREATE TABLE IF NOT EXISTS checklist_run_tasks (
    id                      INTEGER PRIMARY KEY,
    venue_id                TEXT    NOT NULL DEFAULT 'default',
    run_id                  INTEGER NOT NULL,             -- soft ref -> checklist_runs.id
    task_template_id        INTEGER,                      -- soft ref -> task_templates.id
    sort_order              INTEGER NOT NULL DEFAULT 0,
    title                   TEXT    NOT NULL,
    description             TEXT,
    photos                  TEXT    NOT NULL DEFAULT '[]',
    estimated_minutes       INTEGER,
    is_mandatory            INTEGER NOT NULL DEFAULT 1,   -- 0/1
    proof_type              TEXT    NOT NULL DEFAULT 'check'
        CHECK (proof_type IN ('check', 'text', 'photo')),
    required_staff          INTEGER NOT NULL DEFAULT 1 CHECK (required_staff >= 1),
    is_bonus                INTEGER NOT NULL DEFAULT 0,   -- 0/1
    bonus_amount            NUMERIC,
    depends_on_template_ids TEXT    NOT NULL DEFAULT '[]',
    visible_to_employee_ids TEXT    NOT NULL DEFAULT '[]', -- [] = whole run audience (R15)
    status                  TEXT    NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'in_progress', 'done')),
    subtasks                TEXT    NOT NULL DEFAULT '[]',
    subtasks_done           TEXT    NOT NULL DEFAULT '[]'
);

CREATE INDEX IF NOT EXISTS idx_crt_run_id ON checklist_run_tasks (run_id);

-- ── checklist_run_task_participants (venue-scoped) ────────────────────────
-- One row per participant; Release / take-over deletes the row — the events
-- table keeps the audit trail.
CREATE TABLE IF NOT EXISTS checklist_run_task_participants (
    id           INTEGER PRIMARY KEY,
    venue_id     TEXT    NOT NULL DEFAULT 'default',
    run_task_id  INTEGER NOT NULL,                        -- soft ref -> checklist_run_tasks.id
    employee_id  INTEGER NOT NULL,                        -- soft ref -> employees.id
    started_at   TEXT    NOT NULL,                        -- ISO timestamp
    finished_at  TEXT,                                    -- ISO timestamp or NULL
    proof_text   TEXT,
    proof_photos TEXT    NOT NULL DEFAULT '[]'
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_crtp_task_emp
    ON checklist_run_task_participants (run_task_id, employee_id);
CREATE INDEX IF NOT EXISTS idx_crtp_run_task ON checklist_run_task_participants (run_task_id);

-- ── checklist_run_assignees — resolved audience snapshot (venue-scoped) ────
CREATE TABLE IF NOT EXISTS checklist_run_assignees (
    id          INTEGER PRIMARY KEY,
    venue_id    TEXT    NOT NULL DEFAULT 'default',
    run_id      INTEGER NOT NULL,                         -- soft ref -> checklist_runs.id
    employee_id INTEGER NOT NULL                          -- soft ref -> employees.id
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_cra_run_emp
    ON checklist_run_assignees (run_id, employee_id);
CREATE INDEX IF NOT EXISTS idx_cra_run      ON checklist_run_assignees (run_id);
CREATE INDEX IF NOT EXISTS idx_cra_employee ON checklist_run_assignees (employee_id);

-- ── checklist_run_events — append-only audit (venue-scoped) ───────────────
CREATE TABLE IF NOT EXISTS checklist_run_events (
    id          INTEGER PRIMARY KEY,
    venue_id    TEXT    NOT NULL DEFAULT 'default',
    run_task_id INTEGER NOT NULL,                         -- soft ref -> checklist_run_tasks.id
    employee_id INTEGER,                                  -- soft ref -> employees.id (NULL = system/admin)
    event       TEXT    NOT NULL
        CHECK (event IN ('started', 'finished', 'released', 'taken_over', 'reopened', 'retro_completed')),
    payload     TEXT    NOT NULL DEFAULT '{}',            -- JSON object
    created_at  TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_cre_run_task ON checklist_run_events (run_task_id);

-- ── task_instances — v1 daily-instance table (venue-scoped, HISTORY ONLY) ──
-- DEPRECATED: superseded by the checklist_runs flow. Retained as a history/FK
-- anchor; no CRUD and no boot generation target this table.
CREATE TABLE IF NOT EXISTS task_instances (
    id               INTEGER PRIMARY KEY,
    venue_id         TEXT    NOT NULL DEFAULT 'default',
    task_template_id INTEGER NOT NULL,                    -- soft ref -> task_templates.id
    checklist_id     INTEGER,                             -- soft ref -> checklists.id
    task_date        TEXT    NOT NULL,                    -- ISO "YYYY-MM-DD"
    status           TEXT    NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'in_progress', 'completed')),
    claimed_by       INTEGER,                             -- soft ref -> employees.id
    completed_by     INTEGER,                             -- soft ref -> employees.id
    claimed_at       TEXT,
    completed_at     TEXT,
    notes            TEXT,
    created_at       TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_ti_date   ON task_instances (task_date);
CREATE INDEX IF NOT EXISTS idx_ti_status ON task_instances (status);
