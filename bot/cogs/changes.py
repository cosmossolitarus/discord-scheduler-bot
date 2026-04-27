"""
Changes cog — post-lock change handling.
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

CHANGE_CLASSIFY_PROMPT = """You are a scheduling bot assistant. A user has sent a message
requesting a change to their schedule. Classify the request.

Possible types:
1. "update" — user is changing their availability or submitting new resources
2. "swap" — user wants to swap their slot with another specific player
3. "unclear" — you can't determine what they want

Respond with ONLY a JSON object:
{"type": "update"|"swap"|"unclear", "details": "brief description of what they want"}

For swaps, also include:
{"type": "swap", "other_user_mention": "<@123456>", "day": 1|2|4, "details": "..."}
"""


class Changes(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def handle_change_request(self, message: discord.Message, day1: datetime):
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

        other_mentions = [m for m in message.mentions if m.id != self.bot.user.id]
        if other_mentions:
            await self._handle_swap_request(message, day1, other_mentions[0])
            return

        try:
            response = await client.messages.create(
                model=ANTHROPIC_MODEL,
                max_tokens=200,
                system=CHANGE_CLASSIFY_PROMPT,
                messages=[{"role": "user", "content": text_content}],
            )
            result = json.loads(response.content[0].text.strip())
        except Exception as e:
            logger.error(f"Failed to classify change request: {e}")
            await message.reply(
                "I had trouble understanding your request. Could you rephrase?\n"
                "• \"I can't make my Day 2 slot, available after 4pm instead\"\n"
                "• \"Swap Day 1 with @player\""
            )
            return

        req_type = result.get("type", "unclear")

        if req_type == "update":
            await self._handle_availability_update(message, day1, text_content)
        elif req_type == "swap":
            await message.reply(
                "For swaps, please @mention the player you want to swap with. "
                "Example: \"@scheduler swap Day 1 with @player\""
            )
        else:
            await message.reply(
                "I wasn't sure what you meant. You can:\n"
                "• Update availability: \"@scheduler I can't make Day 2, available after 18:00 instead\"\n"
                "• Swap: \"@scheduler swap Day 1 with @player\"\n"
                "• New screenshot: just attach it when you @mention me"
            )

    async def _handle_resource_update(self, message: discord.Message, day1: datetime):
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

            old_priorities = {
                "x": submission.priority_x,
                "y": submission.priority_y,
                "z": submission.priority_z,
            }

            submission.resource_x = result["resource_x"]
            submission.resource_y = result["resource_y"]
            submission.resource_z = result["resource_z"]
            submission.resource_generic = result["resource_generic"]
            submission.compute_priorities(GENERIC_SPLIT)
            submission.screenshot_url = message.attachments[0].url

            bump_opportunities = await self._check_auto_bump(session, event, submission)

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

            await message.reply(
                f"✅ Resources updated. Your request is **pending admin review**.\n"
                f"Priority — X: {submission.priority_x:,.0f}, "
                f"Y: {submission.priority_y:,.0f}, "
                f"Z: {submission.priority_z:,.0f}"
            )

            await self._post_approval_request(message.guild, change, event, submission)

    async def _handle_availability_update(self, message, day1, text):
        slot_reference = generate_slot_times(day1)
        day1_str = day1.strftime("%B %d, %Y")
        avail_result = await parse_availability(text, day1_str, slot_reference)

        if "error" in avail_result:
            await message.reply(f"❌ {avail_result['error']}. Please try again.")
            return

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

            change = ChangeRequest(
                event_id=event.event_id,
                requested_by=message.author.id,
                change_type=ChangeType.UPDATE,
                status=ChangeStatus.PENDING_ADMIN,
                details={
                    "type": "availability_update",
                    "old_availability": old_availability,
                    "new_availability": avail_result["available_slots"],
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
            await message.reply(
                f"✅ Availability update received: {interp}\n"
                f"Your request is **pending admin review**."
            )
            await self._post_approval_request(message.guild, change, event, submission)

    async def _handle_swap_request(self, message, day1, other_user):
        async with async_session() as session:
            event = await self._get_event(session, day1)
            if event is None:
                return

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

            req_slots = {(a.slot.day, a.slot.track): a for a in requester_assignments}
            other_slots = {(a.slot.day, a.slot.track): a for a in other_assignments}
            common = set(req_slots.keys()) & set(other_slots.keys())

            if not common:
                await message.reply(
                    f"You and {other_user.display_name} don't share any day/track assignments to swap."
                )
                return

            if len(common) > 1:
                days_str = ", ".join(f"Day {d} {t}" for d, t in sorted(common))
                await message.reply(
                    f"You share multiple days: {days_str}. Please specify which day."
                )
                return

            day_track = list(common)[0]
            req_assignment = req_slots[day_track]
            other_assignment = other_slots[day_track]

            earlier_slot = min(
                req_assignment.slot, other_assignment.slot,
                key=lambda s: s.start_time
            )
            user_deadline = earlier_slot.start_time - timedelta(minutes=SWAP_USER_DEADLINE_MINUTES)
            admin_deadline = earlier_slot.start_time

            now = datetime.now(timezone.utc)
            if now >= user_deadline:
                await message.reply("It's too late to request this swap.")
                return

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

            req_time = req_assignment.slot.start_time.strftime("%H:%M UTC")
            other_time = other_assignment.slot.start_time.strftime("%H:%M UTC")
            await message.reply(
                f"Swap request: your {req_time} slot ↔ "
                f"{other_user.mention}'s {other_time} slot on "
                f"Day {day_track[0]} ({day_track[1]}).\n"
                f"Waiting for {other_user.display_name} to confirm."
            )

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
        if payload.user_id == self.bot.user.id:
            return
        emoji = str(payload.emoji)
        if emoji not in ("✅", "❌"):
            return

        async with async_session() as session:
            result = await session.execute(
                select(ChangeRequest).where(
                    ChangeRequest.swap_confirm_message_id == payload.message_id
                )
            )
            change = result.scalar_one_or_none()
            if change and change.status == ChangeStatus.PENDING_CONFIRMATION:
                await self._handle_swap_confirmation(session, change, emoji, payload)
                return

            result = await session.execute(
                select(ChangeRequest).where(
                    ChangeRequest.approval_message_id == payload.message_id
                )
            )
            change = result.scalar_one_or_none()
            if change and change.status == ChangeStatus.PENDING_ADMIN:
                guild = self.bot.get_guild(payload.guild_id) if payload.guild_id else None
                if guild:
                    member = guild.get_member(payload.user_id)
                    admin_role = discord.utils.get(guild.roles, name=ADMIN_ROLE)
                    if member and admin_role and admin_role in member.roles:
                        await self._handle_admin_decision(session, change, emoji, payload.user_id)

    async def _handle_swap_confirmation(self, session, change, emoji, payload):
        now = datetime.now(timezone.utc)
        if change.user_deadline and now >= change.user_deadline:
            change.status = ChangeStatus.EXPIRED
            await session.commit()
            return

        if emoji == "❌":
            change.status = ChangeStatus.REJECTED
            change.resolved_at = now
            await session.commit()
            for guild in self.bot.guilds:
                requester = guild.get_member(change.details["requester_id"])
                if requester:
                    try:
                        await requester.send("Your swap request was declined.")
                    except discord.Forbidden:
                        pass
            return

        change.status = ChangeStatus.PENDING_ADMIN
        await session.commit()
        for guild in self.bot.guilds:
            await self._post_swap_approval(guild, change)

    async def _handle_admin_decision(self, session, change, emoji, admin_id):
        now = datetime.now(timezone.utc)
        if change.admin_deadline and now >= change.admin_deadline:
            change.status = ChangeStatus.EXPIRED
            change.resolved_at = now
            await session.commit()
            return

        change.resolved_at = now
        change.resolved_by = admin_id

        if emoji == "❌":
            change.status = ChangeStatus.REJECTED
            await session.commit()
            await self._notify_change_result(change, approved=False)
            return

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

        event = await session.get(Event, change.event_id)
        if event:
            scheduling_cog = self.bot.get_cog("Scheduling")
            if scheduling_cog:
                csv_file = await scheduling_cog._generate_csv(event.day1_date)
                for guild in self.bot.guilds:
                    log_ch = discord.utils.get(guild.text_channels, name=SCHEDULE_LOG_CHANNEL)
                    if log_ch:
                        await log_ch.send(
                            f"Schedule updated (change #{change.change_id}).",
                            file=discord.File(csv_file, filename=f"schedule_updated.csv"),
                        )

    async def _apply_swap(self, session, change):
        details = change.details
        r1 = await session.execute(
            select(Assignment).where(
                Assignment.event_id == change.event_id,
                Assignment.slot_id == details["requester_slot"],
            )
        )
        r2 = await session.execute(
            select(Assignment).where(
                Assignment.event_id == change.event_id,
                Assignment.slot_id == details["other_slot"],
            )
        )
        a1, a2 = r1.scalar_one_or_none(), r2.scalar_one_or_none()
        if a1 and a2:
            a1.discord_id, a2.discord_id = a2.discord_id, a1.discord_id

    async def _apply_update(self, session, change):
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

    async def _apply_bump(self, session, change):
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

    async def _check_auto_bump(self, session, event, submission):
        bumps = []
        day_resource_map = {1: "priority_x", 2: "priority_y", 4: "priority_z"}

        for day, priority_field in day_resource_map.items():
            new_priority = getattr(submission, priority_field, 0) or 0
            if new_priority == 0:
                continue

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
                    break
        return bumps

    async def _post_approval_request(self, guild, change, event, submission):
        channel = discord.utils.get(guild.text_channels, name=SCHEDULE_APPROVE_CHANNEL)
        admin_role = discord.utils.get(guild.roles, name=ADMIN_ROLE)
        if not channel:
            return

        member = guild.get_member(change.requested_by)
        name = member.display_name if member else str(change.requested_by)
        details = change.details

        desc = f"**Change Request #{change.change_id}**\n"
        desc += f"Player: {name}\nType: {change.change_type.value}\n"

        if details.get("type") == "resource_update":
            desc += f"Updated priorities: X={details['new_priorities']['x']:,.0f}, "
            desc += f"Y={details['new_priorities']['y']:,.0f}, "
            desc += f"Z={details['new_priorities']['z']:,.0f}\n"
            if details.get("bump_opportunities"):
                for bump in details["bump_opportunities"]:
                    desc += (
                        f"⚠️ **Auto-bump eligible** on Day {bump['day']}: "
                        f"new priority {bump['new_priority']:,.0f} > "
                        f"2x current lowest {bump['bumped_priority']:,.0f}\n"
                    )
        elif details.get("type") == "availability_update":
            desc += f"Interpretation: {details.get('interpretation', 'N/A')}\n"

        desc += "\nReact ✅ to approve or ❌ to reject."

        msg = await channel.send(f"{admin_role.mention if admin_role else ''}\n{desc}")
        await msg.add_reaction("✅")
        await msg.add_reaction("❌")

        async with async_session() as session2:
            change_obj = await session2.get(ChangeRequest, change.change_id)
            change_obj.approval_message_id = msg.id
            await session2.commit()

    async def _post_swap_approval(self, guild, change):
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

    async def _notify_change_result(self, change, approved):
        status_word = "approved" if approved else "declined"
        for guild in self.bot.guilds:
            if change.change_type == ChangeType.SWAP:
                details = change.details
                for user_id in [details["requester_id"], details["other_id"]]:
                    member = guild.get_member(user_id)
                    if member:
                        try:
                            if approved:
                                await member.send(f"Your swap on Day {details['day']} has been {status_word} and implemented.")
                            else:
                                await member.send(f"The swap request on Day {details['day']} was {status_word}.")
                        except discord.Forbidden:
                            pass
            else:
                member = guild.get_member(change.requested_by)
                if member:
                    try:
                        await member.send(f"Your change request (#{change.change_id}) has been {status_word}.")
                    except discord.Forbidden:
                        pass

    async def _get_event(self, session, day1):
        result = await session.execute(select(Event).where(Event.day1_date == day1))
        return result.scalar_one_or_none()

    async def _get_user_assignments(self, session, event_id, discord_id):
        result = await session.execute(
            select(Assignment, Slot)
            .join(Slot, Assignment.slot_id == Slot.slot_id)
            .where(
                Assignment.event_id == event_id,
                Assignment.discord_id == discord_id,
            )
        )
        rows = result.all()
        assignments = []
        for assignment, slot in rows:
            assignment.slot = slot
            assignments.append(assignment)
        return assignments


async def setup(bot: commands.Bot):
    await bot.add_cog(Changes(bot))
