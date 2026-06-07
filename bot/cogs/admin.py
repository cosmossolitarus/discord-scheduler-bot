"""
Admin cog — admin-only slash commands under the /schedule group.

Commands:
    /schedule status                  Active event phase, dates, stats
    /schedule pending                 List pending change requests
    /schedule lookup <player>         Show one player's submission + assignment
    /schedule create day1:<date>      Create a new event in COLLECTING phase
    /schedule test [date]             Create a test event in COLLECTING
    /schedule lock                    Run optimizer → LOCKED (admin review)
    /schedule publish                 Release schedule to players → PUBLISHED
    /schedule unlock confirm:true     Roll LOCKED back to COLLECTING
    /schedule reset confirm:true      Delete the active event (all data)
    /schedule export                  Download CSV of the active event
    /schedule assign                  Assign a player to a specific slot (admin override)
    /schedule remove-player           Remove a player's slot assignment on a given day
    /schedule swap-players            Swap two players' slot assignments on a given day
    /schedule waitlist [day]          Show players without an assignment
"""

import logging
from datetime import datetime, timedelta, timezone

import discord
from discord import app_commands
from discord.ext import commands
from sqlalchemy import func, select

from bot.config import ADMIN_ROLE
from bot.cycle import get_cycle_dates
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
    Slot,
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
    """Return the most-recent event (any phase), or None.

    Events never expire automatically; the admin controls all transitions.
    By convention only one event exists at a time.
    """
    result = await session.execute(
        select(Event).order_by(Event.day1_date.desc())
    )
    return result.scalars().first()


def _parse_date_arg(raw: str | None) -> datetime:
    """Parse YYYY-MM-DD into a UTC datetime at 00:00. Defaults to tomorrow."""
    if raw is None:
        tomorrow = datetime.now(timezone.utc) + timedelta(days=1)
        return tomorrow.replace(hour=0, minute=0, second=0, microsecond=0)
    try:
        dt = datetime.strptime(raw, "%Y-%m-%d")
    except ValueError as e:
        raise ValueError(f"Could not parse {raw!r} as YYYY-MM-DD: {e}") from e
    return dt.replace(tzinfo=timezone.utc)


def _parse_hhmm(raw: str) -> tuple[int, int] | None:
    """Parse 'HH:MM' string into (hour, minute), or None on failure."""
    parts = raw.strip().split(":")
    if len(parts) != 2:
        return None
    try:
        h, m = int(parts[0]), int(parts[1])
        if 0 <= h <= 23 and 0 <= m <= 59:
            return h, m
    except ValueError:
        pass
    return None


def _is_editable_slot(slot: Slot, event: Event, now: datetime) -> bool:
    """Slots can be freely edited during LOCKED. During PUBLISHED, only future slots."""
    if event.phase == EventPhase.LOCKED:
        return True
    return slot.start_time >= now


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
                await interaction.response.send_message(
                    "No active event. Use `/schedule create day1:YYYY-MM-DD` to open submissions.\n"
                    "Use `/schedule test` to create a test event."
                )
                return

            day1 = event.day1_date
            dates = get_cycle_dates(day1)
            test_tag = " (TEST EVENT)" if event.is_test else ""

            lines = [
                f"**Cycle Status**{test_tag}",
                f"Day 1: {day1.strftime('%A, %B %d, %Y')}",
                f"Phase: **{event.phase.value}**",
                "",
                f"Submissions open: {dates['submissions_open'].strftime('%b %d, %H:%M UTC')}",
                f"Lock offset:      {dates['lock'].strftime('%b %d, %H:%M UTC')}",
                f"Day 1 blocks:     {dates['day1_start'].strftime('%b %d, %H:%M')} – {dates['day1_end'].strftime('%b %d, %H:%M UTC')}",
                f"Day 2 blocks:     {dates['day2_start'].strftime('%b %d, %H:%M')} – {dates['day2_end'].strftime('%b %d, %H:%M UTC')}",
                f"Day 4 blocks:     {dates['day4_start'].strftime('%b %d, %H:%M')} – {dates['day4_end'].strftime('%b %d, %H:%M UTC')}",
            ]

            if event.locked_at:
                lines.append(f"Locked at:        {event.locked_at.strftime('%b %d, %H:%M UTC')}")
            if event.published_at:
                lines.append(f"Published at:     {event.published_at.strftime('%b %d, %H:%M UTC')}")

            event_id = event.event_id

            total_subs = (await session.execute(
                select(func.count()).select_from(Submission).where(
                    Submission.event_id == event_id
                )
            )).scalar()
            complete_subs = (await session.execute(
                select(func.count()).select_from(Submission).where(
                    Submission.event_id == event_id,
                    Submission.has_screenshot == True,    # noqa: E712
                    Submission.has_availability == True,  # noqa: E712
                    Submission.has_player_id == True,     # noqa: E712
                )
            )).scalar()
            total_assignments = (await session.execute(
                select(func.count()).select_from(Assignment).where(
                    Assignment.event_id == event_id
                )
            )).scalar()
            pending_changes = (await session.execute(
                select(func.count()).select_from(ChangeRequest).where(
                    ChangeRequest.event_id == event_id,
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

        lines = [f"**Lookup: {player.display_name}** (discord: {player.id})", ""]
        lines.append("**Submission**")
        complete_flags = []
        if submission.has_screenshot:
            complete_flags.append("screenshot ✓")
        else:
            complete_flags.append("screenshot ✗")
        if submission.has_availability:
            complete_flags.append("availability ✓")
        else:
            complete_flags.append("availability ✗")
        if submission.has_player_id:
            complete_flags.append("player ID ✓")
        else:
            complete_flags.append("player ID ✗")
        lines.append("  ".join(complete_flags))

        if submission.player_ingame_id:
            lines.append(f"In-game ID: {submission.player_ingame_id}")

        if submission.has_screenshot:
            lines.append(
                f"Speedups: construction={submission.speedup_construction or 0:.1f}, "
                f"research={submission.speedup_research or 0:.1f}, "
                f"training={submission.speedup_training or 0:.1f}, "
                f"general={submission.speedup_general or 0:.1f}"
            )
            ttg = submission.ttg or 0
            tg = submission.tg or 0
            dust = submission.dust or 0
            if ttg or tg or dust:
                lines.append(f"Resources: TTG={ttg:.0f}, TG={tg:.0f}, Dust={dust:.0f}")
            lines.append(
                f"Priority: x={submission.priority_x or 0:.0f}pts (D1), "
                f"y={submission.priority_y or 0:.0f}pts (D2), "
                f"z={submission.priority_z or 0:.2f}d (D4)"
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

    # ─── create ──────────────────────────────────────────────

    @schedule.command(
        name="create",
        description="Create a new event and open submissions",
    )
    @app_commands.describe(
        day1="Day 1 date in YYYY-MM-DD (UTC). Required.",
    )
    @is_admin()
    async def schedule_create(
        self,
        interaction: discord.Interaction,
        day1: str,
    ):
        try:
            day1_dt = _parse_date_arg(day1)
        except ValueError as e:
            await interaction.response.send_message(f"Bad date: {e}")
            return

        async with async_session() as session:
            existing = await _get_active_event(session)
            if existing is not None:
                tag = "test event" if existing.is_test else "event"
                await interaction.response.send_message(
                    f"Cannot create an event while another {tag} is active "
                    f"(Day 1 = {existing.day1_date.date()}, phase={existing.phase.value}). "
                    f"Run /schedule reset confirm:True first."
                )
                return

            collide = await session.execute(
                select(Event).where(Event.day1_date == day1_dt)
            )
            if collide.scalar_one_or_none() is not None:
                await interaction.response.send_message(
                    f"An event already exists in the DB for Day 1 = {day1_dt.date()}. "
                    f"Pick a different date."
                )
                return

            event = await create_event(session, day1_dt, is_test=False)
            await session.commit()
            event_id = event.event_id

        submissions_cog = self.bot.get_cog("Submissions")
        if submissions_cog is not None and hasattr(submissions_cog, "announce_event_opened"):
            async with async_session() as session:
                fresh_event = await session.get(Event, event_id)
                await submissions_cog.announce_event_opened(fresh_event)

        await interaction.response.send_message(
            f"Event created (Day 1 = {day1_dt.date()}, event_id={event_id}, phase=collecting). "
            f"Submissions are now open."
        )
        logger.info(f"Admin {interaction.user} created event {event_id} (day1={day1_dt.date()})")

    # ─── test ────────────────────────────────────────────────

    @schedule.command(
        name="test",
        description="Create a test event (admin-driven; does not affect real cycle)",
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
            day1_dt = _parse_date_arg(date)
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
                    f"Run /schedule reset confirm:True first."
                )
                return

            collide = await session.execute(
                select(Event).where(Event.day1_date == day1_dt)
            )
            if collide.scalar_one_or_none() is not None:
                await interaction.response.send_message(
                    f"An event already exists in the DB for Day 1 = {day1_dt.date()}. "
                    f"Pick a different date."
                )
                return

            event = await create_event(session, day1_dt, is_test=True)
            await session.commit()
            event_id = event.event_id

        submissions_cog = self.bot.get_cog("Submissions")
        if submissions_cog is not None and hasattr(submissions_cog, "announce_event_opened"):
            async with async_session() as session:
                fresh_event = await session.get(Event, event_id)
                await submissions_cog.announce_event_opened(fresh_event)

        await interaction.response.send_message(
            f"Test event created (Day 1 = {day1_dt.date()}, event_id={event_id}, "
            f"phase=collecting).\n"
            f"Use /schedule lock to run the optimizer, then /schedule publish to release. "
            f"Use /schedule reset confirm:True to delete it."
        )
        logger.info(
            f"Admin {interaction.user} created test event {event_id} (day1={day1_dt.date()})"
        )

    # ─── lock ────────────────────────────────────────────────

    @schedule.command(
        name="lock",
        description="Run the optimizer and move to LOCKED (admin review phase)",
    )
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
            f"Done — event locked for Day 1 = {event.day1_date.date()}. "
            f"Check #schedule_log for the draft CSV. "
            f"Use `/schedule publish` when ready to notify players."
        )
        logger.info(f"Admin {interaction.user} locked event {event.event_id}")

    # ─── publish ─────────────────────────────────────────────

    @schedule.command(
        name="publish",
        description="Publish the schedule: send player DMs and move to PUBLISHED",
    )
    @is_admin()
    async def schedule_publish(self, interaction: discord.Interaction):
        async with async_session() as session:
            event = await _get_active_event(session)

        if event is None:
            await interaction.response.send_message("No active event.")
            return
        if event.phase != EventPhase.LOCKED:
            await interaction.response.send_message(
                f"Event is not in LOCKED (current phase: {event.phase.value}). "
                f"Run /schedule lock first."
            )
            return

        scheduling_cog = self.bot.get_cog("Scheduling")
        if scheduling_cog is None:
            await interaction.response.send_message("Scheduling cog not loaded.")
            return

        await interaction.response.defer()
        await scheduling_cog.publish(event)
        await interaction.followup.send(
            f"Schedule published for Day 1 = {event.day1_date.date()}. "
            f"Players have been DM'd their assignments."
        )
        logger.info(f"Admin {interaction.user} published event {event.event_id}")

    # ─── unlock ──────────────────────────────────────────────

    @schedule.command(
        name="unlock",
        description="Roll a LOCKED event back to COLLECTING (drops assignments + pending changes)",
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
                    f"Event is not LOCKED (current phase: {event.phase.value}). "
                    f"Unlock only works on LOCKED events."
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
                "This deletes the event plus all its submissions, slots, "
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

        tag = "test event" if is_test else "event"
        await interaction.response.send_message(
            f"Reset complete: deleted {tag} #{event_id} "
            f"(Day 1 = {day1.date()}, was {phase.value})."
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

    # ─── assign ──────────────────────────────────────────────

    @schedule.command(
        name="assign",
        description="Assign a player to a slot (admin override, no ChangeRequest)",
    )
    @app_commands.describe(
        player="Player to assign",
        day="Day number (1, 2, or 4)",
        time="Slot start time in HH:MM UTC (e.g. 23:45, 14:15)",
        track="Track: CM or NA (NA only valid for Day 4; default CM)",
    )
    @is_admin()
    async def schedule_assign(
        self,
        interaction: discord.Interaction,
        player: discord.Member,
        day: int,
        time: str,
        track: str = "CM",
    ):
        now = datetime.now(timezone.utc)
        track = track.upper()

        if day not in (1, 2, 4):
            await interaction.response.send_message("Day must be 1, 2, or 4.")
            return
        if track not in ("CM", "NA"):
            await interaction.response.send_message("Track must be CM or NA.")
            return
        if track == "NA" and day != 4:
            await interaction.response.send_message("NA track is only available on Day 4.")
            return

        parsed = _parse_hhmm(time)
        if parsed is None:
            await interaction.response.send_message("Time must be in HH:MM format (e.g. 23:45).")
            return
        hour, minute = parsed

        async with async_session() as session:
            event = await _get_active_event(session)
            if event is None:
                await interaction.response.send_message("No active event.")
                return
            if event.phase == EventPhase.COLLECTING:
                await interaction.response.send_message(
                    "Cannot edit assignments while event is COLLECTING. Lock it first."
                )
                return

            event_id = event.event_id

            result = await session.execute(
                select(Slot).where(
                    Slot.event_id == event_id,
                    Slot.day == day,
                    Slot.track == track,
                )
            )
            day_slots = list(result.scalars().all())

            target_slot = next(
                (
                    s for s in day_slots
                    if s.start_time.hour == hour and s.start_time.minute == minute
                ),
                None,
            )
            if target_slot is None:
                candidates = sorted({f"{s.start_time.hour:02d}:{s.start_time.minute:02d}" for s in day_slots})
                await interaction.response.send_message(
                    f"No Day {day} {track} slot starts at {time} UTC.\n"
                    f"Slots start at: {', '.join(candidates[:10])}{'…' if len(candidates) > 10 else ''}"
                )
                return

            if not _is_editable_slot(target_slot, event, now):
                await interaction.response.send_message(
                    f"Cannot edit a past slot (started {target_slot.start_time.strftime('%b %d %H:%M UTC')})."
                )
                return

            # Remove whoever currently holds this slot
            await session.execute(
                Assignment.__table__.delete().where(
                    Assignment.event_id == event_id,
                    Assignment.slot_id == target_slot.slot_id,
                )
            )

            # Remove player's existing assignment on this day+track (one per day+track)
            player_existing = await session.execute(
                select(Assignment)
                .join(Slot, Assignment.slot_id == Slot.slot_id)
                .where(
                    Assignment.event_id == event_id,
                    Assignment.discord_id == player.id,
                    Slot.day == day,
                    Slot.track == track,
                )
            )
            for old_assn in player_existing.scalars().all():
                await session.delete(old_assn)

            session.add(Assignment(
                event_id=event_id,
                slot_id=target_slot.slot_id,
                discord_id=player.id,
            ))
            session.add(AuditLog(
                event_id=event_id,
                action="Admin assigned player to slot",
                actor=str(interaction.user),
                details={
                    "player": player.id,
                    "slot_id": target_slot.slot_id,
                },
            ))
            await session.commit()

        start_str = target_slot.start_time.strftime("%a %b %d, %H:%M UTC")
        await interaction.response.send_message(
            f"Assigned {player.display_name} → [{target_slot.slot_id}] ({start_str})."
        )
        logger.info(
            f"Admin {interaction.user} assigned {player.id} to {target_slot.slot_id}"
        )

    # ─── remove-player ───────────────────────────────────────

    @schedule.command(
        name="remove-player",
        description="Remove a player's slot assignment(s) on a given day",
    )
    @app_commands.describe(
        player="Player to unassign",
        day="Day number (1, 2, or 4)",
        track="Track to remove: CM or NA. Leave blank to remove all slots on that day.",
    )
    @is_admin()
    async def schedule_remove_player(
        self,
        interaction: discord.Interaction,
        player: discord.Member,
        day: int,
        track: str | None = None,
    ):
        now = datetime.now(timezone.utc)

        if day not in (1, 2, 4):
            await interaction.response.send_message("Day must be 1, 2, or 4.")
            return
        if track is not None:
            track = track.upper()
            if track not in ("CM", "NA"):
                await interaction.response.send_message("Track must be CM or NA.")
                return

        async with async_session() as session:
            event = await _get_active_event(session)
            if event is None:
                await interaction.response.send_message("No active event.")
                return
            if event.phase == EventPhase.COLLECTING:
                await interaction.response.send_message(
                    "Cannot edit assignments while event is COLLECTING."
                )
                return

            event_id = event.event_id

            q = (
                select(Assignment, Slot)
                .join(Slot, Assignment.slot_id == Slot.slot_id)
                .where(
                    Assignment.event_id == event_id,
                    Assignment.discord_id == player.id,
                    Slot.day == day,
                )
            )
            if track is not None:
                q = q.where(Slot.track == track)

            result = await session.execute(q)
            rows = result.all()

            if not rows:
                desc = f"Day {day}" + (f" {track}" if track else "")
                await interaction.response.send_message(
                    f"{player.display_name} has no assignments on {desc}."
                )
                return

            removed = []
            blocked = []
            for assn, slot in rows:
                if not _is_editable_slot(slot, event, now):
                    blocked.append(slot.slot_id)
                    continue
                removed.append(slot.slot_id)
                await session.delete(assn)

            if removed:
                session.add(AuditLog(
                    event_id=event_id,
                    action="Admin removed player from slot(s)",
                    actor=str(interaction.user),
                    details={"player": player.id, "slots": removed},
                ))

            await session.commit()

        parts = []
        if removed:
            parts.append(f"Removed {player.display_name} from: {', '.join(removed)}.")
        if blocked:
            parts.append(f"Skipped past slots: {', '.join(blocked)}.")
        await interaction.response.send_message(" ".join(parts) or "Nothing changed.")
        logger.info(
            f"Admin {interaction.user} removed {player.id} from day {day} "
            f"slots: {removed}, blocked: {blocked}"
        )

    # ─── swap-players ─────────────────────────────────────────

    @schedule.command(
        name="swap-players",
        description="Swap two players' slot assignments on a given day",
    )
    @app_commands.describe(
        player_a="First player",
        player_b="Second player",
        day="Day number (1, 2, or 4)",
    )
    @is_admin()
    async def schedule_swap_players(
        self,
        interaction: discord.Interaction,
        player_a: discord.Member,
        player_b: discord.Member,
        day: int,
    ):
        now = datetime.now(timezone.utc)

        if day not in (1, 2, 4):
            await interaction.response.send_message("Day must be 1, 2, or 4.")
            return
        if player_a.id == player_b.id:
            await interaction.response.send_message("Cannot swap a player with themselves.")
            return

        async with async_session() as session:
            event = await _get_active_event(session)
            if event is None:
                await interaction.response.send_message("No active event.")
                return
            if event.phase == EventPhase.COLLECTING:
                await interaction.response.send_message(
                    "Cannot edit assignments while event is COLLECTING."
                )
                return

            event_id = event.event_id

            def _get_assns(discord_id: int):
                return (
                    select(Assignment, Slot)
                    .join(Slot, Assignment.slot_id == Slot.slot_id)
                    .where(
                        Assignment.event_id == event_id,
                        Assignment.discord_id == discord_id,
                        Slot.day == day,
                    )
                )

            a_rows = (await session.execute(_get_assns(player_a.id))).all()
            b_rows = (await session.execute(_get_assns(player_b.id))).all()

            if not a_rows and not b_rows:
                await interaction.response.send_message(
                    f"Neither {player_a.display_name} nor {player_b.display_name} "
                    f"has a Day {day} assignment."
                )
                return

            # Check all involved slots are editable
            all_rows = a_rows + b_rows
            blocked = [slot.slot_id for _, slot in all_rows if not _is_editable_slot(slot, event, now)]
            if blocked:
                await interaction.response.send_message(
                    f"Cannot swap — these slots have already started: {', '.join(blocked)}."
                )
                return

            # Collect slot_ids for each player
            a_slot_ids = [slot.slot_id for _, slot in a_rows]
            b_slot_ids = [slot.slot_id for _, slot in b_rows]

            # Delete all existing assignments for both players on this day
            for assn, _ in a_rows:
                await session.delete(assn)
            for assn, _ in b_rows:
                await session.delete(assn)

            await session.flush()

            # Reassign: A gets B's old slots, B gets A's old slots
            for slot_id in b_slot_ids:
                session.add(Assignment(event_id=event_id, slot_id=slot_id, discord_id=player_a.id))
            for slot_id in a_slot_ids:
                session.add(Assignment(event_id=event_id, slot_id=slot_id, discord_id=player_b.id))

            session.add(AuditLog(
                event_id=event_id,
                action="Admin swapped players' assignments",
                actor=str(interaction.user),
                details={
                    "player_a": player_a.id,
                    "player_b": player_b.id,
                    "day": day,
                    "a_slots": a_slot_ids,
                    "b_slots": b_slot_ids,
                },
            ))
            await session.commit()

        lines = [f"Swapped Day {day} assignments between {player_a.display_name} and {player_b.display_name}:"]
        if a_slot_ids and b_slot_ids:
            lines.append(f"• {player_a.display_name} now has: {', '.join(b_slot_ids)}")
            lines.append(f"• {player_b.display_name} now has: {', '.join(a_slot_ids)}")
        elif b_slot_ids:
            lines.append(f"• {player_a.display_name} now has: {', '.join(b_slot_ids)}")
            lines.append(f"• {player_b.display_name} has no Day {day} slot (was unassigned)")
        else:
            lines.append(f"• {player_a.display_name} has no Day {day} slot (was unassigned)")
            lines.append(f"• {player_b.display_name} now has: {', '.join(a_slot_ids)}")

        await interaction.response.send_message("\n".join(lines))
        logger.info(
            f"Admin {interaction.user} swapped {player_a.id} and {player_b.id} on day {day}"
        )

    # ─── incomplete ───────────────────────────────────────────

    @schedule.command(
        name="incomplete",
        description="List players with incomplete submissions",
    )
    @is_admin()
    async def schedule_incomplete(self, interaction: discord.Interaction):
        async with async_session() as session:
            event = await _get_active_event(session)
            if event is None:
                await interaction.response.send_message("No active event.")
                return

            result = await session.execute(
                select(Submission).where(Submission.event_id == event.event_id)
            )
            all_subs = list(result.scalars().all())

        incomplete = [
            s for s in all_subs
            if not (s.has_screenshot and s.has_availability and s.has_player_id and s.has_resources)
        ]

        if not incomplete:
            await interaction.response.send_message("All submissions are complete.")
            return

        lines = [f"**{len(incomplete)} incomplete submission(s):**\n"]
        for sub in incomplete:
            member = interaction.guild.get_member(sub.discord_id)
            name = member.display_name if member else str(sub.discord_id)
            missing = []
            if not sub.has_screenshot:
                missing.append("screenshot")
            if not sub.has_availability:
                missing.append("availability")
            if not sub.has_player_id:
                missing.append("player ID")
            if not sub.has_resources:
                missing.append("resources")
            lines.append(f"• **{name}** — missing: {', '.join(missing)}")

        await interaction.response.send_message("\n".join(lines))

    # ─── nudge ────────────────────────────────────────────────

    @schedule.command(
        name="nudge",
        description="DM players with incomplete submissions about what they're still missing",
    )
    @app_commands.describe(
        player="DM only this player. Leave blank to nudge all incomplete players.",
    )
    @is_admin()
    async def schedule_nudge(
        self,
        interaction: discord.Interaction,
        player: discord.Member | None = None,
    ):
        async with async_session() as session:
            event = await _get_active_event(session)
            if event is None:
                await interaction.response.send_message("No active event.")
                return

            if player is not None:
                result = await session.execute(
                    select(Submission).where(
                        Submission.event_id == event.event_id,
                        Submission.discord_id == player.id,
                    )
                )
                targets = list(result.scalars().all())
            else:
                result = await session.execute(
                    select(Submission).where(Submission.event_id == event.event_id)
                )
                targets = [
                    s for s in result.scalars().all()
                    if not (s.has_screenshot and s.has_availability and s.has_player_id and s.has_resources)
                ]

        from bot.config import SCHEDULING_CHANNEL
        sent, skipped = 0, 0
        for sub in targets:
            if sub.has_screenshot and sub.has_availability and sub.has_player_id and sub.has_resources:
                continue
            missing = []
            if not sub.has_screenshot:
                missing.append("a **screenshot** of your in-game Speedups page")
            if not sub.has_availability:
                missing.append("your **availability** for Day 1, Day 2, or Day 4")
            if not sub.has_player_id:
                missing.append("your **in-game player ID**")
            if not sub.has_resources:
                missing.append("your **TTG, TG, and Dust** counts (say '0 TTG, 0 TG, 0 Dust' if you have none)")

            member = interaction.guild.get_member(sub.discord_id)
            if member is None:
                skipped += 1
                continue

            dm_lines = [
                f"Hey {member.display_name}! Your submission for the upcoming KvK schedule is not yet complete.",
                "",
                "**You still need to provide:**",
            ]
            for item in missing:
                dm_lines.append(f"• {item}")
            dm_lines.append("")
            dm_lines.append(f"Please @mention the bot in #{SCHEDULING_CHANNEL} with the missing info.")

            try:
                await member.send("\n".join(dm_lines))
                sent += 1
            except discord.Forbidden:
                skipped += 1

        parts = [f"Nudged {sent} player(s)."]
        if skipped:
            parts.append(f"{skipped} couldn't be DM'd (not in server or DMs disabled).")
        await interaction.response.send_message(" ".join(parts))
        logger.info(f"Admin {interaction.user} nudged {sent} players (skipped {skipped})")

    # ─── admin-submit ─────────────────────────────────────────

    @schedule.command(
        name="admin-submit",
        description="Create or update a full submission on behalf of a player (e.g. no-Discord members)",
    )
    @app_commands.describe(
        player="The player to submit for",
        construction="Construction Speedups in days",
        research="Research Speedups in days",
        training="Training/Soldier Speedups in days",
        general="General (wildcard) Speedups in days",
        ttg="Tempered Truegold count (0 if none)",
        tg="Truegold count (0 if none)",
        dust="Truegold Dust count (0 if none)",
        player_id="In-game numeric player ID (6–12 digits)",
        available_days="Comma-separated days to mark fully available (e.g. '1,2,4'). Default: all days.",
    )
    @is_admin()
    async def schedule_admin_submit(
        self,
        interaction: discord.Interaction,
        player: discord.Member,
        construction: float,
        research: float,
        training: float,
        general: float,
        ttg: int,
        tg: int,
        dust: int,
        player_id: str,
        available_days: str = "1,2,4",
    ):
        import re as _re
        if not _re.match(r"^\d{6,12}$", player_id.strip()):
            await interaction.response.send_message(
                f"Invalid player ID '{player_id}' — expected 6–12 digits."
            )
            return

        days_raw = [d.strip() for d in available_days.split(",")]
        valid_days = []
        for d in days_raw:
            try:
                n = int(d)
                if n in (1, 2, 4):
                    valid_days.append(n)
            except ValueError:
                pass
        if not valid_days:
            await interaction.response.send_message(
                "available_days must contain at least one of: 1, 2, 4."
            )
            return

        async with async_session() as session:
            event = await _get_active_event(session)
            if event is None:
                await interaction.response.send_message("No active event.")
                return
            if event.phase not in (EventPhase.COLLECTING, EventPhase.LOCKED):
                await interaction.response.send_message(
                    f"admin-submit only works during COLLECTING or LOCKED "
                    f"(current phase: {event.phase.value})."
                )
                return

            event_id = event.event_id
            day1 = event.day1_date

            # Collect all slot IDs for the requested days
            slot_result = await session.execute(
                select(Slot).where(
                    Slot.event_id == event_id,
                    Slot.day.in_(valid_days),
                )
            )
            all_day_slots = list(slot_result.scalars().all())
            availability = [s.slot_id for s in all_day_slots]

            # Upsert submission
            sub_result = await session.execute(
                select(Submission).where(
                    Submission.event_id == event_id,
                    Submission.discord_id == player.id,
                )
            )
            sub = sub_result.scalar_one_or_none()
            if sub is None:
                sub = Submission(
                    event_id=event_id,
                    discord_id=player.id,
                    discord_name=player.display_name,
                )
                session.add(sub)

            sub.discord_name = player.display_name
            sub.speedup_construction = float(construction)
            sub.speedup_research = float(research)
            sub.speedup_training = float(training)
            sub.speedup_general = float(general)
            sub.has_screenshot = True
            sub.ttg = float(ttg)
            sub.tg = float(tg)
            sub.dust = float(dust)
            sub.has_resources = True
            sub.player_ingame_id = player_id.strip()
            sub.has_player_id = True
            sub.availability = availability
            sub.has_availability = bool(availability)
            sub.compute_priorities()

            # Update cross-event PlayerProfile
            from bot.llm.handlers_collecting import _upsert_player_profile
            await session.flush()
            await _upsert_player_profile(session, player.id, player_id.strip())

            session.add(AuditLog(
                event_id=event_id,
                action="Admin created/updated submission on behalf of player",
                actor=str(interaction.user),
                details={"player": player.id, "days": valid_days},
            ))
            await session.commit()

        await interaction.response.send_message(
            f"Submission set for **{player.display_name}**:\n"
            f"Speedups: construction={construction}, research={research}, "
            f"training={training}, general={general}\n"
            f"Resources: TTG={ttg}, TG={tg}, Dust={dust}\n"
            f"Player ID: {player_id.strip()}\n"
            f"Available days: {', '.join(str(d) for d in valid_days)} "
            f"({len(availability)} slots)\n"
            f"Priorities: D1={sub.priority_x:.0f}pts, D2={sub.priority_y:.0f}pts, "
            f"D4={sub.priority_z:.2f}d"
        )
        logger.info(
            f"Admin {interaction.user} admin-submitted for {player.id} "
            f"days={valid_days} id={player_id.strip()}"
        )

    # ─── waitlist ─────────────────────────────────────────────

    @schedule.command(
        name="waitlist",
        description="Show players who submitted but have no assignment (optionally filtered by day)",
    )
    @app_commands.describe(
        day="Filter by day (1, 2, or 4). Leave blank to show players with no assignment at all.",
    )
    @is_admin()
    async def schedule_waitlist(
        self,
        interaction: discord.Interaction,
        day: int | None = None,
    ):
        if day is not None and day not in (1, 2, 4):
            await interaction.response.send_message("Day must be 1, 2, or 4.")
            return

        async with async_session() as session:
            event = await _get_active_event(session)
            if event is None:
                await interaction.response.send_message("No active event.")
                return

            event_id = event.event_id

            # All submissions
            subs_result = await session.execute(
                select(Submission).where(Submission.event_id == event_id)
            )
            all_subs = list(subs_result.scalars().all())

            # Assigned discord_ids, optionally filtered by day
            assn_q = select(Assignment.discord_id).where(Assignment.event_id == event_id)
            if day is not None:
                assn_q = (
                    select(Assignment.discord_id)
                    .join(Slot, Assignment.slot_id == Slot.slot_id)
                    .where(Assignment.event_id == event_id, Slot.day == day)
                )
            assigned_result = await session.execute(assn_q)
            assigned_ids = set(assigned_result.scalars().all())

        unassigned = [s for s in all_subs if s.discord_id not in assigned_ids]

        if not unassigned:
            scope = f"Day {day}" if day else "any day"
            await interaction.response.send_message(
                f"All players with submissions are assigned on {scope}."
            )
            return

        scope_label = f"Day {day}" if day else "all days"
        lines = [f"**{len(unassigned)} player(s) without a slot ({scope_label}):**\n"]
        for sub in sorted(unassigned, key=lambda s: (s.priority_x or 0), reverse=True):
            member = interaction.guild.get_member(sub.discord_id)
            name = member.display_name if member else str(sub.discord_id)
            complete = "✓" if (sub.has_screenshot and sub.has_availability and sub.has_player_id) else "✗"
            lines.append(
                f"• {name} [{complete}complete] "
                f"— D1={sub.priority_x or 0:.0f}pts "
                f"D2={sub.priority_y or 0:.0f}pts "
                f"D4={sub.priority_z or 0:.1f}d"
            )

        await interaction.response.send_message("\n".join(lines))


async def setup(bot: commands.Bot):
    await bot.add_cog(Admin(bot))
