"""
Reminders cog — daily and personal reminders, expiring changes.
"""

import logging
from datetime import datetime, timezone, timedelta

import discord
from discord.ext import commands
from sqlalchemy import select

from bot.config import PLAYER_ROLE, SCHEDULING_CHANNEL, PERSONAL_REMINDER_MINUTES
from bot.database import async_session
from bot.models import (
    Event, Slot, Assignment, ChangeRequest,
    EventPhase, AssignmentStatus, ChangeStatus,
)

logger = logging.getLogger("scheduler.reminders")


class Reminders(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._sent_reminders: set[tuple] = set()

    async def check_reminders(self, now: datetime, day1: datetime):
        async with async_session() as session:
            result = await session.execute(
                select(Event).where(Event.day1_date == day1)
            )
            event = result.scalar_one_or_none()
            if event is None or event.phase == EventPhase.ARCHIVED:
                return

            daily_reminder_times = {
                1: day1 - timedelta(hours=1),
                2: day1 + timedelta(hours=23),
                4: day1 + timedelta(days=2, hours=23),
            }

            for game_day, reminder_time in daily_reminder_times.items():
                key = (event.event_id, "daily", game_day)
                if key not in self._sent_reminders:
                    if reminder_time <= now < reminder_time + timedelta(minutes=1):
                        await self._send_daily_reminder(event, game_day)
                        self._sent_reminders.add(key)

            target_start = now + timedelta(minutes=PERSONAL_REMINDER_MINUTES)
            target_end = target_start + timedelta(minutes=1)

            result = await session.execute(
                select(Assignment, Slot)
                .join(Slot, Assignment.slot_id == Slot.slot_id)
                .where(
                    Assignment.event_id == event.event_id,
                    Assignment.status == AssignmentStatus.ASSIGNED,
                    Slot.start_time >= target_start,
                    Slot.start_time < target_end,
                )
            )
            upcoming = result.all()

            for assignment, slot in upcoming:
                key = (event.event_id, "personal", slot.slot_id)
                if key not in self._sent_reminders:
                    await self._send_personal_reminder(assignment, slot)
                    self._sent_reminders.add(key)

            await self._expire_overdue_changes(session, now)

    async def _send_daily_reminder(self, event, game_day):
        async with async_session() as session:
            result = await session.execute(
                select(Assignment, Slot)
                .join(Slot, Assignment.slot_id == Slot.slot_id)
                .where(
                    Assignment.event_id == event.event_id,
                    Assignment.status == AssignmentStatus.ASSIGNED,
                    Slot.day == game_day,
                )
            )
            assignments = result.all()

        if not assignments:
            return

        for guild in self.bot.guilds:
            channel = discord.utils.get(guild.text_channels, name=SCHEDULING_CHANNEL)
            player_role = discord.utils.get(guild.roles, name=PLAYER_ROLE)
            if channel:
                track_label = " (Noble Advisor & Chief Minister)" if game_day == 4 else ""
                await channel.send(
                    f"{player_role.mention if player_role else ''} — "
                    f"**Day {game_day}{track_label} starts tonight!** "
                    f"{len(assignments)} players are scheduled. "
                    f"You'll get a personal DM 15 minutes before your block."
                )

    async def _send_personal_reminder(self, assignment, slot):
        for guild in self.bot.guilds:
            member = guild.get_member(assignment.discord_id)
            if member is None:
                continue
            start_str = slot.start_time.strftime("%H:%M UTC")
            end_str = slot.end_time.strftime("%H:%M UTC")
            track_label = "Noble Advisor" if slot.track == "NA" else "Chief Minister"
            try:
                await member.send(
                    f"⏰ Your Day {slot.day} block ({track_label}) starts in "
                    f"{PERSONAL_REMINDER_MINUTES} minutes!\n"
                    f"Time: {start_str} - {end_str}"
                )
            except discord.Forbidden:
                logger.warning(f"Could not DM reminder to {assignment.discord_id}")

    async def _expire_overdue_changes(self, session, now):
        result = await session.execute(
            select(ChangeRequest).where(
                ChangeRequest.status == ChangeStatus.PENDING_CONFIRMATION,
                ChangeRequest.user_deadline != None,
                ChangeRequest.user_deadline <= now,
            )
        )
        for change in result.scalars().all():
            change.status = ChangeStatus.EXPIRED
            change.resolved_at = now

        result = await session.execute(
            select(ChangeRequest).where(
                ChangeRequest.status == ChangeStatus.PENDING_ADMIN,
                ChangeRequest.admin_deadline != None,
                ChangeRequest.admin_deadline <= now,
            )
        )
        for change in result.scalars().all():
            change.status = ChangeStatus.EXPIRED
            change.resolved_at = now

        await session.commit()

    def clear_sent_reminders(self):
        self._sent_reminders.clear()


async def setup(bot: commands.Bot):
    await bot.add_cog(Reminders(bot))
