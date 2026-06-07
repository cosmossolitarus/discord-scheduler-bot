"""
Pre-lock action handlers (COLLECTING phase).

Handler signature:
    async def handle_xxx(input, state, message, bot) -> str | None
Returns None on success, error string on failure (agent prefixes with 🚨).
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from sqlalchemy import select

from bot.database import async_session
from bot.llm.slots import slots_in_windows, slots_for_day
from bot.models import Event, PlayerProfile, Submission

if TYPE_CHECKING:
    import discord
    from discord.ext import commands

logger = logging.getLogger("scheduler.llm.handlers_collecting")

_PLAYER_ID_RE = re.compile(r"^\d{6,12}$")


# ─── Internal helpers ────────────────────────────────────────────


async def _upsert_player_profile(session, discord_id: int, player_id: str) -> None:
    """Create or update the player's cross-event profile entry."""
    profile = await session.get(PlayerProfile, discord_id)
    if profile is None:
        profile = PlayerProfile(
            discord_id=discord_id,
            ingame_player_id=player_id,
            updated_at=datetime.now(timezone.utc),
        )
        session.add(profile)
    else:
        profile.ingame_player_id = player_id
        profile.updated_at = datetime.now(timezone.utc)


async def _maybe_prefill_player_id(session, submission: Submission) -> None:
    """If a PlayerProfile exists for this discord_id, copy the stored player ID."""
    if submission.has_player_id:
        return
    profile = await session.get(PlayerProfile, submission.discord_id)
    if profile is not None:
        submission.player_ingame_id = profile.ingame_player_id
        submission.has_player_id = True


# ─── Action handlers ─────────────────────────────────────────────


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
        ev_result = await session.execute(
            select(Event).where(Event.day1_date == datetime.fromisoformat(
                state["event"]["day1_date"]
            ).replace(tzinfo=timezone.utc))
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
            await session.flush()
            await _maybe_prefill_player_id(session, submission)
        else:
            existing = submission.availability or []
            day_other = [s for s in existing if not s.startswith(f"D{day}-")]
            merged = day_other + new_day_slots

            all_slots = (
                slots_for_day(day1, 1) + slots_for_day(day1, 2) + slots_for_day(day1, 4)
            )
            order_index = {s["slot_id"]: i for i, s in enumerate(all_slots)}
            merged.sort(key=lambda sid: order_index.get(sid, 9999))

            submission.availability = merged if merged else None
            submission.has_availability = bool(merged)
            submission.discord_name = user_name

        if submission.has_screenshot:
            submission.compute_priorities()

        await session.commit()
        logger.info(
            f"set_availability: user={user_id} day={day} windows={len(windows)} "
            f"-> {len(new_day_slots)} slot(s)"
        )

    return None


async def handle_set_player_id(
    action_input: dict,
    state: dict,
    message: "discord.Message",
    bot: "commands.Bot",
) -> str | None:
    """Save the user's in-game player ID and update their PlayerProfile."""
    raw_id = str(action_input.get("player_id", "")).strip()
    if not _PLAYER_ID_RE.match(raw_id):
        return (
            f"set_player_id: '{raw_id}' doesn't look like a valid in-game player ID "
            f"(expected 6–12 digits). Please re-enter it."
        )

    user_id = state["user"]["discord_id"]
    user_name = state["user"]["display_name"]

    async with async_session() as session:
        ev_result = await session.execute(
            select(Event).where(Event.day1_date == datetime.fromisoformat(
                state["event"]["day1_date"]
            ).replace(tzinfo=timezone.utc))
        )
        event = ev_result.scalar_one_or_none()
        if event is None:
            return "set_player_id: event not found"

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
                player_ingame_id=raw_id,
                has_player_id=True,
            )
            session.add(submission)
        else:
            submission.player_ingame_id = raw_id
            submission.has_player_id = True
            submission.discord_name = user_name

        await _upsert_player_profile(session, user_id, raw_id)
        await session.commit()
        logger.info(f"set_player_id: user={user_id} id={raw_id!r}")

    return None


async def handle_set_resources(
    action_input: dict,
    state: dict,
    message: "discord.Message",
    bot: "commands.Bot",
) -> str | None:
    """Save the user's premium resource counts (TTG, TG, Dust).

    Only updates fields that were explicitly provided in action_input.
    """
    user_id = state["user"]["discord_id"]
    user_name = state["user"]["display_name"]

    ttg = action_input.get("ttg")
    tg = action_input.get("tg")
    dust = action_input.get("dust")

    if ttg is None and tg is None and dust is None:
        return "set_resources: no resource values provided"

    async with async_session() as session:
        ev_result = await session.execute(
            select(Event).where(Event.day1_date == datetime.fromisoformat(
                state["event"]["day1_date"]
            ).replace(tzinfo=timezone.utc))
        )
        event = ev_result.scalar_one_or_none()
        if event is None:
            return "set_resources: event not found"

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
            )
            session.add(submission)
            await session.flush()
            await _maybe_prefill_player_id(session, submission)

        if ttg is not None:
            submission.ttg = float(ttg)
        if tg is not None:
            submission.tg = float(tg)
        if dust is not None:
            submission.dust = float(dust)
        submission.discord_name = user_name

        # Mark resources complete only when all three have been explicitly set
        # (even as 0). A player must report TTG, TG, and Dust to be complete.
        if submission.ttg is not None and submission.tg is not None and submission.dust is not None:
            submission.has_resources = True

        if submission.has_screenshot:
            submission.compute_priorities()

        await session.commit()
        parts = []
        if ttg is not None:
            parts.append(f"TTG={ttg}")
        if tg is not None:
            parts.append(f"TG={tg}")
        if dust is not None:
            parts.append(f"Dust={dust}")
        logger.info(f"set_resources: user={user_id} {' '.join(parts)}")

    return None


async def handle_query(
    action_input: dict,
    state: dict,
    message: "discord.Message",
    bot: "commands.Bot",
) -> str | None:
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


async def handle_greet(
    action_input: dict,
    state: dict,
    message: "discord.Message",
    bot: "commands.Bot",
) -> str | None:
    logger.info(
        f"greet: user={state['user']['discord_id']} kind={action_input.get('kind')!r}"
    )
    return None


COLLECTING_HANDLERS = {
    "set_availability": handle_set_availability,
    "set_player_id": handle_set_player_id,
    "set_resources": handle_set_resources,
    "query": handle_query,
    "greet": handle_greet,
    "out_of_scope": handle_out_of_scope,
    "clarify": handle_clarify,
}

# During LOCKED (admin review), players can only query/greet — no changes.
LOCKED_REVIEW_HANDLERS = {
    "query": handle_query,
    "greet": handle_greet,
    "out_of_scope": handle_out_of_scope,
    "clarify": handle_clarify,
}
