"""
Admin cog — admin-only commands.
"""

import logging
from datetime import datetime, timezone

import discord
from discord.ext import commands
from sqlalchemy import select, func

from bot.config import ADMIN_ROLE, SCHEDULE_LOG_CHANNEL
from bot.database import async_session
from bot.models import (
    Event, Submission, Assignment, ChangeRequest,
    EventPhase, ChangeStatus,
)
from bot.cycle import get_current_phase, get_current_cycle_day1, get_cycle_dates, Phase

logger = logging.getLogger("scheduler.admin")


class Admin(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.command(name="status")
    @commands.has_role(ADMIN_ROLE)
    async def status(self, ctx: commands.Context):
        now = datetime.now(timezone.utc)
        phase, day1 = get_current_phase(now)
        dates = get_cycle_dates(day1)

        day1_str = day1.strftime("%A, %B %d, %Y")
        lines = [
            f"**Cycle Status**",
            f"Day 1: {day1_str}",
            f"Current phase: **{phase.value}**",
            f"",
            f"Submissions open: {dates['submissions_open'].strftime('%b %d, %H:%M UTC')}",
            f"Lock: {dates['lock'].strftime('%b %d, %H:%M UTC')}",
            f"Day 1 blocks: {dates['day1_start'].strftime('%b %d, %H:%M')} - {dates['day1_end'].strftime('%b %d, %H:%M UTC')}",
            f"Day 2 blocks: {dates['day2_start'].strftime('%b %d, %H:%M')} - {dates['day2_end'].strftime('%b %d, %H:%M UTC')}",
            f"Day 4 blocks: {dates['day4_start'].strftime('%b %d, %H:%M')} - {dates['day4_end'].strftime('%b %d, %H:%M UTC')}",
            f"Archive: {dates['archive'].strftime('%b %d, %H:%M UTC')}",
        ]

        async with async_session() as session:
            result = await session.execute(
                select(Event).where(Event.day1_date == day1)
            )
            event = result.scalar_one_or_none()

            if event:
                sub_count = await session.execute(
                    select(func.count()).select_from(Submission).where(
                        Submission.event_id == event.event_id
                    )
                )
                total_subs = sub_count.scalar()

                complete_count = await session.execute(
                    select(func.count()).select_from(Submission).where(
                        Submission.event_id == event.event_id,
                        Submission.has_screenshot == True,
                        Submission.has_availability == True,
                    )
                )
                complete_subs = complete_count.scalar()

                assignment_count = await session.execute(
                    select(func.count()).select_from(Assignment).where(
                        Assignment.event_id == event.event_id
                    )
                )
                total_assignments = assignment_count.scalar()

                pending_count = await session.execute(
                    select(func.count()).select_from(ChangeRequest).where(
                        ChangeRequest.event_id == event.event_id,
                        ChangeRequest.status.in_([
                            ChangeStatus.PENDING_ADMIN,
                            ChangeStatus.PENDING_CONFIRMATION,
                        ]),
                    )
                )
                pending_changes = pending_count.scalar()

                lines.extend([
                    f"",
                    f"**Stats**",
                    f"Submissions: {complete_subs} complete / {total_subs} total",
                    f"Assignments: {total_assignments}",
                    f"Pending changes: {pending_changes}",
                ])

        await ctx.send("\n".join(lines))

    @commands.command(name="pending")
    @commands.has_role(ADMIN_ROLE)
    async def pending(self, ctx: commands.Context):
        day1 = get_current_cycle_day1()

        async with async_session() as session:
            result = await session.execute(
                select(Event).where(Event.day1_date == day1)
            )
            event = result.scalar_one_or_none()
            if event is None:
                await ctx.send("No active event.")
                return

            result = await session.execute(
                select(ChangeRequest).where(
                    ChangeRequest.event_id == event.event_id,
                    ChangeRequest.status.in_([
                        ChangeStatus.PENDING_ADMIN,
                        ChangeStatus.PENDING_CONFIRMATION,
                    ]),
                ).order_by(ChangeRequest.created_at)
            )
            changes = list(result.scalars().all())

        if not changes:
            await ctx.send("No pending changes.")
            return

        lines = [f"**{len(changes)} pending change(s):**\n"]
        for c in changes:
            member = ctx.guild.get_member(c.requested_by)
            name = member.display_name if member else str(c.requested_by)
            status_label = "awaiting player" if c.status == ChangeStatus.PENDING_CONFIRMATION else "awaiting admin"
            deadline = ""
            if c.admin_deadline:
                deadline = f" (deadline: {c.admin_deadline.strftime('%b %d %H:%M UTC')})"
            lines.append(f"#{c.change_id} — {c.change_type.value} by {name} [{status_label}]{deadline}")

        await ctx.send("\n".join(lines))

    @commands.command(name="force_lock")
    @commands.has_role(ADMIN_ROLE)
    async def force_lock(self, ctx: commands.Context):
        day1 = get_current_cycle_day1()
        scheduling_cog = self.bot.get_cog("Scheduling")
        if scheduling_cog:
            await ctx.send("Locking submissions and running optimizer...")
            await scheduling_cog.lock_and_release(day1)
            await ctx.send("Done. Check #schedule_log.")
        else:
            await ctx.send("Scheduling cog not loaded.")

    @commands.command(name="force_archive")
    @commands.has_role(ADMIN_ROLE)
    async def force_archive(self, ctx: commands.Context):
        day1 = get_current_cycle_day1()
        scheduling_cog = self.bot.get_cog("Scheduling")
        if scheduling_cog:
            await scheduling_cog.archive(day1)
            await ctx.send("Event archived.")
        else:
            await ctx.send("Scheduling cog not loaded.")


async def setup(bot: commands.Bot):
    await bot.add_cog(Admin(bot))
