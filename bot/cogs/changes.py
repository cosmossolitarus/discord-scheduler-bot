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

Players often refer to days by their resource type:
- "construction" / "building" = Day 1
- "research" = Day 2
- "troops" / "training" / "soldiers" = Day 4

{assignment_context}

Classify the request into one of these types:
1. "query" — user is asking about their current schedule, times, resources, or status.
   They are NOT requesting a change. Examples: "what are my times?", "when am I scheduled?",
   "show my info", "what are my speedups?"
   {{"type": "query"}}

2. "swap" — user wants to swap their slot with another specific player (they mention a name)
   {{"type": "swap", "other_player_name": "PlayerName", "day": 1, "details": "brief description"}}

3. "update" — user is changing their availability, requesting a time change, dropping a slot,
   or any other schedule modification. This includes relative requests like "push my slot back
   30 minutes" or "move me earlier on Day 1".
   IMPORTANT: Identify which days are being modified. Only include days the user explicitly
   mentions or clearly intends to change. If they say "set Day 2 to 3-5 UTC", days_modified
   is [2] — do NOT include Day 1 or Day 4.
   Rewrite ONLY the modified days as an availability statement. Do NOT rewrite days the user
   didn't mention.
   {{"type": "update", "days_modified": [2], "rewritten_availability": "Day 2: 3-5 UTC only", "details": "brief description"}}

4. "nonsense" — the message is clearly a joke, trolling, irrelevant, or makes no sense as a
   scheduling request. The user is not genuinely trying to interact with the scheduler.
   {{"type": "nonsense", "details": "brief description of what they said"}}

5. "unclear" — you genuinely can't determine whether they want a query, update, or swap
   {{"type": "unclear", "details": "what was confusing"}}

Respond with ONLY the JSON object. No explanation.
"""


class Changes(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

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

        # Direct @mention of another user → straight to swap
        other_mentions = [m for m in message.mentions if m.id != self.bot.user.id]
        if other_mentions:
            await self._handle_swap_request(message, day1, other_mentions[0])
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
                await self._handle_swap_request(message, day1, other_member)
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
        elif req_type == "nonsense":
            await self._handle_nonsense(message, result.get("details", ""))
        else:
            await message.reply(
                "I wasn't sure what you meant. You can:\n"
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

    async def _handle_nonsense(self, message: discord.Message, details: str):
        """Respond to nonsense/joke messages with personality."""
        from bot.llm.utils import generate_witty_response
        reply = await generate_witty_response(message.content)
        await message.reply(reply)

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
        avail_result = await parse_availability(text, day1_str, slot_reference)

        if "error" in avail_result:
            await message.reply(f"❌ {avail_result['error']}. Please try again.")
            return

        new_slots = avail_result["available_slots"]

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
                    "interpretation": avail_result.get("interpretation", ""),
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

            interp = avail_result.get("interpretation", "")
            days_str = (
                f" (Day {', '.join(str(d) for d in days_modified)})"
                if days_modified else ""
            )
            await message.reply(
                f"⏳ Availability update{days_str}: {interp}\n"
                f"Your request is **pending admin approval**."
            )

            await self._post_approval_request(message.guild, change, event, submission)

    async def _handle_swap_request(
        self, message: discord.Message, day1: datetime, other_user: discord.Member
    ):
        """Process a swap request between two users."""
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

            # For simplicity, if there's exactly one common day/track, use it.
            # Otherwise, ask for clarification.
            if len(common) > 1:
                days_str = ", ".join(f"Day {d} {t}" for d, t in sorted(common))
                await message.reply(
                    f"You and {other_user.display_name} share multiple days: {days_str}. "
                    f"Please specify which day, e.g., "
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
            await message.reply(
                f"Swap request: your {req_time} slot ↔ "
                f"{other_user.mention}'s {other_time} slot on "
                f"Day {day_track[0]} ({day_track[1]}).\n"
                f"Waiting for {other_user.display_name} to confirm."
            )

            # DM the other user for confirmation
            try:
                deadline_str = user_deadline.strftime("%b %d, %H:%M UTC")
                dm_msg = await other_user.send(
                    f"**Swap request from {message.author.display_name}:**\n"
                    f"Your {other_time} slot ↔ their {req_time} slot on "
                    f"Day {day_track[0]} ({day_track[1]})\n\n"
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
        """Apply an availability or resource update."""
        details = change.details
        sub_result = await session.execute(
            select(Submission).where(
                Submission.event_id == change.event_id,
                Submission.discord_id == change.requested_by,
            )
        )
        submission = sub_result.scalar_one_or_none()
        if submission and "new_availability" in details:
            submission.availability = details["new_availability"]

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
            desc += f"Priorities: Day 1={details['new_priorities']['x']:.1f}d, "
            desc += f"Day 2={details['new_priorities']['y']:.1f}d, "
            desc += f"Day 4={details['new_priorities']['z']:.1f}d\n"
            if details.get("bump_opportunities"):
                for bump in details["bump_opportunities"]:
                    desc += (
                        f"⚠️ **Auto-bump eligible** on Day {bump['day']}: "
                        f"new priority {bump['new_priority']:,.0f} > "
                        f"2x current lowest {bump['bumped_priority']:,.0f}\n"
                    )
        elif details.get("type") == "availability_update":
            desc += f"Interpretation: {details.get('interpretation', 'N/A')}\n"

        desc += f"\nReact ✅ to approve or ❌ to reject."

        msg = await channel.send(
            f"{admin_role.mention if admin_role else ''}\n{desc}"
        )
        await msg.add_reaction("✅")
        await msg.add_reaction("❌")

        # Store the message ID
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

        deadline_str = change.admin_deadline.strftime("%b %d, %H:%M UTC") if change.admin_deadline else "N/A"

        desc = (
            f"**Swap Request #{change.change_id}**\n"
            f"{req_name} ({details['requester_slot']}) ↔ "
            f"{other_name} ({details['other_slot']})\n"
            f"Day {details['day']} ({details['track']})\n"
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

        for guild in self.bot.guilds:
            if change.change_type == ChangeType.SWAP:
                details = change.details
                for user_id in [details["requester_id"], details["other_id"]]:
                    member = guild.get_member(user_id)
                    if member:
                        try:
                            if approved:
                                await member.send(
                                    f"Your swap on Day {details['day']} has been {status_word} "
                                    f"and implemented."
                                )
                            else:
                                await member.send(
                                    f"The swap request on Day {details['day']} was {status_word}."
                                )
                        except discord.Forbidden:
                            pass
            else:
                member = guild.get_member(change.requested_by)
                if member:
                    try:
                        await member.send(
                            f"Your change request (#{change.change_id}) has been {status_word}."
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
