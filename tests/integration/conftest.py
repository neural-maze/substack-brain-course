"""Shared fixtures for integration tests.

Real Postgres (references/testing.md: "Never mock the database"), wrapped in
one outer transaction per test that's always rolled back — every `get_session()`
call in the pipeline code joins the same connection via a SAVEPOINT
(`join_transaction_mode="create_savepoint"`, SQLAlchemy 2.0+), so an inner
`session.commit()` only commits the savepoint. Rolling back the outer
transaction at teardown undoes everything, so tests never leave residue and
can be re-run against the same dev database indefinitely.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Generator
from typing import TypeVar

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

import kb.db.session as db_session
from kb.config import settings

T = TypeVar("T")

_test_engine = create_async_engine(settings.database_url, poolclass=NullPool)


@pytest.fixture
async def db(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[AsyncSession]:
    async with _test_engine.connect() as connection:
        outer_transaction = await connection.begin()
        session_factory = async_sessionmaker(
            bind=connection,
            expire_on_commit=False,
            join_transaction_mode="create_savepoint",
        )
        monkeypatch.setattr(db_session, "async_session_factory", session_factory)

        async with session_factory() as session:
            try:
                yield session
            finally:
                await outer_transaction.rollback()


class SyncDB:
    session_factory: async_sessionmaker

    def __init__(
        self, loop: asyncio.AbstractEventLoop, session_factory: async_sessionmaker
    ) -> None:
        self._loop = loop
        self.session_factory = session_factory

    def run(self, coro: Awaitable[T]) -> T:
        return self._loop.run_until_complete(coro)


@pytest.fixture
def sync_db(monkeypatch: pytest.MonkeyPatch) -> Generator[SyncDB, None, None]:
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    connection_cm = _test_engine.connect()
    connection = loop.run_until_complete(connection_cm.__aenter__())
    outer_transaction = loop.run_until_complete(connection.begin())
    session_factory = async_sessionmaker(
        bind=connection,
        expire_on_commit=False,
        join_transaction_mode="create_savepoint",
    )
    monkeypatch.setattr(db_session, "async_session_factory", session_factory)

    try:
        yield SyncDB(loop, session_factory)
    finally:
        loop.run_until_complete(outer_transaction.rollback())
        loop.run_until_complete(connection_cm.__aexit__(None, None, None))
        loop.close()
        asyncio.set_event_loop(None)
