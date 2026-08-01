-- =====================================================================
-- 0008_suggestions.sql  —  STAFF SUGGESTIONS domain source-of-truth
-- =====================================================================
--
-- The suggestions domain's authoritative store: a company-wide idea/bug/
-- improvement board any employee can post to, that other staff can +1 and
-- comment on, that admins can hide (per-area visibility) or close, and whose
-- new-post notification recipient is a single configurable email.
--
-- Builds on 0004_hr.sql (employees, admin_users RBAC) via SOFT references
-- (plain INTEGER columns, no cross-table FKs) so each migration stays
-- independently additive and dialect-portable.
--
-- DIALECT PORTABILITY (runs verbatim on sqlite in tests AND postgres in prod via
-- be/migrate.py — mirrors 0002..0007):
--   * unqualified table names        (no CREATE SCHEMA / no `employees_stg.` prefix)
--   * INTEGER PRIMARY KEY; surrogate ids allocated app-side (crud _next_id)
--   * INTEGER 0/1 for booleans       (is_hidden)
--   * TEXT for timestamps            (ISO-8601, written from Python — no now())
--   * category/status kept as TEXT CHECK constraints
--   * no ON CONFLICT / no ANY(...)   (plus-one idempotency is select-then-insert;
--                                      the settings single-row is seeded lazily in
--                                      crud; comment-image fan-in uses an expanding
--                                      IN bind)
--   * NO San-Mai-specific default    (suggestion_settings.notification_email
--                                      defaults to '' — the deploy sets the real
--                                      recipient via the admin settings endpoint)
--
-- VENUE SCOPING:
--   * suggestions carry a venue_id tag (defaults 'default') so the board is
--     venue_id-ready, but the domain treats suggestions as COMPANY-level (the
--     staff/admin lists span venues); children inherit their parent's scope.
--
-- DEFERRED (noted, NOT built here):
--   * the actual new-suggestion notification transport — routed through the
--     Notifier seam (NoopNotifier by default); no email is sent from core.
--   * any bilingual (he/en) title/body specifics.
-- =====================================================================

-- ── suggestions (company-level, venue-tagged) ─────────────────────────────
CREATE TABLE IF NOT EXISTS suggestions (
    id               INTEGER PRIMARY KEY,              -- app-allocated surrogate
    venue_id         TEXT    NOT NULL DEFAULT 'default',
    employee_id      INTEGER NOT NULL,                 -- soft ref -> employees.id
    title            TEXT    NOT NULL DEFAULT 'Untitled suggestion',
    category         TEXT    NOT NULL DEFAULT 'other'
        CHECK (category IN ('foh', 'kitchen', 'storage', 'system', 'other')),
    description      TEXT    NOT NULL DEFAULT '',
    status           TEXT    NOT NULL DEFAULT 'open'
        CHECK (status IN ('open', 'closed')),
    is_hidden        INTEGER NOT NULL DEFAULT 0,       -- 0/1
    hidden_by_email  TEXT,
    hidden_at        TEXT,
    closed_by_email  TEXT,
    closed_at        TEXT,
    created_at       TEXT    NOT NULL,
    updated_at       TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_suggestions_created_at
    ON suggestions (created_at);
CREATE INDEX IF NOT EXISTS idx_suggestions_status_hidden
    ON suggestions (status, is_hidden);

-- ── suggestion_images ─────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS suggestion_images (
    id             INTEGER PRIMARY KEY,
    suggestion_id  INTEGER NOT NULL,                   -- soft ref -> suggestions.id
    url            TEXT    NOT NULL,
    sort_order     INTEGER NOT NULL DEFAULT 0,
    created_at     TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_suggestion_images_suggestion
    ON suggestion_images (suggestion_id, sort_order);

-- ── suggestion_plus_ones (composite-key membership) ───────────────────────
CREATE TABLE IF NOT EXISTS suggestion_plus_ones (
    suggestion_id  INTEGER NOT NULL,                   -- soft ref -> suggestions.id
    employee_id    INTEGER NOT NULL,                   -- soft ref -> employees.id
    created_at     TEXT    NOT NULL,
    PRIMARY KEY (suggestion_id, employee_id)
);

-- ── suggestion_comments ───────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS suggestion_comments (
    id             INTEGER PRIMARY KEY,
    suggestion_id  INTEGER NOT NULL,                   -- soft ref -> suggestions.id
    employee_id    INTEGER NOT NULL,                   -- soft ref -> employees.id
    body           TEXT    NOT NULL DEFAULT '',
    created_at     TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_suggestion_comments_suggestion
    ON suggestion_comments (suggestion_id, created_at);

-- ── suggestion_comment_images ─────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS suggestion_comment_images (
    id          INTEGER PRIMARY KEY,
    comment_id  INTEGER NOT NULL,                      -- soft ref -> suggestion_comments.id
    url         TEXT    NOT NULL,
    sort_order  INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_suggestion_comment_images_comment
    ON suggestion_comment_images (comment_id, sort_order);

-- ── suggestion_settings (single-row config; seeded lazily in crud) ─────────
CREATE TABLE IF NOT EXISTS suggestion_settings (
    id                  INTEGER PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    notification_email  TEXT NOT NULL DEFAULT '',
    updated_at          TEXT
);
