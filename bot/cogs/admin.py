"""
Admin cog — admin-only slash commands.
"""

import logging
from datetime import datetime, timezone

import discord
from discord import app_commands
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


def is_admin():
    """Check that the invoking user has the admin role."""
    async def predicate(interaction: discord.Interaction) -> bool:
        role = discord.utils.get(interaction.guild.roles, name=ADMIN_ROLE)
        if role and role in interaction.user.roles:
            return True
        raise app_commands.MissingRole(ADMIN_ROLE)
    return app_commands.check(predicate)


class Admin(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="status", description="Show current cycle phase, dates, and submission stats")
    @is_admin()
    async def status(self, interaction: discord.Interaction):
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

        await interaction.response.send_message("\n".join(lines))

    @app_commands.command(name="pending", description="List pending change requests")
    @is_admin()
    async def pending(self, interaction: discord.Interaction):
        day1 = get_current_cycle_day1()

        async with async_session() as session:
            result = await session.execute(
                select(Event).where(Event.day1_date == day1)
            )
            event = result.scalar_one_or_none()
            if event is None:
                await interaction.response.send_message("No active event.")
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
            await interaction.response.send_message("No pending changes.")
            return

        lines = [f"**{len(changes)} pending change(s):**\n"]
        for c in changes:
            member = interaction.guild.get_member(c.requested_by)
            name = member.display_name if member else str(c.requested_by)
            status_label = "awaiting player" if c.status == ChangeStatus.PENDING_CONFIRMATION else "awaiting admin"
            deadline = ""
            if c.admin_deadline:
                deadline = f" (deadline: {c.admin_deadline.strftime('%b %d %H:%M UTC')})"
            lines.append(f"#{c.change_id} — {c.change_type.value} by {name} [{status_label}]{deadline}")

        await interaction.response.send_message("\n".join(lines))

    @app_commands.command(name="force_lock", description="Lock submissions and run the optimizer now")
    @is_admin()
    async def force_lock(self, interaction: discord.Interaction):
        day1 = get_current_cycle_day1()
        scheduling_cog = self.bot.get_cog("Scheduling")
        if scheduling_cog:
            await interaction.response.defer()
            await scheduling_cog.lock_and_release(day1)
            await interaction.followup.send("Done. Check #schedule_log.")
        else:
            await interaction.response.send_message("Scheduling cog not loaded.")

    @app_commands.command(name="reset_event", description="Delete the current event and all its data")
    @is_admin()
    async def reset_event(self, interaction: discord.Interaction):
        """Delete the current event and all its data so the cycle restarts fresh."""
        async with async_session() as session:
            result = await session.execute(
                select(Event).order_by(Event.day1_date.desc())
            )
            event = result.scalar_one_or_none()
            if event is None:
                await interaction.response.send_message("No event to reset.")
                return

            event_id = event.event_id
            phase = event.phase
            day1 = event.day1_date

            await session.delete(event)
            await session.commit()

        reminders_cog = self.bot.get_cog("Reminders")
        if reminders_cog:
            reminders_cog.clear_sent_reminders()

        await interaction.response.send_message(
            f"Event reset (was {phase}, event_id={event_id}, day1={day1.date()}). "
            f"Lifecycle loop will recreate it on next tick if we're in the collecting window."
        )
        logger.info(f"Admin {interaction.user} reset event {event_id} (day1={day1.date()})")

    @app_commands.command(name="force_archive", description="Archive the current event immediately")
    @is_admin()
    async def force_archive(self, interaction: discord.Interaction):
        async with async_session() as session:
            result = await session.execute(
                select(Event)
                .where(Event.phase != EventPhase.ARCHIVED)
                .order_by(Event.day1_date.desc())
            )
            event = result.scalar_one_or_none()

        if event is None:
            await interaction.response.send_message("No active event to archive.")
            return

        scheduling_cog = self.bot.get_cog("Scheduling")
        if scheduling_cog:
            await interaction.response.defer()
            await scheduling_cog.archive(event.day1_date)
            await interaction.followup.send(f"Event archived (day1={event.day1_date.date()}).")
        else:
            await interaction.response.send_message("Scheduling cog not loaded.")

    @app_commands.command(name="view_schedule", description="Download the current schedule as a CSV")
    @is_admin()
    async def view_schedule(self, interaction: discord.Interaction):
        day1 = get_current_cycle_day1()
        scheduling_cog = self.bot.get_cog("Scheduling")
        if scheduling_cog:
            csv_file = await scheduling_cog._generate_csv(day1)
            await interaction.response.send_message(
                "Current schedule:",
                file=discord.File(csv_file, filename=f"schedule_{day1.date()}.csv"),
            )
        else:
            await interaction.response.send_message("Scheduling cog not loaded.")


async def setup(bot: commands.Bot):
    await bot.add_cog(Admin(bot))
