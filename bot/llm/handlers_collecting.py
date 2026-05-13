"""
Pre-lock action handlers.

Each handler has the signature:
    async def handle_xxx(input, state, message, bot) -> str | None

Returns None on success (LLM's text response is the user-facing reply) or
an error message string on failure (agent will prefix with 🚨).

In COLLECTING phase the user can change their own submission directly —
no admin approval needed.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import select

from bot.database import async_session
from bot.llm.slots import slots_in_windows, slots_for_day
from bot.models import Event, Submission

if TYPE_CHECKING:
    import discord
    from discord.ext import commands

logger = logging.getLogger("scheduler.llm.handlers_collecting")


async def handle_set_availability(
    action_input: dict,
    state: dict,
    message: "discord.Message",
    bot: "commands.Bot",
) -> str | None:
    """Replace the user's availability for ONE day."""
    day = action_input.get("day")
    windows = action_input.get("windows", [])
    if day not in (1, 2, 4):
        return f"set_availability: invalid day {day!r} (must be 1, 2, or 4)"

    user_id = state["user"]["discord_id"]
    user_name = state["user"]["display_name"]

    async with async_session() as session:
        # Re-fetch the event from this session
        ev_result = await session.execute(
            select(Event).where(Event.day1_date == datetime.fromisoformat(
                state["event"]["day1_date"]
            ).replace(tzinfo=__import__("datetime").timezone.utc))
        )
        event = ev_result.scalar_one_or_none()
        if event is None:
            return "set_availability: event not found"
        day1 = event.day1_date

        new_day_slots = slots_in_windows(day1, day, windows)

        sub_result = await session.execute(
            select(Submission).where(
                Submission.event_id == event.event_id,
                Submission.discord_id == user_id,
            )
        )
        submission = sub_result.scalar_one_or_none()

        if submission is None:
            submission = Submission(
                event_id=event.event_id,
                discord_id=user_id,
                discord_name=user_name,
                availability=new_day_slots if new_day_slots else None,
                has_availability=bool(new_day_slots),
            )
            session.add(submission)
        else:
            # Keep slots for other days, replace this day's slots.
            existing = submission.availability or []
            day_other = [s for s in existing if not s.startswith(f"D{day}-")]
            # But Day 1's slots include the boundary D1-CM-49 which can also be
            # part of "Day 2 territory" conceptually. We only replace strict
            # D{day}- prefixes, so D1-CM-49 stays under the Day 1 bucket.
            merged = day_other + new_day_slots

            # Stable order: sort by slot_index within each day
            all_slots = (
                slots_for_day(day1, 1) + slots_for_day(day1, 2) + slots_for_day(day1, 4)
            )
            order_index = {s["slot_id"]: i for i, s in enumerate(all_slots)}
            merged.sort(key=lambda sid: order_index.get(sid, 9999))

            submission.availability = merged if merged else None
            submission.has_availability = bool(merged)
            submission.discord_name = user_name  # keep fresh

        # If we have resources too, recompute priorities (cheap, idempotent).
        if submission.has_screenshot:
            submission.compute_priorities()

        await session.commit()
        logger.info(
            f"set_availability: user={user_id} day={day} windows={len(windows)} "
            f"-> {len(new_day_slots)} slot(s)"
        )

    return None


async def handle_query(
    action_input: dict,
    state: dict,
    message: "discord.Message",
    bot: "commands.Bot",
) -> str | None:
    """No-op signal — the LLM has the state and answers in its text reply."""
    logger.info(f"query: user={state['user']['discord_id']} subject={action_input.get('subject')!r}")
    return None


async def handle_out_of_scope(
    action_input: dict,
    state: dict,
    message: "discord.Message",
    bot: "commands.Bot",
) -> str | None:
    logger.info(
        f"out_of_scope: user={state['user']['discord_id']} reason={action_input.get('reason')!r}"
    )
    return None


async def handle_clarify(
    action_input: dict,
    state: dict,
    message: "discord.Message",
    bot: "commands.Bot",
) -> str | None:
    logger.info(
        f"clarify: user={state['user']['discord_id']} ambiguity={action_input.get('ambiguity')!r}"
    )
    return None


# Map action names to handler functions. The agent uses this for dispatch.
COLLECTING_HANDLERS = {
    "set_availability": handle_set_availability,
    "query": handle_query,
    "out_of_scope": handle_out_of_scope,
    "clarify": handle_clarify,
}
