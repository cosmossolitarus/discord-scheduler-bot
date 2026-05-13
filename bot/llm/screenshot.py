"""
Screenshot parsing via Anthropic vision API.

Sends the game's "Resources & Speedups" screenshot to Claude,
extracts speedup durations, converts to days, and returns them.
"""

import base64
import logging

import anthropic

from bot.config import ANTHROPIC_MODEL

logger = logging.getLogger("scheduler.llm.screenshot")

client = anthropic.AsyncAnthropic()

SYSTEM_PROMPT = """You are a speedup resource extractor for a mobile strategy game.

The user will send a screenshot of the "Resources & Speedups" popup showing the Speedups tab.
This screen appears in MANY languages (English, French, Spanish, German, Korean, Arabic, Chinese, etc.).
The layout and icons are always the same regardless of language.

THE ROWS (always in this order, top to bottom):
1. General Speedup — icon: plain blue double arrows (>>). This is the GENERIC resource.
2. Soldier Training Speedup — icon: blue double arrows with a small helmet emblem. This is for DAY 4.
3. Construction Speedup — icon: blue double arrows with a small hammer emblem. This is for DAY 1.
4. Research Speedup — icon: blue double arrows with a small book emblem. This is for DAY 2.
5. Learning Speedups — icon: blue double arrows with a crown/star emblem. IGNORE this row.
6. Soldier Healing Speedup — icon: blue/teal arrows with a green cross (+). IGNORE this row.

NOTE: Row 5 (Learning) may not appear on older screenshots. Row 6 (Healing) may be partially
cut off at the bottom. Both are irrelevant — just ignore them.

ARABIC LAYOUT: In Arabic, the screen is mirrored — icons appear on the RIGHT, names on the RIGHT,
and time values on the LEFT. The row order from top to bottom is the same.

TIME FORMAT: Values can be displayed in three different units depending on a checkbox at the bottom:
- Days mode: "36 day(s)17 hr(s)12 min(s)" or localized equivalents (e.g., "36 jour(s)17 h12 min")
- Hours mode: "881 hr(s)12 min(s)"
- Minutes mode: "52,872 min(s)"
The checkbox selection varies per screenshot. You must parse whatever format is shown.

If a row shows "No items" (or equivalent in any language), that value is 0.

YOUR TASK:
1. Identify the four relevant rows by their position (1st through 4th) and/or icons.
2. Extract the time value from each row.
3. Convert ALL values to DAYS as a decimal number rounded to 2 decimal places.
   - If shown in minutes: divide by 1440
   - If shown in hours and minutes: convert to total minutes first, then divide by 1440
   - If shown in days, hours, minutes: convert to total minutes first, then divide by 1440
4. Return the result.

Respond with ONLY a JSON object. No markdown, no explanation, no extra text:
{"resource_generic": <days>, "resource_x": <days>, "resource_y": <days>, "resource_z": <days>}

Where:
- resource_generic = General Speedup (row 1) in days
- resource_x = Construction Speedup (row 3) in days  
- resource_y = Research Speedup (row 4) in days
- resource_z = Soldier Training Speedup (row 2) in days

If you cannot confidently extract the values, respond with:
{"error": "description of what went wrong"}

CRITICAL: Output ONLY the JSON object. No explanation, no calculations, no markdown, no reasoning.
Wrong: "General Speedup: 73h 5m = 3.05 days... {json}"
Right: {"resource_generic": 3.05, "resource_x": 1.73, "resource_y": 2.66, "resource_z": 3.60}
"""


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
    # Fallback
    return "image/png"


async def parse_screenshot(image_data: bytes, media_type: str = "image/png") -> dict:
    """
    Parse a game screenshot to extract speedup values in days.

    Args:
        image_data: Raw image bytes.
        media_type: MIME type of the image (used as fallback only).

    Returns:
        Dict with keys resource_x, resource_y, resource_z, resource_generic
        (all in days as floats), or a dict with key "error" if parsing failed.
    """
    # Detect actual format from bytes — Discord's content_type can be wrong
    media_type = _detect_media_type(image_data)
    b64_image = base64.b64encode(image_data).decode("utf-8")

    try:
        response = await client.messages.create(
            model=ANTHROPIC_MODEL,
            max_tokens=300,
            system=SYSTEM_PROMPT,
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
                            "text": "Extract the speedup values from this screenshot.",
                        },
                    ],
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
        return {"error": "Could not extract speedup values from screenshot. Please try again."}

    if "error" in parsed:
        return parsed

    required = ["resource_x", "resource_y", "resource_z", "resource_generic"]
    for key in required:
        if key not in parsed:
            return {"error": f"Missing key: {key}"}
        try:
            parsed[key] = round(float(parsed[key]), 2)
        except (ValueError, TypeError):
            return {"error": f"Non-numeric value for {key}: {parsed[key]}"}

    return parsed
