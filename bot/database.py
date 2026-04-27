"""
Database setup using async SQLAlchemy.

Uses PostgreSQL in production (via DATABASE_URL env var from Railway)
or SQLite locally as a fallback.
"""

import os

from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase


def get_database_url() -> str:
    """
    Build the async database URL.

    Railway sets DATABASE_URL as: postgresql://user:pass@host:port/dbname
    SQLAlchemy async needs:       postgresql+asyncpg://user:pass@host:port/dbname

    Falls back to local SQLite if DATABASE_URL is not set.
    """
    url = os.environ.get("DATABASE_URL")

    if url is None:
        return "sqlite+aiosqlite:///scheduler.db"

    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql+asyncpg://", 1)
    elif url.startswith("postgresql://"):
        url = url.replace("postgresql://", "postgresql+asyncpg://", 1)

    return url


DATABASE_URL = get_database_url()

engine = create_async_engine(DATABASE_URL, echo=False)
async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


async def init_db():
    """Create all tables if they don't exist."""
    from bot.models import Event, Submission, Slot, Assignment, ChangeRequest, AuditLog  # noqa
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def get_session() -> AsyncSession:
    async with async_session() as session:
        yield session
