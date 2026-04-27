"""
Cycle timing calculations.

All event dates derive from a single anchor (Day 1 of a known cycle).
Cycles repeat every 28 days.

Terminology:
    Day 1  = first game day (0:00 UTC). This is the date stored/displayed.
    Day 0  = lock day. 24 hours before Day 1.
    Day -4 = submissions open. 5 days before Day 1.
    Day 7  = archive. 6 days after Day 1.
"""

from datetime import datetime, timezone, timedelta
from enum import Enum
from bot.config import (
    ANCHOR_DAY1,
    CYCLE_LENGTH_DAYS,
    SUBMISSIONS_OPEN_OFFSET,
    LOCK_OFFSET,
    ARCHIVE_OFFSET,
)


class Phase(str, Enum):
    IDLE = "idle"
    COLLECTING = "collecting"
    LOCKED = "locked"
    ACTIVE = "active"
    ARCHIVED = "archived"


def get_current_cycle_day1(now: datetime | None = None) -> datetime:
    """
    Return the Day 1 datetime for the cycle that 'now' falls within.
    If 'now' is between cycles (post-archive, pre-next-submissions),
    returns the NEXT upcoming Day 1.
    """
    if now is None:
        now = datetime.now(timezone.utc)

    delta = (now - ANCHOR_DAY1).total_seconds() / 86400
    cycle_num = int(delta // CYCLE_LENGTH_DAYS)

    candidate = ANCHOR_DAY1 + timedelta(days=cycle_num * CYCLE_LENGTH_DAYS)

    archive_time = candidate + ARCHIVE_OFFSET
    if now >= archive_time:
        candidate += timedelta(days=CYCLE_LENGTH_DAYS)

    return candidate


def get_cycle_dates(day1: datetime) -> dict[str, datetime]:
    """Return all key timestamps for a cycle given its Day 1."""
    return {
        "submissions_open": day1 + SUBMISSIONS_OPEN_OFFSET,
        "lock": day1 + LOCK_OFFSET,
        "day1_start": day1 + timedelta(hours=-0.25),
        "day2_start": day1 + timedelta(days=1, hours=-0.25),
        "day4_start": day1 + timedelta(days=3, hours=-0.25),
        "day1_end": day1 + timedelta(days=1, minutes=15),
        "day2_end": day1 + timedelta(days=2, minutes=15),
        "day4_end": day1 + timedelta(days=4, minutes=15),
        "archive": day1 + ARCHIVE_OFFSET,
    }


def get_current_phase(now: datetime | None = None) -> tuple[Phase, datetime]:
    """Return the current phase and the relevant Day 1."""
    if now is None:
        now = datetime.now(timezone.utc)

    day1 = get_current_cycle_day1(now)
    dates = get_cycle_dates(day1)

    if now < dates["submissions_open"]:
        return Phase.IDLE, day1
    elif now < dates["lock"]:
        return Phase.COLLECTING, day1
    elif now < dates["day1_start"]:
        return Phase.LOCKED, day1
    elif now < dates["day4_end"]:
        return Phase.ACTIVE, day1
    elif now < dates["archive"]:
        return Phase.ACTIVE, day1
    else:
        return Phase.IDLE, day1


def generate_slot_times(day1: datetime) -> list[dict]:
    """
    Generate all 195 slot definitions for an event.

    Returns a list of dicts with keys:
        slot_id, day, track, slot_index, start_time, end_time
    """
    slots = []
    slot_duration = timedelta(minutes=30)

    # Day 1 CM: 49 blocks starting at 23:45 Day 0
    d1_start = day1 - timedelta(minutes=15)
    for i in range(49):
        start = d1_start + i * slot_duration
        end = start + slot_duration
        slots.append({
            "slot_id": f"D1-CM-{i+1:02d}",
            "day": 1,
            "track": "CM",
            "slot_index": i + 1,
            "start_time": start,
            "end_time": end,
        })

    # Day 2 CM: 48 blocks starting at 0:15 Day 2
    d2_start = d1_start + 49 * slot_duration
    for i in range(48):
        start = d2_start + i * slot_duration
        end = start + slot_duration
        slots.append({
            "slot_id": f"D2-CM-{i+1:02d}",
            "day": 2,
            "track": "CM",
            "slot_index": i + 1,
            "start_time": start,
            "end_time": end,
        })

    # Day 4 NA: 49 blocks starting at 23:45 Day 3
    d4_start = day1 + timedelta(days=3) - timedelta(minutes=15)
    for i in range(49):
        start = d4_start + i * slot_duration
        end = start + slot_duration
        slots.append({
            "slot_id": f"D4-NA-{i+1:02d}",
            "day": 4,
            "track": "NA",
            "slot_index": i + 1,
            "start_time": start,
            "end_time": end,
        })

    # Day 4 CM: 49 blocks, same times as NA
    for i in range(49):
        start = d4_start + i * slot_duration
        end = start + slot_duration
        slots.append({
            "slot_id": f"D4-CM-{i+1:02d}",
            "day": 4,
            "track": "CM",
            "slot_index": i + 1,
            "start_time": start,
            "end_time": end,
        })

    return slots
