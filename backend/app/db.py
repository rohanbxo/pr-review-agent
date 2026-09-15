from collections.abc import AsyncIterator

from sqlalchemy import JSON
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.config import get_settings

# JSONB on Postgres, plain JSON on SQLite so the test suite needs no container.
JSONType = JSONB().with_variant(JSON(), "sqlite")


class Base(DeclarativeBase):
    pass


_engine = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None


def get_engine():
    global _engine
    if _engine is None:
        _engine = create_async_engine(get_settings().database_url, pool_pre_ping=True)
    return _engine


def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    global _sessionmaker
    if _sessionmaker is None:
        _sessionmaker = async_sessionmaker(get_engine(), expire_on_commit=False)
    return _sessionmaker


def set_sessionmaker(maker: async_sessionmaker[AsyncSession]) -> None:
    """Used by tests and by background tasks that must share the test engine."""
    global _sessionmaker
    _sessionmaker = maker


async def get_session() -> AsyncIterator[AsyncSession]:
    async with get_sessionmaker()() as session:
        yield session
