"""
Admin cog — admin-only slash commands under the /schedule group.

Commands:
    /schedule status                  Active event phase, dates, stats
    /schedule pending                 List pending change requests
    /schedule lookup <player>         Show one player's submission + assignment
    /schedule lock                    Force lock + optimize the active event
    /schedule unlock confirm:true     Roll LOCKED back to COLLECTING
                                      (deletes assignments + pending changes,
                                      keeps submissions)
    /schedule archive                 Force archive the active event
    /schedule reset confirm:true      Delete the active event (all data)
    /schedule export                  Download CSV of the active event
    /schedule test [date]             Create a test event in COLLECTING
"""

import logging
from datetime import datetime, timedelta, timezone

import discord
from discord import app_commands
from discord.ext import commands
from sqlalchemy import func, select

from bot.config import ADMIN_ROLE
from bot.cycle import compute_active_cycle_day1, get_cycle_dates
from bot.database import async_session
from bot.events import create_event
from bot.models import (
    Assignment,
    AuditLog,
    ChangeRequest,
    ChangeStatus,
    Event,
    EventPhase,
    SentReminder,
    Submission,
)

logger = logging.getLogger("scheduler.admin")


# ─── Helpers ─────────────────────────────────────────────────────


def is_admin():
    async def predicate(interaction: discord.Interaction) -> bool:
        role = discord.utils.get(interaction.guild.roles, name=ADMIN_ROLE)
        if role and role in interaction.user.roles:
            return True
        raise app_commands.MissingRole(ADMIN_ROLE)
    return app_commands.check(predicate)


async def _get_active_event(session) -> Event | None:
    """The single non-archived event, or None.

    By design, only one non-archived event exists at a time (test events
    block real-event auto-creation, and there is only ever one real event
    per cycle). If multiple exist due to manual intervention, we return
    the most-recent by day1_date.
    """
    result = await session.execute(
        select(Event)
        .where(Event.phase != EventPhase.ARCHIVED)
        .order_by(Event.day1_date.desc())
    )
    return result.scalars().first()


def _parse_date_arg(raw: str | None) -> datetime:
    """Parse a YYYY-MM-DD admin argument into a UTC datetime at 00:00.

    None defaults to tomorrow at 00:00 UTC (so a test event never collides
    with today's natural cycle window).
    """
    if raw is None:
        tomorrow = datetime.now(timezone.utc) + timedelta(days=1)
        return tomorrow.replace(hour=0, minute=0, second=0, microsecond=0)
    try:
        dt = datetime.strptime(raw, "%Y-%m-%d")
    except ValueError as e:
        raise ValueError(
            f"Could not parse {raw!r} as YYYY-MM-DD: {e}"
        ) from e
    return dt.replace(tzinfo=timezone.utc)


# ─── Cog ─────────────────────────────────────────────────────────


class Admin(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    schedule = app_commands.Group(
        name="schedule",
        description="Schedule management (admin only)",
    )

    # ─── status ──────────────────────────────────────────────

    @schedule.command(name="status", description="Active event phase, dates, and stats")
    @is_admin()
    async def schedule_status(self, interaction: discord.Interaction):
        now = datetime.now(timezone.utc)

        async with async_session() as session:
            event = await _get_active_event(session)

            if event is None:
                # No active event — describe what's coming
                projected_day1 = compute_active_cycle_day1(now)
                if projected_day1 is not None:
                    lines = [
                        "**Cycle Status:** idle, but the natural cycle window is currently open.",
                        f"The next lifecycle tick will create an event for Day 1 = "
                        f"{projected_day1.strftime('%A, %B %d, %Y')}.",
                    ]
                else:
                    # In the idle gap; compute the next natural day1
                    # by finding the smallest cycle whose subs_open > now.
                    from bot.config import (
                        ANCHOR_DAY1,
                        CYCLE_LENGTH_DAYS,
                        SUBMISSIONS_OPEN_OFFSET,
                    )
                    cycle_n = max(0, int(
                        (now - ANCHOR_DAY1).total_seconds() / 86400 // CYCLE_LENGTH_DAYS
                    ) + 1)
                    next_day1 = ANCHOR_DAY1 + timedelta(days=cycle_n * CYCLE_LENGTH_DAYS)
                    subs_open = next_day1 + SUBMISSIONS_OPEN_OFFSET
                    lines = [
                        "**Cycle Status:** idle (between cycles).",
                        f"Next cycle's Day 1: {next_day1.strftime('%A, %B %d, %Y')}",
                        f"Submissions open: {subs_open.strftime('%a %b %d, %H:%M UTC')}",
                    ]
                await interaction.response.send_message("\n".join(lines))
                return

            day1 = event.day1_date
            dates = get_cycle_dates(day1)
            test_tag = " (TEST EVENT)" if event.is_test else ""

            # Compute a display phase: "Active" once the schedule's running
            display_phase = event.phase.value
            if event.phase == EventPhase.LOCKED and now >= dates["day1_start"]:
                display_phase = "active"

            lines = [
                f"**Cycle Status**{test_tag}",
                f"Day 1: {day1.strftime('%A, %B %d, %Y')}",
                f"Phase: **{display_phase}**",
                "",
                f"Submissions open: {dates['submissions_open'].strftime('%b %d, %H:%M UTC')}",
                f"Lock:             {dates['lock'].strftime('%b %d, %H:%M UTC')}",
                f"Day 1 blocks:     {dates['day1_start'].strftime('%b %d, %H:%M')} – {dates['day1_end'].strftime('%b %d, %H:%M UTC')}",
                f"Day 2 blocks:     {dates['day2_start'].strftime('%b %d, %H:%M')} – {dates['day2_end'].strftime('%b %d, %H:%M UTC')}",
                f"Day 4 blocks:     {dates['day4_start'].strftime('%b %d, %H:%M')} – {dates['day4_end'].strftime('%b %d, %H:%M UTC')}",
                f"Archive:          {dates['archive'].strftime('%b %d, %H:%M UTC')}",
            ]

            if event.locked_at:
                lines.append(f"Locked at:        {event.locked_at.strftime('%b %d, %H:%M UTC')}")
            if event.archived_at:
                lines.append(f"Archived at:      {event.archived_at.strftime('%b %d, %H:%M UTC')}")

            total_subs = (await session.execute(
                select(func.count()).select_from(Submission).where(
                    Submission.event_id == event.event_id
                )
            )).scalar()
            complete_subs = (await session.execute(
                select(func.count()).select_from(Submission).where(
                    Submission.event_id == event.event_id,
                    Submission.has_screenshot == True,    # noqa: E712
                    Submission.has_availability == True,  # noqa: E712
                )
            )).scalar()
            total_assignments = (await session.execute(
                select(func.count()).select_from(Assignment).where(
                    Assignment.event_id == event.event_id
                )
            )).scalar()
            pending_changes = (await session.execute(
                select(func.count()).select_from(ChangeRequest).where(
                    ChangeRequest.event_id == event.event_id,
                    ChangeRequest.status.in_([
                        ChangeStatus.PENDING_ADMIN,
                        ChangeStatus.PENDING_CONFIRMATION,
                    ]),
                )
            )).scalar()

            lines.extend([
                "",
                "**Stats**",
                f"Submissions:     {complete_subs} complete / {total_subs} total",
                f"Assignments:     {total_assignments}",
                f"Pending changes: {pending_changes}",
            ])

        await interaction.response.send_message("\n".join(lines))

    # ─── pending ─────────────────────────────────────────────

    @schedule.command(name="pending", description="List pending change requests")
    @is_admin()
    async def schedule_pending(self, interaction: discord.Interaction):
        async with async_session() as session:
            event = await _get_active_event(session)
            if event is None:
                await interaction.response.send_message("No active event.")
                return

            result = await session.execute(
                select(ChangeRequest)
                .where(
                    ChangeRequest.event_id == event.event_id,
                    ChangeRequest.status.in_([
                        ChangeStatus.PENDING_ADMIN,
                        ChangeStatus.PENDING_CONFIRMATION,
                    ]),
                )
                .order_by(ChangeRequest.created_at)
            )
            changes = list(result.scalars().all())

        if not changes:
            await interaction.response.send_message("No pending changes.")
            return

        lines = [f"**{len(changes)} pending change(s):**\n"]
        for c in changes:
            member = interaction.guild.get_member(c.requested_by)
            name = member.display_name if member else str(c.requested_by)
            status_label = (
                "awaiting player"
                if c.status == ChangeStatus.PENDING_CONFIRMATION
                else "awaiting admin"
            )
            deadline = ""
            if c.admin_deadline:
                deadline = f" (deadline: {c.admin_deadline.strftime('%b %d %H:%M UTC')})"
            lines.append(
                f"#{c.change_id} — {c.change_type.value} by {name} [{status_label}]{deadline}"
            )

        await interaction.response.send_message("\n".join(lines))

    # ─── lookup ──────────────────────────────────────────────

    @schedule.command(
        name="lookup",
        description="Look up a player's submission and assignments in the active event",
    )
    @app_commands.describe(player="The player to look up")
    @is_admin()
    async def schedule_lookup(
        self,
        interaction: discord.Interaction,
        player: discord.Member,
    ):
        async with async_session() as session:
            event = await _get_active_event(session)
            if event is None:
                await interaction.response.send_message("No active event.")
                return

            result = await session.execute(
                select(Submission).where(
                    Submission.event_id == event.event_id,
                    Submission.discord_id == player.id,
                )
            )
            submission = result.scalar_one_or_none()

            if submission is None:
                await interaction.response.send_message(
                    f"{player.display_name} has not submitted anything for this event."
                )
                return

            from bot.models import Slot
            result = await session.execute(
                select(Assignment, Slot)
                .join(Slot, Assignment.slot_id == Slot.slot_id)
                .where(
                    Assignment.event_id == event.event_id,
                    Assignment.discord_id == player.id,
                )
                .order_by(Slot.day, Slot.track.desc(), Slot.slot_index)
            )
            rows = result.all()

        lines = [f"**Lookup: {player.display_name}** (id: {player.id})", ""]
        lines.append("**Submission**")
        lines.append(
            f"Screenshot: {'✓' if submission.has_screenshot else '✗'}    "
            f"Availability: {'✓' if submission.has_availability else '✗'}"
        )
        if submission.has_screenshot:
            lines.append(
                f"Speedups: construction={submission.speedup_construction or 0:.0f}, "
                f"research={submission.speedup_research or 0:.0f}, "
                f"training={submission.speedup_training or 0:.0f}, "
                f"general={submission.speedup_general or 0:.0f}"
            )
            lines.append(
                f"Priority: x={submission.priority_x or 0:.0f}, "
                f"y={submission.priority_y or 0:.0f}, "
                f"z={submission.priority_z or 0:.0f}"
            )
        if submission.has_availability:
            avail = submission.availability or []
            lines.append(f"Available in {len(avail)} slot(s)")

        lines.append("")
        lines.append("**Assignments**")
        if not rows:
            lines.append("(none — waitlisted)")
        else:
            for _, slot in rows:
                start_str = slot.start_time.strftime("%a %b %d, %H:%M UTC")
                track_label = "Noble Advisor" if slot.track == "NA" else "Chief Minister"
                lines.append(f"• Day {slot.day} ({track_label}): {start_str}  [{slot.slot_id}]")

        await interaction.response.send_message("\n".join(lines))

    # ─── lock ────────────────────────────────────────────────

    @schedule.command(name="lock", description="Force lock and optimize the active event now")
    @is_admin()
    async def schedule_lock(self, interaction: discord.Interaction):
        async with async_session() as session:
            event = await _get_active_event(session)

        if event is None:
            await interaction.response.send_message("No active event.")
            return
        if event.phase != EventPhase.COLLECTING:
            await interaction.response.send_message(
                f"Event is not in COLLECTING (current phase: {event.phase.value})."
            )
            return

        scheduling_cog = self.bot.get_cog("Scheduling")
        if scheduling_cog is None:
            await interaction.response.send_message("Scheduling cog not loaded.")
            return

        await interaction.response.defer()
        await scheduling_cog.lock_and_release(event)
        await interaction.followup.send(
            f"Done — locked event for Day 1 = {event.day1_date.date()}. Check #schedule_log."
        )
        logger.info(f"Admin {interaction.user} locked event {event.event_id}")

    # ─── unlock ──────────────────────────────────────────────

    @schedule.command(
        name="unlock",
        description="Roll a locked event back to COLLECTING (drops assignments + pending changes)",
    )
    @app_commands.describe(
        confirm="Set to True to confirm this destructive action.",
    )
    @is_admin()
    async def schedule_unlock(
        self,
        interaction: discord.Interaction,
        confirm: bool,
    ):
        if not confirm:
            await interaction.response.send_message(
                "Refusing to unlock without confirm=True. "
                "This deletes all assignments and pending change requests "
                "(submissions are kept)."
            )
            return

        async with async_session() as session:
            event = await _get_active_event(session)
            if event is None:
                await interaction.response.send_message("No active event.")
                return
            if event.phase != EventPhase.LOCKED:
                await interaction.response.send_message(
                    f"Event is not LOCKED (current phase: {event.phase.value})."
                )
                return

            event_id = event.event_id

            await session.execute(
                Assignment.__table__.delete().where(Assignment.event_id == event_id)
            )
            await session.execute(
                ChangeRequest.__table__.delete().where(
                    ChangeRequest.event_id == event_id,
                    ChangeRequest.status.in_([
                        ChangeStatus.PENDING_ADMIN,
                        ChangeStatus.PENDING_CONFIRMATION,
                    ]),
                )
            )
            # Clear sent-reminder bookkeeping so reminders re-fire on the
            # next lock (assignments may differ after re-optimization).
            await session.execute(
                SentReminder.__table__.delete().where(SentReminder.event_id == event_id)
            )

            event.phase = EventPhase.COLLECTING
            event.locked_at = None

            session.add(AuditLog(
                event_id=event_id,
                action="Event unlocked (rolled back to COLLECTING)",
                actor=str(interaction.user),
            ))

            await session.commit()

        await interaction.response.send_message(
            f"Event for Day 1 = {event.day1_date.date()} rolled back to COLLECTING. "
            f"Assignments and pending changes deleted; submissions kept."
        )
        logger.info(f"Admin {interaction.user} unlocked event {event_id}")

    # ─── archive ─────────────────────────────────────────────

    @schedule.command(name="archive", description="Force archive the active event immediately")
    @is_admin()
    async def schedule_archive(self, interaction: discord.Interaction):
        async with async_session() as session:
            event = await _get_active_event(session)

        if event is None:
            await interaction.response.send_message("No active event.")
            return

        scheduling_cog = self.bot.get_cog("Scheduling")
        if scheduling_cog is None:
            await interaction.response.send_message("Scheduling cog not loaded.")
            return

        await interaction.response.defer()
        await scheduling_cog.archive(event)
        await interaction.followup.send(f"Event archived (Day 1 = {event.day1_date.date()}).")
        logger.info(f"Admin {interaction.user} archived event {event.event_id}")

    # ─── reset ───────────────────────────────────────────────

    @schedule.command(
        name="reset",
        description="Delete the active event and all its data (irreversible)",
    )
    @app_commands.describe(
        confirm="Set to True to confirm this destructive action.",
    )
    @is_admin()
    async def schedule_reset(
        self,
        interaction: discord.Interaction,
        confirm: bool,
    ):
        if not confirm:
            await interaction.response.send_message(
                "Refusing to reset without confirm=True. "
                "This deletes the event row plus all its submissions, slots, "
                "assignments, change requests, and audit logs."
            )
            return

        async with async_session() as session:
            event = await _get_active_event(session)
            if event is None:
                await interaction.response.send_message("No active event to reset.")
                return

            event_id = event.event_id
            day1 = event.day1_date
            phase = event.phase
            is_test = event.is_test

            await session.delete(event)
            await session.commit()

        # Note: SentReminder rows cascade-delete with the Event row, so no
        # explicit cache-clearing call is needed here.

        tag = "test event" if is_test else "event"
        await interaction.response.send_message(
            f"Reset complete: deleted {tag} #{event_id} "
            f"(Day 1 = {day1.date()}, was {phase.value}).\n"
            f"If we're in the natural cycle window, the lifecycle loop will "
            f"recreate it on the next tick."
        )
        logger.info(
            f"Admin {interaction.user} reset event {event_id} "
            f"(day1={day1.date()}, is_test={is_test})"
        )

    # ─── export ──────────────────────────────────────────────

    @schedule.command(name="export", description="Download the active event's schedule as CSV")
    @is_admin()
    async def schedule_export(self, interaction: discord.Interaction):
        async with async_session() as session:
            event = await _get_active_event(session)

        if event is None:
            await interaction.response.send_message("No active event.")
            return

        scheduling_cog = self.bot.get_cog("Scheduling")
        if scheduling_cog is None:
            await interaction.response.send_message("Scheduling cog not loaded.")
            return

        csv_file = await scheduling_cog._generate_csv(event.event_id)
        await interaction.response.send_message(
            f"Schedule for Day 1 = {event.day1_date.date()}:",
            file=discord.File(csv_file, filename=f"schedule_{event.day1_date.date()}.csv"),
        )

    # ─── test ────────────────────────────────────────────────

    @schedule.command(
        name="test",
        description="Create a test event (admin-driven; does not auto-transition)",
    )
    @app_commands.describe(
        date="Day 1 date in YYYY-MM-DD (UTC). Defaults to tomorrow.",
    )
    @is_admin()
    async def schedule_test(
        self,
        interaction: discord.Interaction,
        date: str | None = None,
    ):
        try:
            day1 = _parse_date_arg(date)
        except ValueError as e:
            await interaction.response.send_message(f"Bad date: {e}")
            return

        async with async_session() as session:
            existing = await _get_active_event(session)
            if existing is not None:
                tag = "test event" if existing.is_test else "event"
                await interaction.response.send_message(
                    f"Cannot create a test event while another {tag} is active "
                    f"(Day 1 = {existing.day1_date.date()}, phase={existing.phase.value}). "
                    f"Run /schedule reset confirm:true first."
                )
                return

            # Also guard against unique-constraint collision with an archived row
            # at the same day1.
            collide = await session.execute(
                select(Event).where(Event.day1_date == day1)
            )
            if collide.scalar_one_or_none() is not None:
                await interaction.response.send_message(
                    f"An event already exists in the DB for Day 1 = {day1.date()} "
                    f"(likely archived). Pick a different date."
                )
                return

            event = await create_event(session, day1, is_test=True)
            await session.commit()
            event_id = event.event_id

        # Forward-looking: ask the submissions cog to announce if it knows how.
        # (The pre-Phase-2 cog won't have this method yet; that's fine.)
        submissions_cog = self.bot.get_cog("Submissions")
        if submissions_cog is not None and hasattr(submissions_cog, "announce_event_opened"):
            # We have to re-fetch since the event from the closed session is detached.
            async with async_session() as session:
                fresh_event = await session.get(Event, event_id)
                await submissions_cog.announce_event_opened(fresh_event)

        await interaction.response.send_message(
            f"Test event created (Day 1 = {day1.date()}, event_id={event_id}, "
            f"phase=collecting).\n"
            f"It will not auto-transition. Use /schedule lock to advance it, "
            f"or /schedule reset to delete it."
        )
        logger.info(
            f"Admin {interaction.user} created test event {event_id} (day1={day1.date()})"
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(Admin(bot))
