"""
Agent: single entry point for handling an @mention to the bot.

Phase 3 changes:
  - Screenshot status is no longer a hardcoded English block when there is
    accompanying text. The parser still runs first (and saves to the DB),
    but the values get passed to the main LLM call as a system note so the
    LLM acknowledges them in the user's language.
  - Handler errors are translated to the same language as the LLM's main
    response via a second LLM call — but only when errors actually occur,
    so the success path remains a single API call.

Text-less screenshot uploads still use a hardcoded English status — with no
text there is no language signal to detect, and saving a second LLM call in
that path is worthwhile.
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


# ─── System prompt ───────────────────────────────────────────────


_BASE_SYSTEM_PROMPT = """You are a scheduling bot for a 28-day game cycle. \
You manage 30-minute time slots on Day 1, Day 2, and Day 4 only. \
Day 3 and Day 5 are NOT tracked.

## RESPONSE FORMAT — MANDATORY

EVERY reply you produce MUST include a text block. Without a text block the user receives nothing.

Structure your reply as:
  (1) A short text block in the user's language — what you're doing or asking. ALWAYS present.
  (2) Optionally followed by one or more tool_use blocks for the actions you decided on.

NEVER reply with only tool_use blocks. NEVER skip the text block "because the action is obvious." \
Even for `out_of_scope`, `clarify`, and `query`, the text block IS the user-facing answer — \
the tool_use is just a structured signal for the backend.

Examples of valid replies:
- Text: "Got it — marking you available on Day 1 from 2pm." + tool_use: set_availability(day=1, ...)
- Text: "Day 3 isn't tracked by the scheduler. I only handle Day 1, 2, and 4." + tool_use: out_of_scope(reason="...")
- Text: "Which day did you mean?" + tool_use: clarify(ambiguity="...")
- Text: "Your current Day 2 slot is 19:00-19:30 UTC." + tool_use: query(subject="my times")

## YOUR JOB

1. ALWAYS produce a text block in the user's language (see above).
2. Decide what the user is asking for and emit one or more tool_use blocks for the action(s).
3. NEVER mention internal slot IDs (e.g. "D1-CM-21"). Always say times like "10:00 UTC".

Multiple actions in one message are fine. Examples:
- "drop my Day 1 and move my Day 4 to 8am" → drop_slot(day=1) + move_slot(day=4, new_start_utc="08:00")
- "I'm free Day 1 after 2pm and Day 2 all day" → set_availability(day=1, ...) + set_availability(day=2, ...)

## LANGUAGE
Detect the user's language from their message. Reply in that same language. \
Spanish input → Spanish reply. Korean input → Korean reply. English → English. \
The tool_use blocks themselves are language-agnostic structured data.

When you mention a day in your reply:
- If the user used a resource word ("construction", "research", "troops"), mirror that word.
- If the user used a day number, use the day number.
- Resource → day mapping:
   construction / building = Day 1
   research                = Day 2
   troops / training / soldiers = Day 4

## TIME RULES
- All times in actions are UTC, HH:MM 24-hour.
- Convert local timezones the user mentions ("3pm EST", "10am PST") to UTC before emitting.
- A time window may cross midnight — emit end_utc earlier than start_utc and the backend handles it.

## RESET SEMANTICS (strict)
- "close to reset" / "near reset" / "around reset (before)" / "late" → 21:15-00:15 UTC
- "after reset" / "just after reset" / "around reset (after)" / "early" → 23:45 (previous day) - 02:45 UTC
- "reset" alone → 00:00 UTC exactly

## RELATIVE TIMES (post-lock)
For requests like "3 hours earlier" or "move it later by 90 minutes": look at the user's CURRENT ASSIGNMENT \
in the state, do the arithmetic yourself, emit the absolute new_start_utc. Slots are aligned to :15 and :45 \
(every 30 minutes from 23:45). If the user's requested offset doesn't land on a slot boundary, use the nearest one.

## PARTIAL UPDATES
If the user only mentions Day 2, only emit set_availability for day=2. Day 1 and Day 4 are preserved automatically. \
DO NOT emit set_availability with empty windows for days the user didn't mention.

## EMPTY WINDOWS
"I'm not available Day X" / "skip Day X" → emit set_availability(day=X, windows=[]). \
This clears that day specifically.

## SWAP RULES
- The user MUST @mention the target. The state lists VALID SWAP PARTNERS — only swap with someone on that list.
- If the user wants to swap but didn't @mention anyone valid, use `clarify` to ask them to @mention.
- If they @mentioned someone who isn't in VALID SWAP PARTNERS, that person isn't assigned in this event — say so with `clarify`.

## OUT OF SCOPE
Use `out_of_scope` for:
- Day 3 or Day 5 scheduling
- Other players' data ("who is at 10am?", "show me Bob's slot") — for v1 we don't expose this
- Setting resources via text ("I have 5 days of construction speedups") — resources MUST come from screenshots
- General chat, jokes, off-topic questions

## CLARIFY
Use `clarify` when the request is ambiguous and you can't safely guess. Example: "move me earlier" without a day specifier and the user has multiple assignments.

## QUERIES
Use `query` for "what are my times?" / "what's my availability?" / "how does this work?" / "what's my deadline?". \
The state has everything you need to answer — put the answer in your text reply.

## SCREENSHOTS
The user may also have uploaded a resources screenshot in this message. \
The parser runs separately and updates their resources directly. If a SYSTEM NOTE in the user content tells you \
about parsed values, briefly acknowledge them in your reply in the user's language (e.g. "I've updated your \
speedup totals"). The screenshot acknowledgment is part of your text reply, not a tool_use.

## NEVER
- Don't expose slot IDs.
- Don't accept resource values from text.
- Don't swap without a valid @mention.
- Don't make up information not in the state. If you don't know, say so or ask.
"""


def _build_system_prompt(state: dict) -> str:
    return _BASE_SYSTEM_PROMPT + "\n\n## CURRENT STATE\n\n" + render_state_for_prompt(state)


# ─── Bot mention stripping ──────────────────────────────────────


def _strip_bot_mention(content: str, bot_id: int) -> str:
    for pattern in (f"<@{bot_id}>", f"<@!{bot_id}>"):
        content = content.replace(pattern, "")
    return content.strip()


# ─── Screenshot parsing (saves to DB, returns parsed dict) ──────


async def _parse_and_save_screenshot(
    attachment: "discord.Attachment",
    event: "Event",
    message: "discord.Message",
) -> dict:
    """Parse one screenshot and save resource values to the user's Submission.

    Returns the parsed dict (resource_x/y/z/generic) on success, or a dict
    with key "error" on failure. Caller decides how to surface this to the
    user — the agent uses the LLM for that when text is present, and a
    hardcoded English block when it isn't.
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


def _format_screenshot_summary_en(r: dict) -> str:
    """Hardcoded English screenshot status (text-less message path)."""
    return (
        f"📸 Resources updated:\n"
        f"  • Construction (Day 1): {r['resource_x']:.2f} days\n"
        f"  • Research (Day 2):     {r['resource_y']:.2f} days\n"
        f"  • Troops (Day 4):       {r['resource_z']:.2f} days\n"
        f"  • General (split by {GENERIC_SPLIT}): {r['resource_generic']:.2f} days"
    )


# ─── User content builder (for the main LLM call) ───────────────


def _build_user_content(text: str, screenshot_data: list[dict]) -> list[dict]:
    """Build the user-message content blocks for the main LLM call.

    Screenshot info is conveyed via labeled SYSTEM NOTE text blocks so the
    LLM knows what was parsed (and can acknowledge it) without it being
    treated as user-typed input.
    """
    content: list[dict] = []

    parsed = [r for r in screenshot_data if "error" not in r]
    errors = [r["error"] for r in screenshot_data if "error" in r]

    if parsed:
        summaries = []
        for r in parsed:
            summaries.append(
                f"construction={r['resource_x']:.2f}d, "
                f"research={r['resource_y']:.2f}d, "
                f"troops={r['resource_z']:.2f}d, "
                f"general={r['resource_generic']:.2f}d "
                f"(general is split into {GENERIC_SPLIT} equal parts)"
            )
        content.append({
            "type": "text",
            "text": (
                "[SYSTEM NOTE — not from the user: A screenshot was attached "
                "and parsed. The user's resources have been saved as: "
                + " | ".join(summaries)
                + ". Briefly acknowledge the updated resources in the user's language."
            ),
        })

    if errors:
        content.append({
            "type": "text",
            "text": (
                "[SYSTEM NOTE — not from the user: An image was attached but "
                "the resources screenshot parser couldn't read it. "
                f"Errors: {'; '.join(errors)}. Tell the user in their "
                "language that the screenshot couldn't be parsed and to "
                "send a clear photo of the Resources & Speedups page."
            ),
        })

    content.append({"type": "text", "text": text})
    return content


# ─── Error translation (only on handler failure) ────────────────


async def _translate_errors(
    original_user_text: str,
    main_reply: str,
    handler_errors: list[str],
) -> str:
    """Translate English handler-error strings into the same language as
    main_reply via a short second LLM call. Returns the translated block
    prefixed with 🚨 lines. Falls back to English on failure.
    """
    error_block = "\n".join(f"- {e}" for e in handler_errors)
    try:
        response = await _client.messages.create(
            model=ANTHROPIC_MODEL,
            max_tokens=400,
            messages=[
                {"role": "user", "content": original_user_text or "(no text)"},
                {"role": "assistant", "content": main_reply or "(no reply)"},
                {"role": "user", "content": (
                    "I tried to perform what you described above, but some actions failed "
                    "with these error messages (English text from the backend):\n\n"
                    f"{error_block}\n\n"
                    "Please write a brief notice IN THE SAME LANGUAGE you used in your "
                    "previous response, summarizing these errors for the user. Start each "
                    "line with 🚨. Keep it short. Don't repeat anything you already said."
                )},
            ],
        )
        for block in response.content:
            if getattr(block, "type", None) == "text":
                translated = block.text.strip()
                if translated:
                    return translated
    except Exception:
        logger.exception("Error translation pass failed")

    return "🚨 " + "\n🚨 ".join(handler_errors)


# ─── Fallback synthesis (when LLM forgets the text block) ───────


def _synthesize_fallback(response_content) -> str:
    """Build a fallback English reply from tool_use inputs alone.

    Called when the LLM emitted tool_use blocks but no text block — the
    user would otherwise see "(no reply)". We use the tool inputs to
    construct something informative. Some inputs (out_of_scope.reason,
    clarify.ambiguity) may have been written by the LLM in the user's
    language; we use those directly when present. Other actions get
    generic English templates.
    """
    parts: list[str] = []
    for block in response_content:
        if getattr(block, "type", None) != "tool_use":
            continue
        name = block.name
        inp = block.input or {}

        if name == "out_of_scope":
            reason = (inp.get("reason") or "").strip()
            parts.append(reason or "I can only help with Day 1, Day 2, or Day 4 scheduling.")
        elif name == "clarify":
            ambig = (inp.get("ambiguity") or "").strip()
            parts.append(ambig or "Could you clarify your request?")
        elif name == "query":
            subj = inp.get("subject", "your question")
            parts.append(
                f"I received your question about {subj!r} but didn't generate a written answer. "
                f"Try asking again with a bit more detail."
            )
        elif name == "set_availability":
            day = inp.get("day")
            n = len(inp.get("windows") or [])
            if n == 0:
                parts.append(f"Marked you as not available on Day {day}.")
            else:
                parts.append(f"Updated your Day {day} availability ({n} window(s)).")
        elif name == "widen_availability":
            day = inp.get("day")
            parts.append(f"Added more availability on Day {day}.")
        elif name == "move_slot":
            day = inp.get("day")
            new_time = inp.get("new_start_utc", "?")
            parts.append(
                f"Move request submitted: Day {day} → {new_time} UTC. Pending admin approval."
            )
        elif name == "drop_slot":
            day = inp.get("day")
            parts.append(f"Drop request submitted for Day {day}. Pending admin approval.")
        elif name == "swap":
            day = inp.get("day")
            parts.append(
                f"Swap request submitted for Day {day}. Waiting for the other player's confirmation."
            )

    return "\n".join(parts).strip()


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

    # Parse + save any screenshots first (silent — no user message yet)
    screenshot_data: list[dict] = []
    for att in image_attachments:
        screenshot_data.append(await _parse_and_save_screenshot(att, event, message))

    # Case A: nothing to work with
    if not text and not image_attachments:
        if event.phase == EventPhase.COLLECTING:
            return (
                "Hi! @mention me with a screenshot of your in-game Resources & Speedups "
                "page and/or your available times for Day 1, Day 2, and Day 4."
            )
        return (
            "Hi! @mention me here with a request — for example: "
            "\"move my Day 4 slot earlier by 2 hours\", \"drop my Day 1\", "
            "or \"@PlayerB swap Day 1\"."
        )

    # Case B: screenshots only, no text. No language signal, so use English.
    if not text:
        parts = []
        for r in screenshot_data:
            if "error" in r:
                parts.append(f"🚨 Couldn't read the screenshot: {r['error']}")
            else:
                parts.append(_format_screenshot_summary_en(r))
        return "\n\n".join(parts)

    # Case C: text (with or without screenshot) — main LLM flow
    async with async_session() as session:
        state = await build_state_envelope(session, event, message)

    tools = tools_for_phase(event.phase.value)
    if not tools:
        return "I can't act on archived or unknown events."

    handlers = (
        COLLECTING_HANDLERS if event.phase == EventPhase.COLLECTING else LOCKED_HANDLERS
    )
    user_content = _build_user_content(text, screenshot_data)

    try:
        response = await _client.messages.create(
            model=ANTHROPIC_MODEL,
            max_tokens=2048,
            system=_build_system_prompt(state),
            tools=tools,
            messages=[{"role": "user", "content": user_content}],
        )
    except Exception:
        logger.exception("Anthropic API call failed")
        return "🚨 Sorry, I couldn't reach my language model just now. Please try again."

    text_parts: list[str] = []
    handler_errors: list[str] = []
    tool_names: list[str] = []  # for logging

    for block in response.content:
        btype = getattr(block, "type", None)
        if btype == "text":
            text_parts.append(block.text)
        elif btype == "tool_use":
            tool_names.append(block.name)
            handler = handlers.get(block.name)
            if handler is None:
                handler_errors.append(f"{block.name}: unknown action")
                continue
            try:
                err = await handler(block.input, state, message, bot)
                if err is not None:
                    handler_errors.append(err)
            except Exception:
                logger.exception(f"Handler {block.name} crashed")
                handler_errors.append(f"{block.name}: internal error")

    logger.info(
        f"LLM response: {len(text_parts)} text block(s), "
        f"tool_uses={tool_names}, errors={len(handler_errors)}"
    )

    main_reply = "\n".join(text_parts).strip()

    # Fallback: the LLM emitted tool_use blocks but no text block. Synthesize
    # a reasonable English reply from the tool inputs so the user always sees
    # something. The system prompt asks for text but this is a defensive net.
    if not main_reply and tool_names:
        main_reply = _synthesize_fallback(response.content)
        if main_reply:
            logger.warning(
                f"LLM omitted text block; used fallback synthesis. "
                f"tool_uses={tool_names}"
            )

    if not handler_errors:
        return main_reply or "I processed your message but didn't generate a reply. Please try again."

    # Translate errors into the same language as the main reply
    translated_errors = await _translate_errors(text, main_reply, handler_errors)
    if main_reply:
        return f"{main_reply}\n\n{translated_errors}"
    return translated_errors
