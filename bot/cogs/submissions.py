"""
Submissions cog.

Routes @mentions in #scheduling to the LLM agent based on event phase:
  COLLECTING — full submission flow (availability, screenshot, player ID, resources)
  LOCKED     — schedule is in admin review; players get a short holding message
  PUBLISHED  — change-request flow (move/drop/swap/etc.)
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

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot:
            return
        if message.guild is None:
            return
        if getattr(message.channel, "name", None) != SCHEDULING_CHANNEL:
            return
        if self.bot.user is None or not self.bot.user.mentioned_in(message):
            return

        async with async_session() as session:
            result = await session.execute(
                select(Event)
                .where(Event.phase.in_([
                    EventPhase.COLLECTING,
                    EventPhase.LOCKED,
                    EventPhase.PUBLISHED,
                ]))
                .order_by(Event.day1_date.desc())
            )
            event = result.scalars().first()

        if event is None:
            await message.reply(
                "There's no active event right now. An admin will open submissions "
                "when the next cycle begins."
            )
            return

        # During LOCKED, the schedule is in admin review — players can't submit
        # changes yet. Let them know it's coming soon.
        if event.phase == EventPhase.LOCKED:
            await message.reply(
                "The schedule is currently being reviewed by admins. "
                "It will be released to everyone shortly — check back soon!"
            )
            return

        async with message.channel.typing():
            try:
                reply = await process_user_message(message, event, self.bot)
            except Exception:
                logger.exception("agent.process_user_message crashed")
                reply = "🚨 Sorry, something went wrong handling your message. Please try again."

        if reply:
            if len(reply) > 1900:
                reply = reply[:1900] + "\n…(reply truncated)"
            needs_ping = "🚨" in reply
            await message.reply(reply, mention_author=needs_ping)

    async def announce_event_opened(self, event: Event):
        """Post the 'submissions are open' announcement in #scheduling."""
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
                f"• A screenshot of your in-game **Speedups** page\n"
                f"• Your available times for **Day 1, Day 2, and Day 4** "
                f"(in your local timezone or UTC — I'll convert)\n"
                f"• Your **in-game player ID** (the numeric ID visible in your profile)\n"
                f"• Optionally: your **TTG, TG, and Dust** counts "
                f"(e.g. 'I have 3 TTG and 50 TG') — these improve your scheduling priority\n\n"
                f"You can update any of the above at any time before the lock."
            )


async def setup(bot: commands.Bot):
    await bot.add_cog(Submissions(bot))
