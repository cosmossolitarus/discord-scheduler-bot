"""
Database setup. PostgreSQL only (via Railway's DATABASE_URL).
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
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError(
            "DATABASE_URL is not set. This bot requires PostgreSQL. "
            "On Railway, attaching a Postgres service sets this automatically; "
            "locally, point it at a Postgres instance you control."
        )

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
    from bot.models import (  # noqa: F401
        Event,
        PlayerProfile,
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
    """Apply one-time schema changes. Safe to run on every boot."""
    renames = [
        ("resource_x",       "speedup_construction"),
        ("resource_y",       "speedup_research"),
        ("resource_z",       "speedup_training"),
        ("resource_generic", "speedup_general"),
    ]

    # Enum additions: list (type_name, member_name) for each new value.
    # member_name must match the Python enum member name (not the .value).
    enum_additions = [
        ("changetype",  "ADD"),       # request_new_slot
        ("eventphase",  "PUBLISHED"), # uppercase: SQLAlchemy stores enum .name not .value
    ]

    # New nullable columns added to existing tables.
    # Tuples of (table, column, pg_type).
    new_columns = [
        ("submissions", "ttg",               "double precision"),
        ("submissions", "tg",                "double precision"),
        ("submissions", "dust",              "double precision"),
        ("submissions", "player_ingame_id",  "varchar(20)"),
        ("submissions", "has_player_id",     "boolean DEFAULT false NOT NULL"),
        ("submissions", "has_resources",     "boolean DEFAULT false NOT NULL"),
        ("events",      "published_at",      "timestamptz"),
    ]

    async with engine.begin() as conn:
        # Column renames (legacy)
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

        # Enum value additions
        for type_name, value in enum_additions:
            await conn.execute(
                text(f"ALTER TYPE {type_name} ADD VALUE IF NOT EXISTS '{value}'")
            )

        # New columns (idempotent via information_schema check)
        for table, col, pg_type in new_columns:
            result = await conn.execute(
                text(
                    "SELECT 1 FROM information_schema.columns "
                    "WHERE table_name = :tbl AND column_name = :col"
                ),
                {"tbl": table, "col": col},
            )
            if not result.fetchone():
                await conn.execute(
                    text(f"ALTER TABLE {table} ADD COLUMN {col} {pg_type}")
                )
