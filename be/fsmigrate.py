"""Firestore backfill migration ledger (Track B) — foundation skeleton.

The reviewers flagged that document-store backfills need the same fail-loud,
resumable, ledgered discipline as SQL migrations. This module provides that
skeleton with **no real Firestore dependency**: when no client is supplied it
uses an in-memory fake store, so the foundation and its tests run with zero
external credentials.

Contract:

* A **checkpoint** is a dict persisted per migration id in a
  ``firestore_migrations`` collection: ``{migration_id, last_key, processed,
  done, updated_at}``.
* ``--dry-run`` mutates **nothing** (neither documents nor the checkpoint).
* Backfills are **resumable by key**: on restart the runner resumes after
  ``last_key`` recorded in the checkpoint.
* Same **fail-loud** contract as :mod:`be.migrate` — any error exits non-zero.

Runnable::

    python -m be.fsmigrate --migration <id> [--dry-run]
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Protocol

_CHECKPOINT_COLLECTION = "firestore_migrations"


class FirestoreLike(Protocol):
    """Minimal surface a real ``google.cloud.firestore.Client`` also satisfies."""

    def get_document(self, collection: str, doc_id: str) -> dict | None: ...
    def set_document(self, collection: str, doc_id: str, data: dict) -> None: ...
    def list_documents(self, collection: str) -> list[tuple[str, dict]]: ...


class FsMigrationError(RuntimeError):
    """Raised on any backfill failure. Caller must exit non-zero."""


@dataclass
class InMemoryFirestore:
    """A tiny in-memory fake store used when no real client is provided."""

    data: dict[str, dict[str, dict]] = field(default_factory=dict)

    def get_document(self, collection: str, doc_id: str) -> dict | None:
        doc = self.data.get(collection, {}).get(doc_id)
        return dict(doc) if doc is not None else None

    def set_document(self, collection: str, doc_id: str, data: dict) -> None:
        self.data.setdefault(collection, {})[doc_id] = dict(data)

    def list_documents(self, collection: str) -> list[tuple[str, dict]]:
        return [(k, dict(v)) for k, v in sorted(self.data.get(collection, {}).items())]


@dataclass
class Checkpoint:
    """Resumable checkpoint for one backfill migration."""

    migration_id: str
    last_key: str | None = None
    processed: int = 0
    done: bool = False
    updated_at: str | None = None

    def to_dict(self) -> dict:
        return {
            "migration_id": self.migration_id,
            "last_key": self.last_key,
            "processed": self.processed,
            "done": self.done,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, d: dict) -> Checkpoint:
        return cls(
            migration_id=d["migration_id"],
            last_key=d.get("last_key"),
            processed=int(d.get("processed", 0)),
            done=bool(d.get("done", False)),
            updated_at=d.get("updated_at"),
        )


# A backfill is: a source of (key, doc) pairs + a transform producing an update
# dict (or None to skip). Foundation ships the runner; concrete backfills plug in.
SourceFn = Callable[[FirestoreLike], Iterable[tuple[str, dict]]]
TransformFn = Callable[[str, dict], dict | None]


def _load_checkpoint(store: FirestoreLike, migration_id: str) -> Checkpoint:
    raw = store.get_document(_CHECKPOINT_COLLECTION, migration_id)
    if raw is None:
        return Checkpoint(migration_id=migration_id)
    return Checkpoint.from_dict(raw)


def _save_checkpoint(store: FirestoreLike, cp: Checkpoint) -> None:
    cp.updated_at = datetime.now(UTC).isoformat()
    store.set_document(_CHECKPOINT_COLLECTION, cp.migration_id, cp.to_dict())


def run_backfill(
    store: FirestoreLike,
    migration_id: str,
    source: SourceFn,
    transform: TransformFn,
    *,
    collection: str,
    dry_run: bool = False,
) -> Checkpoint:
    """Run (or resume) a backfill.

    Iterates ``source`` in key order, skipping anything at or before the
    checkpoint's ``last_key`` (resume). For each item ``transform`` produces an
    update dict merged into the document; ``None`` skips it. On ``dry_run`` no
    documents and no checkpoint are written. Raises :class:`FsMigrationError`
    (fail-loud) on any transform/write error.
    """
    cp = _load_checkpoint(store, migration_id)
    if cp.done and not dry_run:
        return cp

    items = sorted(source(store), key=lambda kv: kv[0])
    resume_after = cp.last_key

    for key, doc in items:
        if resume_after is not None and key <= resume_after:
            continue
        try:
            update = transform(key, doc)
        except Exception as exc:  # noqa: BLE001 — fail loud
            raise FsMigrationError(f"transform failed for {key!r}: {exc}") from exc

        if update is not None and not dry_run:
            merged = {**doc, **update}
            store.set_document(collection, key, merged)

        cp.processed += 1
        cp.last_key = key
        if not dry_run:
            _save_checkpoint(store, cp)

    if not dry_run:
        cp.done = True
        _save_checkpoint(store, cp)
    return cp


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def _noop_source(_store: FirestoreLike) -> Iterable[tuple[str, dict]]:
    return []


def _noop_transform(_key: str, _doc: dict) -> dict | None:
    return None


def _parse_args(argv: Iterable[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="be.fsmigrate", description="Firestore backfill ledger (skeleton)"
    )
    parser.add_argument("--migration", required=True, help="migration id")
    parser.add_argument(
        "--collection", default="documents", help="target collection to backfill"
    )
    parser.add_argument("--dry-run", action="store_true", help="mutate nothing")
    return parser.parse_args(list(argv) if argv is not None else None)


def main(argv: Iterable[str] | None = None) -> int:
    args = _parse_args(argv)
    # Foundation has no real Firestore client; use the in-memory fake. Real
    # deployments inject a client + registered source/transform for the id.
    store: FirestoreLike = InMemoryFirestore()
    try:
        cp = run_backfill(
            store,
            args.migration,
            _noop_source,
            _noop_transform,
            collection=args.collection,
            dry_run=args.dry_run,
        )
    except FsMigrationError as exc:
        print(f"FS MIGRATION FAILED: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001 — fail loud, non-zero
        print(f"FS MIGRATION FAILED: {exc}", file=sys.stderr)
        return 1
    mode = "dry-run" if args.dry_run else "applied"
    print(f"{mode}: migration={cp.migration_id} processed={cp.processed} done={cp.done}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
