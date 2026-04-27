"""
Availability parsing via Anthropic text API.

Takes free-text availability (e.g., "Day 1 anytime after 2pm,
Day 2 10-14 and 18-22") and converts to a structured list of
slot IDs the user is available for.
"""

import json
import logging

import anthropic

from bot.config import ANTHROPIC_MODEL
from bot.cycle import generate_slot_times

logger = logging.getLogger("scheduler.llm.availability")

client = anthropic.AsyncAnthropic()


def build_system_prompt(day1_date_str: str, slot_reference: list[dict]) -> str:
    """
    Build a system prompt that includes the slot reference table
    so the LLM knows exactly which slot IDs correspond to which times.
    """
    # Build a condensed reference: slot_id → "HH:MM - HH:MM UTC"
    slot_lines = []
    for s in slot_reference:
        start_str = s["start_time"].strftime("%Y-%m-%d %H:%M UTC")
        end_str = s["end_time"].strftime("%H:%M UTC")
        slot_lines.append(f"  {s['slot_id']}: {start_str} - {end_str}")

    slot_table = "\n".join(slot_lines)

    return f"""You are an availability parser for a scheduling bot.
The event starts on {day1_date_str} (Day 1). Slot times are in UTC.

Here are all available slots:
{slot_table}

The user will describe their availability in free text. They may use:
- Day numbers (Day 1, Day 2, Day 4)
- Time ranges ("10am to 6pm", "14:00-18:00", "after 3pm", "all day")
- Timezone names ("3pm EST", "10am PST") — convert these to UTC
- General phrases ("anytime", "morning", "evening", "not available")
- "reset" or "daily reset" — this means exactly 0:00 UTC. So "just after reset" means
  roughly 0:00-3:00 UTC, "before reset" means the hours before midnight UTC, and
  "around reset" means a window on either side of 0:00 UTC.

Your job: determine which slot IDs the user is available for.

Respond with ONLY a JSON object, no markdown, no explanation:
{{"available_slots": ["D1-CM-01", "D1-CM-02", ...], "interpretation": "brief human-readable summary of what you understood"}}

If the input is nonsensical or you cannot parse it:
{{"error": "description of what went wrong"}}

Be generous in interpretation — if a user says "Day 1 after 2pm" and a slot
starts at 1:45pm, include it since most of the slot is after 2pm.
"""


async def parse_availability(
    text: str,
    day1_date_str: str,
    slot_reference: list[dict],
) -> dict:
    """
    Parse free-text availability into a list of slot IDs.

    Args:
        text: User's free-text availability description.
        day1_date_str: Human-readable Day 1 date, e.g., "May 18, 2026".
        slot_reference: Output of generate_slot_times() for this event.

    Returns:
        Dict with keys:
            available_slots: list of slot_id strings
            interpretation: human-readable summary
        Or dict with key "error" if parsing failed.
    """
    system = build_system_prompt(day1_date_str, slot_reference)

    try:
        response = await client.messages.create(
            model=ANTHROPIC_MODEL,
            max_tokens=4096,  # Slot list can be long
            system=system,
            messages=[
                {
                    "role": "user",
                    "content": text,
                }
            ],
        )
    except Exception as e:
        logger.error(f"Anthropic API call failed: {e}")
        return {"error": f"API call failed: {e}"}

    raw_text = response.content[0].text.strip()

    try:
        parsed = json.loads(raw_text)
    except json.JSONDecodeError:
        logger.warning(f"LLM returned non-JSON: {raw_text}")
        return {"error": f"Could not parse LLM response: {raw_text}"}

    if "error" in parsed:
        return parsed

    if "available_slots" not in parsed:
        return {"error": "LLM response missing 'available_slots' key"}

    # Validate slot IDs against the reference
    valid_ids = {s["slot_id"] for s in slot_reference}
    invalid = [sid for sid in parsed["available_slots"] if sid not in valid_ids]
    if invalid:
        logger.warning(f"LLM returned invalid slot IDs: {invalid}")
        parsed["available_slots"] = [
            sid for sid in parsed["available_slots"] if sid in valid_ids
        ]

    return parsed
