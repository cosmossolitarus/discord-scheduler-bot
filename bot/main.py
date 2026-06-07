"""
Main bot entry point and lifecycle loop.

All event lifecycle transitions are now admin-manual (no auto-transitions).
The lifecycle loop only handles:
  1. Personal 15-minute slot reminders and daily channel reminders
     (for LOCKED and PUBLISHED events).
  2. Expiration of overdue change requests.
"""

import asyncio
import logging
import os
from datetime import datetime, timezone

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

import discord
from discord.ext import commands, tasks
from sqlalchemy import select

from bot.config import GUILD_ID
from bot.database import async_session, init_db, migrate_db
from bot.models import Event, EventPhase

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("scheduler")

intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)


# ─── Lifecycle loop ──────────────────────────────────────────────


@tasks.loop(minutes=1)
async def lifecycle_loop():
    """Tick reminders and expire overdue change requests. No auto-transitions."""
    try:
        now = datetime.now(timezone.utc)

        async with async_session() as session:
            result = await session.execute(
                select(Event).where(
                    Event.phase.in_([EventPhase.LOCKED, EventPhase.PUBLISHED])
                )
            )
            active = list(result.scalars().all())

        reminders_cog = bot.get_cog("Reminders")
        if reminders_cog is not None:
            for event in active:
                await reminders_cog.check_reminders(now, event)

    except Exception as e:
        logger.error(f"Lifecycle loop error: {e}", exc_info=True)


@lifecycle_loop.before_loop
async def before_lifecycle():
    await bot.wait_until_ready()


# ─── Boot ────────────────────────────────────────────────────────


@bot.event
async def on_ready():
    logger.info(f"Logged in as {bot.user} (ID: {bot.user.id})")

    await init_db()
    await migrate_db()
    logger.info("Database initialized")

    try:
        if GUILD_ID:
            guild = discord.Object(id=int(GUILD_ID))
            bot.tree.copy_global_to(guild=guild)
            bot.tree.clear_commands(guild=None)
            await bot.tree.sync()
            synced = await bot.tree.sync(guild=guild)
            logger.info(f"Synced {len(synced)} slash command(s) to guild {GUILD_ID}")
        else:
            synced = await bot.tree.sync()
            logger.info(f"Synced {len(synced)} slash command(s) globally")
    except Exception as e:
        logger.error(f"Failed to sync slash commands: {e}")

    if not lifecycle_loop.is_running():
        lifecycle_loop.start()
    logger.info("Lifecycle loop started")


async def load_cogs():
    cog_modules = [
        "bot.cogs.submissions",
        "bot.cogs.scheduling",
        "bot.cogs.changes",
        "bot.cogs.reminders",
        "bot.cogs.admin",
    ]
    for module in cog_modules:
        try:
            await bot.load_extension(module)
            logger.info(f"Loaded cog: {module}")
        except Exception as e:
            logger.error(f"Failed to load cog {module}: {e}")


async def main():
    token = os.environ.get("DISCORD_BOT_TOKEN")
    if not token:
        raise ValueError("DISCORD_BOT_TOKEN environment variable not set")

    async with bot:
        await load_cogs()
        await bot.start(token)


if __name__ == "__main__":
    asyncio.run(main())
