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
            # Check if event already exists
            result = await session.execute(
                select(Event).where(Event.day1_date == day1)
            )
            event = result.scalar_one_or_none()
            if event is not None:
                return  # Already created

            # Create event
            event = Event(day1_date=day1, phase=EventPhase.COLLECTING)
            session.add(event)
            await session.flush()

            # Generate slots
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

            # Audit log
            session.add(AuditLog(
                event_id=event.event_id,
                action="Submissions opened",
                actor="system",
                details={"day1": day1.isoformat()},
            ))

            await session.commit()
            logger.info(f"Event created for Day 1 = {day1.date()}")

        # Ping @player in #scheduling
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

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        """Process messages that @mention the bot."""
        # Ignore messages from bots (including self)
        if message.author.bot:
            return

        # Only process if bot is mentioned
        if self.bot.user not in message.mentions:
            return

        # Only process in #scheduling channel
        if message.channel.name != SCHEDULING_CHANNEL:
            return

        phase, day1 = get_current_phase()

        if phase == Phase.IDLE:
            await message.reply(
                "There's no active scheduling period right now. "
                "I'll ping everyone when the next one opens."
            )
            return

        # Determine if this is a submission or a change request
        if phase in (Phase.LOCKED, Phase.ACTIVE):
            # Post-lock: route to changes cog
            changes_cog = self.bot.get_cog("Changes")
            if changes_cog:
                await changes_cog.handle_change_request(message, day1)
            return

        # Phase is COLLECTING — process submission
        await self._process_submission(message, day1)

    async def _process_submission(self, message: discord.Message, day1: datetime):
        """Process a submission during the collecting phase."""
        async with async_session() as session:
            # Get or create the event
            result = await session.execute(
                select(Event).where(Event.day1_date == day1)
            )
            event = result.scalar_one_or_none()
            if event is None:
                await message.reply("Something went wrong — no event found. Please contact an admin.")
                return

            # Get or create submission for this user
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

            # Check for screenshot attachment
            screenshot_result = None
            if message.attachments:
                for attachment in message.attachments:
                    if attachment.content_type and attachment.content_type.startswith("image/"):
                        image_data = await attachment.read()
                        screenshot_result = await parse_screenshot(
                            image_data, attachment.content_type
                        )
                        break

            # Check for availability text (message content minus the @mention)
            text_content = message.content
            # Remove the bot mention from the text
            for mention in message.mentions:
                text_content = text_content.replace(f"<@{mention.id}>", "").replace(f"<@!{mention.id}>", "")
            text_content = text_content.strip()

            availability_result = None
            if text_content:
                slot_reference = generate_slot_times(day1)
                day1_str = day1.strftime("%B %d, %Y")
                availability_result = await parse_availability(
                    text_content, day1_str, slot_reference
                )

            # Handle results
            response_parts = []
            has_error = False

            # Process screenshot
            if screenshot_result is not None:
                if "error" in screenshot_result:
                    response_parts.append(
                        f"❌ **Screenshot:** {screenshot_result['error']}\n"
                        f"Please try again with a clearer screenshot."
                    )
                    has_error = True
                else:
                    submission.resource_x = screenshot_result["resource_x"]
                    submission.resource_y = screenshot_result["resource_y"]
                    submission.resource_z = screenshot_result["resource_z"]
                    submission.resource_generic = screenshot_result["resource_generic"]
                    submission.compute_priorities(GENERIC_SPLIT)
                    submission.has_screenshot = True
                    submission.screenshot_url = message.attachments[0].url
                    response_parts.append(
                        f"✅ **Speedups received:**\n"
                        f"Construction (Day 1): {submission.resource_x:.1f} days\n"
                        f"Research (Day 2): {submission.resource_y:.1f} days\n"
                        f"Soldier Training (Day 4): {submission.resource_z:.1f} days\n"
                        f"General: {submission.resource_generic:.1f} days\n"
                        f"*(Priority — Day 1: {submission.priority_x:.1f}d, "
                        f"Day 2: {submission.priority_y:.1f}d, "
                        f"Day 4: {submission.priority_z:.1f}d)*"
                    )

            # Process availability
            if availability_result is not None:
                if "error" in availability_result:
                    response_parts.append(
                        f"❌ **Availability:** {availability_result['error']}\n"
                        f"Please try again — describe your available times for "
                        f"Day 1, Day 2, and/or Day 4."
                    )
                    has_error = True
                else:
                    submission.availability = availability_result["available_slots"]
                    submission.has_availability = True
                    submission.raw_availability_text = text_content
                    interp = availability_result.get("interpretation", "")
                    response_parts.append(
                        f"✅ **Availability received:**\n"
                        f"{interp}\n"
                        f"({len(availability_result['available_slots'])} slots matched)"
                    )

            # Nothing was submitted
            if not response_parts:
                await message.reply(
                    "I didn't find a screenshot or availability info in your message. "
                    "Please send a screenshot of your resources and/or describe your "
                    "available times."
                )
                return

            # Check what's still missing
            missing_parts = []
            if not submission.has_screenshot:
                missing_parts.append("a **screenshot** of your resources")
            if not submission.has_availability:
                missing_parts.append("your **available times** for each day")

            if missing_parts:
                response_parts.append(
                    f"\n⏳ I still need {' and '.join(missing_parts)}. "
                    f"Just @mention me again with the missing info."
                )
            elif not has_error:
                response_parts.append(
                    "\n✅ Your submission is complete! "
                    "To update anything, just @mention me again."
                )

            submission.updated_at = datetime.now(timezone.utc)

            # Audit log
            session.add(AuditLog(
                event_id=event.event_id,
                action="Submission updated" if submission.submission_id else "Submission created",
                actor=str(message.author.id),
                details={
                    "has_screenshot": submission.has_screenshot,
                    "has_availability": submission.has_availability,
                    "is_complete": submission.is_complete,
                },
            ))

            await session.commit()

        await message.reply("\n\n".join(response_parts))


async def setup(bot: commands.Bot):
    await bot.add_cog(Submissions(bot))
