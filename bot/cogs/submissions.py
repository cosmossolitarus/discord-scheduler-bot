"""
Submissions cog.

Handles:
    - Opening submissions (pinging @player in #scheduling)
    - Processing user @mentions with screenshots and/or availability text
    - Echoing parsed data back for confirmation
    - Storing submissions in the database
    - Prompting for missing info (screenshot or availability)
"""

import logging
from datetime import datetime, timezone

import discord
from discord.ext import commands
from sqlalchemy import select

from bot.config import PLAYER_ROLE, SCHEDULING_CHANNEL, SCHEDULE_LOG_CHANNEL, GENERIC_SPLIT
from bot.database import async_session
from bot.models import Event, Submission, Slot, EventPhase, AuditLog
from bot.cycle import (
    get_current_phase, get_current_cycle_day1, get_cycle_dates,
    generate_slot_times, Phase,
)
from bot.llm.screenshot import parse_screenshot
from bot.llm.availability import parse_availability
from bot.llm.utils import classify_message, off_day_reply, BASIC_PROMPT_REPLY

logger = logging.getLogger("scheduler.submissions")


class Submissions(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def open_submissions(self, day1: datetime):
        """
        Open submissions for an event. Called by the lifecycle loop.
        Idempotent — checks if event already exists.
        """
        async with async_session() as session:
            result = await session.execute(
                select(Event).where(Event.day1_date == day1)
            )
            event = result.scalar_one_or_none()
            if event is not None:
                return  # Already created

            event = Event(day1_date=day1, phase=EventPhase.COLLECTING)
            session.add(event)
            await session.flush()

            slot_defs = generate_slot_times(day1)
            for sd in slot_defs:
                slot = Slot(
                    slot_id=sd["slot_id"],
                    event_id=event.event_id,
                    day=sd["day"],
                    track=sd["track"],
                    slot_index=sd["slot_index"],
                    start_time=sd["start_time"],
                    end_time=sd["end_time"],
                )
                session.add(slot)

            session.add(AuditLog(
                event_id=event.event_id,
                action="Submissions opened",
                actor="system",
                details={"day1": day1.isoformat()},
            ))

            await session.commit()
            logger.info(f"Event created for Day 1 = {day1.date()}")

        for guild in self.bot.guilds:
            channel = discord.utils.get(guild.text_channels, name=SCHEDULING_CHANNEL)
            role = discord.utils.get(guild.roles, name=PLAYER_ROLE)
            if channel and role:
                day1_str = day1.strftime("%A, %B %d, %Y")
                lock_date = get_cycle_dates(day1)["lock"]
                lock_str = lock_date.strftime("%A, %B %d at %H:%M UTC")
                await channel.send(
                    f"{role.mention} — Scheduling is open for the event starting "
                    f"**{day1_str}**!\n\n"
                    f"To submit, @mention me in this channel with:\n"
                    f"1. A screenshot of your resources\n"
                    f"2. Your available times for each day (in UTC)\n\n"
                    f"You can send both together or separately. "
                    f"Example: \"@scheduler Day 1 anytime after 14:00, "
                    f"Day 2 10:00-18:00, Day 4 all day\"\n\n"
                    f"Submissions close automatically on **{lock_str}**."
                )

    # ─── Message Router ──────────────────────────────────────────

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        """Process messages that @mention the bot."""
        if message.author.bot:
            return
        if self.bot.user not in message.mentions:
            return
        if message.channel.name != SCHEDULING_CHANNEL:
            return

        phase, day1 = get_current_phase()

        # Check DB phase too — handles force_lock ahead of calendar
        db_phase = None
        async with async_session() as session:
            result = await session.execute(
                select(Event).where(Event.day1_date == day1)
            )
            event = result.scalar_one_or_none()
            if event is not None:
                db_phase = event.phase

        if phase == Phase.IDLE and db_phase is None:
            await message.reply(
                "There's no active scheduling period right now. "
                "I'll ping everyone when the next one opens."
            )
            return

        effective_locked = (
            db_phase in (EventPhase.LOCKED, EventPhase.ACTIVE)
            or phase in (Phase.LOCKED, Phase.ACTIVE)
        )

        if effective_locked:
            changes_cog = self.bot.get_cog("Changes")
            if changes_cog:
                await changes_cog.handle_change_request(message, day1)
            return

        await self._process_submission(message, day1)

    # ─── Submission Processing ───────────────────────────────────

    async def _process_submission(self, message: discord.Message, day1: datetime):
        """Process a submission during the collecting phase."""
        async with async_session() as session:
            # Get or create event
            result = await session.execute(
                select(Event).where(Event.day1_date == day1)
            )
            event = result.scalar_one_or_none()
            if event is None:
                await message.reply("Something went wrong — no event found. Please contact an admin.")
                return

            # Get or create submission
            result = await session.execute(
                select(Submission).where(
                    Submission.event_id == event.event_id,
                    Submission.discord_id == message.author.id,
                )
            )
            submission = result.scalar_one_or_none()

            if submission is None:
                submission = Submission(
                    event_id=event.event_id,
                    discord_id=message.author.id,
                    discord_name=message.author.display_name,
                )
                session.add(submission)
                await session.flush()

            # ── Process screenshot (if attached) ──

            screenshot_result = None
            if message.attachments:
                for attachment in message.attachments:
                    if attachment.content_type and attachment.content_type.startswith("image/"):
                        image_data = await attachment.read()
                        screenshot_result = await parse_screenshot(
                            image_data, attachment.content_type
                        )
                        break

            # ── Extract and classify text ──

            text_content = message.content
            for mention in message.mentions:
                text_content = text_content.replace(
                    f"<@{mention.id}>", ""
                ).replace(
                    f"<@!{mention.id}>", ""
                )
            text_content = text_content.strip()

            availability_result = None
            text_type = None

            if text_content:
                triage = await classify_message(text_content)
                text_type = triage.get("type")

                if text_type == "query" and screenshot_result is None:
                    await self._handle_query(message, submission)
                    return

                if text_type == "off_day" and screenshot_result is None:
                    await message.reply(off_day_reply(triage.get("days", [])))
                    return

                if text_type == "other" and screenshot_result is None:
                    await message.reply(BASIC_PROMPT_REPLY)
                    return

                # "availability" — or any type when a screenshot is also attached
                if text_type == "availability":
                    slot_reference = generate_slot_times(day1)
                    day1_str = day1.strftime("%B %d, %Y")
                    availability_result = await parse_availability(
                        text_content, day1_str, slot_reference
                    )

            # ── Build response ──

            response_lines = []
            screenshot_ok = False
            availability_ok = False

            # Screenshot results
            if screenshot_result is not None:
                if "error" in screenshot_result:
                    response_lines.append(
                        f"**Screenshot:** {screenshot_result['error']}\n"
                        f"Please try again with a clearer screenshot."
                    )
                else:
                    submission.resource_x = screenshot_result["resource_x"]
                    submission.resource_y = screenshot_result["resource_y"]
                    submission.resource_z = screenshot_result["resource_z"]
                    submission.resource_generic = screenshot_result["resource_generic"]
                    submission.compute_priorities(GENERIC_SPLIT)
                    submission.has_screenshot = True
                    submission.screenshot_url = message.attachments[0].url
                    response_lines.append(
                        f"**Speedups:** General {submission.resource_generic:.1f}d · "
                        f"Construction {submission.resource_x:.1f}d · "
                        f"Research {submission.resource_y:.1f}d · "
                        f"Troops {submission.resource_z:.1f}d"
                    )
                    screenshot_ok = True

            # Availability results
            if availability_result is not None:
                if "error" in availability_result:
                    response_lines.append(
                        f"**Availability:** {availability_result['error']}\n"
                        f"Please try again — describe your available times for "
                        f"Day 1, Day 2, and/or Day 4."
                    )
                else:
                    new_slots = availability_result["available_slots"]

                    # Merge: keep existing slots for days not mentioned
                    existing_slots = submission.availability or []
                    if existing_slots and new_slots:
                        new_days = {sid.split("-")[0] for sid in new_slots}
                        old_days = {sid.split("-")[0] for sid in existing_slots}
                        unmentioned_days = old_days - new_days
                        if unmentioned_days:
                            kept = [
                                sid for sid in existing_slots
                                if sid.split("-")[0] in unmentioned_days
                            ]
                            new_slots = sorted(set(kept + new_slots))

                    submission.availability = new_slots
                    submission.has_availability = True
                    submission.raw_availability_text = text_content
                    summary = availability_result.get("player_summary", "")
                    response_lines.append(f"**Availability:**\n{summary}")
                    availability_ok = True

            # Nothing processed
            if not response_lines:
                await message.reply(
                    "I didn't find a screenshot or availability info in your message. "
                    "Please send a screenshot of your resources and/or describe your "
                    "available times."
                )
                return

            # ── Status indicator + missing info ──

            has_error = (
                (screenshot_result is not None and not screenshot_ok)
                or (availability_result is not None and not availability_ok)
            )

            missing = []
            if not submission.has_screenshot:
                missing.append("a **screenshot** of your resources")
            if not submission.has_availability:
                missing.append("your **available times** for each day")

            if has_error:
                status = "❌"
            elif missing:
                status = "⏳"
            else:
                status = "✅"

            body = "\n".join(response_lines)

            if missing:
                body += (
                    f"\n\nI still need {' and '.join(missing)}. "
                    f"Just @mention me again with the missing info."
                )
            elif not has_error:
                body += "\n\nSubmission complete — to update anything, just @mention me again."

            submission.updated_at = datetime.now(timezone.utc)

            session.add(AuditLog(
                event_id=event.event_id,
                action="Submission updated",
                actor=str(message.author.id),
                details={
                    "has_screenshot": submission.has_screenshot,
                    "has_availability": submission.has_availability,
                    "is_complete": submission.is_complete,
                },
            ))

            await session.commit()

        await message.reply(f"{status} {body}")

    # ─── Query Handler ───────────────────────────────────────────

    async def _handle_query(self, message: discord.Message, submission: Submission):
        """Show the user their current submission data."""
        lines = []

        if submission.has_screenshot:
            lines.append(
                f"**Speedups:** General {submission.resource_generic:.1f}d · "
                f"Construction {submission.resource_x:.1f}d · "
                f"Research {submission.resource_y:.1f}d · "
                f"Troops {submission.resource_z:.1f}d"
            )

        if submission.raw_availability_text:
            lines.append(f"**Availability on file:** {submission.raw_availability_text}")

        if not lines:
            lines.append("I don't have any data for you yet.")

        missing = []
        if not submission.has_screenshot:
            missing.append("screenshot")
        if not submission.has_availability:
            missing.append("availability")
        if missing:
            lines.append(f"Still needed: {', '.join(missing)}")

        await message.reply("\n".join(lines))


async def setup(bot: commands.Bot):
    await bot.add_cog(Submissions(bot))
