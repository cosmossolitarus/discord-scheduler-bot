"""
Changes cog.

Handles post-lock change requests:
    - User updates (new availability, new screenshot)
    - Swap requests between two users
    - Auto-bump detection (>2x resources)
    - Admin approval workflow in #schedule_approve
"""

import json
import logging
from datetime import datetime, timezone, timedelta

import discord
from discord.ext import commands
from sqlalchemy import select, and_

from bot.config import (
    ADMIN_ROLE, SCHEDULE_APPROVE_CHANNEL, SCHEDULE_LOG_CHANNEL,
    AUTO_BUMP_THRESHOLD, SWAP_USER_DEADLINE_MINUTES,
    GENERIC_SPLIT, ANTHROPIC_MODEL,
)
from bot.database import async_session
from bot.models import (
    Event, Submission, Slot, Assignment, ChangeRequest, AuditLog,
    EventPhase, AssignmentStatus, ChangeStatus, ChangeType,
)
from bot.cycle import get_current_cycle_day1, generate_slot_times
from bot.llm.screenshot import parse_screenshot
from bot.llm.availability import parse_availability

logger = logging.getLogger("scheduler.changes")

# LLM prompt to classify change requests — {assignment_context} is filled at runtime
CHANGE_CLASSIFY_PROMPT = """You are a scheduling bot assistant. A user has sent a message
in a channel where they @mention you for scheduling changes.

Tracked days: Day 1, Day 2, Day 4 only. Players refer to days by resource type:
- "construction" / "building" = Day 1
- "research" = Day 2
- "troops" / "training" / "soldiers" = Day 4

Untracked days: Day 3 and Day 5. The bot does not handle these.

{assignment_context}

Classify the request:
1. "query" — user is asking about their current schedule, times, resources, or status.
   They are NOT requesting a change.
   {{"type": "query"}}

2. "swap" — user wants to swap their slot with another specific player (they mention a name)
   {{"type": "swap", "other_player_name": "PlayerName", "day": 1, "details": "brief description"}}

3. "update" — user is changing their availability, requesting a time change, dropping a slot,
   or any other schedule modification for a TRACKED day. Includes relative requests like
   "push my slot back 30 minutes".
   IMPORTANT: Identify which days are being modified. Only include days the user explicitly
   mentions or clearly intends to change. If they say "set Day 2 to 3-5 UTC", days_modified
   is [2] — do NOT include Day 1 or Day 4.
   Rewrite ONLY the modified days as an availability statement.
   {{"type": "update", "days_modified": [2], "rewritten_availability": "Day 2: 3-5 UTC only", "details": "brief description"}}

4. "off_day" — message references Day 3 or Day 5 in a scheduling context.
   {{"type": "off_day", "days": [3]}}

5. "other" — message is a joke, irrelevant, or doesn't fit any category above. Used for
   anything not a genuine scheduling interaction.
   {{"type": "other"}}

Respond with ONLY the JSON object. No explanation.
"""


class Changes(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ─── Formatting helpers ─────────────────────────────────────

    @staticmethod
    def _extract_day_from_text(text: str) -> int | None:
        """Best-effort regex extraction of a day number from a swap message.

        Handles 'day 4', 'd4', and resource aliases (construction/research/troops).
        Returns None if no day reference found.
        """
        import re
        if not text:
            return None
        lower = text.lower()
        # "day N" or "d N" or "dN"
        match = re.search(r"\b(?:day|d)\s*([124])\b", lower)
        if match:
            return int(match.group(1))
        # Resource aliases
        if any(w in lower for w in ("construction", "build", "building")):
            return 1
        if "research" in lower:
            return 2
        if any(w in lower for w in ("troops", "troop", "training", "soldier", "soldiers")):
            return 4
        return None

    @staticmethod
    def _track_name(track: str) -> str:
        """Convert internal track code to display name."""
        return "Noble Advisor" if track == "NA" else "Chief Minister"

    @staticmethod
    def _format_slot(slot: Slot) -> str:
        """Format a Slot object as 'Day X - Track - HH:MM UTC'."""
        return (
            f"Day {slot.day} - {Changes._track_name(slot.track)} - "
            f"{slot.start_time.strftime('%H:%M UTC')}"
        )

    async def _format_slot_ids(self, slot_ids: list[str], event_id: int) -> list[str]:
        """Convert a list of internal slot IDs into human-readable strings."""
        if not slot_ids:
            return []
        async with async_session() as session:
            result = await session.execute(
                select(Slot).where(
                    Slot.event_id == event_id,
                    Slot.slot_id.in_(slot_ids),
                ).order_by(Slot.day, Slot.start_time)
            )
            slots = list(result.scalars().all())
        return [self._format_slot(s) for s in slots]

    async def _summarize_availability_change(
        self, old_ids: list[str], new_ids: list[str], event_id: int,
        days_modified: list[int] | None,
    ) -> str:
        """Build a short human-readable summary of an availability change.

        Shows added and removed windows per day for the modified days.
        """
        old_set = set(old_ids or [])
        new_set = set(new_ids or [])
        added = sorted(new_set - old_set)
        removed = sorted(old_set - new_set)

        if not added and not removed:
            return "No effective change in available slots."

        async with async_session() as session:
            result = await session.execute(
                select(Slot).where(
                    Slot.event_id == event_id,
                    Slot.slot_id.in_(list(added) + list(removed)),
                )
            )
            slot_map = {s.slot_id: s for s in result.scalars().all()}

        def windows(slot_ids: list[str]) -> dict:
            """Group contiguous slots into time windows per day."""
            by_day = {}
            for sid in slot_ids:
                slot = slot_map.get(sid)
                if slot:
                    by_day.setdefault(slot.day, []).append(slot)
            for day, slots in by_day.items():
                slots.sort(key=lambda s: s.start_time)
            return by_day

        added_by_day = windows(added)
        removed_by_day = windows(removed)

        lines = []
        days_to_show = days_modified or sorted(set(added_by_day.keys()) | set(removed_by_day.keys()))
        for day in days_to_show:
            day_added = added_by_day.get(day, [])
            day_removed = removed_by_day.get(day, [])
            if not day_added and not day_removed:
                continue
            line = f"Day {day}: "
            parts = []
            if day_added:
                start = day_added[0].start_time.strftime("%H:%M")
                end = day_added[-1].end_time.strftime("%H:%M UTC")
                parts.append(f"now available {start}-{end}")
            if day_removed:
                start = day_removed[0].start_time.strftime("%H:%M")
                end = day_removed[-1].end_time.strftime("%H:%M UTC")
                parts.append(f"no longer available {start}-{end}")
            lines.append(line + "; ".join(parts))
        return "\n".join(lines) if lines else "No effective change."

    async def handle_change_request(self, message: discord.Message, day1: datetime):
        """
        Entry point for post-lock @mentions.
        Looks up user's current assignments for context, classifies, and routes.
        """
        import anthropic
        client = anthropic.AsyncAnthropic()

        text_content = message.content
        for mention in message.mentions:
            if mention.id == self.bot.user.id:
                text_content = text_content.replace(f"<@{mention.id}>", "").replace(f"<@!{mention.id}>", "")
        text_content = text_content.strip()

        has_image = any(
            a.content_type and a.content_type.startswith("image/")
            for a in message.attachments
        )

        if has_image:
            await self._handle_resource_update(message, day1)
            return

        # Direct @mention of another user → straight to swap.
        # Try to extract the day from the message text so the swap handler
        # can filter when both users share multiple day/track combos.
        other_mentions = [m for m in message.mentions if m.id != self.bot.user.id]
        if other_mentions:
            requested_day = self._extract_day_from_text(text_content)
            await self._handle_swap_request(
                message, day1, other_mentions[0], requested_day
            )
            return

        # Look up current assignments for LLM context
        assignment_context = await self._build_assignment_context(message.author.id, day1)
        prompt = CHANGE_CLASSIFY_PROMPT.format(assignment_context=assignment_context)

        from bot.llm.utils import extract_json
        try:
            response = await client.messages.create(
                model=ANTHROPIC_MODEL,
                max_tokens=300,
                system=prompt,
                messages=[{"role": "user", "content": text_content}],
            )
            result = extract_json(response.content[0].text.strip())
        except Exception as e:
            logger.error(f"Failed to classify change request: {e}")
            result = None

        if result is None:
            await message.reply(
                "I had trouble understanding your request. Could you rephrase?\n"
                "• \"Push my Day 1 spot back by 30 minutes\"\n"
                "• \"Swap Day 1 with @player\"\n"
                "• \"Drop my Day 2 slot\"\n"
                "• \"What are my current times?\""
            )
            return

        req_type = result.get("type", "unclear")

        if req_type == "query":
            await self._handle_query(message, day1)
        elif req_type == "swap":
            other_name = result.get("other_player_name", "")
            other_member = None
            if other_name and message.guild:
                other_member = discord.utils.find(
                    lambda m: m.display_name.lower() == other_name.lower()
                    or m.name.lower() == other_name.lower(),
                    message.guild.members,
                )
            if other_member:
                raw_day = result.get("day")
                try:
                    requested_day = int(raw_day) if raw_day is not None else None
                except (TypeError, ValueError):
                    requested_day = None
                # Fallback: try extracting from text if classifier didn't give it
                if requested_day is None:
                    requested_day = self._extract_day_from_text(text_content)
                await self._handle_swap_request(message, day1, other_member, requested_day)
            else:
                await message.reply(
                    f"I think you want to swap with **{other_name}**, but I couldn't "
                    f"find them. Please @mention them directly.\n"
                    f"Example: \"@scheduler swap Day {result.get('day', 1)} with @{other_name}\""
                )
        elif req_type == "update":
            rewritten = result.get("rewritten_availability", text_content)
            days_modified = result.get("days_modified", None)
            await self._handle_availability_update(message, day1, rewritten, days_modified)
        elif req_type == "off_day":
            from bot.llm.utils import off_day_reply
            await message.reply(off_day_reply(result.get("days", [])))
        else:
            # "other" or anything else falls through to the basic prompt
            await message.reply(
                "You can:\n"
                "• \"What are my current times?\"\n"
                "• \"Push my Day 1 spot back by 30 minutes\"\n"
                "• \"Swap Day 2 with @player\"\n"
                "• \"Drop my Day 4 slot\"\n"
                "• Submit a new screenshot: just attach it when you @mention me"
            )

    async def _build_assignment_context(self, discord_id: int, day1: datetime) -> str:
        """Build a text description of the user's current data for the LLM."""
        async with async_session() as session:
            event = await self._get_event(session, day1)
            if event is None:
                return "The user has no current data."

            # Get submission (resources, availability)
            sub_result = await session.execute(
                select(Submission).where(
                    Submission.event_id == event.event_id,
                    Submission.discord_id == discord_id,
                )
            )
            submission = sub_result.scalar_one_or_none()

            # Get assignments
            assignments = await self._get_user_assignments(session, event.event_id, discord_id)

        lines = []

        if submission:
            if submission.has_screenshot:
                lines.append(
                    f"Speedups: Construction {submission.resource_x:.1f}d, "
                    f"Research {submission.resource_y:.1f}d, "
                    f"Troops {submission.resource_z:.1f}d, "
                    f"General {submission.resource_generic:.1f}d"
                )
            if submission.raw_availability_text:
                lines.append(f"Availability (what they originally said): \"{submission.raw_availability_text}\"")

        if assignments:
            lines.append("Current assigned slots:")
            for a in sorted(assignments, key=lambda x: x.slot.start_time):
                start = a.slot.start_time.strftime("%H:%M UTC")
                end = a.slot.end_time.strftime("%H:%M UTC")
                track = "Noble Advisor" if a.slot.track == "NA" else "Chief Minister"
                lines.append(f"  Day {a.slot.day} ({track}): {start} - {end}")
        else:
            lines.append("The user has no current slot assignments.")

        if not submission:
            lines.append("The user has no submission for this event.")

        return "\n".join(lines)

    async def _handle_query(self, message: discord.Message, day1: datetime):
        """Respond to informational queries about the user's current data."""
        async with async_session() as session:
            event = await self._get_event(session, day1)
            if event is None:
                await message.reply("No active event found.")
                return

            sub_result = await session.execute(
                select(Submission).where(
                    Submission.event_id == event.event_id,
                    Submission.discord_id == message.author.id,
                )
            )
            submission = sub_result.scalar_one_or_none()
            assignments = await self._get_user_assignments(
                session, event.event_id, message.author.id
            )

        if not submission and not assignments:
            await message.reply("I don't have any data for you in the current event.")
            return

        lines = []

        if assignments:
            lines.append("**Your assigned slots:**")
            for a in sorted(assignments, key=lambda x: x.slot.start_time):
                start = a.slot.start_time.strftime("%a %b %d, %H:%M")
                end = a.slot.end_time.strftime("%H:%M UTC")
                track = "Noble Advisor" if a.slot.track == "NA" else "Chief Minister"
                lines.append(f"  Day {a.slot.day} ({track}): {start} - {end}")
        elif submission:
            lines.append("You have a submission but no slot assignments yet.")

        if submission and submission.has_screenshot:
            lines.append(
                f"\n**Speedups:** Construction {submission.resource_x:.1f}d · "
                f"Research {submission.resource_y:.1f}d · "
                f"Troops {submission.resource_z:.1f}d · "
                f"General {submission.resource_generic:.1f}d"
            )

        if submission and submission.raw_availability_text:
            lines.append(f"\n**Availability on file:** {submission.raw_availability_text}")

        await message.reply("\n".join(lines))

    async def _handle_resource_update(self, message: discord.Message, day1: datetime):
        """Process a new screenshot submission after lock."""
        # Parse screenshot
        for attachment in message.attachments:
            if attachment.content_type and attachment.content_type.startswith("image/"):
                image_data = await attachment.read()
                result = await parse_screenshot(image_data, attachment.content_type)
                break
        else:
            return

        if "error" in result:
            await message.reply(f"❌ {result['error']}. Please try again.")
            return

        async with async_session() as session:
            event = await self._get_event(session, day1)
            if event is None:
                return

            # Get existing submission
            sub_result = await session.execute(
                select(Submission).where(
                    Submission.event_id == event.event_id,
                    Submission.discord_id == message.author.id,
                )
            )
            submission = sub_result.scalar_one_or_none()

            if submission is None:
                await message.reply(
                    "You don't have a submission for this event. "
                    "Since the schedule is locked, please contact an admin."
                )
                return

            old_priorities = {
                "x": submission.priority_x,
                "y": submission.priority_y,
                "z": submission.priority_z,
            }

            # Update resources
            submission.resource_x = result["resource_x"]
            submission.resource_y = result["resource_y"]
            submission.resource_z = result["resource_z"]
            submission.resource_generic = result["resource_generic"]
            submission.compute_priorities(GENERIC_SPLIT)
            submission.screenshot_url = message.attachments[0].url

            # Check for auto-bump opportunity on each day
            bump_opportunities = await self._check_auto_bump(
                session, event, submission
            )

            # Create change request
            change = ChangeRequest(
                event_id=event.event_id,
                requested_by=message.author.id,
                change_type=ChangeType.UPDATE,
                status=ChangeStatus.PENDING_ADMIN,
                details={
                    "type": "resource_update",
                    "old_priorities": old_priorities,
                    "new_priorities": {
                        "x": submission.priority_x,
                        "y": submission.priority_y,
                        "z": submission.priority_z,
                    },
                    "bump_opportunities": bump_opportunities,
                },
            )
            session.add(change)
            await session.flush()

            session.add(AuditLog(
                event_id=event.event_id,
                action="Post-lock resource update",
                actor=str(message.author.id),
                details=change.details,
            ))

            await session.commit()

            # Notify user
            await message.reply(
                f"⏳ Resource update received and **pending admin approval**.\n"
                f"Priority — Day 1: {submission.priority_x:.1f}d, "
                f"Day 2: {submission.priority_y:.1f}d, "
                f"Day 4: {submission.priority_z:.1f}d"
            )

            # Post to #schedule_approve
            await self._post_approval_request(message.guild, change, event, submission)

    async def _handle_availability_update(
        self, message: discord.Message, day1: datetime, text: str,
        days_modified: list[int] | None = None,
    ):
        """Process an availability change after lock.

        If days_modified is provided, only slots for those days are replaced;
        existing slots for other days are preserved.
        """
        slot_reference = generate_slot_times(day1)
        day1_str = day1.strftime("%B %d, %Y")

        # Fetch the user's current availability so we can pass it to the LLM
        async with async_session() as session:
            event = await self._get_event(session, day1)
            if event is None:
                return

            sub_result = await session.execute(
                select(Submission).where(
                    Submission.event_id == event.event_id,
                    Submission.discord_id == message.author.id,
                )
            )
            submission = sub_result.scalar_one_or_none()
            if submission is None:
                await message.reply("You don't have a submission for this event.")
                return

            old_availability = submission.availability or []

        from bot.cogs.submissions import Submissions
        existing_summary = Submissions._build_existing_summary(
            old_availability, slot_reference
        )

        avail_result = await parse_availability(
            text, day1_str, slot_reference, existing_summary=existing_summary,
        )

        if "error" in avail_result:
            await message.reply(f"❌ {avail_result['error']}. Please try again.")
            return

        new_slots = avail_result["available_slots"]

        async with async_session() as session:
            # Re-fetch event/submission in the new session for the write
            event = await self._get_event(session, day1)
            if event is None:
                return

            sub_result = await session.execute(
                select(Submission).where(
                    Submission.event_id == event.event_id,
                    Submission.discord_id == message.author.id,
                )
            )
            submission = sub_result.scalar_one_or_none()

            if submission is None:
                await message.reply("You don't have a submission for this event.")
                return

            old_availability = submission.availability or []

            # Merge: if days_modified is specified, keep existing slots for
            # unmodified days and only replace slots for modified days
            if days_modified and old_availability:
                # Map day number to slot ID prefix (D1, D2, D4)
                modified_prefixes = {f"D{d}-" for d in days_modified}

                # Keep old slots for days NOT being modified
                kept_slots = [
                    sid for sid in old_availability
                    if not any(sid.startswith(p) for p in modified_prefixes)
                ]
                # New slots are only for the modified days (filter just in case
                # parse_availability returned slots for other days too)
                new_day_slots = [
                    sid for sid in new_slots
                    if any(sid.startswith(p) for p in modified_prefixes)
                ]
                merged = sorted(set(kept_slots + new_day_slots))
            else:
                merged = new_slots

            change = ChangeRequest(
                event_id=event.event_id,
                requested_by=message.author.id,
                change_type=ChangeType.UPDATE,
                status=ChangeStatus.PENDING_ADMIN,
                details={
                    "type": "availability_update",
                    "old_availability": old_availability,
                    "new_availability": merged,
                    "days_modified": days_modified,
                    "player_summary": avail_result.get("player_summary", ""),
                },
            )
            session.add(change)
            await session.flush()

            session.add(AuditLog(
                event_id=event.event_id,
                action="Post-lock availability update",
                actor=str(message.author.id),
                details=change.details,
            ))

            await session.commit()

            player_summary = avail_result.get("player_summary", "")
            days_str = (
                f" (Day {', '.join(str(d) for d in days_modified)})"
                if days_modified else ""
            )
            await message.reply(
                f"⏳ Availability update{days_str}:\n{player_summary}\n\n"
                f"Your request is **pending admin approval**."
            )

            await self._post_approval_request(message.guild, change, event, submission)

    async def _handle_swap_request(
        self, message: discord.Message, day1: datetime, other_user: discord.Member,
        requested_day: int | None = None,
    ):
        """Process a swap request between two users.

        If requested_day is provided (from classifier), use it to narrow down
        which day to swap when multiple are shared.
        """
        async with async_session() as session:
            event = await self._get_event(session, day1)
            if event is None:
                return

            # Get both users' assignments
            requester_assignments = await self._get_user_assignments(
                session, event.event_id, message.author.id
            )
            other_assignments = await self._get_user_assignments(
                session, event.event_id, other_user.id
            )

            if not requester_assignments:
                await message.reply("You don't have any assigned slots to swap.")
                return
            if not other_assignments:
                await message.reply(f"{other_user.display_name} doesn't have any assigned slots.")
                return

            # Find overlapping days/tracks
            req_slots = {(a.slot.day, a.slot.track): a for a in requester_assignments}
            other_slots = {(a.slot.day, a.slot.track): a for a in other_assignments}
            common = set(req_slots.keys()) & set(other_slots.keys())

            if not common:
                await message.reply(
                    f"You and {other_user.display_name} don't share any day/track "
                    f"assignments to swap."
                )
                return

            # If user specified a day, filter to that day
            if requested_day is not None:
                filtered = {dt for dt in common if dt[0] == requested_day}
                if not filtered:
                    shared_days = sorted({d for d, _ in common})
                    await message.reply(
                        f"You and {other_user.display_name} don't share a Day {requested_day} "
                        f"slot. You share: Day {', Day '.join(str(d) for d in shared_days)}."
                    )
                    return
                common = filtered

            # If multiple still match, ask for clarification
            if len(common) > 1:
                days_str = ", ".join(f"Day {d} {self._track_name(t)}" for d, t in sorted(common))
                await message.reply(
                    f"You and {other_user.display_name} share multiple slots: {days_str}. "
                    f"Please specify which one, e.g., "
                    f"\"@scheduler swap Day 1 with @{other_user.display_name}\""
                )
                return

            day_track = list(common)[0]
            req_assignment = req_slots[day_track]
            other_assignment = other_slots[day_track]

            # Calculate deadlines
            earlier_slot = min(
                req_assignment.slot, other_assignment.slot,
                key=lambda s: s.start_time
            )
            user_deadline = earlier_slot.start_time - timedelta(minutes=SWAP_USER_DEADLINE_MINUTES)
            admin_deadline = earlier_slot.start_time

            now = datetime.now(timezone.utc)
            if now >= user_deadline:
                await message.reply(
                    "It's too late to request this swap — the earlier block "
                    "starts in less than 30 minutes."
                )
                return

            # Create change request
            change = ChangeRequest(
                event_id=event.event_id,
                requested_by=message.author.id,
                change_type=ChangeType.SWAP,
                status=ChangeStatus.PENDING_CONFIRMATION,
                details={
                    "requester_id": message.author.id,
                    "requester_slot": req_assignment.slot_id,
                    "other_id": other_user.id,
                    "other_slot": other_assignment.slot_id,
                    "day": day_track[0],
                    "track": day_track[1],
                },
                user_deadline=user_deadline,
                admin_deadline=admin_deadline,
            )
            session.add(change)
            await session.flush()

            session.add(AuditLog(
                event_id=event.event_id,
                action="Swap requested",
                actor=str(message.author.id),
                details=change.details,
            ))

            await session.commit()

            # Echo in channel
            req_time = req_assignment.slot.start_time.strftime("%H:%M UTC")
            other_time = other_assignment.slot.start_time.strftime("%H:%M UTC")
            track_label = self._track_name(day_track[1])
            await message.reply(
                f"Swap request: your {req_time} slot ↔ "
                f"{other_user.mention}'s {other_time} slot on "
                f"Day {day_track[0]} ({track_label}).\n"
                f"Waiting for {other_user.display_name} to confirm."
            )

            # DM the other user for confirmation
            try:
                deadline_str = user_deadline.strftime("%b %d, %H:%M UTC")
                dm_msg = await other_user.send(
                    f"**Swap request from {message.author.display_name}:**\n"
                    f"Your {other_time} slot ↔ their {req_time} slot on "
                    f"Day {day_track[0]} ({track_label})\n\n"
                    f"React ✅ to accept or ❌ to decline.\n"
                    f"Deadline: {deadline_str}"
                )
                await dm_msg.add_reaction("✅")
                await dm_msg.add_reaction("❌")

                # Store message ID for reaction tracking
                async with async_session() as session2:
                    change_obj = await session2.get(ChangeRequest, change.change_id)
                    change_obj.swap_confirm_message_id = dm_msg.id
                    await session2.commit()

            except discord.Forbidden:
                await message.reply(
                    f"I couldn't DM {other_user.display_name}. "
                    f"They may need to enable DMs from server members."
                )

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent):
        """Handle reactions on swap confirmations and admin approvals."""
        if payload.user_id == self.bot.user.id:
            return

        emoji = str(payload.emoji)
        if emoji not in ("✅", "❌"):
            return

        async with async_session() as session:
            # Check if this is a swap confirmation
            result = await session.execute(
                select(ChangeRequest).where(
                    ChangeRequest.swap_confirm_message_id == payload.message_id
                )
            )
            change = result.scalar_one_or_none()

            if change and change.status == ChangeStatus.PENDING_CONFIRMATION:
                await self._handle_swap_confirmation(session, change, emoji, payload)
                return

            # Check if this is an admin approval
            result = await session.execute(
                select(ChangeRequest).where(
                    ChangeRequest.approval_message_id == payload.message_id
                )
            )
            change = result.scalar_one_or_none()

            if change and change.status == ChangeStatus.PENDING_ADMIN:
                # Verify the reactor has admin role
                guild = self.bot.get_guild(payload.guild_id) if payload.guild_id else None
                if guild:
                    member = guild.get_member(payload.user_id)
                    admin_role = discord.utils.get(guild.roles, name=ADMIN_ROLE)
                    if member and admin_role and admin_role in member.roles:
                        await self._handle_admin_decision(
                            session, change, emoji, payload.user_id
                        )

    async def _handle_swap_confirmation(
        self, session, change: ChangeRequest, emoji: str, payload
    ):
        """Process swap confirmation from the second user."""
        now = datetime.now(timezone.utc)

        if change.user_deadline and now >= change.user_deadline:
            change.status = ChangeStatus.EXPIRED
            await session.commit()
            return

        if emoji == "❌":
            change.status = ChangeStatus.REJECTED
            change.resolved_at = now
            await session.commit()

            # Notify requester
            guild = self.bot.get_guild(payload.guild_id) if payload.guild_id else None
            if guild:
                requester = guild.get_member(change.details["requester_id"])
                other = guild.get_member(change.details["other_id"])
                if requester:
                    try:
                        await requester.send(
                            f"Your swap request with {other.display_name if other else 'the other player'} "
                            f"was declined."
                        )
                    except discord.Forbidden:
                        pass
            return

        # Accepted — move to admin approval
        change.status = ChangeStatus.PENDING_ADMIN
        await session.commit()

        # Post to #schedule_approve
        for guild in self.bot.guilds:
            await self._post_swap_approval(guild, change)

    async def _handle_admin_decision(
        self, session, change: ChangeRequest, emoji: str, admin_id: int
    ):
        """Process admin approval or rejection."""
        now = datetime.now(timezone.utc)

        if change.admin_deadline and now >= change.admin_deadline:
            change.status = ChangeStatus.EXPIRED
            change.resolved_at = now
            await session.commit()
            # TODO: notify affected users of expiration
            return

        change.resolved_at = now
        change.resolved_by = admin_id

        if emoji == "❌":
            change.status = ChangeStatus.REJECTED
            await session.commit()
            await self._notify_change_result(change, approved=False)
            return

        # Approved — apply the change
        change.status = ChangeStatus.APPROVED

        if change.change_type == ChangeType.SWAP:
            await self._apply_swap(session, change)
        elif change.change_type == ChangeType.UPDATE:
            await self._apply_update(session, change)
        elif change.change_type == ChangeType.AUTO_BUMP_FLAG:
            await self._apply_bump(session, change)

        session.add(AuditLog(
            event_id=change.event_id,
            action=f"Change {change.change_id} approved",
            actor=str(admin_id),
            details=change.details,
        ))

        await session.commit()
        await self._notify_change_result(change, approved=True)

        # Regenerate CSV
        event = await session.get(Event, change.event_id)
        if event:
            scheduling_cog = self.bot.get_cog("Scheduling")
            if scheduling_cog:
                csv_file = await scheduling_cog._generate_csv(event.day1_date)
                for guild in self.bot.guilds:
                    log_ch = discord.utils.get(
                        guild.text_channels, name=SCHEDULE_LOG_CHANNEL
                    )
                    if log_ch:
                        await log_ch.send(
                            f"Schedule updated (change #{change.change_id}).",
                            file=discord.File(
                                csv_file,
                                filename=f"schedule_updated_{event.day1_date.date()}.csv",
                            ),
                        )

    async def _apply_swap(self, session, change: ChangeRequest):
        """Swap two assignments."""
        details = change.details
        result1 = await session.execute(
            select(Assignment).where(
                Assignment.event_id == change.event_id,
                Assignment.slot_id == details["requester_slot"],
            )
        )
        result2 = await session.execute(
            select(Assignment).where(
                Assignment.event_id == change.event_id,
                Assignment.slot_id == details["other_slot"],
            )
        )
        a1 = result1.scalar_one_or_none()
        a2 = result2.scalar_one_or_none()

        if a1 and a2:
            a1.discord_id, a2.discord_id = a2.discord_id, a1.discord_id

    async def _apply_update(self, session, change: ChangeRequest):
        """Apply an availability or resource update.

        For availability updates: also delete any existing Assignment rows
        for the user whose slot is no longer in their availability. This is
        what makes "drop my Day X" actually drop the assignment.
        """
        details = change.details
        sub_result = await session.execute(
            select(Submission).where(
                Submission.event_id == change.event_id,
                Submission.discord_id == change.requested_by,
            )
        )
        submission = sub_result.scalar_one_or_none()
        if not submission:
            return

        if "new_availability" in details:
            new_avail = details["new_availability"]
            submission.availability = new_avail
            new_avail_set = set(new_avail or [])

            # Remove assignments whose slot is no longer in the user's availability
            assign_result = await session.execute(
                select(Assignment).where(
                    Assignment.event_id == change.event_id,
                    Assignment.discord_id == change.requested_by,
                )
            )
            assignments = list(assign_result.scalars().all())

            removed_slots = []
            for a in assignments:
                if a.slot_id not in new_avail_set:
                    removed_slots.append(a.slot_id)
                    await session.delete(a)

            if removed_slots:
                session.add(AuditLog(
                    event_id=change.event_id,
                    action="Assignment removed via availability update",
                    actor=str(change.requested_by),
                    details={
                        "change_id": change.change_id,
                        "removed_slots": removed_slots,
                    },
                ))

    async def _apply_bump(self, session, change: ChangeRequest):
        """Apply an auto-bump: replace one user with another in a slot."""
        details = change.details
        result = await session.execute(
            select(Assignment).where(
                Assignment.event_id == change.event_id,
                Assignment.slot_id == details["slot_id"],
            )
        )
        assignment = result.scalar_one_or_none()
        if assignment:
            assignment.discord_id = details["new_user"]

    async def _check_auto_bump(
        self, session, event: Event, submission: Submission
    ) -> list[dict]:
        """Check if this submission qualifies for auto-bump on any day."""
        bumps = []
        day_resource_map = {1: "priority_x", 2: "priority_y", 4: "priority_z"}

        for day, priority_field in day_resource_map.items():
            new_priority = getattr(submission, priority_field, 0) or 0
            if new_priority == 0:
                continue

            # Find the lowest-priority assigned user on this day
            # that the new user could replace (within availability)
            result = await session.execute(
                select(Assignment, Slot, Submission)
                .join(Slot, Assignment.slot_id == Slot.slot_id)
                .join(
                    Submission,
                    and_(
                        Submission.event_id == Assignment.event_id,
                        Submission.discord_id == Assignment.discord_id,
                    ),
                )
                .where(
                    Assignment.event_id == event.event_id,
                    Slot.day == day,
                )
            )
            assignments = result.all()

            for assignment, slot, assigned_sub in assignments:
                assigned_priority = getattr(assigned_sub, priority_field, 0) or 0
                if (
                    new_priority > assigned_priority * AUTO_BUMP_THRESHOLD
                    and slot.slot_id in (submission.availability or [])
                ):
                    bumps.append({
                        "day": day,
                        "slot_id": slot.slot_id,
                        "bumped_user": assignment.discord_id,
                        "bumped_priority": assigned_priority,
                        "new_priority": new_priority,
                    })
                    break  # One bump per day max

        return bumps

    async def _post_approval_request(
        self, guild: discord.Guild, change: ChangeRequest,
        event: Event, submission: Submission
    ):
        """Post a change request to #schedule_approve."""
        channel = discord.utils.get(guild.text_channels, name=SCHEDULE_APPROVE_CHANNEL)
        admin_role = discord.utils.get(guild.roles, name=ADMIN_ROLE)
        if not channel:
            return

        member = guild.get_member(change.requested_by)
        name = member.display_name if member else str(change.requested_by)

        details = change.details
        desc = f"**Change Request #{change.change_id}**\n"
        desc += f"Player: {name}\n"
        desc += f"Type: {change.change_type.value}\n"

        if details.get("type") == "resource_update":
            desc += (
                f"Priorities: Day 1 = {details['new_priorities']['x']:.1f}d, "
                f"Day 2 = {details['new_priorities']['y']:.1f}d, "
                f"Day 4 = {details['new_priorities']['z']:.1f}d\n"
            )
            if details.get("bump_opportunities"):
                for bump in details["bump_opportunities"]:
                    desc += (
                        f"⚠️ **Auto-bump eligible** on Day {bump['day']}: "
                        f"new priority {bump['new_priority']:,.0f} > "
                        f"2x current lowest {bump['bumped_priority']:,.0f}\n"
                    )
        elif details.get("type") == "availability_update":
            summary_text = await self._summarize_availability_change(
                details.get("old_availability", []),
                details.get("new_availability", []),
                event.event_id,
                details.get("days_modified"),
            )
            player_summary = details.get("player_summary", "")
            if player_summary:
                desc += f"Player said: {player_summary}\n"
            desc += f"Changes:\n{summary_text}\n"

        desc += f"\nReact ✅ to approve or ❌ to reject."

        msg = await channel.send(
            f"{admin_role.mention if admin_role else ''}\n{desc}"
        )
        await msg.add_reaction("✅")
        await msg.add_reaction("❌")

        async with async_session() as session2:
            change_obj = await session2.get(ChangeRequest, change.change_id)
            change_obj.approval_message_id = msg.id
            await session2.commit()

    async def _post_swap_approval(self, guild: discord.Guild, change: ChangeRequest):
        """Post a confirmed swap to #schedule_approve for admin action."""
        channel = discord.utils.get(guild.text_channels, name=SCHEDULE_APPROVE_CHANNEL)
        admin_role = discord.utils.get(guild.roles, name=ADMIN_ROLE)
        if not channel:
            return

        details = change.details
        requester = guild.get_member(details["requester_id"])
        other = guild.get_member(details["other_id"])

        req_name = requester.display_name if requester else str(details["requester_id"])
        other_name = other.display_name if other else str(details["other_id"])

        # Look up actual slot times for both sides
        async with async_session() as session:
            result = await session.execute(
                select(Slot).where(
                    Slot.event_id == change.event_id,
                    Slot.slot_id.in_([details["requester_slot"], details["other_slot"]]),
                )
            )
            slot_map = {s.slot_id: s for s in result.scalars().all()}

        req_slot = slot_map.get(details["requester_slot"])
        other_slot = slot_map.get(details["other_slot"])
        req_time = req_slot.start_time.strftime("%H:%M UTC") if req_slot else "?"
        other_time = other_slot.start_time.strftime("%H:%M UTC") if other_slot else "?"
        track_label = self._track_name(details["track"])

        deadline_str = (
            change.admin_deadline.strftime("%b %d, %H:%M UTC")
            if change.admin_deadline else "N/A"
        )

        desc = (
            f"**Swap Request #{change.change_id}**\n"
            f"@{req_name} ({req_time}) ↔ @{other_name} ({other_time})\n"
            f"Day {details['day']} ({track_label})\n"
            f"Both players confirmed.\n"
            f"Admin deadline: {deadline_str}\n\n"
            f"React ✅ to approve or ❌ to reject."
        )

        msg = await channel.send(f"{admin_role.mention if admin_role else ''}\n{desc}")
        await msg.add_reaction("✅")
        await msg.add_reaction("❌")

        async with async_session() as session:
            change_obj = await session.get(ChangeRequest, change.change_id)
            change_obj.approval_message_id = msg.id
            await session.commit()

    async def _notify_change_result(self, change: ChangeRequest, approved: bool):
        """DM affected users about the result of a change."""
        status_word = "approved" if approved else "declined"
        details = change.details

        # Build a description of what changed
        change_desc = ""
        if change.change_type == ChangeType.UPDATE:
            if details.get("type") == "availability_update":
                # Look up before/after times in human form
                summary_text = await self._summarize_availability_change(
                    details.get("old_availability", []),
                    details.get("new_availability", []),
                    change.event_id,
                    details.get("days_modified"),
                )
                player_summary = details.get("player_summary", "")
                if player_summary:
                    change_desc = f"\n**Your request:** {player_summary}\n**Changes:**\n{summary_text}"
                else:
                    change_desc = f"\n**Changes:**\n{summary_text}"
            elif details.get("type") == "resource_update":
                np = details.get("new_priorities", {})
                change_desc = (
                    f"\n**New priorities:** "
                    f"Day 1 = {np.get('x', 0):.1f}d, "
                    f"Day 2 = {np.get('y', 0):.1f}d, "
                    f"Day 4 = {np.get('z', 0):.1f}d"
                )

        for guild in self.bot.guilds:
            if change.change_type == ChangeType.SWAP:
                # Build human-readable swap description
                async with async_session() as session:
                    result = await session.execute(
                        select(Slot).where(
                            Slot.event_id == change.event_id,
                            Slot.slot_id.in_([
                                details["requester_slot"],
                                details["other_slot"],
                            ]),
                        )
                    )
                    slot_map = {s.slot_id: s for s in result.scalars().all()}

                req_slot = slot_map.get(details["requester_slot"])
                other_slot = slot_map.get(details["other_slot"])
                track_label = self._track_name(details["track"])

                for user_id in [details["requester_id"], details["other_id"]]:
                    member = guild.get_member(user_id)
                    if not member:
                        continue

                    # Their original slot and the new one they get after swap
                    if user_id == details["requester_id"]:
                        old_time = req_slot.start_time.strftime("%H:%M UTC") if req_slot else "?"
                        new_time = other_slot.start_time.strftime("%H:%M UTC") if other_slot else "?"
                    else:
                        old_time = other_slot.start_time.strftime("%H:%M UTC") if other_slot else "?"
                        new_time = req_slot.start_time.strftime("%H:%M UTC") if req_slot else "?"

                    try:
                        if approved:
                            await member.send(
                                f"Your swap on Day {details['day']} ({track_label}) "
                                f"has been **{status_word}** and applied.\n"
                                f"Your new slot: **{new_time}** (was {old_time})."
                            )
                        else:
                            await member.send(
                                f"Your swap request on Day {details['day']} "
                                f"({track_label}) was **{status_word}**.\n"
                                f"Your slot remains at {old_time}."
                            )
                    except discord.Forbidden:
                        pass
            else:
                member = guild.get_member(change.requested_by)
                if member:
                    try:
                        await member.send(
                            f"Your change request (#{change.change_id}) has been "
                            f"**{status_word}**.{change_desc}"
                        )
                    except discord.Forbidden:
                        pass

    async def _get_event(self, session, day1: datetime) -> Event | None:
        result = await session.execute(
            select(Event).where(Event.day1_date == day1)
        )
        return result.scalar_one_or_none()

    async def _get_user_assignments(self, session, event_id: int, discord_id: int):
        result = await session.execute(
            select(Assignment)
            .join(Slot, Assignment.slot_id == Slot.slot_id)
            .where(
                Assignment.event_id == event_id,
                Assignment.discord_id == discord_id,
            )
            .options()
        )
        # Need to eagerly load slot data
        result = await session.execute(
            select(Assignment, Slot)
            .join(Slot, Assignment.slot_id == Slot.slot_id)
            .where(
                Assignment.event_id == event_id,
                Assignment.discord_id == discord_id,
            )
        )
        rows = result.all()
        # Attach slot to assignment for convenience
        assignments = []
        for assignment, slot in rows:
            assignment.slot = slot
            assignments.append(assignment)
        return assignments


async def setup(bot: commands.Bot):
    await bot.add_cog(Changes(bot))
