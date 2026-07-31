"""Menu read-copy projection seam — DEFERRED implementation.

In the live system every version save/restore also republishes the chosen snapshot
to a fast document-store read copy (Firestore ``menu_items``) that the menu page and
kiosk read, and the very first tracked edit snapshots the *current* published state
as a revertable baseline.

The core has **no document-store adapter yet**, so this ships as a no-op seam:

* :meth:`MenuProjection.read_snapshot` returns ``{}`` (no external published state to
  baseline against), so the first-edit baseline row is simply skipped.
* :meth:`MenuProjection.publish` does nothing.

The Postgres source-of-truth (versions + recipes) is fully functional without it.
A later increment binds a real document-store-backed projection here — no domain-logic
change required, only a different :class:`MenuProjection` injected at the seam.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class MenuProjection(Protocol):
    """Read/write seam to the fast published menu read-copy (deferred backend)."""

    def read_snapshot(self, item_id: str, venue_id: str) -> dict:
        """Return the currently published content snapshot for an item (``{}`` if none)."""
        ...

    def publish(self, item_id: str, venue_id: str, snapshot: dict) -> None:
        """Publish *snapshot* to the read copy for an item (no-op until wired)."""
        ...


class NullMenuProjection:
    """No-op projection: no external published state, no publish side effect."""

    def read_snapshot(self, item_id: str, venue_id: str) -> dict:
        return {}

    def publish(self, item_id: str, venue_id: str, snapshot: dict) -> None:
        return None


__all__ = ["MenuProjection", "NullMenuProjection"]
