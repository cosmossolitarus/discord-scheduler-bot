"""
Shared utilities for LLM response parsing.

In Phase 2 the action pattern replaces the old triage classifier
(TRIAGE_PROMPT / classify_message) — those were removed. The JSON
extraction helper is kept because screenshot.py still uses it.
"""

import json
import logging
import re

logger = logging.getLogger("scheduler.llm.utils")


def extract_json(text: str) -> dict | None:
    """Pull a JSON object out of text that may include extra content.

    Tries: whole-text, fenced markdown, first { ... } block, first {
    to last }. Returns the parsed dict or None.
    """
    text = text.strip()

    # 1: whole-text
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # 2: strip ``` fences
    cleaned = re.sub(r"^```(?:json)?\s*", "", text, flags=re.MULTILINE)
    cleaned = re.sub(r"\s*```\s*$", "", cleaned, flags=re.MULTILINE)
    cleaned = cleaned.strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass

    # 3: first balanced-looking { ... } block (no nesting)
    match = re.search(r"\{[^{}]*\}", text)
    if match:
        try:
            return json.loads(match.group())
        except json.JSONDecodeError:
            pass

    # 4: first { to last } (handles nesting)
    first = text.find("{")
    last = text.rfind("}")
    if first != -1 and last > first:
        try:
            return json.loads(text[first:last + 1])
        except json.JSONDecodeError:
            pass

    return None
