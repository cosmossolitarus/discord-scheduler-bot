"""
Screenshot parsing via Anthropic vision API.
"""

import base64
import json
import logging

import anthropic

from bot.config import ANTHROPIC_MODEL

logger = logging.getLogger("scheduler.llm.screenshot")

client = anthropic.AsyncAnthropic()

# TODO: Replace placeholder resource names with actual in-game names.
# Describe where each resource appears on the screenshot for better accuracy.
SYSTEM_PROMPT = """You are a resource extraction assistant for a mobile game.
The user will send a screenshot showing their resource counts.
Extract exactly four values:
- Resource X (Chief Minister resource for Day 1)
- Resource Y (Chief Minister resource for Day 2)
- Resource Z (Noble Advisor / Chief Minister resource for Day 4)
- Generic resource (can be converted to any specific resource)

Respond with ONLY a JSON object, no markdown, no explanation:
{"resource_x": <number>, "resource_y": <number>, "resource_z": <number>, "resource_generic": <number>}

If you cannot confidently extract all four values, respond with:
{"error": "description of what went wrong"}
"""


async def parse_screenshot(image_data: bytes, media_type: str = "image/png") -> dict:
    b64_image = base64.b64encode(image_data).decode("utf-8")

    try:
        response = await client.messages.create(
            model=ANTHROPIC_MODEL,
            max_tokens=200,
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
                            "text": "Extract the resource values from this screenshot.",
                        },
                    ],
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

    required = ["resource_x", "resource_y", "resource_z", "resource_generic"]
    for key in required:
        if key not in parsed:
            return {"error": f"Missing key: {key}"}
        try:
            parsed[key] = float(parsed[key])
        except (ValueError, TypeError):
            return {"error": f"Non-numeric value for {key}: {parsed[key]}"}

    return parsed
