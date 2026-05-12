"""
Shared utilities for LLM response parsing and message classification.
"""

import json
import logging
import re

import anthropic

from bot.config import ANTHROPIC_MODEL

logger = logging.getLogger("scheduler.llm.utils")

client = anthropic.AsyncAnthropic()

# ─── JSON Extraction ────────────────────────────────────────────


def extract_json(text: str) -> dict | None:
    """
    Extract a JSON object from text that may contain extra content.

    The LLM sometimes includes reasoning or markdown before/after the JSON.
    This function tries multiple strategies to find and parse the JSON.

    Returns the parsed dict, or None if no valid JSON found.
    """
    text = text.strip()

    # Strategy 1: Try parsing the whole thing
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # Strategy 2: Strip markdown code fences
    cleaned = re.sub(r'^```(?:json)?\s*', '', text, flags=re.MULTILINE)
    cleaned = re.sub(r'\s*```\s*$', '', cleaned, flags=re.MULTILINE)
    cleaned = cleaned.strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass

    # Strategy 3: Find the first { ... } block
    match = re.search(r'\{[^{}]*\}', text)
    if match:
        try:
            return json.loads(match.group())
        except json.JSONDecodeError:
            pass

    # Strategy 4: Find the first { and last } (handles nested braces)
    first_brace = text.find('{')
    last_brace = text.rfind('}')
    if first_brace != -1 and last_brace > first_brace:
        try:
            return json.loads(text[first_brace:last_brace + 1])
        except json.JSONDecodeError:
            pass

    return None


# ─── Message Classification ─────────────────────────────────────

TRIAGE_PROMPT = """You are triaging messages for a game scheduling bot. The user @mentioned the bot.

Tracked days: Day 1, Day 2, Day 4 only. Players refer to days by resource type:
- "construction" / "building" = Day 1
- "research" = Day 2
- "troops" / "training" / "soldiers" = Day 4
"Reset" means 0:00 UTC.

Untracked days: Day 3 and Day 5. The bot does not handle these.

Classify the message into one type:
1. "availability" — providing or updating available times for Day 1, 2, or 4. Includes
   partial updates like "change Day 2 to after 19 UTC". When in doubt, choose this.
2. "query" — asking about their current data, times, speedups, status, or how the bot works.
   NOT a request to change anything.
3. "off_day" — message references Day 3 or Day 5 in a scheduling context (they appear to
   be giving availability or asking about a slot for an untracked day).
4. "other" — anything that doesn't fit above (jokes, irrelevant chatter, unclear messages).

Respond with ONLY one of:
{"type": "availability"}
{"type": "query"}
{"type": "off_day", "days": [3]}
{"type": "other"}
"""


async def classify_message(text: str) -> dict:
    """
    Classify a user's text message.
    Returns the parsed result dict with at least a 'type' key.
    Defaults to {'type': 'availability'} on failure — parse_availability handles bad input.
    """
    try:
        response = await client.messages.create(
            model=ANTHROPIC_MODEL,
            max_tokens=80,
            system=TRIAGE_PROMPT,
            messages=[{"role": "user", "content": text}],
        )
        result = extract_json(response.content[0].text.strip())
        if result and "type" in result:
            return result
    except Exception as e:
        logger.error(f"Message classification failed: {e}")

    return {"type": "availability"}


# ─── Standard Responses ─────────────────────────────────────────

BASIC_PROMPT_REPLY = (
    "I help with scheduling. @mention me with a screenshot of your resources "
    "and/or your available times for Day 1, Day 2, and Day 4 (in UTC)."
)


def off_day_reply(days: list[int]) -> str:
    """Build the response for messages referencing untracked Day 3 or Day 5."""
    day_str = " and ".join(f"Day {d}" for d in sorted(set(days))) if days else "Day 3 or Day 5"
    return (
        f"You mentioned {day_str}, which I don't track — only Day 1, Day 2, and Day 4.\n"
        f"If that was a mistake, send your correct availability. "
        f"If it was intentional, please contact an admin."
    )
