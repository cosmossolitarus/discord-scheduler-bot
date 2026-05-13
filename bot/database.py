"""
Database setup. PostgreSQL only (via Railway's DATABASE_URL).

There is no local SQLite fallback. Even local development requires a
Postgres connection string — point DATABASE_URL at a local Postgres
container if you need to test without Railway.
"""

import os

from sqlalchemy import text
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


async def migrate_db() -> None:
    """Apply one-time column renames and enum-value additions. Safe to run on
    every boot — skips renames where the old column no longer exists, and
    enum value adds use IF NOT EXISTS so they're no-ops if already applied."""
    renames = [
        ("resource_x",       "speedup_construction"),
        ("resource_y",       "speedup_research"),
        ("resource_z",       "speedup_training"),
        ("resource_generic", "speedup_general"),
    ]

    # Postgres enums don't get extended by Base.metadata.create_all — that only
    # creates them fresh. Adding a new ChangeType / ChangeStatus / EventPhase
    # member in models.py requires an explicit ALTER TYPE. List each one here
    # the first time it's added so existing DBs pick it up on next boot.
    # SQLAlchemy stores enum members by .name, so values listed below should
    # match the Python member name (e.g. "ADD", not "add").
    enum_additions = [
        ("changetype", "ADD"),  # request_new_slot — added when post-lock new-slot requests were introduced
    ]

    async with engine.begin() as conn:
        for old, new in renames:
            result = await conn.execute(
                text(
                    "SELECT 1 FROM information_schema.columns "
                    "WHERE table_name = 'submissions' AND column_name = :col"
                ),
                {"col": old},
            )
            if result.fetchone():
                await conn.execute(
                    text(f"ALTER TABLE submissions RENAME COLUMN {old} TO {new}")
                )

        for type_name, value in enum_additions:
            # ALTER TYPE ... ADD VALUE IF NOT EXISTS requires PG 9.6+. Railway
            # Postgres is modern enough. The IF NOT EXISTS avoids errors when
            # the value is already present (e.g. fresh DB where create_all
            # built the enum with the full set).
            await conn.execute(
                text(f"ALTER TYPE {type_name} ADD VALUE IF NOT EXISTS '{value}'")
            )