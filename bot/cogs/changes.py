"""
Changes cog.

Two reaction-driven flows:

  1. Admin approval. The Submissions cog hands a message to the LLM agent
     which (post-lock) may create a ChangeRequest in PENDING_ADMIN and post
     an approval message in #schedule_approve. Admins react ✅ to approve
     (the change is applied + CSV regenerated + user DM'd) or ❌ to reject
     (DM'd to the user with the rejection).

  2. Swap user-B confirmation. The agent's swap handler creates a
     ChangeRequest in PENDING_CONFIRMATION and DMs user B with ✅/❌.
     User B reacts ✅ → transitions to PENDING_ADMIN and posts the admin
     approval message. User B reacts ❌ → marks REJECTED and DMs user A.

This cog also exposes nothing else — submissions.py owns the on_message
listener and dispatches every text message through the agent regardless of
phase.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

import discord
from discord.ext import commands
from sqlalchemy import select

from bot.config import (
    ADMIN_ROLE,
    SCHEDULE_APPROVE_CHANNEL,
    SCHEDULE_LOG_CHANNEL,
)
from bot.database import async_session
from bot.models import (
    Assignment,
    AuditLog,
    ChangeRequest,
    ChangeStatus,
    Event,
    Slot,
)

logger = logging.getLogger("scheduler.changes")

_OK = "✅"
_NO = "❌"


class Changes(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ─── Reaction listener ───────────────────────────────────

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent):
        if self.bot.user is None or payload.user_id == self.bot.user.id:
            return  # ignore our own initial reactions
        emoji = str(payload.emoji)
        if emoji not in (_OK, _NO):
            return

        channel = self.bot.get_channel(payload.channel_id)
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(payload.channel_id)
            except (discord.NotFound, discord.Forbidden):
                return

        try:
            message = await channel.fetch_message(payload.message_id)
        except (discord.NotFound, discord.Forbidden):
            return

        if message.author.id != self.bot.user.id:
            return  # only react to OUR messages

        if isinstance(channel, discord.DMChannel):
            await self._handle_swap_dm_reaction(payload, message, emoji)
        elif getattr(channel, "name", None) == SCHEDULE_APPROVE_CHANNEL:
            await self._handle_admin_reaction(payload, message, emoji)

    # ─── Admin approval handler ─────────────────────────────

    async def _handle_admin_reaction(
        self,
        payload: discord.RawReactionActionEvent,
        message: discord.Message,
        emoji: str,
    ):
        guild = self.bot.get_guild(payload.guild_id) if payload.guild_id else None
        if guild is None:
            return
        admin_role = discord.utils.get(guild.roles, name=ADMIN_ROLE)
        member = guild.get_member(payload.user_id)
        if member is None or admin_role is None or admin_role not in member.roles:
            return  # not an admin — ignore

        async with async_session() as session:
            result = await session.execute(
                select(ChangeRequest).where(
                    ChangeRequest.approval_message_id == message.id,
                    ChangeRequest.status == ChangeStatus.PENDING_ADMIN,
                )
            )
            change = result.scalar_one_or_none()
            if change is None:
                return  # already processed, or message is something else

            change_id = change.change_id
            now = datetime.now(timezone.utc)
            details = dict(change.details or {})

            if emoji == _OK:
                ok, apply_err = await self._apply_change(session, change)
                if ok:
                    change.status = ChangeStatus.APPROVED
                    change.resolved_at = now
                    session.add(AuditLog(
                        event_id=change.event_id,
                        action=f"Change #{change_id} ({details.get('action')}) approved",
                        actor=str(member),
                        details=details,
                    ))
                    await session.commit()
                    await self._dm_change_outcome(change, approved=True)
                    await self._regenerate_csv_to_log(change.event_id)
                    await self._stamp_approval_message(message, member, approved=True)
                    logger.info(f"change #{change_id} approved by {member}")
                else:
                    # Apply failed — leave status as PENDING_ADMIN so admin can retry
                    await session.commit()
                    try:
                        await message.reply(f"🚨 Couldn't apply: {apply_err}", mention_author=False)
                    except discord.HTTPException:
                        pass
                    logger.warning(f"change #{change_id} apply failed: {apply_err}")
            else:  # _NO
                change.status = ChangeStatus.REJECTED
                change.resolved_at = now
                session.add(AuditLog(
                    event_id=change.event_id,
                    action=f"Change #{change_id} ({details.get('action')}) rejected",
                    actor=str(member),
                    details=details,
                ))
                await session.commit()
                await self._dm_change_outcome(change, approved=False)
                await self._stamp_approval_message(message, member, approved=False)
                logger.info(f"change #{change_id} rejected by {member}")

    # ─── Swap-DM confirmation handler ───────────────────────

    async def _handle_swap_dm_reaction(
        self,
        payload: discord.RawReactionActionEvent,
        message: discord.Message,
        emoji: str,
    ):
        async with async_session() as session:
            result = await session.execute(
                select(ChangeRequest).where(
                    ChangeRequest.swap_confirm_message_id == message.id,
                    ChangeRequest.status == ChangeStatus.PENDING_CONFIRMATION,
                )
            )
            change = result.scalar_one_or_none()
            if change is None:
                return

            details = dict(change.details or {})
            user_b_id = details.get("user_b_id")
            user_a_id = details.get("user_a_id")
            if payload.user_id != user_b_id:
                return  # only user B's reaction counts

            now = datetime.now(timezone.utc)
            change_id = change.change_id

            if emoji == _NO:
                change.status = ChangeStatus.REJECTED
                change.resolved_at = now
                session.add(AuditLog(
                    event_id=change.event_id,
                    action=f"Swap #{change_id} declined by user B",
                    actor=str(user_b_id),
                    details=details,
                ))
                await session.commit()

                # DM user A
                try:
                    user_a = self.bot.get_user(user_a_id) or await self.bot.fetch_user(user_a_id)
                    await user_a.send(
                        f"❌ Your swap request for Day {details.get('day')} was declined."
                    )
                except (discord.NotFound, discord.Forbidden):
                    pass
                # Acknowledge to user B
                try:
                    await message.reply("Got it — swap declined. The other player has been notified.")
                except discord.HTTPException:
                    pass
                logger.info(f"swap #{change_id} declined by user B")
                return

            # _OK: transition to PENDING_ADMIN and post approval message
            change.status = ChangeStatus.PENDING_ADMIN

            # Look up slots for the approval message body
            user_a_slot = await session.get(Slot, details["user_a_slot_id"])
            user_b_slot = await session.get(Slot, details["user_b_slot_id"])

            user_a_member = self._find_member(user_a_id)
            user_a_name = user_a_member.display_name if user_a_member else str(user_a_id)
            user_b_member = self._find_member(user_b_id)
            user_b_name = user_b_member.display_name if user_b_member else str(user_b_id)

            body_lines = [
                f"**Change #{change_id} — swap (confirmed by both players)**",
                f"Day {details['day']}",
                f"{user_a_name} ↔ {user_b_name}",
            ]
            if user_a_slot:
                body_lines.append(
                    f"  {user_a_name}: {self._slot_time_label(user_a_slot)} "
                    f"on {user_a_slot.start_time.strftime('%Y-%m-%d')}"
                )
            if user_b_slot:
                body_lines.append(
                    f"  {user_b_name}: {self._slot_time_label(user_b_slot)} "
                    f"on {user_b_slot.start_time.strftime('%Y-%m-%d')}"
                )
            if details.get("reason"):
                body_lines.append(f"Reason: {details['reason']}")

            new_msg_id = await self._post_approval_message("\n".join(body_lines))
            if new_msg_id is not None:
                change.approval_message_id = new_msg_id

            await session.commit()
            try:
                await message.reply(
                    "Thanks — the swap is now pending admin approval."
                )
            except discord.HTTPException:
                pass
            logger.info(f"swap #{change_id} confirmed by user B, now pending admin")

    # ─── Apply approved changes ─────────────────────────────

    async def _apply_change(
        self,
        session,
        change: ChangeRequest,
    ) -> tuple[bool, str | None]:
        """Apply an approved change. Returns (ok, error_message_or_None)."""
        details = dict(change.details or {})
        action = details.get("action")
        event_id = change.event_id

        if action == "move_slot":
            from_id = details["from_slot_id"]
            to_id = details["to_slot_id"]
            user_id = change.requested_by

            # Delete the old assignment if it still exists
            await session.execute(
                Assignment.__table__.delete().where(
                    Assignment.event_id == event_id,
                    Assignment.discord_id == user_id,
                    Assignment.slot_id == from_id,
                )
            )

            # Check the target slot isn't taken by someone else (race)
            occupied = await session.execute(
                select(Assignment).where(
                    Assignment.event_id == event_id,
                    Assignment.slot_id == to_id,
                )
            )
            if occupied.scalar_one_or_none() is not None:
                # Re-add the old one — we can't apply safely
                session.add(Assignment(
                    event_id=event_id,
                    discord_id=user_id,
                    slot_id=from_id,
                ))
                return False, "target slot is no longer free"

            session.add(Assignment(
                event_id=event_id,
                discord_id=user_id,
                slot_id=to_id,
            ))
            return True, None

        if action == "drop_slot":
            from_id = details["from_slot_id"]
            user_id = change.requested_by
            await session.execute(
                Assignment.__table__.delete().where(
                    Assignment.event_id == event_id,
                    Assignment.discord_id == user_id,
                    Assignment.slot_id == from_id,
                )
            )
            return True, None

        if action == "swap":
            a_id = details["user_a_id"]
            b_id = details["user_b_id"]
            a_slot = details["user_a_slot_id"]
            b_slot = details["user_b_slot_id"]

            # Verify both still own their slots
            for uid, sid in ((a_id, a_slot), (b_id, b_slot)):
                exists = await session.execute(
                    select(Assignment).where(
                        Assignment.event_id == event_id,
                        Assignment.discord_id == uid,
                        Assignment.slot_id == sid,
                    )
                )
                if exists.scalar_one_or_none() is None:
                    return False, f"player {uid} no longer holds the slot to swap"

            # Delete both, then create swapped pair
            await session.execute(
                Assignment.__table__.delete().where(
                    Assignment.event_id == event_id,
                    Assignment.discord_id.in_([a_id, b_id]),
                    Assignment.slot_id.in_([a_slot, b_slot]),
                )
            )
            session.add(Assignment(event_id=event_id, discord_id=a_id, slot_id=b_slot))
            session.add(Assignment(event_id=event_id, discord_id=b_id, slot_id=a_slot))
            return True, None

        return False, f"unknown action: {action!r}"

    # ─── DM helpers ─────────────────────────────────────────

    async def _dm_change_outcome(self, change: ChangeRequest, approved: bool):
        """Notify the requester (and other player for swaps) of the outcome."""
        details = dict(change.details or {})
        action = details.get("action", "request")
        day = details.get("day", "?")

        # Build a human-readable summary based on action + outcome
        if action == "move_slot":
            from_id = details.get("from_slot_id")
            to_id = details.get("to_slot_id")
            from_lbl, to_lbl = await self._look_up_two_slot_labels(from_id, to_id)
            if approved:
                msg = f"✅ Your Day {day} slot was moved from {from_lbl} to {to_lbl}."
            else:
                msg = f"❌ Your request to move your Day {day} slot from {from_lbl} to {to_lbl} was declined."
            await self._dm_user(change.requested_by, msg)

        elif action == "drop_slot":
            from_id = details.get("from_slot_id")
            from_lbl, _ = await self._look_up_two_slot_labels(from_id, None)
            if approved:
                msg = f"✅ Your Day {day} assignment ({from_lbl}) was dropped."
            else:
                msg = f"❌ Your request to drop your Day {day} assignment was declined."
            await self._dm_user(change.requested_by, msg)

        elif action == "swap":
            a_id = details.get("user_a_id")
            b_id = details.get("user_b_id")
            a_slot = details.get("user_a_slot_id")
            b_slot = details.get("user_b_slot_id")
            a_lbl, b_lbl = await self._look_up_two_slot_labels(a_slot, b_slot)
            if approved:
                # After swap, A has b_slot, B has a_slot
                await self._dm_user(
                    a_id,
                    f"✅ Swap approved. Your new Day {day} slot is {b_lbl}.",
                )
                await self._dm_user(
                    b_id,
                    f"✅ Swap approved. Your new Day {day} slot is {a_lbl}.",
                )
            else:
                await self._dm_user(a_id, f"❌ Your swap on Day {day} was declined.")
                await self._dm_user(b_id, f"❌ The swap on Day {day} was declined.")

    async def _look_up_two_slot_labels(
        self,
        a_id: str | None,
        b_id: str | None,
    ) -> tuple[str, str]:
        async with async_session() as session:
            a = await session.get(Slot, a_id) if a_id else None
            b = await session.get(Slot, b_id) if b_id else None
        return (
            self._slot_time_label(a) if a else "(unknown)",
            self._slot_time_label(b) if b else "(unknown)",
        )

    async def _dm_user(self, user_id: int, content: str):
        try:
            user = self.bot.get_user(user_id) or await self.bot.fetch_user(user_id)
            await user.send(content)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            logger.warning(f"Could not DM user {user_id}")

    # ─── CSV regeneration ───────────────────────────────────

    async def _regenerate_csv_to_log(self, event_id: int):
        """Post the updated schedule CSV to #schedule_log."""
        scheduling_cog = self.bot.get_cog("Scheduling")
        if scheduling_cog is None:
            return
        csv_file = await scheduling_cog._generate_csv(event_id)

        async with async_session() as session:
            event = await session.get(Event, event_id)
        day1 = event.day1_date if event else datetime.now(timezone.utc)

        for guild in self.bot.guilds:
            log_channel = discord.utils.get(guild.text_channels, name=SCHEDULE_LOG_CHANNEL)
            if log_channel is None:
                continue
            try:
                await log_channel.send(
                    "🔄 Schedule updated after a change.",
                    file=discord.File(csv_file, filename=f"schedule_{day1.date()}.csv"),
                )
            except discord.HTTPException as e:
                logger.warning(f"Failed to post updated CSV: {e}")
            # csv_file is a BytesIO; consumed by one send. We'd need to
            # re-generate for additional guilds, but the typical setup is one.
            break

    # ─── Approval-message helpers ───────────────────────────

    def _slot_time_label(self, slot: Slot) -> str:
        return (
            f"{slot.start_time.strftime('%H:%M')}-"
            f"{slot.end_time.strftime('%H:%M')} UTC"
        )

    def _find_member(self, user_id: int) -> discord.Member | None:
        for guild in self.bot.guilds:
            m = guild.get_member(user_id)
            if m is not None:
                return m
        return None

    async def _post_approval_message(self, body: str) -> int | None:
        """Post to #schedule_approve and seed ✅/❌ reactions. Returns msg id."""
        for guild in self.bot.guilds:
            ch = discord.utils.get(guild.text_channels, name=SCHEDULE_APPROVE_CHANNEL)
            if ch is None:
                continue
            admin_role = discord.utils.get(guild.roles, name=ADMIN_ROLE)
            mention = admin_role.mention if admin_role else ""
            msg = await ch.send(f"{mention}\n{body}".strip())
            try:
                await msg.add_reaction(_OK)
                await msg.add_reaction(_NO)
            except discord.HTTPException:
                pass
            return msg.id
        return None

    async def _stamp_approval_message(
        self,
        message: discord.Message,
        admin: discord.Member,
        approved: bool,
    ):
        """Mark the original approval message as resolved (stamp + clear reactions)."""
        stamp = "**✅ APPROVED**" if approved else "**❌ REJECTED**"
        try:
            new_content = f"{message.content}\n\n{stamp} by {admin.display_name}"
            if len(new_content) <= 2000:
                await message.edit(content=new_content)
        except (discord.NotFound, discord.HTTPException):
            pass
        try:
            await message.clear_reactions()
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            pass


async def setup(bot: commands.Bot):
    await bot.add_cog(Changes(bot))
