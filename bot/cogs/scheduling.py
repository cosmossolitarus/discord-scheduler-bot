"""
Scheduling cog.

Handles:
  - lock_and_release(): run optimizer, post admin-only CSV + summary to #schedule_log
  - publish(): send player DMs and announce in #scheduling
  - CSV generation (all submission columns included)

lock() is now a two-step process:
  1. Admin runs /schedule lock → optimizer runs → CSV sent to admins only (LOCKED phase)
  2. Admin reviews, optionally edits via admin commands
  3. Admin runs /schedule publish → player DMs sent, PUBLISHED phase begins
"""

import csv
import io
import logging

import discord
from discord.ext import commands
from sqlalchemy import select

from bot.config import (
    ADMIN_ROLE,
    MOJ_ROLE,
    PLAYER_ROLE,
    SCHEDULE_LOG_CHANNEL,
    SCHEDULING_CHANNEL,
)
from bot.cycle import is_boundary_slot
from bot.database import async_session
from bot.events import mark_locked, mark_published
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


BOUNDARY_NOTICE = (
    "\n\n**Boundary slot.** The slot at 23:45-00:15 (your last Day 1 block) "
    "spans the Day 1 / Day 2 boundary. You'll be the only player in that "
    "window — use Day 1 (construction) Speedups for the first 15 minutes "
    "and Day 2 (research) Speedups for the last 15 minutes."
)


class Scheduling(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ─── Lock: run optimizer, notify admins only ─────────────

    async def lock_and_release(self, event: Event):
        """Run optimizer and post CSV to #schedule_log for admin review.

        Does NOT DM players. Idempotent if already LOCKED.
        """
        event_id = event.event_id

        async with async_session() as session:
            db_event = await session.get(Event, event_id)
            if db_event is None or db_event.phase != EventPhase.COLLECTING:
                return

            mark_locked(db_event)
            day1 = db_event.day1_date

            result = await session.execute(
                select(Submission).where(
                    Submission.event_id == event_id,
                    Submission.has_screenshot == True,    # noqa: E712
                    Submission.has_availability == True,  # noqa: E712
                )
            )
            submissions = list(result.scalars().all())

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

            await session.execute(
                Assignment.__table__.delete().where(Assignment.event_id == event_id)
            )

            assigned_users: set[int] = set()
            for pass_name, assignments in results.items():
                if pass_name == "boundary":
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

        # Post CSV to #schedule_log for admins; no player notifications yet
        csv_file = await self._generate_csv(event_id)

        for guild in self.bot.guilds:
            log_channel = discord.utils.get(guild.text_channels, name=SCHEDULE_LOG_CHANNEL)
            if log_channel:
                staff_mention = " ".join(
                    r.mention for r in [
                        discord.utils.get(guild.roles, name=ADMIN_ROLE),
                        discord.utils.get(guild.roles, name=MOJ_ROLE),
                    ] if r is not None
                )
                test_tag = " (test)" if event.is_test else ""
                await log_channel.send(
                    f"{staff_mention} — Schedule locked and optimized{test_tag}. "
                    f"**Review before publishing.** "
                    f"{len(assigned_users)} players assigned across "
                    f"{sum(len(v) for v in results.values() if isinstance(v, list))} slots.\n"
                    f"Use `/schedule publish` when ready to release to players, "
                    f"or edit with `/schedule assign`, `/schedule remove-player`, "
                    f"`/schedule swap-players`.",
                    file=discord.File(csv_file, filename=f"schedule_draft_{day1.date()}.csv"),
                )

    # ─── Publish: send player DMs, make schedule public ──────

    async def publish(self, event: Event):
        """Transition LOCKED → PUBLISHED and DM all players their assignments."""
        event_id = event.event_id

        async with async_session() as session:
            db_event = await session.get(Event, event_id)
            if db_event is None or db_event.phase != EventPhase.LOCKED:
                return

            mark_published(db_event)
            day1 = db_event.day1_date

            result = await session.execute(
                select(Submission).where(Submission.event_id == event_id)
            )
            submissions = list(result.scalars().all())

            # Collect all assigned discord IDs
            assn_result = await session.execute(
                select(Assignment.discord_id).where(Assignment.event_id == event_id)
            )
            assigned_users = set(assn_result.scalars().all())

            session.add(AuditLog(
                event_id=event_id,
                action="Schedule published",
                actor="system",
                details={"assigned_users": len(assigned_users)},
            ))
            await session.commit()

        csv_file = await self._generate_csv(event_id)

        for guild in self.bot.guilds:
            log_channel = discord.utils.get(guild.text_channels, name=SCHEDULE_LOG_CHANNEL)
            scheduling_channel = discord.utils.get(guild.text_channels, name=SCHEDULING_CHANNEL)
            player_role = discord.utils.get(guild.roles, name=PLAYER_ROLE)

            if log_channel:
                staff_mention = " ".join(
                    r.mention for r in [
                        discord.utils.get(guild.roles, name=ADMIN_ROLE),
                        discord.utils.get(guild.roles, name=MOJ_ROLE),
                    ] if r is not None
                )
                test_tag = " (test)" if event.is_test else ""
                await log_channel.send(
                    f"{staff_mention} — Schedule published{test_tag}. "
                    f"{len(assigned_users)} players notified.",
                    file=discord.File(csv_file, filename=f"schedule_{day1.date()}.csv"),
                )

            await self._notify_players(guild, event_id, submissions, assigned_users)

            if scheduling_channel and player_role:
                day1_str = day1.strftime("%A, %B %d, %Y")
                await scheduling_channel.send(
                    f"{player_role.mention} — The schedule for **{day1_str}** "
                    f"has been released! Check your DMs for your assigned times.\n\n"
                    f"To request changes, @mention me in this channel with what you need."
                )

    # ─── Player DMs ──────────────────────────────────────────

    async def _notify_players(
        self,
        guild: discord.Guild,
        event_id: int,
        submissions: list[Submission],
        assigned_users: set[int],
    ):
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

    # ─── CSV generation ──────────────────────────────────────

    async def _generate_csv(self, event_id: int) -> io.BytesIO:
        """Build a full CSV of the current schedule, including all submission data."""
        async with async_session() as session:
            event = await session.get(Event, event_id)
            if event is None:
                return io.BytesIO(b"No event found")

            # All assignments with slot + submission data
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
            assigned_rows = result.all()

            # Waitlisted players (complete submissions with no assignment)
            assigned_ids = {a.discord_id for a, _, _ in assigned_rows}
            waitlist_result = await session.execute(
                select(Submission).where(
                    Submission.event_id == event_id,
                    Submission.discord_id.notin_(assigned_ids),
                )
            )
            waitlisted = list(waitlist_result.scalars().all())

        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow([
            "Day", "Minister Position", "Slot ID", "Start Time (UTC)",
            "Player Name", "Discord ID", "In-Game Player ID",
            "Speedup Construction (days)", "Speedup Research (days)",
            "Speedup Training (days)", "Speedup General (days)",
            "TTG", "TG", "Dust",
            "Priority Day1 (pts)", "Priority Day2 (pts)", "Priority Day4 (days)",
            "Available Slots", "Status",
        ])

        for assignment, slot, submission in assigned_rows:
            position = "Noble Advisor" if slot.track == "NA" else "Chief Minister"
            start_str = slot.start_time.strftime("%Y-%m-%d %H:%M")
            writer.writerow([
                slot.day,
                position,
                slot.slot_id,
                start_str,
                submission.discord_name,
                submission.discord_id,
                submission.player_ingame_id or "",
                f"{submission.speedup_construction or 0:.2f}",
                f"{submission.speedup_research or 0:.2f}",
                f"{submission.speedup_training or 0:.2f}",
                f"{submission.speedup_general or 0:.2f}",
                f"{submission.ttg or 0:.0f}",
                f"{submission.tg or 0:.0f}",
                f"{submission.dust or 0:.0f}",
                f"{submission.priority_x or 0:.0f}",
                f"{submission.priority_y or 0:.0f}",
                f"{submission.priority_z or 0:.2f}",
                len(submission.availability or []),
                "assigned",
            ])

        for submission in waitlisted:
            writer.writerow([
                "", "", "", "",
                submission.discord_name,
                submission.discord_id,
                submission.player_ingame_id or "",
                f"{submission.speedup_construction or 0:.2f}",
                f"{submission.speedup_research or 0:.2f}",
                f"{submission.speedup_training or 0:.2f}",
                f"{submission.speedup_general or 0:.2f}",
                f"{submission.ttg or 0:.0f}",
                f"{submission.tg or 0:.0f}",
                f"{submission.dust or 0:.0f}",
                f"{submission.priority_x or 0:.0f}",
                f"{submission.priority_y or 0:.0f}",
                f"{submission.priority_z or 0:.2f}",
                len(submission.availability or []),
                "waitlisted",
            ])

        return io.BytesIO(output.getvalue().encode("utf-8"))


async def setup(bot: commands.Bot):
    await bot.add_cog(Scheduling(bot))
