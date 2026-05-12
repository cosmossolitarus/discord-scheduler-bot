"""
Scheduling cog.

Handles:
    - Locking submissions and running the optimizer
    - Generating schedule CSVs
    - Releasing the schedule to players
    - Archiving the final schedule
"""

import io
import csv
import logging
from datetime import datetime, timezone

import discord
from discord.ext import commands
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from bot.config import (
    ADMIN_ROLE, PLAYER_ROLE,
    SCHEDULING_CHANNEL, SCHEDULE_LOG_CHANNEL,
)
from bot.database import async_session
from bot.models import (
    Event, Submission, Slot, Assignment, AuditLog,
    EventPhase, AssignmentStatus,
)
from bot.cycle import get_current_cycle_day1, get_cycle_dates, generate_slot_times
from bot.optimizer.solver import (
    optimize_pass, run_full_optimization,
    SlotForOptimizer, AssignmentResult,
)

logger = logging.getLogger("scheduler.scheduling")


class Scheduling(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def lock_and_release(self, day1: datetime):
        """
        Lock submissions, run optimizer, notify players.
        Called by lifecycle loop. Idempotent.
        """
        async with async_session() as session:
            result = await session.execute(
                select(Event).where(Event.day1_date == day1)
            )
            event = result.scalar_one_or_none()
            if event is None or event.phase != EventPhase.COLLECTING:
                return  # Not ready or already locked

            # Lock
            event.phase = EventPhase.LOCKED

            # Fetch all complete submissions
            result = await session.execute(
                select(Submission).where(
                    Submission.event_id == event.event_id,
                    Submission.has_screenshot == True,
                    Submission.has_availability == True,
                )
            )
            submissions = list(result.scalars().all())

            # Fetch all slots
            result = await session.execute(
                select(Slot).where(Slot.event_id == event.event_id)
            )
            all_slots = list(result.scalars().all())

            # Organize slots by pass
            slots_by_pass = {
                "D1-CM": [],
                "D2-CM": [],
                "D4-NA": [],
                "D4-CM": [],
            }
            for slot in all_slots:
                key = f"D{slot.day}-{slot.track}"
                if key in slots_by_pass:
                    slots_by_pass[key].append(
                        SlotForOptimizer(slot_id=slot.slot_id, slot_index=slot.slot_index)
                    )

            # Prepare submission data for optimizer
            sub_data = [
                {
                    "discord_id": s.discord_id,
                    "priority_x": s.priority_x or 0,
                    "priority_y": s.priority_y or 0,
                    "priority_z": s.priority_z or 0,
                    "availability": set(s.availability or []),
                }
                for s in submissions
            ]

            # Run optimizer
            results = run_full_optimization(sub_data, slots_by_pass)

            # Clear existing assignments for this event
            await session.execute(
                Assignment.__table__.delete().where(
                    Assignment.event_id == event.event_id
                )
            )

            # Create assignments from optimizer results
            assigned_users = set()
            for pass_name, assignments in results.items():
                if pass_name == "boundary":
                    # The boundary player is assigned to D1-CM-49 already.
                    # We don't create a separate D2-BOUNDARY slot assignment.
                    continue
                for a in assignments:
                    assignment = Assignment(
                        event_id=event.event_id,
                        slot_id=a.slot_id,
                        discord_id=a.discord_id,
                        status=AssignmentStatus.ASSIGNED,
                    )
                    session.add(assignment)
                    assigned_users.add(a.discord_id)

            # Audit log
            session.add(AuditLog(
                event_id=event.event_id,
                action="Schedule locked and optimized",
                actor="system",
                details={
                    "total_submissions": len(submissions),
                    "complete_submissions": len(sub_data),
                    "assigned_users": len(assigned_users),
                    "assignments_by_pass": {k: len(v) for k, v in results.items()},
                },
            ))

            await session.commit()

        # Generate and post CSV
        csv_file = await self._generate_csv(day1)

        # Post to #schedule_log
        for guild in self.bot.guilds:
            log_channel = discord.utils.get(guild.text_channels, name=SCHEDULE_LOG_CHANNEL)
            admin_role = discord.utils.get(guild.roles, name=ADMIN_ROLE)
            scheduling_channel = discord.utils.get(guild.text_channels, name=SCHEDULING_CHANNEL)
            player_role = discord.utils.get(guild.roles, name=PLAYER_ROLE)

            if log_channel and admin_role:
                await log_channel.send(
                    f"{admin_role.mention} — Schedule locked and optimized.\n"
                    f"{len(assigned_users)} users assigned across {sum(len(v) for v in results.values())} slots.",
                    file=discord.File(csv_file, filename=f"schedule_{day1.date()}.csv"),
                )

            # Notify players via DM
            await self._notify_players(guild, event, submissions, assigned_users)

            # Announce in #scheduling
            if scheduling_channel and player_role:
                day1_str = day1.strftime("%A, %B %d, %Y")
                await scheduling_channel.send(
                    f"{player_role.mention} — The schedule for **{day1_str}** "
                    f"has been released! Check your DMs for your assigned times.\n\n"
                    f"To request changes, @mention me in this channel with "
                    f"what you need."
                )

    async def _notify_players(
        self,
        guild: discord.Guild,
        event: Event,
        submissions: list[Submission],
        assigned_users: set[int],
    ):
        """DM each player their assignment (or waitlist status)."""
        async with async_session() as session:
            result = await session.execute(
                select(Assignment, Slot)
                .join(Slot, Assignment.slot_id == Slot.slot_id)
                .where(Assignment.event_id == event.event_id)
                .order_by(Slot.day, Slot.track.desc(), Slot.slot_index)
            )
            all_assignments = result.all()

        # Group assignments by user
        user_assignments: dict[int, list[tuple[Assignment, Slot]]] = {}
        for assignment, slot in all_assignments:
            user_assignments.setdefault(assignment.discord_id, []).append((assignment, slot))

        for submission in submissions:
            member = guild.get_member(submission.discord_id)
            if member is None:
                continue

            try:
                if submission.discord_id in assigned_users:
                    # Build assignment summary
                    lines = ["**Your scheduled times:**\n"]
                    for assignment, slot in user_assignments.get(submission.discord_id, []):
                        start_str = slot.start_time.strftime("%a %b %d, %H:%M")
                        end_str = slot.end_time.strftime("%H:%M UTC")
                        track_label = "Noble Advisor" if slot.track == "NA" else "Chief Minister"
                        lines.append(f"• Day {slot.day} ({track_label}): {start_str} - {end_str}")
                    await member.send("\n".join(lines))
                else:
                    await member.send(
                        "You were not assigned a slot for this event. "
                        "You're on the waitlist — I'll notify you if a spot opens up."
                    )
            except discord.Forbidden:
                logger.warning(f"Could not DM user {submission.discord_id} — DMs may be disabled.")

    async def _generate_csv(self, day1: datetime) -> io.BytesIO:
        """Generate a CSV of the current schedule."""
        async with async_session() as session:
            result = await session.execute(
                select(Event).where(Event.day1_date == day1)
            )
            event = result.scalar_one_or_none()
            if event is None:
                return io.BytesIO(b"No event found")

            result = await session.execute(
                select(Assignment, Slot, Submission)
                .join(Slot, Assignment.slot_id == Slot.slot_id)
                .join(
                    Submission,
                    (Submission.event_id == Assignment.event_id) &
                    (Submission.discord_id == Assignment.discord_id),
                )
                .where(Assignment.event_id == event.event_id)
                .order_by(Slot.day, Slot.track.desc(), Slot.slot_index)
            )
            rows = result.all()

        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(["Day", "Minister Position", "Player", "Time"])

        for assignment, slot, submission in rows:
            position = "Noble Advisor" if slot.track == "NA" else "Chief Minister"
            time_str = slot.start_time.strftime("%H:%M UTC")
            writer.writerow([
                slot.day,
                position,
                submission.discord_name,
                time_str,
            ])

        csv_bytes = output.getvalue().encode("utf-8")
        return io.BytesIO(csv_bytes)

    async def archive(self, day1: datetime):
        """Archive the event. Called by lifecycle loop. Idempotent."""
        async with async_session() as session:
            result = await session.execute(
                select(Event).where(Event.day1_date == day1)
            )
            event = result.scalar_one_or_none()
            if event is None or event.phase == EventPhase.ARCHIVED:
                return

            event.phase = EventPhase.ARCHIVED

            session.add(AuditLog(
                event_id=event.event_id,
                action="Event archived",
                actor="system",
            ))

            await session.commit()

        # Post final CSV
        csv_file = await self._generate_csv(day1)
        for guild in self.bot.guilds:
            log_channel = discord.utils.get(guild.text_channels, name=SCHEDULE_LOG_CHANNEL)
            if log_channel:
                msg = await log_channel.send(
                    f"📋 **Final schedule archive** — Event {day1.date()}",
                    file=discord.File(csv_file, filename=f"schedule_final_{day1.date()}.csv"),
                )
                try:
                    await msg.pin()
                except discord.Forbidden:
                    pass

        logger.info(f"Event archived for Day 1 = {day1.date()}")


async def setup(bot: commands.Bot):
    await bot.add_cog(Scheduling(bot))
