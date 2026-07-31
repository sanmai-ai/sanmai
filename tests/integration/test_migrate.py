"""Integration tests for the ledgered SQL migration runner (be/migrate.py).

Runs entirely on SQLite — no real Postgres required. Exercises the module both
as an importable library and as ``python -m be.migrate`` so the CLI contract
(non-zero exit on failure) is verified end to end.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text

from be import migrate

REPO_ROOT = Path(__file__).resolve().parents[2]


def _sqlite_url(db_path: Path) -> str:
    return f"sqlite:///{db_path}"


def _ledgered_versions(db_path: Path) -> list[str]:
    engine = create_engine(_sqlite_url(db_path), future=True)
    try:
        with engine.connect() as conn:
            rows = conn.execute(
                text("SELECT version FROM schema_migrations ORDER BY version")
            ).all()
        return [r[0] for r in rows]
    finally:
        engine.dispose()


def _run_cli(db_path: Path, mig_dir: Path, env_extra: dict | None = None) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env.pop("SANMAI_ALLOW_DESTRUCTIVE", None)
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "be.migrate",
            "--database-url",
            _sqlite_url(db_path),
            "--dir",
            str(mig_dir),
        ],
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
    )


def test_two_files_are_ledgered(tmp_path: Path) -> None:
    mig_dir = tmp_path / "migrations"
    mig_dir.mkdir()
    (mig_dir / "0001_first.sql").write_text(
        "CREATE TABLE widget (id INTEGER PRIMARY KEY, name TEXT);"
    )
    (mig_dir / "0002_second.sql").write_text(
        "CREATE TABLE gadget (id INTEGER PRIMARY KEY);\n"
        "INSERT INTO widget (id, name) VALUES (1, 'a');"
    )
    db_path = tmp_path / "app.db"

    result = _run_cli(db_path, mig_dir)
    assert result.returncode == 0, result.stderr

    assert _ledgered_versions(db_path) == ["0001", "0002"]

    # Re-run is idempotent: nothing new applied, still exit 0.
    rerun = _run_cli(db_path, mig_dir)
    assert rerun.returncode == 0, rerun.stderr
    assert _ledgered_versions(db_path) == ["0001", "0002"]


def test_broken_sql_exits_nonzero_and_stops(tmp_path: Path) -> None:
    mig_dir = tmp_path / "migrations"
    mig_dir.mkdir()
    (mig_dir / "0001_ok.sql").write_text("CREATE TABLE ok_tbl (id INTEGER PRIMARY KEY);")
    (mig_dir / "0002_broken.sql").write_text("CREATE TABLE (this is not valid sql;")
    db_path = tmp_path / "app.db"

    result = _run_cli(db_path, mig_dir)
    assert result.returncode != 0
    assert "FAILED" in (result.stderr + result.stdout).upper()

    # First (valid) migration committed in its own txn; broken one did NOT ledger.
    assert _ledgered_versions(db_path) == ["0001"]

    # Library-level call raises MigrationError (fail-loud contract).
    engine = create_engine(_sqlite_url(tmp_path / "lib.db"), future=True)
    try:
        migs = migrate.discover(mig_dir)
        with pytest.raises(migrate.MigrationError):
            migrate.apply_migrations(engine, migs)
    finally:
        engine.dispose()


def test_destructive_ddl_rejected_unless_opted_in(tmp_path: Path) -> None:
    mig_dir = tmp_path / "migrations"
    mig_dir.mkdir()
    (mig_dir / "0001_base.sql").write_text("CREATE TABLE keep_me (id INTEGER PRIMARY KEY);")
    (mig_dir / "0002_drop.sql").write_text("DROP TABLE keep_me;")

    # Without the opt-in env var: rejected, non-zero, drop NOT applied.
    db_path = tmp_path / "guarded.db"
    result = _run_cli(db_path, mig_dir)
    assert result.returncode != 0
    assert "destructive" in (result.stderr + result.stdout).lower()
    assert _ledgered_versions(db_path) == ["0001"]

    # With SANMAI_ALLOW_DESTRUCTIVE=1: allowed, both ledgered.
    db_path2 = tmp_path / "allowed.db"
    result2 = _run_cli(db_path2, mig_dir, env_extra={"SANMAI_ALLOW_DESTRUCTIVE": "1"})
    assert result2.returncode == 0, result2.stderr
    assert _ledgered_versions(db_path2) == ["0001", "0002"]


def test_is_destructive_ignores_comments() -> None:
    assert migrate.is_destructive("DROP TABLE foo;")
    assert migrate.is_destructive("ALTER TABLE foo DROP COLUMN bar;")
    assert not migrate.is_destructive("-- this will drop nothing\nCREATE TABLE foo (id INT);")
    assert not migrate.is_destructive("CREATE TABLE dropbox (id INT);")  # word-boundary
