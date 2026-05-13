"""
Agent: single entry point for handling an @mention to the bot.

Architecture (parser + render):

  1. PARSE — one LLM call with tool_choice="any" forces the model to emit
     tool_use blocks. The model's text output is not used.
  2. DISPATCH — each tool_use goes to a handler in handlers_collecting or
     handlers_locked; handlers return None on success, an error string on
     failure.
  3. RENDER — code templates the user-facing reply from the action list,
     handler results, screenshot results, and a completeness check. The
     reply is built deterministically; no LLM is in the response path.

The reply is currently English-only. Multilingual support previously came
from letting the LLM author the reply in the user's language; this rewrite
trades that for predictability. To restore multilingual support, add a
post-render translation pass keyed off detected language.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import anthropic
from sqlalchemy import select

from bot.config import ANTHROPIC_MODEL, GENERIC_SPLIT
from bot.database import async_session
from bot.llm.handlers_collecting import COLLECTING_HANDLERS
from bot.llm.handlers_locked import LOCKED_HANDLERS
from bot.llm.screenshot import parse_screenshot
from bot.llm.state import build_state_envelope, render_state_for_prompt
from bot.llm.tools import tools_for_phase
from bot.models import EventPhase, Submission

if TYPE_CHECKING:
    import discord
    from discord.ext import commands

    from bot.models import Event

logger = logging.getLogger("scheduler.llm.agent")

_client = anthropic.AsyncAnthropic()


# ─── System prompt (parsing only) ────────────────────────────────


_PARSE_PROMPT = """You are an intent parser for a scheduling bot. Read the \
user's message and emit one or more tool_use blocks representing what they want. \
DO NOT include any text in your response — only tool_use blocks. The text part \
of your response is ignored; only the structured tool_use blocks are used.

The bot manages 30-minute slots on Day 1, Day 2, and Day 4 of a 28-day cycle. \
Day 3 and Day 5 are not tracked.

## TOOL SELECTION

- `set_availability` (pre-lock): user is telling you their available times for a specific day. \
Empty `windows` means "not available that day".
- `widen_availability` (post-lock): user wants to ADD more available times on top of what they already have.
- `move_slot` (post-lock): user wants to change the start time of an existing assignment. Compute \
the absolute UTC time yourself for relative requests like "3 hours earlier".
- `drop_slot` (post-lock): user wants to give up an assignment.
- `swap` (post-lock): user wants to trade slots with another player. Only call this if the user \
@mentioned a player who appears in VALID SWAP PARTNERS in the state. If not, call `clarify` instead.
- `query`: user is asking about their current state (their times, resources, status, deadlines).
- `greet`: user said hi/hello/test, asked for help, or sent an opener without a specific action.
- `out_of_scope`: user is asking about Day 3 or Day 5, other players' data, trying to set \
resources via text (resources MUST come from screenshots), or sending unrelated chatter.
- `clarify`: user's request is ambiguous and you cannot reliably act on it.

## MULTI-DAY MESSAGES

Emit one tool_use per day. Example: "Day 1 anytime, Day 2 after 8pm" → \
set_availability(day=1, windows=[full day]) + set_availability(day=2, windows=[20:00-23:59]).

Do NOT emit set_availability for days the user did NOT mention — partial updates preserve other days.

## TIME RULES

- All times in tool_use inputs are UTC, HH:MM 24-hour format.
- Convert local timezones the user mentions to UTC before emitting.
- A window may cross midnight — emit end_utc earlier than start_utc, the backend handles it.
- "Any day" / "anytime" / "all day" for Day X → windows=[{start_utc:"00:00", end_utc:"23:59"}].
- "Not available Day X" / "skip Day X" → windows=[] (empty list).

## RESET SEMANTICS (strict)

- "close to reset" / "near reset" / "around reset (before)" / "late" → 21:15-00:15 UTC
- "after reset" / "just after reset" / "around reset (after)" / "early" → 23:45 (previous day) - 02:45 UTC
- "reset" alone → 00:00 UTC exactly

## DAY MAPPING

Players often refer to days by resource word instead of number:
- construction / building = Day 1
- research = Day 2
- troops / training / soldiers = Day 4

"build any day" means "Day 1 with full availability". NOT "construction on every day". \
Each resource word maps to exactly one day number.

## SCREENSHOTS

Screenshots are parsed separately by code; you do not need to acknowledge them or \
emit a tool for them. Just process the text portion of the message.

## OUTPUT

Emit only tool_use blocks. Do not write any text response.
"""


def _build_parse_prompt(state: dict) -> str:
    return _PARSE_PROMPT + "\n\n## CURRENT STATE\n\n" + render_state_for_prompt(state)


# ─── Bot mention stripping ──────────────────────────────────────


def _strip_bot_mention(content: str, bot_id: int) -> str:
    for pattern in (f"<@{bot_id}>", f"<@!{bot_id}>"):
        content = content.replace(pattern, "")
    return content.strip()


# ─── Screenshot parsing (saves to DB) ───────────────────────────


async def _parse_and_save_screenshot(
    attachment: "discord.Attachment",
    event: "Event",
    message: "discord.Message",
) -> dict:
    """Parse one screenshot and save resource values. Returns the parsed dict
    on success, or {"error": "..."} on failure.
    """
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

        submission.resource_x = parsed["resource_x"]
        submission.resource_y = parsed["resource_y"]
        submission.resource_z = parsed["resource_z"]
        submission.resource_generic = parsed["resource_generic"]
        submission.has_screenshot = True
        submission.discord_name = user_name
        submission.compute_priorities()
        await session.commit()

    return parsed


# ─── Render templates (code-generated responses) ────────────────


def _render_screenshot(parsed: dict) -> str:
    if "error" in parsed:
        return f"🚨 Couldn't read the screenshot: {parsed['error']}"
    return (
        "📸 **Resources updated:**\n"
        f"  • Construction (Day 1): {parsed['resource_x']:.2f} days\n"
        f"  • Research (Day 2):     {parsed['resource_y']:.2f} days\n"
        f"  • Troops (Day 4):       {parsed['resource_z']:.2f} days\n"
        f"  • General (split into {GENERIC_SPLIT}): {parsed['resource_generic']:.2f} days"
    )


def _format_windows(windows: list[dict]) -> str:
    return ", ".join(
        f"{w.get('start_utc', '?')}-{w.get('end_utc', '?')} UTC"
        for w in windows
    )


def _render_set_availability(inp: dict) -> list[str]:
    day = inp.get("day")
    windows = inp.get("windows") or []
    if not windows:
        return [f"📅 Marked Day {day} as **not available**."]
    return [f"📅 Recorded availability for **Day {day}**: {_format_windows(windows)}."]


def _render_widen_availability(inp: dict) -> list[str]:
    day = inp.get("day")
    windows = inp.get("windows") or []
    if not windows:
        return [f"📅 No additional windows to add for Day {day}."]
    return [f"📅 Added availability for **Day {day}**: {_format_windows(windows)}."]


def _render_move_slot(inp: dict, state: dict) -> list[str]:
    day = inp.get("day")
    new_time = inp.get("new_start_utc", "?")
    current = next((a for a in state.get("assignments") or [] if a.get("day") == day), None)
    if current:
        return [
            f"📋 Move request submitted: **Day {day}** "
            f"{current['start_utc']}-{current['end_utc']} → **{new_time} UTC**. "
            f"Pending admin approval."
        ]
    return [f"📋 Move request submitted: **Day {day} → {new_time} UTC**. Pending admin approval."]


def _render_drop_slot(inp: dict, state: dict) -> list[str]:
    day = inp.get("day")
    current = next((a for a in state.get("assignments") or [] if a.get("day") == day), None)
    if current:
        return [
            f"📋 Drop request submitted: **Day {day}** "
            f"({current['start_utc']}-{current['end_utc']} UTC). Pending admin approval."
        ]
    return [f"📋 Drop request submitted for **Day {day}**. Pending admin approval."]


def _render_swap(inp: dict, state: dict) -> list[str]:
    day = inp.get("day")
    other_id = inp.get("other_player_discord_id")
    other_name = "the other player"
    for p in state.get("valid_swap_partners") or []:
        if p.get("discord_id") == other_id:
            other_name = p.get("display_name", other_name)
            break
    return [
        f"🔁 Swap request submitted: **Day {day}** with **{other_name}**. "
        f"Waiting for their confirmation."
    ]


def _render_query(state: dict) -> list[str]:
    lines = ["📋 **Your status:**"]

    sub = state["submission"]
    if sub["resources"]:
        r = sub["resources"]
        lines.append(
            f"  Resources: construction {r['construction_days']:.2f}d, "
            f"research {r['research_days']:.2f}d, "
            f"troops {r['troops_days']:.2f}d, "
            f"general {r['general_days']:.2f}d (split into {r['generic_split']})"
        )
    else:
        lines.append("  Resources: *not on file*")

    if sub["availability_summary"]:
        lines.append("  Availability:")
        for line in sub["availability_summary"].split("\n"):
            lines.append(f"    {line}")
    else:
        lines.append("  Availability: *not on file*")

    if state["phase"] == "locked":
        assignments = state.get("assignments") or []
        if assignments:
            lines.append("  Assignments:")
            for a in assignments:
                boundary = "  *(boundary slot)*" if a.get("is_boundary") else ""
                lines.append(
                    f"    Day {a['day']} ({a['track_label']}): "
                    f"{a['start_utc']}-{a['end_utc']} UTC{boundary}"
                )
        else:
            lines.append("  Assignments: *waitlisted (no current slots)*")

    return lines


def _render_out_of_scope(inp: dict) -> list[str]:
    return [
        "❓ I can only help with scheduling for **Day 1**, **Day 2**, and **Day 4**. "
        "Resources have to come from a screenshot of your in-game Resources & Speedups page, "
        "not text."
    ]


def _render_clarify(inp: dict) -> list[str]:
    ambig = (inp.get("ambiguity") or "").strip()
    if ambig:
        return [f"❓ I need clarification: {ambig}"]
    return ["❓ I'm not sure what you meant — could you rephrase?"]


def _render_greet(state: dict) -> list[str]:
    sub = state["submission"]
    avail_tag = " *(on file)*" if sub["has_availability"] else ""
    screen_tag = " *(on file)*" if sub["has_screenshot"] else ""
    return [
        "👋 **Hi!** I'm the scheduling bot. I track 30-minute time slots on **Day 1, Day 2, and Day 4**.",
        "",
        "**To be added to the schedule, I need both:**",
        f"1. 📅 **Availability** — at least one time window on Day 1, 2, or 4{avail_tag}",
        f"2. 📸 **Resources screenshot** — your in-game Resources & Speedups page{screen_tag}",
        "",
        "**Examples:**",
        "• \"Day 1 from 2pm to 6pm EST\"",
        "• \"Day 2 anytime, Day 4 after 8pm UTC\"",
        "• \"Not available Day 1\"",
        "• \"What are my times?\"",
    ]


def _render_action(name: str, inp: dict, state: dict) -> list[str]:
    if name == "set_availability":
        return _render_set_availability(inp)
    if name == "widen_availability":
        return _render_widen_availability(inp)
    if name == "move_slot":
        return _render_move_slot(inp, state)
    if name == "drop_slot":
        return _render_drop_slot(inp, state)
    if name == "swap":
        return _render_swap(inp, state)
    if name == "query":
        return _render_query(state)
    if name == "greet":
        return _render_greet(state)
    if name == "out_of_scope":
        return _render_out_of_scope(inp)
    if name == "clarify":
        return _render_clarify(inp)
    return [f"🚨 Unknown action: {name}"]


# ─── Completeness check ─────────────────────────────────────────


async def _completeness_warning(event: "Event", user_id: int) -> str | None:
    """Standardized 🚨 warning if the user's submission is missing a piece.
    Pre-lock only. Returns None when complete or not applicable.
    """
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
        return None
    if sub.has_screenshot and sub.has_availability:
        return None
    if sub.has_availability and not sub.has_screenshot:
        return (
            "🚨 You still need to send a screenshot of your in-game "
            "**Resources & Speedups** page — without it, I can't add you to the schedule."
        )
    if sub.has_screenshot and not sub.has_availability:
        return (
            "🚨 You still need to tell me your availability for Day 1, Day 2, or Day 4 — "
            "without it, I can't add you to the schedule."
        )
    return (
        "🚨 You still need to send a screenshot of your **Resources & Speedups** page AND "
        "tell me your availability for Day 1, Day 2, or Day 4 before I can add you to the schedule."
    )


# ─── LLM parse call ─────────────────────────────────────────────


async def _parse_intent(
    text: str,
    state: dict,
    event: "Event",
) -> list[tuple[str, dict]] | None:
    """Run the text through the LLM and return the list of (action_name, input)
    tuples. Returns None on API failure.
    """
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


async def process_user_message(
    message: "discord.Message",
    event: "Event",
    bot: "commands.Bot",
) -> str:
    """Handle one @mention to the bot. Returns the reply text to send."""
    if bot.user is None:
        return "(bot not ready)"

    text = _strip_bot_mention(message.content, bot.user.id)
    image_attachments = [
        a for a in message.attachments
        if (a.content_type or "").startswith("image/")
        or a.filename.lower().endswith((".png", ".jpg", ".jpeg", ".webp", ".gif"))
    ]

    # 1. Parse + save screenshots silently
    screenshot_results: list[dict] = []
    for att in image_attachments:
        screenshot_results.append(await _parse_and_save_screenshot(att, event, message))

    # 2. Build state envelope (reflects updated resources)
    async with async_session() as session:
        state = await build_state_envelope(session, event, message)

    sections: list[str] = []

    # 3. Screenshot section (one per attachment)
    for r in screenshot_results:
        sections.append(_render_screenshot(r))

    # 4. Decide what else to do
    if not text and not image_attachments:
        # No text, no image — treat as a greeting
        sections.append("\n".join(_render_greet(state)))
    elif text:
        # 5. Parse intent and dispatch
        actions = await _parse_intent(text, state, event)
        if actions is None:
            sections.append("🚨 Sorry, I couldn't reach my language model. Please try again.")
        else:
            handlers = (
                COLLECTING_HANDLERS if event.phase == EventPhase.COLLECTING else LOCKED_HANDLERS
            )
            action_lines: list[str] = []
            for action_name, action_input in actions:
                handler = handlers.get(action_name)
                if handler is None:
                    action_lines.append(f"🚨 Unknown action: {action_name}")
                    continue
                try:
                    err = await handler(action_input, state, message, bot)
                except Exception:
                    logger.exception(f"Handler {action_name} crashed")
                    action_lines.append(f"🚨 {action_name}: internal error")
                    continue
                if err is not None:
                    action_lines.append(f"🚨 {err}")
                    continue
                # Success — render the templated line(s) for this action
                action_lines.extend(_render_action(action_name, action_input, state))

            if action_lines:
                sections.append("\n".join(action_lines))

    # 6. Completeness backstop (pre-lock, single warning)
    completeness = await _completeness_warning(event, message.author.id)
    if completeness:
        sections.append(completeness)

    return "\n\n".join(s for s in sections if s).strip() or "I processed your message but had nothing to say."
