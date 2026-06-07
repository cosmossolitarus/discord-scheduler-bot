"""
Reminders cog — daily channel announcements, personal 15-min DM warnings,
and expiration of overdue change requests.

Phase 3 changes:
  - The in-memory `_sent_reminders` set is gone; sent reminders are now
    persisted in the SentReminder table. A bot restart no longer re-sends.
  - When a swap change request expires (user B never confirmed in time),
    we DM both user A and user B so neither is left wondering.
"""

import logging
from datetime import datetime, timedelta

import discord
from discord.ext import commands
from sqlalchemy import select

from bot.config import PERSONAL_REMINDER_MINUTES, PLAYER_ROLE, SCHEDULING_CHANNEL
from bot.cycle import is_boundary_slot
from bot.database import async_session
from bot.models import (
    Assignment,
    ChangeRequest,
    ChangeStatus,
    Event,
    EventPhase,
    SentReminder,
    Slot,
)

logger = logging.getLogger("scheduler.reminders")


class Reminders(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ─── Lifecycle entry point ──────────────────────────────

    async def check_reminders(self, now: datetime, event: Event):
        """Per-tick reminder work for LOCKED and PUBLISHED events.

        Called by the lifecycle loop. Daily channel reminders and personal DMs
        are sent only during PUBLISHED (players don't know their slots until
        then). Change-request expiration runs regardless of phase.
        """
        event_id = event.event_id
        day1 = event.day1_date
        is_published = event.phase == EventPhase.PUBLISHED

        async with async_session() as session:
            if is_published:
                daily_reminder_times = {
                    1: day1 - timedelta(hours=1),
                    2: day1 + timedelta(hours=23),
                    4: day1 + timedelta(days=2, hours=23),
                }
                for game_day, reminder_time in daily_reminder_times.items():
                    if not (reminder_time <= now < reminder_time + timedelta(minutes=1)):
                        continue
                    if await self._already_sent(session, event_id, "daily", str(game_day)):
                        continue
                    if await self._send_daily_reminder(event_id, game_day):
                        session.add(SentReminder(
                            event_id=event_id, kind="daily", key=str(game_day),
                        ))

                target_start = now + timedelta(minutes=PERSONAL_REMINDER_MINUTES)
                target_end = target_start + timedelta(minutes=1)
                result = await session.execute(
                    select(Assignment, Slot)
                    .join(Slot, Assignment.slot_id == Slot.slot_id)
                    .where(
                        Assignment.event_id == event_id,
                        Slot.start_time >= target_start,
                        Slot.start_time < target_end,
                    )
                )
                for assignment, slot in result.all():
                    if await self._already_sent(session, event_id, "personal", slot.slot_id):
                        continue
                    await self._send_personal_reminder(assignment, slot)
                    session.add(SentReminder(
                        event_id=event_id, kind="personal", key=slot.slot_id,
                    ))

            await self._expire_overdue_changes(session, now)
            await session.commit()

    # ─── SentReminder bookkeeping ───────────────────────────

    async def _already_sent(
        self, session, event_id: int, kind: str, key: str
    ) -> bool:
        result = await session.execute(
            select(SentReminder).where(
                SentReminder.event_id == event_id,
                SentReminder.kind == kind,
                SentReminder.key == key,
            )
        )
        return result.scalar_one_or_none() is not None

    # ─── Reminder senders ───────────────────────────────────

    async def _send_daily_reminder(self, event_id: int, game_day: int) -> bool:
        """Post daily channel reminder. Returns True if it should be marked sent
        (always True, including when there are no assignments — we don't want
        to retry every minute).
        """
        async with async_session() as session:
            result = await session.execute(
                select(Assignment, Slot)
                .join(Slot, Assignment.slot_id == Slot.slot_id)
                .where(
                    Assignment.event_id == event_id,
                    Slot.day == game_day,
                )
            )
            assignments = result.all()

        if not assignments:
            return True  # nothing to announce, but don't retry

        for guild in self.bot.guilds:
            channel = discord.utils.get(guild.text_channels, name=SCHEDULING_CHANNEL)
            player_role = discord.utils.get(guild.roles, name=PLAYER_ROLE)
            if channel is None:
                continue
            track_label = " (Noble Advisor & Chief Minister)" if game_day == 4 else ""
            try:
                await channel.send(
                    f"{player_role.mention if player_role else ''} — "
                    f"**Day {game_day}{track_label} starts in one hour!** "
                    f"{len(assignments)} players are scheduled. "
                    f"You'll get a DM 15 minutes before your block."
                )
            except discord.HTTPException as e:
                logger.warning(f"Failed to post daily reminder: {e}")
        return True

    async def _send_personal_reminder(self, assignment: Assignment, slot: Slot):
        for guild in self.bot.guilds:
            member = guild.get_member(assignment.discord_id)
            if member is None:
                continue
            start_str = slot.start_time.strftime("%H:%M UTC")
            end_str = slot.end_time.strftime("%H:%M UTC")
            track_label = "Noble Advisor" if slot.track == "NA" else "Chief Minister"

            message = (
                f"⏰ Your Day {slot.day} block ({track_label}) starts in "
                f"{PERSONAL_REMINDER_MINUTES} minutes!\n"
                f"Time: {start_str} - {end_str}"
            )
            if is_boundary_slot(slot.slot_id):
                message += (
                    "\n\n**Boundary slot reminder.** Your 30-minute block "
                    "spans the Day 1 / Day 2 boundary. Use Day 1 "
                    "(construction) Speedups for the first 15 minutes "
                    "(23:45-00:00 UTC) and Day 2 (research) Speedups for "
                    "the last 15 minutes (00:00-00:15 UTC)."
                )

            try:
                await member.send(message)
            except discord.Forbidden:
                logger.warning(f"Could not DM reminder to {assignment.discord_id}")
            return  # one DM per member; don't loop across all guilds

    # ─── Change-request expiration ──────────────────────────

    async def _expire_overdue_changes(self, session, now: datetime):
        result = await session.execute(
            select(ChangeRequest).where(
                ChangeRequest.status == ChangeStatus.PENDING_CONFIRMATION,
                ChangeRequest.user_deadline != None,  # noqa: E711
                ChangeRequest.user_deadline <= now,
            )
        )
        for change in result.scalars().all():
            change.status = ChangeStatus.EXPIRED
            change.resolved_at = now
            await self._notify_expired(change)

        result = await session.execute(
            select(ChangeRequest).where(
                ChangeRequest.status == ChangeStatus.PENDING_ADMIN,
                ChangeRequest.admin_deadline != None,  # noqa: E711
                ChangeRequest.admin_deadline <= now,
            )
        )
        for change in result.scalars().all():
            change.status = ChangeStatus.EXPIRED
            change.resolved_at = now
            await self._notify_expired(change)

    async def _notify_expired(self, change: ChangeRequest):
        details = change.details or {}
        action = details.get("action", "request")
        day = details.get("day", "?")

        if action == "swap":
            user_a = details.get("user_a_id")
            user_b = details.get("user_b_id")
            if user_a is not None:
                await self._dm(
                    user_a,
                    f"⏰ Your swap request for Day {day} expired before it was completed.",
                )
            if user_b is not None:
                await self._dm(
                    user_b,
                    f"⏰ A swap request you were asked to confirm for Day {day} expired.",
                )
        else:
            await self._dm(
                change.requested_by,
                f"⏰ Your {action} request for Day {day} expired before admin review.",
            )

    async def _dm(self, user_id: int, content: str):
        try:
            user = self.bot.get_user(user_id) or await self.bot.fetch_user(user_id)
            await user.send(content)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            logger.warning(f"Could not DM user {user_id}")


async def setup(bot: commands.Bot):
    await bot.add_cog(Reminders(bot))
