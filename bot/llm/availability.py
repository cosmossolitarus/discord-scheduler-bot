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
- Day numbers (Day 1, Day 2, Day 4 only — Day 3 and Day 5 are NOT tracked)
- Time ranges ("10am to 6pm", "14:00-18:00", "after 3pm", "all day")
- Timezone names ("3pm EST", "10am PST") — convert these to UTC
- General phrases ("anytime", "morning", "evening", "not available")

Reset-related phrases have STRICT meanings:
- "close to reset", "near reset", "around reset (before)", "late" (in context of end of day) → 21:15 - 00:15 UTC
- "after reset", "just after reset", "around reset (after)", "early" (in context of start of day) → 23:45 (previous day) - 02:45 UTC
- "reset" by itself means exactly 0:00 UTC

IMPORTANT — Players often refer to days by their resource type instead of number:
- "construction" / "building" = Day 1
- "research" = Day 2
- "troops" / "training" / "soldiers" = Day 4
So "before noon for construction" means "Day 1: before noon", NOT "all days for construction".
Each resource phrase maps to exactly one day.

If the user provides times for Day 3 or Day 5, IGNORE those days — only use times for
Day 1, Day 2, and Day 4 when building the slot list. Other Day 3/5 references like
"troops on Day 3" (which mixes a Day 4 resource word with a Day 3 number) are ambiguous —
do your best to extract whatever IS clear from the rest of the message and skip the
ambiguous part. Do NOT return an error just because one part of the message is unclear.

"I don't need X" / "skip Day X" / "not available Day X" means that day should map to NO
slots (empty for that day). This is valid — return an empty list for that day but still
process the other days normally.

Process whatever you CAN extract. Even if only one day is clear, return slots for that
day. Only return an error if NO day's availability can be determined at all.

Your job: determine which slot IDs the user is available for.

Respond with ONLY a JSON object, no markdown, no explanation:
{{"available_slots": ["D1-CM-01", "D1-CM-02", ...], "interpretation": "detailed log of what you understood", "player_summary": "formatted summary for the player"}}

The player_summary format: one line per day the user mentioned, with the actual UTC slot
time range followed by the player's own phrasing in parentheses. Use HH:MM UTC format.
For multiple windows on the same day, separate with commas.

Example formats:
"Day 1: 00:00 - 23:45 UTC (anytime)"
"Day 2: 19:00 - 23:00 UTC (2-6pm EST)"
"Day 4: 21:15 - 00:15 UTC (late, close to reset)"
"Day 1: 08:00 - 12:00 UTC, 16:00 - 20:00 UTC (mornings and evenings)"

Always use 24-hour HH:MM UTC. Never include slot IDs or slot counts in the player_summary.

If the input is nonsensical or you cannot parse it:
{{"error": "description of what went wrong"}}

Be generous in interpretation — if a user says "Day 1 after 2pm" and a slot
starts at 1:45pm, include it since most of the slot is after 2pm.

CRITICAL: Output ONLY the JSON object. No explanation, no reasoning, no step-by-step work.
"""


async def parse_availability(
    text: str,
    day1_date_str: str,
    slot_reference: list[dict],
    existing_summary: str | None = None,
) -> dict:
    """
    Parse free-text availability into a list of slot IDs.

    Args:
        text: User's free-text availability description.
        day1_date_str: Human-readable Day 1 date, e.g., "May 18, 2026".
        slot_reference: Output of generate_slot_times() for this event.
        existing_summary: Optional summary of the user's existing availability
            so the LLM can interpret partial updates correctly.

    Returns:
        Dict with keys:
            available_slots: list of slot_id strings
            interpretation: human-readable summary
        Or dict with key "error" if parsing failed.
    """
    system = build_system_prompt(day1_date_str, slot_reference)

    if existing_summary:
        user_content = (
            f"The user already has this availability on file:\n{existing_summary}\n\n"
            f"Their new message is below. Interpret it as a PARTIAL update — only "
            f"change the days they mention. For days they don't mention, return the "
            f"slot IDs for their existing availability so they are preserved.\n\n"
            f"User message:\n{text}"
        )
    else:
        user_content = text

    try:
        response = await client.messages.create(
            model=ANTHROPIC_MODEL,
            max_tokens=4096,  # Slot list can be long
            system=system,
            messages=[
                {
                    "role": "user",
                    "content": user_content,
                }
            ],
        )
    except Exception as e:
        logger.error(f"Anthropic API call failed: {e}")
        return {"error": "API call failed. Please try again later."}

    raw_text = response.content[0].text.strip()

    from bot.llm.utils import extract_json
    parsed = extract_json(raw_text)

    if parsed is None:
        logger.warning(f"LLM returned unparseable response: {raw_text[:200]}")
        return {"error": "Could not parse your availability. Please try rephrasing."}

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
