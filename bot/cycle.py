"""
Cycle timing helpers.

The Event row in the database is the source of truth for what cycle exists
and what phase it's in. The functions here are pure date math used by the
lifecycle loop to decide WHEN to create or transition events. They do not
read from or write to the database.

Real cycles repeat every 28 days from the anchor. Test events have arbitrary
day1 dates and do not auto-transition — they are driven by admin commands.
"""

from datetime import datetime, timezone, timedelta

from bot.config import (
    ANCHOR_DAY1,
    CYCLE_LENGTH_DAYS,
    SUBMISSIONS_OPEN_OFFSET,
    LOCK_OFFSET,
    ARCHIVE_OFFSET,
)


# ─── Public API ──────────────────────────────────────────────────


def get_cycle_dates(day1: datetime) -> dict[str, datetime]:
    """Key timestamps for a cycle given its Day 1.

    day1_start, day2_start, day4_start: start of the first slot that "counts"
    for that day (Day 2's first slot of play is D1-CM-49, the boundary slot;
    that's why day2_start sits at 23:45 of Day 1).

    day1_end, day2_end, day4_end: end of the last slot of that day.
    """
    return {
        "submissions_open": day1 + SUBMISSIONS_OPEN_OFFSET,
        "lock": day1 + LOCK_OFFSET,
        "day1_start": day1 - timedelta(minutes=15),                          # 23:45 Day 0
        "day1_end":   day1 + timedelta(days=1) + timedelta(minutes=15),      # 00:15 Day 2
        "day2_start": day1 + timedelta(days=1) - timedelta(minutes=15),      # 23:45 Day 1 (boundary start)
        "day2_end":   day1 + timedelta(days=2) + timedelta(minutes=15),      # 00:15 Day 3
        "day4_start": day1 + timedelta(days=3) - timedelta(minutes=15),      # 23:45 Day 3
        "day4_end":   day1 + timedelta(days=4) + timedelta(minutes=15),      # 00:15 Day 5
        "archive":    day1 + ARCHIVE_OFFSET,
    }


def compute_active_cycle_day1(now: datetime | None = None) -> datetime | None:
    """Return the Day 1 of the cycle whose submissions_open..archive window
    contains `now`, or None if we are in the idle gap between cycles.

    This is used ONLY when the database has no non-archived event and the
    lifecycle loop needs to decide whether to create a new real event.

    The function never advances past archive into a future cycle's day1 unless
    that next cycle's submissions are actually open. That's the fix for the
    old gotcha where get_current_cycle_day1 would silently jump ahead.
    """
    if now is None:
        now = datetime.now(timezone.utc)

    delta_days = (now - ANCHOR_DAY1).total_seconds() / 86400
    cycle_num = int(delta_days // CYCLE_LENGTH_DAYS)

    # Check the current cycle (if past the anchor) and the next one.
    # No two cycle windows overlap so at most one of these can match.
    candidates_to_check = []
    if cycle_num >= 0:
        candidates_to_check.append(cycle_num)
    candidates_to_check.append(max(0, cycle_num + 1))

    for cn in candidates_to_check:
        day1 = ANCHOR_DAY1 + timedelta(days=cn * CYCLE_LENGTH_DAYS)
        open_time = day1 + SUBMISSIONS_OPEN_OFFSET
        archive_time = day1 + ARCHIVE_OFFSET
        if open_time <= now < archive_time:
            return day1

    return None


def should_lock(event_day1: datetime, now: datetime | None = None) -> bool:
    """True if an event in COLLECTING should transition to LOCKED."""
    if now is None:
        now = datetime.now(timezone.utc)
    return now >= event_day1 + LOCK_OFFSET


def should_archive(event_day1: datetime, now: datetime | None = None) -> bool:
    """True if an event in LOCKED should transition to ARCHIVED."""
    if now is None:
        now = datetime.now(timezone.utc)
    return now >= event_day1 + ARCHIVE_OFFSET


def generate_slot_times(day1: datetime) -> list[dict]:
    """Generate slot definitions for an event.

    Slot counts: 49 + 48 + 49 + 49 = 195 total.
      Day 1 CM: 49 slots. The last one (D1-CM-49, 23:45 Day 1 - 00:15 Day 2)
                is the BOUNDARY slot. The assignee uses Day 1 (construction)
                resources for the first 15 minutes and Day 2 (research)
                resources for the last 15 minutes.
      Day 2 CM: 48 slots starting at 00:15 Day 2 (no first slot — the boundary
                slot above covers the Day 2 start in-game).
      Day 4 NA: 49 slots starting at 23:45 Day 3. Priority track.
      Day 4 CM: 49 slots, same time windows as Day 4 NA.
    """
    slots = []
    slot_duration = timedelta(minutes=30)

    # Day 1 CM (49)
    d1_start = day1 - timedelta(minutes=15)
    for i in range(49):
        start = d1_start + i * slot_duration
        slots.append({
            "slot_id":    f"D1-CM-{i+1:02d}",
            "day":        1,
            "track":      "CM",
            "slot_index": i + 1,
            "start_time": start,
            "end_time":   start + slot_duration,
        })

    # Day 2 CM (48) — starts where D1-CM-49 ends
    d2_start = d1_start + 49 * slot_duration  # = day1 + 1 day + 15 min
    for i in range(48):
        start = d2_start + i * slot_duration
        slots.append({
            "slot_id":    f"D2-CM-{i+1:02d}",
            "day":        2,
            "track":      "CM",
            "slot_index": i + 1,
            "start_time": start,
            "end_time":   start + slot_duration,
        })

    # Day 4 NA + Day 4 CM (49 each, same time windows)
    d4_start = day1 + timedelta(days=3) - timedelta(minutes=15)
    for track in ("NA", "CM"):
        for i in range(49):
            start = d4_start + i * slot_duration
            slots.append({
                "slot_id":    f"D4-{track}-{i+1:02d}",
                "day":        4,
                "track":      track,
                "slot_index": i + 1,
                "start_time": start,
                "end_time":   start + slot_duration,
            })

    return slots


def is_boundary_slot(slot_id: str) -> bool:
    """True if this is the dual-resource boundary slot (D1-CM-49).

    The assignee gets a special notice on top of the normal schedule release
    DM and reminder, explaining that they use Day 1 (construction) resources
    for 23:45-00:00 Day 2 and Day 2 (research) resources for 00:00-00:15.
    """
    return slot_id == "D1-CM-49"
