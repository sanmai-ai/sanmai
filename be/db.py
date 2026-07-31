"""Async request-path database seam (the one runtime DB session for domains).

``be.migrate`` remains the **synchronous** DDL tool (applies ``*.sql`` files). This
module is its runtime counterpart: it builds an :class:`AsyncEngine` and an
``async_sessionmaker`` from :data:`Settings.database_url` and exposes a FastAPI
dependency (:func:`get_session`) plus a plain :func:`session_scope` context manager
for scripts/tests.

The configured URL uses a *sync* dialect (``sqlite:///…`` in tests,
``postgresql+asyncpg://…`` or ``postgresql://…`` in prod). :func:`to_async_url`
coerces it to an async driver (``sqlite+aiosqlite``/``postgresql+asyncpg``) so the
same ``SANMAI_DATABASE_URL`` drives both the migration tool and the request path.

Engines are cached per-process (``lru_cache``) so config is honoured once; tests
that need an isolated database build their own engine via :func:`make_sessionmaker`.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from functools import lru_cache

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from be.config import get_settings


def to_async_url(url: str) -> str:
    """Coerce a sync SQLAlchemy URL to its async-driver equivalent.

    ``sqlite:///x`` -> ``sqlite+aiosqlite:///x``; ``postgresql://`` /
    ``postgres://`` -> ``postgresql+asyncpg://``. URLs that already name an async
    driver (or an unrecognised scheme) are returned unchanged.
    """
    if url.startswith("sqlite+"):
        return url
    if url.startswith("sqlite:"):
        return "sqlite+aiosqlite:" + url[len("sqlite:") :]
    if url.startswith("postgresql+"):
        return url
    if url.startswith("postgresql://"):
        return "postgresql+asyncpg://" + url[len("postgresql://") :]
    if url.startswith("postgres://"):
        return "postgresql+asyncpg://" + url[len("postgres://") :]
    return url


def make_engine(url: str) -> AsyncEngine:
    """Build a fresh :class:`AsyncEngine` from a sync-or-async URL (no caching)."""
    return create_async_engine(to_async_url(url), future=True)


def make_sessionmaker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Build an ``async_sessionmaker`` bound to *engine* (``expire_on_commit=False``)."""
    return async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


@lru_cache(maxsize=1)
def get_engine() -> AsyncEngine:
    """Process-wide cached :class:`AsyncEngine` built from configured settings."""
    return make_engine(get_settings().database_url)


@lru_cache(maxsize=1)
def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    """Process-wide cached sessionmaker bound to the configured engine."""
    return make_sessionmaker(get_engine())


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    """Async context manager yielding a session from the configured sessionmaker."""
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        yield session


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency: yield a request-scoped :class:`AsyncSession`."""
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        yield session


def reset_engine_cache() -> None:
    """Clear the cached engine/sessionmaker (used by tests that reconfigure env)."""
    get_sessionmaker.cache_clear()
    get_engine.cache_clear()


__all__ = [
    "to_async_url",
    "make_engine",
    "make_sessionmaker",
    "get_engine",
    "get_sessionmaker",
    "session_scope",
    "get_session",
    "reset_engine_cache",
]
