"""Async SQLite engine and the single unit-of-work entry point.

Concurrency strategy (see IMPLEMENTATION_NOTES.md):

* Every transaction starts with ``BEGIN IMMEDIATE`` so SQLite takes the write lock up front.
  A read-then-write race (two check-ins, two batch exports) therefore cannot interleave.
* Inside this process, transactions are additionally serialized by an ``asyncio.Lock`` so
  bursts of button clicks queue in Python instead of spinning on ``SQLITE_BUSY``.
* ``busy_timeout`` covers other processes (Alembic, a backup tool) touching the file.
* WAL journal mode keeps readers from blocking the writer.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from sqlalchemy import event
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

log = logging.getLogger(__name__)

BUSY_TIMEOUT_MS = 10_000


def _on_connect(dbapi_connection: Any, _record: Any) -> None:
    # Let SQLAlchemy (not the driver) decide when transactions begin, so BEGIN IMMEDIATE works.
    dbapi_connection.isolation_level = None
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA foreign_keys = ON")
        cursor.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
        cursor.execute("PRAGMA journal_mode = WAL")
        cursor.execute("PRAGMA synchronous = NORMAL")
    finally:
        cursor.close()


def _on_begin(connection: Any) -> None:
    connection.exec_driver_sql("BEGIN IMMEDIATE")


class Database:
    def __init__(self, url: str, *, echo: bool = False) -> None:
        self.url = url
        self.engine: AsyncEngine = create_async_engine(url, echo=echo)
        event.listen(self.engine.sync_engine, "connect", _on_connect)
        event.listen(self.engine.sync_engine, "begin", _on_begin)
        self._sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        self._lock = asyncio.Lock()

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[AsyncSession]:
        """One atomic unit of work. Commits on success, rolls back on any exception.

        Never nest calls: the in-process lock is not re-entrant.
        """
        async with self._lock, self._sessions() as session, session.begin():
            yield session

    async def dispose(self) -> None:
        await self.engine.dispose()
        log.info("Database connections closed")
