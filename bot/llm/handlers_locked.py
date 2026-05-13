"""
Post-lock action handlers.

Same handler signature as the COLLECTING set:
    async def handle_xxx(input, state, message, bot) -> str | None

Returns None on success, an English error string on failure.

Three of the actions (move_slot, drop_slot, swap) create ChangeRequest
rows that need admin (and for swaps, also other-player) approval before
they actually change assignments. Those approvals are processed by the
Changes cog's reaction listener — that's Phase 2c. The handlers here
just create the row and post the message that the listener will react to.

widen_availability is applied immediately (no approval): it only adds
to the user's availability list and does not change current assignments.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone, timedelta
from typing import TYPE_CHECKING

import discord
from sqlalchemy import select

from bot.config import (
    ADMIN_ROLE,
    SCHEDULE_APPROVE_CHANNEL,
    SWAP_USER_DEADLINE_MINUTES,
)
from bot.database import async_session
from bot.llm.slots import (
    find_slot_by_start,
    slots_for_day,
    slots_in_windows,
)
from bot.models import (
    Assignment,
    ChangeRequest,
    ChangeStatus,
    ChangeType,
    Event,
    Slot,
    Submission,
)

if TYPE_CHECKING:
    from discord.ext import commands

logger = logging.getLogger("scheduler.llm.handlers_locked")


# ─── Internal helpers ────────────────────────────────────────────


def _parse_day1(state: dict) -> datetime:
    """Convert the state envelope's day1 string back to an aware datetime."""
    return datetime.fromisoformat(state["event"]["day1_date"]).replace(tzinfo=timezone.utc)


async def _fetch_event(session, day1: datetime) -> Event | None:
    result = await session.execute(select(Event).where(Event.day1_date == day1))
    return result.scalar_one_or_none()


async def _fetch_slot(session, slot_id: str) -> Slot | None:
    return await session.get(Slot, slot_id)


def _slot_time_label(slot: Slot) -> str:
    """Human-readable 'HH:MM-HH:MM UTC' for a slot."""
    return (
        f"{slot.start_time.strftime('%H:%M')}-"
        f"{slot.end_time.strftime('%H:%M')} UTC"
    )


async def _find_admin_channel(bot: "commands.Bot") -> tuple[discord.TextChannel | None, discord.Role | None]:
    """Find #schedule_approve in any guild the bot is in, plus the admin role."""
    for guild in bot.guilds:
        ch = discord.utils.get(guild.text_channels, name=SCHEDULE_APPROVE_CHANNEL)
        if ch is not None:
            role = discord.utils.get(guild.roles, name=ADMIN_ROLE)
            return ch, role
    return None, None


def _find_member(bot: "commands.Bot", user_id: int) -> discord.Member | None:
    for guild in bot.guilds:
        m = guild.get_member(user_id)
        if m is not None:
            return m
    return None


async def _post_approval_message(
    bot: "commands.Bot",
    body: str,
) -> int | None:
    """Post a pending-approval message to #schedule_approve with ✅/❌ reactions.
    Returns the message id (so it can be stored on the ChangeRequest) or None.
    """
    channel, admin_role = await _find_admin_channel(bot)
    if channel is None:
        logger.warning("No #schedule_approve channel found — admin won't see the request")
        return None

    mention = admin_role.mention if admin_role else ""
    msg = await channel.send(f"{mention}\n{body}".strip())
    try:
        await msg.add_reaction("✅")
        await msg.add_reaction("❌")
    except discord.HTTPException as e:
        logger.warning(f"Could not add reactions to approval message: {e}")
    return msg.id


# ─── Action handlers ─────────────────────────────────────────────


async def handle_move_slot(
    action_input: dict,
    state: dict,
    message: discord.Message,
    bot: "commands.Bot",
) -> str | None:
    day = action_input.get("day")
    new_start_utc = action_input.get("new_start_utc")
    reason = action_input.get("reason") or ""
    if day not in (1, 2, 4) or not new_start_utc:
        return f"move_slot: bad input day={day!r} new_start_utc={new_start_utc!r}"

    # Find the user's current assignment for that day (from state)
    current = next((a for a in state["assignments"] if a["day"] == day), None)
    if current is None:
        return f"You don't currently have a Day {day} assignment to move."

    day1 = _parse_day1(state)
    target_slot_id = find_slot_by_start(day1, day, new_start_utc, track=current["track"])
    if target_slot_id is None:
        return (
            f"No slot starts exactly at {new_start_utc} UTC on Day {day} "
            f"(track {current['track']}). Slots start every 30 minutes."
        )
    if target_slot_id == current["slot_id"]:
        return f"You're already assigned to {new_start_utc} UTC on Day {day} — nothing to do."

    async with async_session() as session:
        event = await _fetch_event(session, day1)
        if event is None:
            return "move_slot: event not found"

        from_slot = await _fetch_slot(session, current["slot_id"])
        to_slot = await _fetch_slot(session, target_slot_id)
        if from_slot is None or to_slot is None:
            return "move_slot: slot lookup failed"

        change = ChangeRequest(
            event_id=event.event_id,
            requested_by=message.author.id,
            change_type=ChangeType.UPDATE,
            status=ChangeStatus.PENDING_ADMIN,
            details={
                "action": "move_slot",
                "day": day,
                "track": current["track"],
                "from_slot_id": from_slot.slot_id,
                "to_slot_id": to_slot.slot_id,
                "reason": reason,
            },
            reason_for_user=reason or None,
        )
        session.add(change)
        await session.flush()
        change_id = change.change_id

        body = (
            f"**Change #{change_id} — move slot**\n"
            f"User: {message.author.display_name} ({message.author.mention})\n"
            f"Day {day} ({current['track_label']})\n"
            f"From: {_slot_time_label(from_slot)} on {from_slot.start_time.strftime('%Y-%m-%d')}\n"
            f"To:   {_slot_time_label(to_slot)} on {to_slot.start_time.strftime('%Y-%m-%d')}"
        )
        if reason:
            body += f"\nReason: {reason}"

        msg_id = await _post_approval_message(bot, body)
        if msg_id is not None:
            change.approval_message_id = msg_id

        await session.commit()
        logger.info(f"move_slot: change #{change_id} created (user={message.author.id})")

    return None


async def handle_drop_slot(
    action_input: dict,
    state: dict,
    message: discord.Message,
    bot: "commands.Bot",
) -> str | None:
    day = action_input.get("day")
    reason = action_input.get("reason") or ""
    if day not in (1, 2, 4):
        return f"drop_slot: bad day {day!r}"

    current = next((a for a in state["assignments"] if a["day"] == day), None)
    if current is None:
        return f"You don't currently have a Day {day} assignment to drop."

    day1 = _parse_day1(state)
    async with async_session() as session:
        event = await _fetch_event(session, day1)
        if event is None:
            return "drop_slot: event not found"

        from_slot = await _fetch_slot(session, current["slot_id"])
        if from_slot is None:
            return "drop_slot: slot lookup failed"

        change = ChangeRequest(
            event_id=event.event_id,
            requested_by=message.author.id,
            change_type=ChangeType.UPDATE,
            status=ChangeStatus.PENDING_ADMIN,
            details={
                "action": "drop_slot",
                "day": day,
                "track": current["track"],
                "from_slot_id": from_slot.slot_id,
                "reason": reason,
            },
            reason_for_user=reason or None,
        )
        session.add(change)
        await session.flush()
        change_id = change.change_id

        body = (
            f"**Change #{change_id} — drop slot**\n"
            f"User: {message.author.display_name} ({message.author.mention})\n"
            f"Day {day} ({current['track_label']}): "
            f"{_slot_time_label(from_slot)} on {from_slot.start_time.strftime('%Y-%m-%d')}"
        )
        if reason:
            body += f"\nReason: {reason}"

        msg_id = await _post_approval_message(bot, body)
        if msg_id is not None:
            change.approval_message_id = msg_id

        await session.commit()
        logger.info(f"drop_slot: change #{change_id} created (user={message.author.id})")

    return None


async def handle_widen_availability(
    action_input: dict,
    state: dict,
    message: discord.Message,
    bot: "commands.Bot",
) -> str | None:
    """Add to the user's availability immediately (no admin approval).

    This does NOT change current assignments — it only widens the pool of
    slots the optimizer would consider. Admin can manually reassign using
    the widened availability if they wish.
    """
    day = action_input.get("day")
    windows = action_input.get("windows", [])
    if day not in (1, 2, 4):
        return f"widen_availability: bad day {day!r}"

    user_id = state["user"]["discord_id"]
    day1 = _parse_day1(state)
    new_slots = slots_in_windows(day1, day, windows)
    if not new_slots:
        return f"widen_availability: no slots matched the requested windows on Day {day}."

    async with async_session() as session:
        event = await _fetch_event(session, day1)
        if event is None:
            return "widen_availability: event not found"

        sub_result = await session.execute(
            select(Submission).where(
                Submission.event_id == event.event_id,
                Submission.discord_id == user_id,
            )
        )
        submission = sub_result.scalar_one_or_none()
        if submission is None:
            return "widen_availability: no submission on file"

        existing = set(submission.availability or [])
        existing.update(new_slots)

        # Stable order by overall slot index
        all_slots = (
            slots_for_day(day1, 1) + slots_for_day(day1, 2) + slots_for_day(day1, 4)
        )
        order_index = {s["slot_id"]: i for i, s in enumerate(all_slots)}
        submission.availability = sorted(existing, key=lambda sid: order_index.get(sid, 9999))
        submission.has_availability = True

        await session.commit()
        logger.info(
            f"widen_availability: user={user_id} day={day} +{len(set(new_slots) - existing)} new"
        )

    return None


async def handle_swap(
    action_input: dict,
    state: dict,
    message: discord.Message,
    bot: "commands.Bot",
) -> str | None:
    """Initiate a swap: validate, create ChangeRequest PENDING_CONFIRMATION,
    DM the other player asking for ✅/❌ confirmation.

    The reaction listener in the Changes cog (Phase 2c) advances the request
    to PENDING_ADMIN on ✅, or marks it REJECTED on ❌.
    """
    other_id = action_input.get("other_player_discord_id")
    day = action_input.get("day")
    reason = action_input.get("reason") or ""

    if day not in (1, 2, 4) or not isinstance(other_id, int):
        return f"swap: bad input day={day!r} other_id={other_id!r}"

    # Validate against the pre-filtered swap partners in state
    valid_ids = {p["discord_id"] for p in state["valid_swap_partners"]}
    if other_id not in valid_ids:
        return (
            "swap: I can only swap with a player who's @mentioned in the message "
            "and already has an assignment in this event."
        )

    # Find our own assignment for the day
    my_assignment = next((a for a in state["assignments"] if a["day"] == day), None)
    if my_assignment is None:
        return f"swap: you don't have a Day {day} assignment to give up."

    user_id = state["user"]["discord_id"]
    day1 = _parse_day1(state)

    async with async_session() as session:
        event = await _fetch_event(session, day1)
        if event is None:
            return "swap: event not found"

        # Look up the other player's assignment for the same day
        other_result = await session.execute(
            select(Assignment, Slot)
            .join(Slot, Assignment.slot_id == Slot.slot_id)
            .where(
                Assignment.event_id == event.event_id,
                Assignment.discord_id == other_id,
                Slot.day == day,
            )
        )
        other_row = other_result.first()
        if other_row is None:
            return f"swap: the other player doesn't have a Day {day} assignment."
        _, other_slot = other_row

        my_slot = await _fetch_slot(session, my_assignment["slot_id"])
        if my_slot is None:
            return "swap: own slot lookup failed"

        # Create the change request in PENDING_CONFIRMATION
        deadline = datetime.now(timezone.utc) + timedelta(minutes=SWAP_USER_DEADLINE_MINUTES)
        change = ChangeRequest(
            event_id=event.event_id,
            requested_by=user_id,
            change_type=ChangeType.SWAP,
            status=ChangeStatus.PENDING_CONFIRMATION,
            details={
                "action": "swap",
                "day": day,
                "user_a_id": user_id,
                "user_b_id": other_id,
                "user_a_slot_id": my_slot.slot_id,
                "user_b_slot_id": other_slot.slot_id,
                "reason": reason,
            },
            reason_for_user=reason or None,
            user_deadline=deadline,
        )
        session.add(change)
        await session.flush()
        change_id = change.change_id

        # DM user B asking for confirmation
        other_member = _find_member(bot, other_id)
        if other_member is None:
            return "swap: couldn't find the other player in the guild."

        dm_body = (
            f"🔁 **Swap request #{change_id}**\n\n"
            f"{message.author.display_name} wants to swap with you on Day {day}:\n"
            f"• You currently have {_slot_time_label(other_slot)} on "
            f"{other_slot.start_time.strftime('%Y-%m-%d')}\n"
            f"• They currently have {_slot_time_label(my_slot)} on "
            f"{my_slot.start_time.strftime('%Y-%m-%d')}\n"
        )
        if reason:
            dm_body += f"• Their reason: {reason}\n"
        dm_body += (
            f"\nReact ✅ to accept, ❌ to decline. "
            f"This request expires in {SWAP_USER_DEADLINE_MINUTES} minutes."
        )

        try:
            dm_message = await other_member.send(dm_body)
            await dm_message.add_reaction("✅")
            await dm_message.add_reaction("❌")
            change.swap_confirm_message_id = dm_message.id
        except discord.Forbidden:
            await session.rollback()
            return (
                f"swap: {other_member.display_name} has DMs disabled, "
                f"so they can't confirm the swap."
            )

        await session.commit()
        logger.info(
            f"swap: change #{change_id} created (a={user_id} b={other_id} day={day})"
        )

    return None


async def handle_query(
    action_input: dict,
    state: dict,
    message: discord.Message,
    bot: "commands.Bot",
) -> str | None:
    logger.info(
        f"query: user={state['user']['discord_id']} subject={action_input.get('subject')!r}"
    )
    return None


async def handle_out_of_scope(
    action_input: dict,
    state: dict,
    message: discord.Message,
    bot: "commands.Bot",
) -> str | None:
    logger.info(
        f"out_of_scope: user={state['user']['discord_id']} reason={action_input.get('reason')!r}"
    )
    return None


async def handle_clarify(
    action_input: dict,
    state: dict,
    message: discord.Message,
    bot: "commands.Bot",
) -> str | None:
    logger.info(
        f"clarify: user={state['user']['discord_id']} ambiguity={action_input.get('ambiguity')!r}"
    )
    return None


LOCKED_HANDLERS = {
    "move_slot": handle_move_slot,
    "drop_slot": handle_drop_slot,
    "widen_availability": handle_widen_availability,
    "swap": handle_swap,
    "query": handle_query,
    "out_of_scope": handle_out_of_scope,
    "clarify": handle_clarify,
}
