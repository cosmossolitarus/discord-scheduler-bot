"""
Main bot entry point.
"""

import os
import asyncio
import logging
from datetime import datetime, timezone, timedelta

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

import discord
from discord.ext import commands, tasks

from bot.database import init_db, async_session
from bot.config import SCHEDULING_CHANNEL, SCHEDULE_LOG_CHANNEL, SCHEDULE_APPROVE_CHANNEL, CYCLE_LENGTH_DAYS, GUILD_ID
from bot.cycle import get_current_phase, get_current_cycle_day1, get_cycle_dates, Phase

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("scheduler")

intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)


def get_channel_by_name(guild: discord.Guild, name: str) -> discord.TextChannel | None:
    return discord.utils.get(guild.text_channels, name=name)


@bot.event
async def on_ready():
    logger.info(f"Logged in as {bot.user} (ID: {bot.user.id})")
    await init_db()
    logger.info("Database initialized")

    # Sync slash commands with Discord
    try:
        # Guild-specific sync is instant; global sync can take up to an hour
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


@tasks.loop(minutes=1)
async def lifecycle_loop():
    try:
        now = datetime.now(timezone.utc)
        phase, day1 = get_current_phase(now)
        dates = get_cycle_dates(day1)

        if phase == Phase.COLLECTING:
            submissions_cog = bot.get_cog("Submissions")
            if submissions_cog:
                await submissions_cog.open_submissions(day1)

        elif phase == Phase.LOCKED:
            scheduling_cog = bot.get_cog("Scheduling")
            if scheduling_cog:
                await scheduling_cog.lock_and_release(day1)

        elif phase == Phase.ACTIVE:
            reminders_cog = bot.get_cog("Reminders")
            if reminders_cog:
                await reminders_cog.check_reminders(now, day1)

        if now >= dates["archive"]:
            scheduling_cog = bot.get_cog("Scheduling")
            reminders_cog = bot.get_cog("Reminders")
            if scheduling_cog:
                await scheduling_cog.archive(day1)
            if reminders_cog:
                reminders_cog.clear_sent_reminders()

        # When phase is IDLE, get_current_cycle_day1 has already jumped to the
        # NEXT cycle, so the archive check above uses the wrong dates.
        # Fix: also check the immediately preceding cycle.
        if phase == Phase.IDLE:
            prev_day1 = day1 - timedelta(days=CYCLE_LENGTH_DAYS)
            prev_dates = get_cycle_dates(prev_day1)
            if now >= prev_dates["archive"]:
                scheduling_cog = bot.get_cog("Scheduling")
                reminders_cog = bot.get_cog("Reminders")
                if scheduling_cog:
                    await scheduling_cog.archive(prev_day1)
                if reminders_cog:
                    reminders_cog.clear_sent_reminders()

    except Exception as e:
        logger.error(f"Lifecycle loop error: {e}", exc_info=True)


@lifecycle_loop.before_loop
async def before_lifecycle():
    await bot.wait_until_ready()


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
