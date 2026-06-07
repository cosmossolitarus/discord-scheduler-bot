"""
Agent: single entry point for handling an @mention to the bot.

Architecture:
  1. PARSE — one LLM call with tool_choice="any" forces tool_use blocks.
  2. DISPATCH — each tool_use runs through a handler.
  3. RENDER — code templates the user-facing reply.

Phase routing:
  COLLECTING — full submission tools (availability, player ID, resources, screenshot)
  LOCKED     — minimal tools only (query/greet); schedule is in admin review
  PUBLISHED  — full change-request tools (move/drop/swap/etc.)

Allowed emoji: ✅ ❌ 🚨 only.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import anthropic
from sqlalchemy import select

from bot.config import ANTHROPIC_MODEL, GENERIC_SPLIT
from bot.database import async_session
from bot.llm.handlers_collecting import (
    COLLECTING_HANDLERS,
    LOCKED_REVIEW_HANDLERS,
)
from bot.llm.handlers_locked import PUBLISHED_HANDLERS
from bot.llm.screenshot import parse_screenshot
from bot.llm.slots import summarize_availability
from bot.llm.state import build_state_envelope, render_state_for_prompt
from bot.llm.tools import tools_for_phase
from bot.models import EventPhase, Submission

if TYPE_CHECKING:
    import discord
    from discord.ext import commands

    from bot.models import Event

logger = logging.getLogger("scheduler.llm.agent")

_client = anthropic.AsyncAnthropic()


# ─── Parsing prompt ──────────────────────────────────────────────


_PARSE_PROMPT = """You are an intent parser for a scheduling bot. Read the \
user's message and emit one or more tool_use blocks representing their intent. \
Do NOT write any text response — only emit tool_use blocks.

The bot manages 30-minute slots on Day 1, Day 2, and Day 4 of a 28-day cycle. \
Day 3 and Day 5 are not tracked.

## TOOL SELECTION

### Submission tools (COLLECTING phase only)
- `set_availability`: user is telling you their available times for a specific day. \
  Empty `windows` means "not available that day".
- `set_player_id`: user is providing their in-game numeric player ID \
  (e.g. "my ID is 12345678"). 6–12 digits only. Do NOT use for Discord IDs.
- `set_resources`: user is reporting how many TTG (Tempered Truegold / refined TG), \
  TG (Truegold), or Dust (Truegold Dust / TG dust) they have. Only set the fields \
  they mentioned. These are integer counts, not time values.

### Change-request tools (PUBLISHED phase only)
- `widen_availability`: user wants to ADD more available times on top of existing.
- `move_slot`: user wants to change the start time of an existing assignment. \
  Compute absolute UTC time yourself for relative requests.
- `drop_slot`: user wants to give up an assignment.
- `request_new_slot`: user has NO assignment on the requested day/track and wants one. \
  Check CURRENT STATE — use `move_slot` instead if they already have one.
- `swap`: user wants to trade slots with another player. Only call this if the user \
  @mentioned a player who appears in VALID SWAP PARTNERS.

### Always available
- `query`: user is asking about their current state.
- `greet`: user said hi/hello/test, asked for help, or sent an opener.
- `out_of_scope`: Day 3/5 chatter, other players' data, or truly unrelated requests.
- `clarify`: request is ambiguous and cannot be reliably acted on.

## MULTI-DAY MESSAGES

Emit one tool_use per day. Example: "Day 1 anytime, Day 2 after 8pm UTC" → \
set_availability(day=1, windows=[full day]) + set_availability(day=2, windows=[20:00-23:59]).

Do NOT emit set_availability for days the user did NOT mention.

## UPDATING AVAILABILITY

set_availability REPLACES the windows for the day specified. Look at CURRENT \
STATE's availability summary to decide how to compose `windows`:
- ADDITIVE language ("also Day 1 evenings", "add Day 1 mornings") → include BOTH \
  the existing windows AND the new ones.
- REPLACEMENT language ("change Day 1 to evenings", "actually Day 1 mornings instead") \
  → emit ONLY the new windows.
- AMBIGUOUS → treat as REPLACEMENT. Latest message wins.

## CLEARING A DAY (pre-lock)

"drop / remove / cancel / clear / scratch / take me off / I can't make / not available / skip" \
for a day → set_availability(day=X, windows=[]).

Pre-lock the word "drop" NEVER refers to `drop_slot`.

## SIGNING UP WITHOUT A TIME

"Sign me up Day X" / "add me Day X" / "I'm in for Day X" with NO time → \
windows=[{start_utc:"00:00", end_utc:"23:59"}] (treat as full day).

## TIME RULES

- All times in tool_use inputs are UTC, HH:MM 24-hour format.
- Convert local timezones to UTC.
- A window may cross midnight — emit end_utc earlier than start_utc, the backend handles it.
- "Any day" / "anytime" / "all day" for Day X → windows=[{start_utc:"00:00", end_utc:"23:59"}].

## MIDNIGHT AMBIGUITY — IMPORTANT

Day 1 and Day 4 each span nearly 25 hours. Their slot windows START at 23:45 of the \
previous calendar night and END at 00:15 of the following morning. This means "0 UTC" \
or "midnight" appears TWICE in each day's schedule:

  Day 1 example (Day 1 = Mon May 18):
    • Near the START: 00:00 on Mon May 18 falls inside the first Day 1 slot \
(Sun May 17 23:45 → Mon May 18 00:15).
    • Near the END: 00:00 on Tue May 19 falls inside the LAST Day 1 slot \
(Mon May 18 23:45 → Tue May 19 00:15) — this is the boundary slot.

  Day 4 has the same pattern (23:45 three nights before Day 1 through 00:15 four nights after).

When a user says "midnight" or "0 UTC" or "reset" in the context of Day 1 or Day 4:
  - If they mean the START of the day (just after the game resets for Day 1/4) → \
    interpret as 00:00 of the Day 1/4 calendar date.
  - If they mean the END of the day / "near Day 2 start" for Day 1 → \
    interpret as 23:45-00:15 of the LAST slot (the boundary slot area).
  - If it is AMBIGUOUS, emit `clarify` asking which end they mean: \
    "Do you mean near the very start of Day 1 (around 00:00 on [Day 1 date]) or \
    near the very end (around 23:45–00:15 crossing into [Day 2 date])?"

For post-lock slot times: slots start at :15 or :45 past the hour. Never emit 00:00 \
as a new_start_utc — use 23:45 or 00:15 depending on context.

## RESET SEMANTICS

- "close to reset" / "near reset" / "late" → 21:15-00:15 UTC
- "after reset" / "just after reset" / "early" → 23:45 (previous day) - 02:45 UTC
- "reset" alone → 00:00 UTC exactly (which falls inside the :45→:15 slot centered on midnight)

## DAY MAPPING

- construction / building = Day 1
- research = Day 2
- troops / training / soldiers = Day 4

## SCREENSHOTS

Screenshots are parsed separately by code — do not emit a tool for them.

## OUTPUT

Emit only tool_use blocks. Do not write any text.
"""


def _build_parse_prompt(state: dict) -> str:
    return _PARSE_PROMPT + "\n\n## CURRENT STATE\n\n" + render_state_for_prompt(state)


# ─── Bot mention stripping ──────────────────────────────────────


def _strip_bot_mention(content: str, bot_id: int) -> str:
    for pattern in (f"<@{bot_id}>", f"<@!{bot_id}>"):
        content = content.replace(pattern, "")
    return content.strip()


# ─── Screenshot parsing ─────────────────────────────────────────


async def _parse_and_save_screenshot(
    attachment: "discord.Attachment",
    event: "Event",
    message: "discord.Message",
) -> dict:
    try:
        image_bytes = await attachment.read()
    except Exception as e:
        logger.exception("Failed to read attachment")
        return {"error": f"couldn't download the image ({e})"}

    media_type = attachment.content_type or "image/png"
    parsed = await parse_screenshot(image_bytes, media_type=media_type)
    if "error" in parsed:
        return parsed

    user_id = message.author.id
    user_name = getattr(message.author, "display_name", str(message.author))

    async with async_session() as session:
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

            # Check PlayerProfile for a known player ID
            from bot.llm.handlers_collecting import _maybe_prefill_player_id
            await session.flush()
            await _maybe_prefill_player_id(session, submission)

        submission.speedup_construction = parsed["speedup_construction"]
        submission.speedup_research = parsed["speedup_research"]
        submission.speedup_training = parsed["speedup_training"]
        submission.speedup_general = parsed["speedup_general"]
        submission.has_screenshot = True
        submission.discord_name = user_name
        submission.compute_priorities()
        await session.commit()

    return parsed


# ─── Render: state summary ──────────────────────────────────────


def _render_state_summary(state: dict, day1, include_assignments: bool = False) -> str:
    sub = state["submission"]
    lines = ["**Current submission:**", ""]

    # Player ID
    if sub["has_player_id"] and sub["player_ingame_id"]:
        lines.append(f"Player ID: {sub['player_ingame_id']}")
    else:
        lines.append("Player ID: not on file")

    lines.append("")

    # Availability
    lines.append("Availability:")
    if sub["availability_summary"]:
        for line in sub["availability_summary"].split("\n"):
            lines.append(f"  {line}")
    else:
        lines.append("  not on file")

    lines.append("")

    # Speedups
    if sub["speedups"]:
        r = sub["speedups"]
        lines.append(
            f"Speedups: construction {r['construction_days']:.2f}d, "
            f"research {r['research_days']:.2f}d, "
            f"troops {r['troops_days']:.2f}d, "
            f"general {r['general_days']:.2f}d"
        )
    else:
        lines.append("Speedups: not on file")

    # Premium resources (shown when any are set)
    if sub["resources"]:
        r = sub["resources"]
        lines.append(
            f"Resources: TTG {r['ttg']:.0f}, TG {r['tg']:.0f}, Dust {r['dust']:.0f}"
        )

    # Assignments (post-publish only)
    if include_assignments and state["phase"] == "published":
        assignments = state.get("assignments") or []
        lines.append("")
        if assignments:
            lines.append("Assignments:")
            for a in assignments:
                boundary = "  (boundary slot)" if a.get("is_boundary") else ""
                lines.append(
                    f"  Day {a['day']} ({a['track_label']}): "
                    f"{a['start_utc']}-{a['end_utc']} UTC on {a['date']}{boundary}"
                )
        else:
            lines.append("Assignments: waitlisted (no current slots)")

    return "\n".join(lines)


# ─── Render: per-action lines ───────────────────────────────────


def _render_move_slot(inp: dict, state: dict) -> str:
    day = inp.get("day")
    new_time = inp.get("new_start_utc", "?")
    current = next((a for a in state.get("assignments") or [] if a.get("day") == day), None)
    if current:
        return (
            f"Move request submitted: **Day {day}** "
            f"{current['start_utc']}-{current['end_utc']} UTC → **{new_time} UTC**. "
            f"Pending admin approval."
        )
    return f"Move request submitted: **Day {day} → {new_time} UTC**. Pending admin approval."


def _render_drop_slot(inp: dict, state: dict) -> str:
    day = inp.get("day")
    current = next((a for a in state.get("assignments") or [] if a.get("day") == day), None)
    if current:
        return (
            f"Drop request submitted: **Day {day}** "
            f"({current['start_utc']}-{current['end_utc']} UTC). Pending admin approval."
        )
    return f"Drop request submitted for **Day {day}**. Pending admin approval."


def _render_request_new_slot(inp: dict) -> str:
    day = inp.get("day")
    new_time = inp.get("new_start_utc", "?")
    track = inp.get("track")
    if day == 4 and track in ("NA", "CM"):
        track_label = "Noble Advisor" if track == "NA" else "Chief Minister"
        return (
            f"New-slot request submitted: **Day {day}** at **{new_time} UTC** "
            f"({track_label}). Pending admin approval."
        )
    return (
        f"New-slot request submitted: **Day {day}** at **{new_time} UTC**. "
        f"Pending admin approval."
    )


def _render_swap(inp: dict, state: dict) -> str:
    day = inp.get("day")
    other_id = inp.get("other_player_discord_id")
    other_name = "the other player"
    for p in state.get("valid_swap_partners") or []:
        if p.get("discord_id") == other_id:
            other_name = p.get("display_name", other_name)
            break
    return (
        f"Swap request submitted: **Day {day}** with **{other_name}**. "
        f"Waiting for their confirmation."
    )


def _render_set_resources(inp: dict) -> str:
    parts = []
    if "ttg" in inp:
        parts.append(f"TTG: {inp['ttg']:.0f}")
    if "tg" in inp:
        parts.append(f"TG: {inp['tg']:.0f}")
    if "dust" in inp:
        parts.append(f"Dust: {inp['dust']:.0f}")
    if parts:
        return f"Resources updated: {', '.join(parts)}."
    return "Resources updated."


def _render_set_player_id(inp: dict) -> str:
    pid = inp.get("player_id", "?")
    return f"Player ID recorded: **{pid}**."


def _render_out_of_scope(inp: dict) -> str:
    reason = (inp.get("reason") or "").strip()
    base = "I only handle availability and Speedups for **Day 1, Day 2, and Day 4**."
    if reason:
        return f"{base}\n_{reason}_"
    return base


def _render_clarify(inp: dict) -> str:
    ambig = (inp.get("ambiguity") or "").strip()
    if ambig:
        return f"I need clarification: {ambig}"
    return "I'm not sure what you meant — could you rephrase?"


def _render_greet(state: dict) -> str:
    sub = state["submission"]
    avail_tag = "  (already on file)" if sub["has_availability"] else ""
    screen_tag = "  (already on file)" if sub["has_screenshot"] else ""
    id_tag = "  (already on file)" if sub["has_player_id"] else ""
    lines = [
        "**Hi!** I'm the scheduling bot. I track 30-minute time slots on **Day 1, Day 2, and Day 4**.",
        "",
        "**To be added to the schedule, I need:**",
        f"1. **Availability** — at least one time window on Day 1, 2, or 4{avail_tag}",
        f"2. **Speedups screenshot** — your in-game Speedups page{screen_tag}",
        f"3. **Player ID** — your in-game numeric player ID{id_tag}",
        "",
        "**Optionally, to improve your priority:**",
        "  Tell me your **TTG, TG, and Dust** counts (e.g. 'I have 3 TTG, 50 TG, 200 dust')",
        "",
        "**Examples:**",
        "  \"Day 1 from 2pm to 6pm EST\"",
        "  \"Day 2 anytime, Day 4 after 8pm UTC\"",
        "  \"My player ID is 12345678\"",
        "  \"I have 5 TTG and 100 TG\"",
        "  \"What are my times?\"",
    ]
    return "\n".join(lines)


# ─── Completeness (pre-lock only) ──────────────────────────────


async def _completeness_status(
    event: "Event",
    user_id: int,
    was_complete_before: bool,
    screenshot_attempted: bool = False,
) -> str | None:
    if event.phase != EventPhase.COLLECTING:
        return None

    async with async_session() as session:
        result = await session.execute(
            select(Submission).where(
                Submission.event_id == event.event_id,
                Submission.discord_id == user_id,
            )
        )
        sub = result.scalar_one_or_none()

    if sub is None:
        missing = []
        if not screenshot_attempted:
            missing.append("a screenshot of your **Speedups** page")
        missing.append("your availability for Day 1, Day 2, or Day 4")
        missing.append("your **in-game player ID**")
        return "🚨 You still need to send: " + "; ".join(missing) + " before I can add you to the schedule."

    is_complete = sub.has_screenshot and sub.has_availability and sub.has_player_id
    if is_complete:
        if not was_complete_before:
            return (
                "✅ Your submission is now complete — you'll be considered when the "
                "schedule is generated."
            )
        return None

    missing = []
    if not sub.has_screenshot:
        if not screenshot_attempted:
            missing.append("a screenshot of your **Speedups** page")
    if not sub.has_availability:
        missing.append("your availability for Day 1, Day 2, or Day 4")
    if not sub.has_player_id:
        missing.append("your **in-game player ID**")

    if not missing:
        return None

    return "🚨 You still need: " + "; ".join(missing) + "."


# ─── LLM parse call ─────────────────────────────────────────────


async def _parse_intent(
    text: str,
    state: dict,
    event: "Event",
) -> list[tuple[str, dict]] | None:
    tools = tools_for_phase(event.phase.value)
    if not tools:
        return []

    try:
        response = await _client.messages.create(
            model=ANTHROPIC_MODEL,
            max_tokens=1024,
            system=_build_parse_prompt(state),
            tools=tools,
            tool_choice={"type": "any"},
            messages=[{"role": "user", "content": text}],
        )
    except Exception:
        logger.exception("Anthropic API call failed")
        return None

    actions: list[tuple[str, dict]] = []
    for block in response.content:
        if getattr(block, "type", None) == "tool_use":
            actions.append((block.name, block.input or {}))

    logger.info(f"Parsed actions: {[a[0] for a in actions]} from text={text!r}")
    return actions


# ─── Public entry point ─────────────────────────────────────────


_SUBMISSION_TOUCHING = {"set_availability", "set_player_id", "set_resources", "widen_availability", "query"}


async def process_user_message(
    message: "discord.Message",
    event: "Event",
    bot: "commands.Bot",
) -> str:
    if bot.user is None:
        return "(bot not ready)"

    text = _strip_bot_mention(message.content, bot.user.id)
    image_attachments = [
        a for a in message.attachments
        if (a.content_type or "").startswith("image/")
        or a.filename.lower().endswith((".png", ".jpg", ".jpeg", ".webp", ".gif"))
    ]

    # Build pre-handler state
    async with async_session() as session:
        pre_state = await build_state_envelope(session, event, message)
    was_complete_before = (
        pre_state["submission"]["has_screenshot"]
        and pre_state["submission"]["has_availability"]
        and pre_state["submission"]["has_player_id"]
    )

    # Parse + save screenshots silently
    screenshot_errors: list[str] = []
    screenshot_succeeded = False
    for att in image_attachments:
        result = await _parse_and_save_screenshot(att, event, message)
        if "error" in result:
            screenshot_errors.append(result["error"])
        else:
            screenshot_succeeded = True

    sections: list[str] = []

    for err in screenshot_errors:
        sections.append(f"🚨 Couldn't read the screenshot: {err}")

    if not text and not image_attachments:
        sections.append(_render_greet(pre_state))

    show_state_summary = screenshot_succeeded

    if text:
        # During LOCKED, use the minimal review-phase handler set
        if event.phase == EventPhase.LOCKED:
            handlers = LOCKED_REVIEW_HANDLERS
        elif event.phase == EventPhase.COLLECTING:
            handlers = COLLECTING_HANDLERS
        else:
            handlers = PUBLISHED_HANDLERS

        actions = await _parse_intent(text, pre_state, event)
        if actions is None:
            sections.append("🚨 Sorry, I couldn't reach my language model. Please try again.")
        else:
            action_lines: list[str] = []
            for action_name, action_input in actions:
                handler = handlers.get(action_name)
                if handler is None:
                    action_lines.append(f"🚨 Unknown action: {action_name}")
                    continue
                try:
                    err = await handler(action_input, pre_state, message, bot)
                except Exception:
                    logger.exception(f"Handler {action_name} crashed")
                    action_lines.append(f"🚨 {action_name}: internal error")
                    continue
                if err is not None:
                    action_lines.append(f"🚨 {err}")
                    continue

                if action_name in _SUBMISSION_TOUCHING:
                    show_state_summary = True
                elif action_name == "set_player_id":
                    action_lines.append(_render_set_player_id(action_input))
                    show_state_summary = True
                elif action_name == "set_resources":
                    action_lines.append(_render_set_resources(action_input))
                    show_state_summary = True
                elif action_name == "move_slot":
                    action_lines.append(_render_move_slot(action_input, pre_state))
                elif action_name == "drop_slot":
                    action_lines.append(_render_drop_slot(action_input, pre_state))
                elif action_name == "request_new_slot":
                    action_lines.append(_render_request_new_slot(action_input))
                elif action_name == "swap":
                    action_lines.append(_render_swap(action_input, pre_state))
                elif action_name in ("greet", "out_of_scope", "clarify"):
                    if not image_attachments:
                        if action_name == "greet":
                            action_lines.append(_render_greet(pre_state))
                        elif action_name == "out_of_scope":
                            action_lines.append(_render_out_of_scope(action_input))
                        else:
                            action_lines.append(_render_clarify(action_input))

            if action_lines:
                sections.append("\n\n".join(action_lines))

    if show_state_summary:
        async with async_session() as session:
            post_state = await build_state_envelope(session, event, message)
        include_assn = event.phase == EventPhase.PUBLISHED
        sections.append(_render_state_summary(post_state, event.day1_date, include_assn))

    screenshot_attempted = bool(image_attachments)
    status = await _completeness_status(
        event, message.author.id, was_complete_before, screenshot_attempted
    )
    if status:
        sections.append(status)

    return "\n\n".join(s for s in sections if s).strip() or "I processed your message but had nothing to say."
