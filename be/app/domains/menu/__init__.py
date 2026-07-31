"""Menu domain — Postgres source-of-truth for versioned menu content + recipes.

Layering mirrors the house style: ``schemas`` (pydantic), ``crud`` (``text()`` SQL
constants + async fns), ``service`` (LLM-backed helpers via the adapter seam),
``projection`` (the deferred read-copy seam), and ``router`` (thin endpoints).
"""

from be.app.domains.menu.router import router

__all__ = ["router"]
