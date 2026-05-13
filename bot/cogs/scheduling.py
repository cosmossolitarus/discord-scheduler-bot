"""
Scheduling cog.

Handles:
    - Locking submissions and running the optimizer
    - Generating schedule CSVs
    - Releasing the schedule to players
    - Archiving the final schedule

Phase 1b changes:
    - lock_and_release and archive take an Event object instead of a day1.
      Internally they re-fetch by event_id within their own session.
    - Uses mark_locked / mark_archived from bot.events for phase transitions
      (sets locked_at / archived_at timestamps).
    - AssignmentStatus is gone; the row's existence carries the information.
    - Boundary-slot DM gets the dual-Speedup notice on schedule release.
"""

import csv
import io
import logging

import discord
from discord.ext import commands
from sqlalchemy import select

from bot.config import (
    ADMIN_ROLE,
    PLAYER_ROLE,
    SCHEDULE_LOG_CHANNEL,
    SCHEDULING_CHANNEL,
)
from bot.cycle import is_boundary_slot
from bot.database import async_session
from bot.events import mark_archived, mark_locked
from bot.models import (
    Assignment,
    AuditLog,
    Event,
    EventPhase,
    Slot,
    Submission,
)
from bot.optimizer.solver import SlotForOptimizer, run_full_optimization

logger = logging.getLogger("scheduler.scheduling")


# ─── Boundary-slot notice ────────────────────────────────────────

BOUNDARY_NOTICE = (
    "\n\n**Boundary slot.** The slot at 23:45-00:15 (your last Day 1 block) "
    "spans the Day 1 / Day 2 boundary. You'll be the only player in that "
    "window — use Day 1 (construction) Speedups for the first 15 minutes "
    "and Day 2 (research) Speedups for the last 15 minutes."
)


class Scheduling(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ─── Public lifecycle methods (called from main.py) ──────────

    async def lock_and_release(self, event: Event):
        """Lock submissions, run optimizer, notify players.

        Receives an Event hint; re-fetches by event_id within its own session.
        Idempotent — if the event is not in COLLECTING, returns without doing
        anything.
        """
        event_id = event.event_id

        async with async_session() as session:
            db_event = await session.get(Event, event_id)
            if db_event is None or db_event.phase != EventPhase.COLLECTING:
                return

            mark_locked(db_event)
            day1 = db_event.day1_date

            # Complete submissions only
            result = await session.execute(
                select(Submission).where(
                    Submission.event_id == event_id,
                    Submission.has_screenshot == True,    # noqa: E712
                    Submission.has_availability == True,  # noqa: E712
                )
            )
            submissions = list(result.scalars().all())

            # All slots for this event, bucketed for the optimizer
            result = await session.execute(
                select(Slot).where(Slot.event_id == event_id)
            )
            all_slots = list(result.scalars().all())

            slots_by_pass = {"D1-CM": [], "D2-CM": [], "D4-NA": [], "D4-CM": []}
            for slot in all_slots:
                key = f"D{slot.day}-{slot.track}"
                if key in slots_by_pass:
                    slots_by_pass[key].append(
                        SlotForOptimizer(slot_id=slot.slot_id, slot_index=slot.slot_index)
                    )

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

            results = run_full_optimization(sub_data, slots_by_pass)

            # Clear any existing assignments for idempotency on re-lock
            await session.execute(
                Assignment.__table__.delete().where(
                    Assignment.event_id == event_id
                )
            )

            assigned_users: set[int] = set()
            for pass_name, assignments in results.items():
                if pass_name == "boundary":
                    # The boundary player is already assigned to D1-CM-49;
                    # we don't materialize a separate D2-BOUNDARY row.
                    continue
                for a in assignments:
                    session.add(Assignment(
                        event_id=event_id,
                        slot_id=a.slot_id,
                        discord_id=a.discord_id,
                    ))
                    assigned_users.add(a.discord_id)

            session.add(AuditLog(
                event_id=event_id,
                action="Schedule locked and optimized",
                actor="system",
                details={
                    "total_submissions": len(submissions),
                    "complete_submissions": len(sub_data),
                    "assigned_users": len(assigned_users),
                    "assignments_by_pass": {k: len(v) for k, v in results.items()},
                    "is_test": db_event.is_test,
                },
            ))

            await session.commit()

        # CSV for #schedule_log
        csv_file = await self._generate_csv(event_id)

        for guild in self.bot.guilds:
            log_channel = discord.utils.get(guild.text_channels, name=SCHEDULE_LOG_CHANNEL)
            admin_role = discord.utils.get(guild.roles, name=ADMIN_ROLE)
            scheduling_channel = discord.utils.get(guild.text_channels, name=SCHEDULING_CHANNEL)
            player_role = discord.utils.get(guild.roles, name=PLAYER_ROLE)

            if log_channel and admin_role:
                test_tag = " (test)" if db_event.is_test else ""
                await log_channel.send(
                    f"{admin_role.mention} — Schedule locked and optimized{test_tag}.\n"
                    f"{len(assigned_users)} users assigned across "
                    f"{sum(len(v) for v in results.values() if isinstance(v, list))} slots.",
                    file=discord.File(csv_file, filename=f"schedule_{day1.date()}.csv"),
                )

            # Per-player DMs
            await self._notify_players(guild, event_id, submissions, assigned_users)

            if scheduling_channel and player_role:
                day1_str = day1.strftime("%A, %B %d, %Y")
                await scheduling_channel.send(
                    f"{player_role.mention} — The schedule for **{day1_str}** "
                    f"has been released! Check your DMs for your assigned times.\n\n"
                    f"To request changes, @mention me in this channel with what you need."
                )

    async def archive(self, event: Event):
        """Archive the event. Idempotent."""
        event_id = event.event_id

        async with async_session() as session:
            db_event = await session.get(Event, event_id)
            if db_event is None or db_event.phase == EventPhase.ARCHIVED:
                return

            mark_archived(db_event)
            day1 = db_event.day1_date

            session.add(AuditLog(
                event_id=event_id,
                action="Event archived",
                actor="system",
            ))

            await session.commit()

        csv_file = await self._generate_csv(event_id)
        for guild in self.bot.guilds:
            log_channel = discord.utils.get(guild.text_channels, name=SCHEDULE_LOG_CHANNEL)
            if log_channel:
                msg = await log_channel.send(
                    f"**Final schedule archive** — Event {day1.date()}",
                    file=discord.File(csv_file, filename=f"schedule_final_{day1.date()}.csv"),
                )
                try:
                    await msg.pin()
                except discord.Forbidden:
                    pass

        logger.info(f"Event archived for Day 1 = {day1.date()}")

    # ─── Internal helpers ────────────────────────────────────────

    async def _notify_players(
        self,
        guild: discord.Guild,
        event_id: int,
        submissions: list[Submission],
        assigned_users: set[int],
    ):
        """DM each player their assignments (or waitlist status)."""
        async with async_session() as session:
            result = await session.execute(
                select(Assignment, Slot)
                .join(Slot, Assignment.slot_id == Slot.slot_id)
                .where(Assignment.event_id == event_id)
                .order_by(Slot.day, Slot.track.desc(), Slot.slot_index)
            )
            all_assignments = result.all()

        user_assignments: dict[int, list[tuple[Assignment, Slot]]] = {}
        for assignment, slot in all_assignments:
            user_assignments.setdefault(assignment.discord_id, []).append((assignment, slot))

        for submission in submissions:
            member = guild.get_member(submission.discord_id)
            if member is None:
                continue

            try:
                if submission.discord_id in assigned_users:
                    lines = ["**Your scheduled times:**\n"]
                    has_boundary = False
                    for assignment, slot in user_assignments.get(submission.discord_id, []):
                        start_str = slot.start_time.strftime("%a %b %d, %H:%M")
                        end_str = slot.end_time.strftime("%H:%M UTC")
                        track_label = "Noble Advisor" if slot.track == "NA" else "Chief Minister"
                        lines.append(
                            f"• Day {slot.day} ({track_label}): {start_str} - {end_str}"
                        )
                        if is_boundary_slot(slot.slot_id):
                            has_boundary = True

                    msg = "\n".join(lines)
                    if has_boundary:
                        msg += BOUNDARY_NOTICE
                    await member.send(msg)
                else:
                    await member.send(
                        "You were not assigned a slot for this event. "
                        "You're on the waitlist — I'll notify you if a spot opens up."
                    )
            except discord.Forbidden:
                logger.warning(f"Could not DM user {submission.discord_id} — DMs may be disabled.")

    async def _generate_csv(self, event_id: int) -> io.BytesIO:
        """Build a CSV of the current schedule for one event."""
        async with async_session() as session:
            event = await session.get(Event, event_id)
            if event is None:
                return io.BytesIO(b"No event found")

            result = await session.execute(
                select(Assignment, Slot, Submission)
                .join(Slot, Assignment.slot_id == Slot.slot_id)
                .join(
                    Submission,
                    (Submission.event_id == Assignment.event_id)
                    & (Submission.discord_id == Assignment.discord_id),
                )
                .where(Assignment.event_id == event_id)
                .order_by(Slot.day, Slot.track.desc(), Slot.slot_index)
            )
            rows = result.all()

        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(["Day", "Minister Position", "Player", "Time"])

        for assignment, slot, submission in rows:
            position = "Noble Advisor" if slot.track == "NA" else "Chief Minister"
            time_str = slot.start_time.strftime("%H:%M UTC")
            writer.writerow([slot.day, position, submission.discord_name, time_str])

        return io.BytesIO(output.getvalue().encode("utf-8"))


async def setup(bot: commands.Bot):
    await bot.add_cog(Scheduling(bot))
