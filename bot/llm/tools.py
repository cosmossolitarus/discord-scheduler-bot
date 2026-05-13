"""
Anthropic tool schemas for the action pattern.

Two separate sets:
  COLLECTING_TOOLS — pre-lock actions (event.phase == COLLECTING)
  LOCKED_TOOLS     — post-lock actions (event.phase == LOCKED)

The agent picks the right set based on the event's phase. Action names map
1:1 to handler functions in bot/llm/handlers_*.py.

Notes on schema design:
  - Times are always HH:MM UTC (24-hour). The day field (1, 2, or 4) tells
    us which calendar day to anchor to.
  - For windows that cross midnight, end_utc can be earlier than start_utc
    and slots.py treats it as "end is next day".
  - swap requires other_player_discord_id (an integer). The agent will
    have pre-validated that this id appeared in message.mentions AND has
    an assignment in this event; the LLM picks it from the
    VALID SWAP PARTNERS list in the state block.
"""


# ─── Shared action shapes ────────────────────────────────────────


_QUERY = {
    "name": "query",
    "description": (
        "Use when the user is asking about their own current state — their "
        "assignments, availability, resources, or how the bot works. The "
        "actual response goes in the text portion of your reply; this tool "
        "is just a signal that you're answering a query, not making changes."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "subject": {
                "type": "string",
                "description": "Short label of what was asked (e.g. 'my times', 'my resources', 'how do I update').",
            },
        },
        "required": ["subject"],
    },
}

_OUT_OF_SCOPE = {
    "name": "out_of_scope",
    "description": (
        "Use when the user is asking about something the bot doesn't handle: "
        "Day 3 or Day 5 scheduling, jokes, other players' data, requests to "
        "set resources via text (those must come from a screenshot), or any "
        "non-scheduling chatter."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "reason": {
                "type": "string",
                "description": "Brief, neutral description of why this is out of scope.",
            },
        },
        "required": ["reason"],
    },
}

_CLARIFY = {
    "name": "clarify",
    "description": (
        "Use when the user's request is ambiguous and you cannot reliably "
        "act on it. The clarifying question goes in the text portion of "
        "your reply; this tool is just a signal."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "ambiguity": {
                "type": "string",
                "description": "What about the request is unclear.",
            },
        },
        "required": ["ambiguity"],
    },
}

_GREET = {
    "name": "greet",
    "description": (
        "Use when the user sent a greeting ('hi', 'hello', 'test'), asked "
        "for help, or sent an opener without a specific scheduling request. "
        "The backend will respond with a standardized help message."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "kind": {
                "type": "string",
                "enum": ["greeting", "help_request", "unclear_opener"],
                "description": "'greeting' for hi/hello/test, 'help_request' for explicit asks for help, 'unclear_opener' for chatter that isn't a real action.",
            },
        },
        "required": ["kind"],
    },
}

_WINDOWS_ARRAY = {
    "type": "array",
    "description": (
        "List of time windows the user is available during, in UTC. "
        "Empty list means 'not available that day'. Multiple windows "
        "allowed (e.g. mornings and evenings)."
    ),
    "items": {
        "type": "object",
        "properties": {
            "start_utc": {
                "type": "string",
                "description": "Start time in HH:MM (24-hour UTC).",
            },
            "end_utc": {
                "type": "string",
                "description": "End time in HH:MM (24-hour UTC). May cross midnight (end < start).",
            },
        },
        "required": ["start_utc", "end_utc"],
    },
}


# ─── Pre-lock toolset ────────────────────────────────────────────


SET_AVAILABILITY = {
    "name": "set_availability",
    "description": (
        "Use when the user is providing or updating their available times "
        "for ONE day. Only call this for Day 1, 2, or 4. Repeat the call "
        "for multiple days. Empty `windows` means 'not available that day' "
        "and removes any existing availability for that day."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "day": {
                "type": "integer",
                "enum": [1, 2, 4],
                "description": "Game day. Day 3 and Day 5 are not tracked.",
            },
            "windows": _WINDOWS_ARRAY,
        },
        "required": ["day", "windows"],
    },
}

COLLECTING_TOOLS = [SET_AVAILABILITY, _QUERY, _GREET, _OUT_OF_SCOPE, _CLARIFY]


# ─── Post-lock toolset ───────────────────────────────────────────


MOVE_SLOT = {
    "name": "move_slot",
    "description": (
        "Use when the user wants to change the START TIME of one of their "
        "current assignments. You must supply an absolute UTC start time "
        "(HH:MM); if the user gave a relative offset like '3 hours earlier', "
        "compute the new time yourself from their current assignment. The "
        "move requires admin approval before it takes effect."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "day": {
                "type": "integer",
                "enum": [1, 2, 4],
                "description": "Which of the user's current assignments to move.",
            },
            "new_start_utc": {
                "type": "string",
                "description": "Desired new slot start time in HH:MM UTC.",
            },
            "reason": {
                "type": "string",
                "description": "Optional short reason from the user (e.g. 'work conflict').",
            },
        },
        "required": ["day", "new_start_utc"],
    },
}

DROP_SLOT = {
    "name": "drop_slot",
    "description": (
        "Use when the user wants to give up one of their current "
        "assignments entirely. Requires admin approval before it takes "
        "effect."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "day": {
                "type": "integer",
                "enum": [1, 2, 4],
                "description": "Which of the user's current assignments to drop.",
            },
            "reason": {
                "type": "string",
                "description": "Optional short reason from the user.",
            },
        },
        "required": ["day"],
    },
}

WIDEN_AVAILABILITY = {
    "name": "widen_availability",
    "description": (
        "Use when the user is offering ADDITIONAL availability (on top of "
        "what they already gave). This does not change current assignments "
        "by itself — it just lets the admin reassign them more easily if "
        "needed. Applied immediately, no admin approval required."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "day": {
                "type": "integer",
                "enum": [1, 2, 4],
            },
            "windows": _WINDOWS_ARRAY,
        },
        "required": ["day", "windows"],
    },
}

SWAP = {
    "name": "swap",
    "description": (
        "Use when the user wants to trade one of their slots for another "
        "player's slot on the same day. Requires: the user @mentioned the "
        "target (only choose an id from VALID SWAP PARTNERS in the state), "
        "both users have an assignment for that day. The other player "
        "must confirm and admin must approve before it applies. If no valid "
        "partner is listed, do NOT call this tool — use `clarify` to ask "
        "the user to @mention the target."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "other_player_discord_id": {
                "type": "integer",
                "description": "Discord ID from VALID SWAP PARTNERS in the state.",
            },
            "day": {
                "type": "integer",
                "enum": [1, 2, 4],
                "description": "The day on which to swap.",
            },
            "reason": {
                "type": "string",
                "description": "Optional short reason from the user.",
            },
        },
        "required": ["other_player_discord_id", "day"],
    },
}

LOCKED_TOOLS = [
    MOVE_SLOT, DROP_SLOT, WIDEN_AVAILABILITY, SWAP, _QUERY, _GREET, _OUT_OF_SCOPE, _CLARIFY,
]


# ─── Dispatch helper ─────────────────────────────────────────────


def tools_for_phase(phase_value: str) -> list[dict]:
    """Pick the right tool list given an Event.phase value string."""
    if phase_value == "collecting":
        return COLLECTING_TOOLS
    if phase_value == "locked":
        return LOCKED_TOOLS
    # ARCHIVED or unknown — return an empty list; the agent should refuse
    # to act on archived events at a higher level.
    return []
