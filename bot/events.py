"""
Event lifecycle helpers — creating events and transitioning phases.

All transitions are now admin-manual; no auto-transitions happen.
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
    """Create a new Event in COLLECTING phase, with all 195 slot rows attached."""
    event = Event(
        day1_date=day1,
        phase=EventPhase.COLLECTING,
        is_test=is_test,
    )
    session.add(event)
    await session.flush()

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
    """In-memory transition: COLLECTING → LOCKED."""
    if now is None:
        now = datetime.now(timezone.utc)
    event.phase = EventPhase.LOCKED
    event.locked_at = now


def mark_published(event: Event, now: datetime | None = None) -> None:
    """In-memory transition: LOCKED → PUBLISHED."""
    if now is None:
        now = datetime.now(timezone.utc)
    event.phase = EventPhase.PUBLISHED
    event.published_at = now
