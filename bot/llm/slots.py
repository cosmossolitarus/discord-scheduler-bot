"""
Slot derivation helpers.

The LLM emits day numbers and HH:MM time windows; this module turns them
into concrete slot IDs. Pure code, no LLM, no DB.
"""

from datetime import datetime, timedelta

from bot.cycle import generate_slot_times


def _day_anchor(day1: datetime, day: int) -> datetime:
    """The 00:00 UTC anchor for a given game day."""
    offsets = {1: 0, 2: 1, 4: 3}
    if day not in offsets:
        raise ValueError(f"Unknown game day {day} (tracked: 1, 2, 4)")
    return day1 + timedelta(days=offsets[day])


def _parse_hhmm(value: str) -> tuple[int, int]:
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

    If end <= start the window crosses midnight forward. Slots are included
    when their time range overlaps the window at all (generous matching).
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
        return []
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

    For Day 4, which has two parallel tracks (NA and CM) at every time window,
    both D4-NA-* and D4-CM-* slots are returned. This ensures players are
    eligible for optimizer assignment on both tracks; the optimizer's pass
    ordering (NA first, then CM with NA-winners excluded) handles deduplication.
    """
    seen: set[str] = set()
    for w in windows:
        try:
            ids = slots_in_window(day1, day, w["start_utc"], w["end_utc"])
        except (KeyError, ValueError):
            continue
        seen.update(ids)

    all_slots = slots_for_day(day1, day)
    return [s["slot_id"] for s in all_slots if s["slot_id"] in seen]


def find_slot_by_start(
    day1: datetime,
    day: int,
    start_hhmm: str,
    track: str | None = None,
) -> str | None:
    """The slot ID whose start_time matches HH:MM on `day`, or None.

    Day 1 and Day 4 have 23:45 appearing twice (once at the very start of
    the day's slot window, once near the end). When the input is 23:45,
    prefer the LATER occurrence — this matches the natural user intent of
    "23:45 Day 1" meaning the end-of-Day-1 boundary slot rather than the
    first slot of the range.

    For Day 4 (NA and CM tracks at every time), pass `track` to disambiguate.
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
    """Render a slot's time as 'HH:MM-HH:MM UTC'."""
    return (
        f"{slot['start_time'].strftime('%H:%M')}-"
        f"{slot['end_time'].strftime('%H:%M')} UTC"
    )


def _window_label(ws: datetime, we: datetime) -> str:
    """Format a time window for display.

    When the window crosses midnight (end date differs from start date),
    include the weekday+date so players can tell which calendar night it falls
    on. This is critical for Day 1 (slots start at 23:45 the night before) and
    Day 4 (same pattern, starts at 23:45 three nights before).
    """
    start_str = ws.strftime("%H:%M")
    end_str = we.strftime("%H:%M")

    if ws.date() == we.date():
        return f"{start_str}-{end_str} UTC"
    # Crosses midnight — show abbreviated weekday+date for clarity
    start_date = ws.strftime("%a %b %-d")
    end_date = we.strftime("%a %b %-d")
    return f"{start_date} {start_str} - {end_date} {end_str} UTC"


def summarize_availability(day1: datetime, slot_ids: list[str]) -> str:
    """One-line-per-day summary of the user's availability.

    Day 1 and Day 4 windows that cross midnight include calendar dates to
    disambiguate (e.g. "Sun May 17 23:45 - Mon May 18 00:15 UTC" vs
    "Mon May 18 00:15-00:45 UTC").
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

        # Day 4 has two tracks (NA and CM) at every time; dedupe by time range
        # for user-facing display.
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

        parts = [_window_label(ws, we) for ws, we in windows]
        lines.append(f"Day {day}: {', '.join(parts)}")

    return "\n".join(lines)
