"""
Shared utilities for LLM response parsing.
"""

import json
import re


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
