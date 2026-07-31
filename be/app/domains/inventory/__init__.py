"""Inventory domain — Postgres source-of-truth for items, stock, suppliers,
purchasing, and the order-import learned-alias memory.

Layering mirrors the menu domain: ``schemas`` (pydantic), ``crud`` (``text()`` SQL
constants + async fns), ``service`` (order-import via the LLM adapter seam),
``projection`` (the deferred read-copy seam), and ``router`` (thin endpoints).
"""

from be.app.domains.inventory.router import router

__all__ = ["router"]
