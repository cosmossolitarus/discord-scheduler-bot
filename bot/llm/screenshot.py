"""
Screenshot parsing via Anthropic vision API.

Sends the game's Speedups screenshot to Claude and extracts
the four speedup durations in days.

The model returns its output via a tool_use block (not free-form JSON text).
This is far more reliable than asking the model to write a JSON object in its
text response — particularly for non-English screenshots where the model
sometimes prepends explanation despite "respond with JSON only" instructions.

Two tools are defined; tool_choice="any" forces the model to use exactly one:
  - extract_speedups: success path, returns the four numbers
  - report_error: graceful failure path with a reason
"""

import base64
import logging

import anthropic

from bot.config import ANTHROPIC_MODEL

logger = logging.getLogger("scheduler.llm.screenshot")

client = anthropic.AsyncAnthropic()


SYSTEM_PROMPT = """You read a screenshot of the Speedups popup \
from a mobile strategy game and extract four speedup totals.

The screen appears in many languages (English, French, Spanish, German, Korean, \
Arabic, Chinese, etc.). Layout and icons are the same regardless of language.

THE ROWS (always in this order, top to bottom):
1. General Speedup — plain blue double arrows (>>).
2. Soldier Training Speedup — blue double arrows with a helmet emblem. Day 4.
3. Construction Speedup — blue double arrows with a hammer emblem. Day 1.
4. Research Speedup — blue double arrows with a book emblem. Day 2.
5. Learning Speedups — blue double arrows with crown/star. IGNORE.
6. Soldier Healing Speedup — blue/teal arrows with a green cross. IGNORE.

Row 5 may not appear on older screenshots. Row 6 may be partially cut off. Both \
are irrelevant — ignore them either way.

ARABIC LAYOUT: in Arabic, the screen is mirrored — icons on the RIGHT, names on \
the RIGHT, time values on the LEFT. Row order top-to-bottom is the same.

TIME FORMAT — chosen by a checkbox at the bottom, varies per screenshot:
- Days mode:    "3 day(s)1 hr(s)5 min(s)"   (or localized equivalents)
- Hours mode:   "73 hr(s)5 min(s)"
- Minutes mode: "4,385 min(s)"

If a row shows "No items" (or localized equivalent), its value is 0.

CONVERSION TO DAYS:
- If shown in days+hours+minutes: total_min = d*1440 + h*60 + m; days = total_min / 1440
- If shown in hours+minutes:      total_min = h*60 + m;          days = total_min / 1440
- If shown in minutes only:       days = m / 1440
Round to 2 decimal places.

OUTPUT:
- If you can confidently read all four needed values, call extract_speedups \
with the four values in days.
- If the image is unreadable, isn't the Speedups screen, or you \
can't extract the values, call report_error with a brief reason.

Do NOT write any text response — only emit the tool_use block."""


_EXTRACT_TOOL = {
    "name": "extract_speedups",
    "description": "Return the four speedup totals in days, rounded to 2 decimals.",
    "input_schema": {
        "type": "object",
        "properties": {
            "speedup_general": {
                "type": "number",
                "description": "General Speedup (row 1) total in days.",
            },
            "speedup_construction": {
                "type": "number",
                "description": "Construction Speedup (row 3, Day 1) total in days.",
            },
            "speedup_research": {
                "type": "number",
                "description": "Research Speedup (row 4, Day 2) total in days.",
            },
            "speedup_training": {
                "type": "number",
                "description": "Soldier Training Speedup (row 2, Day 4) total in days.",
            },
        },
        "required": ["speedup_general", "speedup_construction", "speedup_research", "speedup_training"],
    },
}

_REPORT_ERROR_TOOL = {
    "name": "report_error",
    "description": (
        "Use only if the image cannot be read as a Speedups screen "
        "or one or more required values are missing or unreadable."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "reason": {
                "type": "string",
                "description": "Brief reason in English (one sentence).",
            },
        },
        "required": ["reason"],
    },
}


def _detect_media_type(image_data: bytes) -> str:
    """Detect actual image MIME type from file signature bytes."""
    if image_data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if image_data[:2] == b"\xff\xd8":
        return "image/jpeg"
    if image_data[:4] == b"RIFF" and image_data[8:12] == b"WEBP":
        return "image/webp"
    if image_data[:3] == b"GIF":
        return "image/gif"
    return "image/png"


async def parse_screenshot(image_data: bytes, media_type: str = "image/png") -> dict:
    """Parse a game screenshot to extract speedup values in days.

    Returns:
        On success: {"speedup_construction": float, "speedup_research": float, "speedup_training": float, "speedup_general": float}
        On failure: {"error": "..."} with the reason.
    """
    media_type = _detect_media_type(image_data)
    b64_image = base64.b64encode(image_data).decode("utf-8")

    try:
        response = await client.messages.create(
            model=ANTHROPIC_MODEL,
            max_tokens=512,
            system=SYSTEM_PROMPT,
            tools=[_EXTRACT_TOOL, _REPORT_ERROR_TOOL],
            tool_choice={"type": "any"},
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": media_type,
                                "data": b64_image,
                            },
                        },
                        {
                            "type": "text",
                            "text": "Extract the four speedup totals from this screenshot.",
                        },
                    ],
                }
            ],
        )
    except Exception as e:
        logger.error(f"Anthropic API call failed: {e}")
        return {"error": "API call failed. Please try again later."}

    # Find the tool_use block. With tool_choice="any" there should be one.
    for block in response.content:
        if getattr(block, "type", None) != "tool_use":
            continue
        if block.name == "report_error":
            reason = (block.input or {}).get("reason", "unknown")
            logger.warning(f"Screenshot parse: model reported error: {reason}")
            return {"error": reason}
        if block.name == "extract_speedups":
            values = block.input or {}
            try:
                result = {
                    "speedup_general": round(float(values["speedup_general"]), 2),
                    "speedup_construction": round(float(values["speedup_construction"]), 2),
                    "speedup_research": round(float(values["speedup_research"]), 2),
                    "speedup_training": round(float(values["speedup_training"]), 2),
                }
            except (KeyError, ValueError, TypeError) as e:
                logger.warning(f"Screenshot parse: missing/invalid value: {e} input={values}")
                return {"error": "Couldn't extract all four speedup values. Please try again."}
            logger.info(f"Screenshot parsed: {result}")
            return result

    # Defensive — shouldn't reach here under tool_choice="any"
    logger.warning(f"Screenshot: model returned no tool_use block: {response.content}")
    return {"error": "Couldn't read the screenshot. Please try again."}
