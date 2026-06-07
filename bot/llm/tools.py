"""
Anthropic tool schemas for the action pattern.

Three tool sets:
  COLLECTING_TOOLS — pre-lock (COLLECTING phase)
  REVIEWING_TOOLS  — player-facing during LOCKED (schedule being finalized); minimal
  PUBLISHED_TOOLS  — post-publish (PUBLISHED phase); full change-request set

tools_for_phase() maps EventPhase values to the right set.
"""


# ─── Shared action shapes ────────────────────────────────────────


_QUERY = {
    "name": "query",
    "description": (
        "Use when the user is asking about their own current state — their "
        "assignments, availability, Speedups, resources, or how the bot works."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "subject": {
                "type": "string",
                "description": "Short label of what was asked (e.g. 'my times', 'my Speedups', 'how do I update').",
            },
        },
        "required": ["subject"],
    },
}

_OUT_OF_SCOPE = {
    "name": "out_of_scope",
    "description": (
        "Use when the user is asking about something the bot doesn't handle: "
        "Day 3 or Day 5 scheduling, other players' data, or truly unrelated chatter."
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
        "Use when the user's request is ambiguous and you cannot reliably act on it."
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
        "for help, or sent an opener without a specific scheduling request."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "kind": {
                "type": "string",
                "enum": ["greeting", "help_request", "unclear_opener"],
            },
        },
        "required": ["kind"],
    },
}

_WINDOWS_ARRAY = {
    "type": "array",
    "description": (
        "List of time windows the user is available during, in UTC. "
        "Empty list means 'not available that day'."
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


# ─── Pre-lock toolset (COLLECTING) ───────────────────────────────


SET_AVAILABILITY = {
    "name": "set_availability",
    "description": (
        "Use when the user is providing or updating their available times "
        "for ONE day. Only call for Day 1, 2, or 4. Repeat for multiple days. "
        "Empty `windows` means 'not available that day'."
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

SET_PLAYER_ID = {
    "name": "set_player_id",
    "description": (
        "Use when the user provides their in-game player ID — a numeric ID "
        "usually 8–10 digits long (e.g. 'my ID is 12345678', 'player ID: 987654321'). "
        "Do NOT call this for Discord IDs or other non-game IDs."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "player_id": {
                "type": "string",
                "description": "The raw in-game player ID string as provided (digits only).",
            },
        },
        "required": ["player_id"],
    },
}

SET_RESOURCES = {
    "name": "set_resources",
    "description": (
        "Use when the user reports their premium resource counts: "
        "Tempered Truegold (TTG / refined TG / refined truegold), "
        "Truegold (TG), and/or Truegold Dust (dust / TG dust). "
        "If the user says they have NONE or ZERO of all resources, set ttg=0, tg=0, dust=0. "
        "Only set the fields the user actually mentioned; omit the rest. "
        "These affect Day 1 (TTG, TG) and Day 2 (Dust) scheduling priority."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "ttg": {
                "type": "number",
                "description": "Tempered Truegold count (TTG / refined truegold).",
            },
            "tg": {
                "type": "number",
                "description": "Truegold count (TG).",
            },
            "dust": {
                "type": "number",
                "description": "Truegold Dust count (dust / TG dust).",
            },
        },
    },
}

COLLECTING_TOOLS = [SET_AVAILABILITY, SET_PLAYER_ID, SET_RESOURCES, _QUERY, _GREET, _OUT_OF_SCOPE, _CLARIFY]


# ─── Post-publish toolset (PUBLISHED) ────────────────────────────


MOVE_SLOT = {
    "name": "move_slot",
    "description": (
        "Use when the user wants to change the START TIME of one of their "
        "current assignments. Supply an absolute UTC start time (HH:MM). "
        "Requires admin approval."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "day": {
                "type": "integer",
                "enum": [1, 2, 4],
            },
            "new_start_utc": {
                "type": "string",
                "description": "Desired new slot start time in HH:MM UTC.",
            },
            "reason": {
                "type": "string",
                "description": "Optional short reason from the user.",
            },
        },
        "required": ["day", "new_start_utc"],
    },
}

DROP_SLOT = {
    "name": "drop_slot",
    "description": (
        "Use when the user wants to give up one of their current assignments. "
        "Requires admin approval."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "day": {
                "type": "integer",
                "enum": [1, 2, 4],
            },
            "reason": {"type": "string"},
        },
        "required": ["day"],
    },
}

WIDEN_AVAILABILITY = {
    "name": "widen_availability",
    "description": (
        "Use when the user is offering ADDITIONAL availability on top of what "
        "they already gave. Does not change current assignments — just widens "
        "the pool for admin reassignment. Applied immediately, no admin approval."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "day": {"type": "integer", "enum": [1, 2, 4]},
            "windows": _WINDOWS_ARRAY,
        },
        "required": ["day", "windows"],
    },
}

SWAP = {
    "name": "swap",
    "description": (
        "Use when the user wants to trade one of their slots for another "
        "player's slot on the same day. The user must @mention the target; "
        "only choose an id from VALID SWAP PARTNERS in the state. "
        "Requires both players to confirm and admin to approve."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "other_player_discord_id": {
                "type": "integer",
                "description": "Discord ID from VALID SWAP PARTNERS in the state.",
            },
            "day": {"type": "integer", "enum": [1, 2, 4]},
            "reason": {"type": "string"},
        },
        "required": ["other_player_discord_id", "day"],
    },
}

REQUEST_NEW_SLOT = {
    "name": "request_new_slot",
    "description": (
        "Use POST-PUBLISH when the user wants to be ADDED to a slot on a day "
        "they do NOT currently have an assignment for. If they already have an "
        "assignment on that day and want a different time, use move_slot instead. "
        "Requires admin approval."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "day": {"type": "integer", "enum": [1, 2, 4]},
            "new_start_utc": {
                "type": "string",
                "description": "Desired slot start in HH:MM UTC. Use a :15 or :45 boundary.",
            },
            "track": {
                "type": "string",
                "enum": ["NA", "CM"],
                "description": "Required for Day 4. NA = Noble Advisor, CM = Chief Minister.",
            },
            "reason": {"type": "string"},
        },
        "required": ["day", "new_start_utc"],
    },
}

PUBLISHED_TOOLS = [
    MOVE_SLOT, DROP_SLOT, REQUEST_NEW_SLOT, WIDEN_AVAILABILITY, SWAP,
    _QUERY, _GREET, _OUT_OF_SCOPE, _CLARIFY,
]

# Minimal tools while schedule is in LOCKED (admin review) — players can query
# their submission but can't request changes yet.
LOCKED_REVIEW_TOOLS = [_QUERY, _GREET, _OUT_OF_SCOPE, _CLARIFY]


# ─── Dispatch helper ─────────────────────────────────────────────


def tools_for_phase(phase_value: str) -> list[dict]:
    """Pick the right tool list given an Event.phase value string."""
    if phase_value == "collecting":
        return COLLECTING_TOOLS
    if phase_value == "locked":
        return LOCKED_REVIEW_TOOLS
    if phase_value == "published":
        return PUBLISHED_TOOLS
    return []
