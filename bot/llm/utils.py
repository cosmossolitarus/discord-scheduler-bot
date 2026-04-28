"""
Shared utilities for LLM response parsing, message classification, and bot personality.
"""

import json
import logging
import random
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

Players refer to days by resource type:
- "construction" / "building" = Day 1
- "research" = Day 2
- "troops" / "training" / "soldiers" = Day 4
"Reset" means 0:00 UTC.

Classify the message:
1. "availability" — providing or updating available times for scheduling. Includes partial
   updates like "change Day 2 to after 19 UTC". When in doubt, choose this — the downstream
   parser will handle errors gracefully.
2. "query" — asking about their current data, times, speedups, status, or how the bot works.
   NOT a request to change anything.
3. "nonsense" — joke, trolling, venting, irrelevant chatter, or anything that isn't a genuine
   scheduling interaction.

Respond with ONLY: {"type": "availability"} or {"type": "query"} or {"type": "nonsense"}
"""


async def classify_message(text: str) -> str:
    """
    Classify a user's text message into: availability, query, or nonsense.
    Returns the type string. Defaults to 'availability' on failure.
    """
    try:
        response = await client.messages.create(
            model=ANTHROPIC_MODEL,
            max_tokens=50,
            system=TRIAGE_PROMPT,
            messages=[{"role": "user", "content": text}],
        )
        result = extract_json(response.content[0].text.strip())
        if result and "type" in result:
            return result["type"]
    except Exception as e:
        logger.error(f"Message classification failed: {e}")

    return "availability"  # Safe default — parse_availability handles bad input


# ─── Witty Nonsense Responses ────────────────────────────────────

_FALLBACK_RESPONSES = [
    "I'm a scheduling bot, not a miracle worker. Try sending me your actual times.",
    "That's fascinating. Now, did you have any actual scheduling to do?",
    "My calendar says it's time for you to send a real request.",
    "I'd roast you back but I'm too busy managing everyone else's schedule.",
]


async def generate_witty_response(message_text: str) -> str:
    """
    Generate a short, sassy response to a nonsense message.
    Falls back to a canned response if the LLM call fails.
    """
    try:
        response = await client.messages.create(
            model=ANTHROPIC_MODEL,
            max_tokens=150,
            system=(
                "You are a sassy scheduling bot. A user just sent you a nonsense message "
                "instead of an actual scheduling request. Respond with a short, witty "
                "one-liner that playfully roasts them while reminding them what you "
                "actually do. Keep it under 2 sentences. Be funny but not mean. "
                "Don't use emojis. Don't use quotation marks around your response."
            ),
            messages=[{"role": "user", "content": message_text}],
        )
        return response.content[0].text.strip()
    except Exception:
        return random.choice(_FALLBACK_RESPONSES)
