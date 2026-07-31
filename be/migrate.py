"""Ledgered SQL migration runner (Track A).

Applies ``*.sql`` files from a directory in lexical order against a SQLAlchemy
engine. Each file runs in its own transaction and is recorded in a
``schema_migrations`` ledger. The runner **exits non-zero on the first
failure** so a Cloud Run Job / CI step fails loudly.

Design contract (frozen):

* Works on **sqlite** (tests) and **postgres** (prod) — no dialect-specific SQL
  in this file itself.
* ``schema_migrations(version TEXT PK, name TEXT, checksum TEXT, applied_at)``.
* A **Postgres advisory lock** is taken best-effort so two concurrent runners
  don't race; on sqlite (or if the lock call fails) it is silently skipped.
* Already-applied versions are skipped. If a file's checksum no longer matches
  the ledgered checksum, that is **drift** and fails loud.
* An **additive-only DDL linter** rejects ``DROP`` / ``ALTER ... DROP`` unless
  ``SANMAI_ALLOW_DESTRUCTIVE=1`` is set in the environment.

Runnable::

    python -m be.migrate --database-url sqlite:///./x.db --dir ./migrations
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import sys
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

# A stable 64-bit key for pg_advisory_lock — arbitrary but constant across runs.
_ADVISORY_LOCK_KEY = 0x5A4D_4149  # "ZMAI"

_LEDGER_DDL = text(
    """
    CREATE TABLE IF NOT EXISTS schema_migrations (
        version    TEXT PRIMARY KEY,
        name       TEXT NOT NULL,
        checksum   TEXT NOT NULL,
        applied_at TEXT NOT NULL
    )
    """
)


class MigrationError(RuntimeError):
    """Raised when a migration cannot be applied. Caller must exit non-zero."""


@dataclass(frozen=True)
class Migration:
    """A single migration file resolved from disk."""

    version: str
    name: str
    checksum: str
    sql: str
    path: Path


# --------------------------------------------------------------------------- #
# Additive-only DDL linter
# --------------------------------------------------------------------------- #

_COMMENT_LINE = re.compile(r"--[^\n]*")
_COMMENT_BLOCK = re.compile(r"/\*.*?\*/", re.DOTALL)
_DROP = re.compile(r"\bdrop\b", re.IGNORECASE)


def _strip_comments(sql: str) -> str:
    """Remove ``--`` line comments and ``/* */`` block comments."""
    return _COMMENT_LINE.sub("", _COMMENT_BLOCK.sub("", sql))


def is_destructive(sql: str) -> bool:
    """True if the SQL contains a destructive DDL verb (``DROP``, ``ALTER ... DROP``).

    Comments are stripped first so a ``-- drop`` note does not trip the linter.
    """
    return bool(_DROP.search(_strip_comments(sql)))


def _destructive_allowed() -> bool:
    return os.environ.get("SANMAI_ALLOW_DESTRUCTIVE") == "1"


# --------------------------------------------------------------------------- #
# SQL statement splitting (dialect-agnostic, respects quotes/comments)
# --------------------------------------------------------------------------- #


def split_statements(sql: str) -> list[str]:
    """Split a SQL script into individual statements on top-level ``;``.

    Honors single/double-quoted strings, ``--`` line comments, ``/* */`` block
    comments, and PostgreSQL ``$tag$ ... $tag$`` dollar-quoting so semicolons
    inside function bodies do not split a statement.
    """
    statements: list[str] = []
    buf: list[str] = []
    i = 0
    n = len(sql)
    in_single = in_double = False
    dollar_tag: str | None = None

    while i < n:
        ch = sql[i]
        two = sql[i : i + 2]

        if dollar_tag is not None:
            if sql.startswith(dollar_tag, i):
                buf.append(dollar_tag)
                i += len(dollar_tag)
                dollar_tag = None
                continue
            buf.append(ch)
            i += 1
            continue

        if in_single:
            buf.append(ch)
            in_single = ch != "'" or two == "''"
            if two == "''":
                buf.append(sql[i + 1])
                i += 2
                continue
            i += 1
            continue

        if in_double:
            buf.append(ch)
            if ch == '"':
                in_double = False
            i += 1
            continue

        if two == "--":
            end = sql.find("\n", i)
            end = n if end == -1 else end
            buf.append(sql[i:end])
            i = end
            continue

        if two == "/*":
            end = sql.find("*/", i)
            end = n if end == -1 else end + 2
            buf.append(sql[i:end])
            i = end
            continue

        if ch == "'":
            in_single = True
            buf.append(ch)
            i += 1
            continue

        if ch == '"':
            in_double = True
            buf.append(ch)
            i += 1
            continue

        if ch == "$":
            m = re.match(r"\$[A-Za-z_0-9]*\$", sql[i:])
            if m:
                dollar_tag = m.group(0)
                buf.append(dollar_tag)
                i += len(dollar_tag)
                continue

        if ch == ";":
            stmt = "".join(buf).strip()
            if stmt:
                statements.append(stmt)
            buf = []
            i += 1
            continue

        buf.append(ch)
        i += 1

    tail = "".join(buf).strip()
    if tail:
        statements.append(tail)
    return statements


# --------------------------------------------------------------------------- #
# Discovery
# --------------------------------------------------------------------------- #


def _parse_name(filename: str) -> tuple[str, str]:
    """Return ``(version, name)`` from a ``0001_baseline.sql`` filename."""
    stem = filename[:-4] if filename.lower().endswith(".sql") else filename
    if "_" in stem:
        version, name = stem.split("_", 1)
    else:
        version, name = stem, stem
    return version, name


def discover(directory: str | os.PathLike[str]) -> list[Migration]:
    """Load every ``*.sql`` file in *directory* in lexical filename order."""
    root = Path(directory)
    if not root.is_dir():
        raise MigrationError(f"migrations dir not found: {root}")
    out: list[Migration] = []
    for path in sorted(root.glob("*.sql")):
        sql = path.read_text(encoding="utf-8")
        version, name = _parse_name(path.name)
        checksum = hashlib.sha256(sql.encode("utf-8")).hexdigest()
        out.append(Migration(version=version, name=name, checksum=checksum, sql=sql, path=path))
    return out


# --------------------------------------------------------------------------- #
# Engine helpers
# --------------------------------------------------------------------------- #


def _is_postgres(engine: Engine) -> bool:
    return engine.dialect.name == "postgresql"


def _applied_versions(engine: Engine) -> dict[str, str]:
    """Return ``{version: checksum}`` already recorded in the ledger."""
    with engine.begin() as conn:
        conn.execute(_LEDGER_DDL)
    with engine.connect() as conn:
        rows = conn.execute(text("SELECT version, checksum FROM schema_migrations")).all()
    return {r[0]: r[1] for r in rows}


# --------------------------------------------------------------------------- #
# Apply
# --------------------------------------------------------------------------- #


def apply_migrations(
    engine: Engine,
    migrations: Sequence[Migration],
    *,
    allow_destructive: bool | None = None,
) -> list[str]:
    """Apply pending *migrations*. Returns the versions newly applied.

    Raises :class:`MigrationError` on the first problem (drift, destructive DDL,
    or a failing statement).
    """
    if allow_destructive is None:
        allow_destructive = _destructive_allowed()

    applied = _applied_versions(engine)
    newly: list[str] = []

    lock_conn = None
    if _is_postgres(engine):
        try:  # best-effort; never block the run on a lock error
            lock_conn = engine.connect()
            lock_conn.execute(text("SELECT pg_advisory_lock(:k)"), {"k": _ADVISORY_LOCK_KEY})
            lock_conn.commit()
        except Exception:
            if lock_conn is not None:
                lock_conn.close()
            lock_conn = None

    try:
        for mig in migrations:
            prior = applied.get(mig.version)
            if prior is not None:
                if prior != mig.checksum:
                    raise MigrationError(
                        f"checksum drift for {mig.version}: ledger={prior} file={mig.checksum}"
                    )
                continue  # already applied, unchanged

            if is_destructive(mig.sql) and not allow_destructive:
                raise MigrationError(
                    f"destructive DDL rejected in {mig.path.name}: set "
                    f"SANMAI_ALLOW_DESTRUCTIVE=1 to allow DROP/ALTER ... DROP"
                )

            try:
                with engine.begin() as conn:
                    for stmt in split_statements(mig.sql):
                        conn.execute(text(stmt))
                    conn.execute(
                        text(
                            "INSERT INTO schema_migrations (version, name, checksum, applied_at) "
                            "VALUES (:v, :n, :c, :t)"
                        ),
                        {
                            "v": mig.version,
                            "n": mig.name,
                            "c": mig.checksum,
                            "t": datetime.now(UTC).isoformat(),
                        },
                    )
            except MigrationError:
                raise
            except Exception as exc:  # noqa: BLE001 — surface any DB error as fail-loud
                raise MigrationError(f"migration {mig.path.name} failed: {exc}") from exc

            newly.append(mig.version)
    finally:
        if lock_conn is not None:
            try:
                lock_conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": _ADVISORY_LOCK_KEY})
                lock_conn.commit()
            except Exception:
                pass
            lock_conn.close()

    return newly


def run(database_url: str, directory: str | os.PathLike[str]) -> list[str]:
    """High-level entry: build engine, discover, apply. Returns applied versions."""
    engine = create_engine(database_url, future=True)
    try:
        migrations = discover(directory)
        return apply_migrations(engine, migrations)
    finally:
        engine.dispose()


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def _parse_args(argv: Iterable[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="be.migrate", description="Ledgered SQL migration runner")
    parser.add_argument("--database-url", required=True, help="SQLAlchemy DB URL")
    parser.add_argument("--dir", required=True, help="directory of *.sql migrations")
    return parser.parse_args(list(argv) if argv is not None else None)


def main(argv: Iterable[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        applied = run(args.database_url, args.dir)
    except MigrationError as exc:
        print(f"MIGRATION FAILED: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001 — fail loud, non-zero
        print(f"MIGRATION FAILED: {exc}", file=sys.stderr)
        return 1
    if applied:
        print("applied: " + ", ".join(applied))
    else:
        print("nothing to apply (up to date)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
