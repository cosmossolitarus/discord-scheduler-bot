"""
Slot derivation helpers.

The LLM emits day numbers and HH:MM time windows; this module turns them
into concrete slot IDs. Pure code, no LLM, no DB.

This replaces the old llm/availability.py indirection (LLM rewrites text
as an availability statement → parse that into slot IDs). Now the LLM
directly emits structured time windows and we derive slot IDs in code.
"""

from datetime import datetime, timedelta

from bot.cycle import generate_slot_times


def _day_anchor(day1: datetime, day: int) -> datetime:
    """The 00:00 UTC anchor for a given game day (Day 1, 2, or 4).

    Day 1's slots span 23:45 of the previous calendar day through 00:15 of
    the next; the anchor is the day1 date itself (which is also 00:00 UTC
    on the Day 1 calendar). Day 2's anchor is day1 + 1d, etc.
    """
    offsets = {1: 0, 2: 1, 4: 3}
    if day not in offsets:
        raise ValueError(f"Unknown game day {day} (tracked: 1, 2, 4)")
    return day1 + timedelta(days=offsets[day])


def _parse_hhmm(value: str) -> tuple[int, int]:
    """Parse 'HH:MM' (24-hour). Raises ValueError on bad input."""
    parts = value.strip().split(":")
    if len(parts) != 2:
        raise ValueError(f"Expected HH:MM, got {value!r}")
    h, m = int(parts[0]), int(parts[1])
    if not (0 <= h <= 23 and 0 <= m <= 59):
        raise ValueError(f"Out-of-range time {value!r}")
    return h, m


def slots_for_day(day1: datetime, day: int) -> list[dict]:
    """All slot definitions for one game day, in chronological order."""
    return [s for s in generate_slot_times(day1) if s["day"] == day]


def slots_in_window(
    day1: datetime,
    day: int,
    start_hhmm: str,
    end_hhmm: str,
) -> list[str]:
    """Slot IDs whose [start, end) overlaps a single window on `day`.

    The window is interpreted on the day's calendar anchor (see _day_anchor).
    If end <= start, the window is treated as crossing midnight forward.

    A slot is included whenever its time range overlaps the window at all —
    even partially — matching the "be generous" interpretation that the old
    availability prompt used to enforce.
    """
    slots = slots_for_day(day1, day)
    if not slots:
        return []

    anchor = _day_anchor(day1, day)
    sh, sm = _parse_hhmm(start_hhmm)
    eh, em = _parse_hhmm(end_hhmm)

    window_start = anchor.replace(hour=sh, minute=sm, second=0, microsecond=0)
    window_end = anchor.replace(hour=eh, minute=em, second=0, microsecond=0)
    if window_end == window_start:
        return []  # zero-length window — treat as malformed
    if window_end < window_start:
        window_end += timedelta(days=1)

    return [
        s["slot_id"]
        for s in slots
        if s["start_time"] < window_end and s["end_time"] > window_start
    ]


def slots_in_windows(
    day1: datetime,
    day: int,
    windows: list[dict],
) -> list[str]:
    """Union of slot IDs across multiple windows on the same day.

    `windows` is a list of {start_utc: "HH:MM", end_utc: "HH:MM"} dicts.
    Result is sorted by slot_index (ascending) and de-duplicated.
    """
    seen: set[str] = set()
    for w in windows:
        try:
            ids = slots_in_window(day1, day, w["start_utc"], w["end_utc"])
        except (KeyError, ValueError):
            continue  # skip malformed windows; agent reports
        seen.update(ids)

    # Sort by slot_index so output is stable
    all_slots = slots_for_day(day1, day)
    return [s["slot_id"] for s in all_slots if s["slot_id"] in seen]


def find_slot_by_start(
    day1: datetime,
    day: int,
    start_hhmm: str,
    track: str | None = None,
) -> str | None:
    """The slot ID whose start_time matches HH:MM on `day`, or None.

    Day 1 and Day 4 have 23:45 appearing twice in the slot list (once at the
    very start, once near the end). When the input is ambiguous, prefer the
    LATER occurrence — that matches the natural user reading of "23:45 on
    Day 1" as the end-of-Day-1 boundary slot rather than the first slot of
    the Day 1 range.

    For Day 4 (which has both NA and CM tracks at every time), pass `track`
    to disambiguate. Without it, returns one match arbitrarily (caller
    should pass the user's current track).
    """
    try:
        sh, sm = _parse_hhmm(start_hhmm)
    except ValueError:
        return None

    matches = [
        s for s in slots_for_day(day1, day)
        if s["start_time"].hour == sh
        and s["start_time"].minute == sm
        and (track is None or s["track"] == track)
    ]
    if not matches:
        return None
    return max(matches, key=lambda s: s["start_time"])["slot_id"]


def format_slot_time(slot: dict) -> str:
    """Render a slot's time as 'HH:MM-HH:MM UTC' for user-facing messages."""
    return (
        f"{slot['start_time'].strftime('%H:%M')}-"
        f"{slot['end_time'].strftime('%H:%M')} UTC"
    )


def summarize_availability(day1: datetime, slot_ids: list[str]) -> str:
    """One-line-per-day summary of the user's availability.

    Output looks like:
        Day 1: 14:00-18:00 UTC
        Day 2: 19:00-23:00 UTC, 02:00-04:00 UTC
        Day 4: not available

    Used in the state envelope so the LLM can echo back what we have on file
    when responding to queries or partial updates.
    """
    if not slot_ids:
        return "Day 1: not set\nDay 2: not set\nDay 4: not set"

    slot_set = set(slot_ids)
    lines = []

    for day in (1, 2, 4):
        day_slots = [
            s for s in slots_for_day(day1, day) if s["slot_id"] in slot_set
        ]
        if not day_slots:
            lines.append(f"Day {day}: not available")
            continue

        # Day 4 has two tracks (NA and CM) at every time. For user-facing
        # display, dedupe by (start_time, end_time) so the same window
        # doesn't appear twice — once from each track.
        seen_ranges: set[tuple[datetime, datetime]] = set()
        unique: list[dict] = []
        for s in day_slots:
            key = (s["start_time"], s["end_time"])
            if key in seen_ranges:
                continue
            seen_ranges.add(key)
            unique.append(s)
        unique.sort(key=lambda s: s["start_time"])

        # Group consecutive slots into windows
        windows: list[tuple[datetime, datetime]] = []
        cur_start = unique[0]["start_time"]
        cur_end = unique[0]["end_time"]
        for s in unique[1:]:
            if s["start_time"] == cur_end:
                cur_end = s["end_time"]
            else:
                windows.append((cur_start, cur_end))
                cur_start = s["start_time"]
                cur_end = s["end_time"]
        windows.append((cur_start, cur_end))

        parts = [
            f"{ws.strftime('%H:%M')}-{we.strftime('%H:%M')} UTC"
            for ws, we in windows
        ]
        lines.append(f"Day {day}: {', '.join(parts)}")

    return "\n".join(lines)
