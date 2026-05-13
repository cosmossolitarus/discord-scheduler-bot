"""
Database setup. PostgreSQL only (via Railway's DATABASE_URL).

There is no local SQLite fallback. Even local development requires a
Postgres connection string — point DATABASE_URL at a local Postgres
container if you need to test without Railway.
"""

import os

from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase


def _resolve_database_url() -> str:
    """Read DATABASE_URL and convert it to the async form SQLAlchemy needs."""
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError(
            "DATABASE_URL is not set. This bot requires PostgreSQL. "
            "On Railway, attaching a Postgres service sets this automatically; "
            "locally, point it at a Postgres instance you control."
        )

    # Railway hands out 'postgresql://' (sometimes 'postgres://'); asyncpg needs
    # 'postgresql+asyncpg://'.
    if url.startswith("postgres://"):
        url = "postgresql+asyncpg://" + url[len("postgres://"):]
    elif url.startswith("postgresql://") and "+asyncpg" not in url:
        url = "postgresql+asyncpg://" + url[len("postgresql://"):]

    return url


DATABASE_URL = _resolve_database_url()

engine = create_async_engine(DATABASE_URL, echo=False)
async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


async def init_db() -> None:
    """Create all tables if they don't exist. Idempotent."""
    # Importing models here avoids a circular import at module-load time.
    from bot.models import (  # noqa: F401
        Event,
        Submission,
        Slot,
        Assignment,
        ChangeRequest,
        AuditLog,
        SentReminder,
    )

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
