"""
Event lifecycle helpers — creating events and transitioning phases.

This module contains the DB-touching counterparts to cycle.py's pure date
math. Channel announcements, optimizer runs, and player notifications live
in the cogs; this module only writes to the Event/Slot tables.
"""

from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from bot.cycle import generate_slot_times
from bot.models import Event, EventPhase, Slot


async def create_event(
    session: AsyncSession,
    day1: datetime,
    is_test: bool = False,
) -> Event:
    """Create a new Event in COLLECTING phase, with all 195 slot rows attached.

    Caller is responsible for committing the session and triggering any
    side-effects (e.g. announcing in #scheduling).
    """
    event = Event(
        day1_date=day1,
        phase=EventPhase.COLLECTING,
        is_test=is_test,
    )
    session.add(event)
    await session.flush()  # need event_id for slot foreign keys

    for sd in generate_slot_times(day1):
        session.add(Slot(
            slot_id=sd["slot_id"],
            event_id=event.event_id,
            day=sd["day"],
            track=sd["track"],
            slot_index=sd["slot_index"],
            start_time=sd["start_time"],
            end_time=sd["end_time"],
        ))

    return event


def mark_locked(event: Event, now: datetime | None = None) -> None:
    """In-memory transition: COLLECTING → LOCKED.

    Caller commits the session and is responsible for running the optimizer
    and posting the schedule.
    """
    if now is None:
        now = datetime.now(timezone.utc)
    event.phase = EventPhase.LOCKED
    event.locked_at = now


def mark_archived(event: Event, now: datetime | None = None) -> None:
    """In-memory transition: LOCKED → ARCHIVED.

    Caller commits the session and is responsible for posting the final
    CSV to #schedule_log.
    """
    if now is None:
        now = datetime.now(timezone.utc)
    event.phase = EventPhase.ARCHIVED
    event.archived_at = now
