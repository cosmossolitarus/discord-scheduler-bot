"""
Submissions cog.

Handles user @mentions in #scheduling and dispatches to the action-pattern
agent. The agent itself picks the right tool set based on the event's
phase — this cog just routes.

Also exposes announce_event_opened(event), which main.py and the admin
/schedule test command call when a new event is created.
"""

import logging

import discord
from discord.ext import commands
from sqlalchemy import select

from bot.config import PLAYER_ROLE, SCHEDULING_CHANNEL
from bot.database import async_session
from bot.llm.agent import process_user_message
from bot.models import Event, EventPhase

logger = logging.getLogger("scheduler.submissions")


class Submissions(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ─── on_message: handle @mentions in #scheduling ─────────

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot:
            return
        if message.guild is None:
            return  # DMs handled by Changes cog's reaction listener
        if getattr(message.channel, "name", None) != SCHEDULING_CHANNEL:
            return
        if self.bot.user is None or not self.bot.user.mentioned_in(message):
            return

        async with async_session() as session:
            result = await session.execute(
                select(Event)
                .where(Event.phase != EventPhase.ARCHIVED)
                .order_by(Event.day1_date.desc())
            )
            event = result.scalars().first()

        if event is None:
            await message.reply(
                "There's no active event right now. I'll announce in this channel "
                "when submissions open."
            )
            return

        if event.phase not in (EventPhase.COLLECTING, EventPhase.LOCKED):
            # ARCHIVED already filtered above; this shouldn't fire, but guard.
            logger.warning(f"Unexpected event phase {event.phase} for live message handling")
            return

        async with message.channel.typing():
            try:
                reply = await process_user_message(message, event, self.bot)
            except Exception:
                logger.exception("agent.process_user_message crashed")
                reply = "🚨 Sorry, something went wrong handling your message. Please try again."

        if reply:
            # Discord messages cap at 2000 chars. Truncate cleanly if needed.
            if len(reply) > 1900:
                reply = reply[:1900] + "\n…(reply truncated)"
            await message.reply(reply, mention_author=False)

    # ─── Lifecycle hook: post the opening announcement ──────

    async def announce_event_opened(self, event: Event):
        """Post the 'submissions are open' announcement in #scheduling.

        Called by main.py's lifecycle loop after creating an event, and by
        /schedule test after creating a test event.
        """
        day1 = event.day1_date
        for guild in self.bot.guilds:
            channel = discord.utils.get(guild.text_channels, name=SCHEDULING_CHANNEL)
            player_role = discord.utils.get(guild.roles, name=PLAYER_ROLE)
            if channel is None:
                continue

            day1_str = day1.strftime("%A, %B %d, %Y")
            test_tag = " — **TEST EVENT** (manual control only)" if event.is_test else ""

            mention = player_role.mention if player_role else ""
            await channel.send(
                f"{mention} — Submissions are open!{test_tag}\n\n"
                f"**Day 1: {day1_str}**\n\n"
                f"@mention me here with:\n"
                f"• A screenshot of your in-game **Resources & Speedups** page\n"
                f"• Your available times for **Day 1, Day 2, and Day 4** "
                f"(in your local timezone or UTC — I'll convert)\n\n"
                f"You can update either at any time before the lock. "
                f"I'll confirm what I understood from each message."
            )


async def setup(bot: commands.Bot):
    await bot.add_cog(Submissions(bot))
