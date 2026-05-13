"""
Main bot entry point and lifecycle loop.

The lifecycle loop reads from the Event table to drive cycle state. This
replaces the older wall-clock-derived approach (get_current_phase /
get_current_cycle_day1) that could silently skip past an archived cycle
into the next one before its submissions had opened.

Test events (is_test=True) are exempt from auto-transition; admin slash
commands drive them.
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
from bot.cycle import compute_active_cycle_day1, should_archive, should_lock
from bot.database import async_session, init_db, migrate_db
from bot.events import create_event
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
    """Drive cycle state. Ticks every minute.

    Algorithm:
      1. Read all non-archived events from the DB.
      2. For each non-test event, auto-transition if its wall-clock offset is
         due (COLLECTING -> LOCKED, LOCKED -> ARCHIVED).
      3. For each LOCKED event, let the reminders cog do its per-tick work.
      4. If no non-archived event exists AND the natural cycle window is open,
         create a new real event in COLLECTING and tell the submissions cog
         to announce it.

    A non-archived TEST event blocks step 4 — no real event is auto-created
    while a test event is active. The admin must `/schedule reset` first.
    """
    try:
        now = datetime.now(timezone.utc)

        async with async_session() as session:
            result = await session.execute(
                select(Event).where(Event.phase != EventPhase.ARCHIVED)
            )
            active = list(result.scalars().all())

            # 1. Auto-transition non-test events that are due.
            for event in active:
                if event.is_test:
                    continue

                if event.phase == EventPhase.COLLECTING and should_lock(event.day1_date, now):
                    scheduling_cog = bot.get_cog("Scheduling")
                    if scheduling_cog is not None:
                        await scheduling_cog.lock_and_release(event)

                elif event.phase == EventPhase.LOCKED and should_archive(event.day1_date, now):
                    scheduling_cog = bot.get_cog("Scheduling")
                    if scheduling_cog is not None:
                        await scheduling_cog.archive(event)

            # 2. Tick reminders for each LOCKED event.
            reminders_cog = bot.get_cog("Reminders")
            if reminders_cog is not None:
                for event in active:
                    if event.phase == EventPhase.LOCKED:
                        await reminders_cog.check_reminders(now, event)

            # 3. Create a new real event if none active and the cycle window is open.
            if not active:
                day1 = compute_active_cycle_day1(now)
                if day1 is not None:
                    event = await create_event(session, day1, is_test=False)
                    await session.commit()
                    logger.info(f"Created new real event for Day 1 = {day1.date()}")

                    submissions_cog = bot.get_cog("Submissions")
                    if submissions_cog is not None and hasattr(
                        submissions_cog, "announce_event_opened"
                    ):
                        await submissions_cog.announce_event_opened(event)

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
            synced = await bot.tree.sync(guild=guild)
            logger.info(f"Synced {len(synced)} slash command(s) to guild {GUILD_ID}")
        global_synced = await bot.tree.sync()
        logger.info(f"Synced {len(global_synced)} slash command(s) globally")
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
