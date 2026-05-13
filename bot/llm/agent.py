"""
Agent: single entry point for handling an @mention to the bot.

Architecture:
  1. PARSE — one LLM call with tool_choice="any" forces tool_use blocks.
     The model's text output is ignored.
  2. DISPATCH — each tool_use runs through a handler in handlers_*.
  3. RENDER — code templates the user-facing reply.

Response shape:
  - For state-changing actions in COLLECTING (set_availability, screenshot
    parsed) and for explicit `query`, a SINGLE merged state summary is
    shown — availability across all three days + Speedups. The per-action
    lines from earlier versions are dropped in favor of this summary so
    multi-day messages produce one coherent confirmation, not a list.
  - Post-lock change requests (move/drop/swap) and `widen_availability`
    each produce a short per-action confirmation line.
  - `greet`/`out_of_scope`/`clarify` produce short stand-alone replies.

Allowed emoji set: ✅ ❌ 🚨 only. No other emoji in templates.

The state envelope is built BEFORE handlers run. To reflect post-handler
state in the summary and completeness check, we re-fetch from the DB.
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


# ─── Parsing prompt (LLM is a parser only) ──────────────────────


_PARSE_PROMPT = """You are an intent parser for a scheduling bot. Read the \
user's message and emit one or more tool_use blocks representing their intent. \
Do NOT write any text response — only emit tool_use blocks. The text portion of \
your output is ignored.

The bot manages 30-minute slots on Day 1, Day 2, and Day 4 of a 28-day cycle. \
Day 3 and Day 5 are not tracked.

## TOOL SELECTION

- `set_availability` (pre-lock): user is telling you their available times for a specific day. \
Empty `windows` means "not available that day".
- `widen_availability` (post-lock): user wants to ADD more available times on top of what they already have.
- `move_slot` (post-lock): user wants to change the start time of an existing assignment. Compute \
the absolute UTC time yourself for relative requests like "3 hours earlier".
- `drop_slot` (post-lock): user wants to give up an assignment.
- `request_new_slot` (post-lock): user does NOT have an assignment on the day they're asking about \
and wants to be added to one (e.g. "can I get a Day 1 spot near reset", "sign me up for a Day 2 \
slot at 7pm UTC", "add me to Day 4 Noble Advisor"). Check CURRENT STATE — only call this if the \
user has no assignment for that day (or for Day 4, no assignment in the requested track). If \
they already have an assignment on that day and want a different time, use `move_slot` instead.
- `swap` (post-lock): user wants to trade slots with another player. Only call this if the user \
@mentioned a player who appears in VALID SWAP PARTNERS in the state.
- `query`: user is asking about their current state (their times, Speedups, status, deadlines).
- `greet`: user said hi/hello/test, asked for help, or sent an opener without a specific action.
- `out_of_scope`: user is asking about Day 3 or Day 5, other players' data, trying to set \
Speedups via TEXT (e.g., "I have 5 days of construction speedups"), or sending truly unrelated \
chatter. Do NOT use this as a fallback for scheduling intents you don't have an exact tool for — \
those have parses (see below).
- `clarify`: user's request is ambiguous and you cannot reliably act on it.

## MULTI-DAY MESSAGES

Emit one tool_use per day. Example: "Day 1 anytime, Day 2 after 8pm UTC" → \
set_availability(day=1, windows=[full day]) + set_availability(day=2, windows=[20:00-23:59]).

Do NOT emit set_availability for days the user did NOT mention — partial updates preserve other days.

## UPDATING A DAY ALREADY ON FILE

set_availability REPLACES the windows for the day specified. Look at CURRENT \
STATE's availability summary to decide how to compose `windows`:

- ADDITIVE language ("also Day 1 evenings", "Day 1 afternoons too", "add Day 1 \
  mornings") → include BOTH the existing windows for that day AND the new ones \
  in your windows list. The handler will replace the day with what you emit, so \
  you must repeat the existing windows to preserve them.
- REPLACEMENT language ("Day 1 only mornings", "change Day 1 to evenings", \
  "actually Day 1 evenings instead", "scratch that, Day 1 mornings") → emit ONLY \
  the new windows.
- AMBIGUOUS ("Day 1 evenings" with prior Day 1 mornings on file) → treat as \
  REPLACEMENT. Latest message wins.

## CLEARING A DAY

These verbs ALWAYS mean "make me unavailable that day" pre-lock, and parse to \
set_availability(day=X, windows=[]):
  drop / remove / cancel / clear / scratch / "take me off" / "I can't make" / \
  "not available" / "skip"

Pre-lock the word "drop" NEVER refers to the post-lock `drop_slot` tool — that \
tool does not exist pre-lock. "Drop my Day 1 time" = set_availability(day=1, windows=[]).

## SIGNING UP WITHOUT A SPECIFIC TIME

"Sign me up Day X" / "add me Day X" / "include me Day X" / "I'm in for Day X" / \
"yes for Day X" with NO time mentioned → windows=[{start_utc:"00:00", end_utc:"23:59"}] \
(treat as full day — it's a friendly default).

## TIME RULES

- All times in tool_use inputs are UTC, HH:MM 24-hour format.
- Convert local timezones the user mentions to UTC.
- A window may cross midnight — emit end_utc earlier than start_utc, the backend handles it.
- "Any day" / "anytime" / "all day" for Day X → windows=[{start_utc:"00:00", end_utc:"23:59"}].

## RESET SEMANTICS (strict)

- "close to reset" / "near reset" / "late" → 21:15-00:15 UTC
- "after reset" / "just after reset" / "early" → 23:45 (previous day) - 02:45 UTC
- "reset" alone → 00:00 UTC exactly

## DAY MAPPING

Players often refer to days by resource word:
- construction / building = Day 1
- research = Day 2
- troops / training / soldiers = Day 4

"build any day" means "Day 1 with full availability" (NOT "construction on every day"). \
Each resource word maps to exactly one day number.

## SCREENSHOTS

Screenshots are parsed separately by code; do not emit a tool for them. Just process the text.

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


# ─── Screenshot parsing (saves to DB) ───────────────────────────


async def _parse_and_save_screenshot(
    attachment: "discord.Attachment",
    event: "Event",
    message: "discord.Message",
) -> dict:
    """Parse one screenshot and save resource values. Returns the parsed dict
    on success or {"error": "..."} on failure.
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
    """Multi-line block showing the user's current availability + Speedups.

    Used both as the response to `query` and as the auto-summary appended
    after any submission-touching action (set_availability, screenshot, or
    widen_availability post-lock).
    """
    sub = state["submission"]
    lines = ["**Current submission:**", ""]

    # Availability section
    lines.append("Availability:")
    if sub["availability_summary"]:
        for line in sub["availability_summary"].split("\n"):
            lines.append(f"  {line}")
    else:
        lines.append("  not on file")

    lines.append("")

    # Speedups section
    if sub["speedups"]:
        r = sub["speedups"]
        lines.append(
            f"Speedups: construction {r['construction_days']:.2f}d, "
            f"research {r['research_days']:.2f}d, "
            f"troops {r['troops_days']:.2f}d, "
            f"general {r['general_days']:.2f}d (split into {r['generic_split']})"
        )
    else:
        lines.append("Speedups: not on file")

    # Assignments (post-lock only)
    if include_assignments and state["phase"] == "locked":
        assignments = state.get("assignments") or []
        lines.append("")
        if assignments:
            lines.append("Assignments:")
            for a in assignments:
                boundary = "  (boundary slot)" if a.get("is_boundary") else ""
                lines.append(
                    f"  Day {a['day']} ({a['track_label']}): "
                    f"{a['start_utc']}-{a['end_utc']} UTC{boundary}"
                )
        else:
            lines.append("Assignments: waitlisted (no current slots)")

    return "\n".join(lines)


# ─── Render: short per-action lines ─────────────────────────────


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


def _render_out_of_scope(inp: dict) -> str:
    """Generic out-of-scope reply. Optionally appends the LLM's reason in italics.

    The previous template hardcoded "Speedups have to come from a screenshot,
    not text", which read as a non-sequitur whenever the LLM reached for
    out_of_scope for any other reason (Day 3 chatter, an unrecognized verb,
    etc.). This version stays neutral and lets the LLM's `reason` field carry
    context if it provided one.
    """
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
    lines = [
        "**Hi!** I'm the scheduling bot. I track 30-minute time slots on **Day 1, Day 2, and Day 4**.",
        "",
        "**To be added to the schedule, I need both:**",
        f"1. **Availability** — at least one time window on Day 1, 2, or 4{avail_tag}",
        f"2. **Speedups screenshot** — your in-game Speedups page{screen_tag}",
        "",
        "**Examples:**",
        "  \"Day 1 from 2pm to 6pm EST\"",
        "  \"Day 2 anytime, Day 4 after 8pm UTC\"",
        "  \"Not available Day 1\"",
        "  \"What are my times?\"",
    ]
    return "\n".join(lines)


# ─── Completeness status (pre-lock only) ────────────────────────


async def _completeness_status(
    event: "Event",
    user_id: int,
    was_complete_before: bool,
    screenshot_attempted: bool = False,
) -> str | None:
    """Return a single status line:
      - 🚨 reminder of what's missing, OR
      - ✅ "just became complete" affirmation (only on transition), OR
      - None when already complete / not applicable.

    When `screenshot_attempted` is True, the user just tried to upload a
    screenshot in this same message (which may have failed). In that case
    we don't repeat "you still need a screenshot" — the parse-error line
    already conveys that. We still warn about missing availability if it's
    the only remaining gap.
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
        if screenshot_attempted:
            return (
                "🚨 You also need to tell me your availability for Day 1, Day 2, "
                "or Day 4 before I can add you to the schedule."
            )
        return (
            "🚨 You still need to send a screenshot of your **Speedups** page AND "
            "tell me your availability for Day 1, Day 2, or Day 4 before I can add you to the schedule."
        )

    is_complete = sub.has_screenshot and sub.has_availability
    if is_complete:
        if not was_complete_before:
            return (
                "✅ Your submission is now complete — you'll be considered when the "
                "schedule is generated."
            )
        return None

    if sub.has_availability and not sub.has_screenshot:
        if screenshot_attempted:
            return None  # error line already covers this
        return (
            "🚨 You still need to send a screenshot of your in-game "
            "**Speedups** page — without it, I can't add you to the schedule."
        )
    if sub.has_screenshot and not sub.has_availability:
        return (
            "🚨 You still need to tell me your availability for Day 1, Day 2, or Day 4 — "
            "without it, I can't add you to the schedule."
        )
    # Neither piece on file
    if screenshot_attempted:
        return (
            "🚨 You also need to tell me your availability for Day 1, Day 2, "
            "or Day 4 before I can add you to the schedule."
        )
    return (
        "🚨 You still need to send a screenshot of your **Speedups** page AND "
        "tell me your availability for Day 1, Day 2, or Day 4 before I can add you to the schedule."
    )


# ─── LLM parse call ─────────────────────────────────────────────


async def _parse_intent(
    text: str,
    state: dict,
    event: "Event",
) -> list[tuple[str, dict]] | None:
    """Single LLM call. Returns [(action_name, action_input), ...] or None on failure."""
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


# Action names that touch the submission (warrant a merged state summary).
_SUBMISSION_TOUCHING = {"set_availability", "widen_availability", "query"}


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

    # 1. Build pre-handler state envelope so we know what was complete BEFORE
    #    this message ran.
    async with async_session() as session:
        pre_state = await build_state_envelope(session, event, message)
    was_complete_before = (
        pre_state["submission"]["has_screenshot"]
        and pre_state["submission"]["has_availability"]
    )

    # 2. Parse + save screenshots silently. Track parse errors for per-attachment reporting.
    screenshot_errors: list[str] = []
    screenshot_succeeded = False
    for att in image_attachments:
        result = await _parse_and_save_screenshot(att, event, message)
        if "error" in result:
            screenshot_errors.append(result["error"])
        else:
            screenshot_succeeded = True

    sections: list[str] = []

    # 3. Surface any screenshot parse errors
    for err in screenshot_errors:
        sections.append(f"🚨 Couldn't read the screenshot: {err}")

    # 4. Idle path — no text, no image: behave like a greeting
    if not text and not image_attachments:
        sections.append(_render_greet(pre_state))

    # 5. Text path — parse and dispatch
    show_state_summary = screenshot_succeeded
    if text:
        actions = await _parse_intent(text, pre_state, event)
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
                    err = await handler(action_input, pre_state, message, bot)
                except Exception:
                    logger.exception(f"Handler {action_name} crashed")
                    action_lines.append(f"🚨 {action_name}: internal error")
                    continue
                if err is not None:
                    action_lines.append(f"🚨 {err}")
                    continue

                # Success — decide what to render for this action
                if action_name in _SUBMISSION_TOUCHING:
                    show_state_summary = True
                    # No per-action line; the merged summary covers it.
                elif action_name == "move_slot":
                    action_lines.append(_render_move_slot(action_input, pre_state))
                elif action_name == "drop_slot":
                    action_lines.append(_render_drop_slot(action_input, pre_state))
                elif action_name == "request_new_slot":
                    action_lines.append(_render_request_new_slot(action_input))
                elif action_name == "swap":
                    action_lines.append(_render_swap(action_input, pre_state))
                elif action_name in ("greet", "out_of_scope", "clarify"):
                    # These three are "non-action" actions. If the user attached
                    # an image, their text is almost certainly commentary on
                    # the screenshot ("here are my speedups", "see attached")
                    # and a help/refusal/clarification reply would be noise.
                    # Suppress in that case; the screenshot result and state
                    # summary already speak for themselves.
                    if not image_attachments:
                        if action_name == "greet":
                            action_lines.append(_render_greet(pre_state))
                        elif action_name == "out_of_scope":
                            action_lines.append(_render_out_of_scope(action_input))
                        else:  # clarify
                            action_lines.append(_render_clarify(action_input))
                # other no-op actions: nothing to render

            if action_lines:
                sections.append("\n\n".join(action_lines))

    # 6. Merged state summary (single block, post-handler)
    if show_state_summary:
        async with async_session() as session:
            post_state = await build_state_envelope(session, event, message)
        include_assn = event.phase == EventPhase.LOCKED
        sections.append(_render_state_summary(post_state, event.day1_date, include_assn))

    # 7. Completeness / just-became-complete (pre-lock only, single line).
    #    `screenshot_attempted` suppresses the "send a screenshot" half of the
    #    warning when one was just uploaded — the parse-error line covers it.
    screenshot_attempted = bool(image_attachments)
    status = await _completeness_status(
        event, message.author.id, was_complete_before, screenshot_attempted
    )
    if status:
        sections.append(status)

    return "\n\n".join(s for s in sections if s).strip() or "I processed your message but had nothing to say."
