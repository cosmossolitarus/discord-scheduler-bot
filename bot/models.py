"""
Database models.

Phase 1 cleanup applied:
  - Event gains is_test, locked_at, archived_at.
  - EventPhase.ACTIVE removed (was never written; "active" is computed at
    display time as 'phase=LOCKED AND now >= day1').
  - AssignmentStatus enum and column removed (only ASSIGNED was ever set,
    so the row's existence already carries the same information).
  - Submission.availability typed as list (it stores a list of slot IDs).

ChangeRequest is left as-is; Phase 2's action-pattern rewrite will replace
ChangeType and reshape ChangeRequest.details.
"""

from datetime import datetime, timezone
from enum import Enum as PyEnum

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from bot.database import Base


# ─── Enums ───────────────────────────────────────────────────────


class EventPhase(str, PyEnum):
    """Lifecycle phases for an Event row.

    Real-event transitions (driven by the lifecycle loop):
        (new)       → COLLECTING   when submissions window opens
        COLLECTING  → LOCKED       when optimizer runs and schedule is released
        LOCKED      → ARCHIVED     after the event days complete

    Test events (is_test=True) stay in COLLECTING (or LOCKED if manually
    locked) until an admin runs /schedule reset, which deletes them. They
    never reach ARCHIVED.
    """
    COLLECTING = "collecting"
    LOCKED = "locked"
    ARCHIVED = "archived"


class ChangeStatus(str, PyEnum):
    PENDING_CONFIRMATION = "pending_confirmation"
    PENDING_ADMIN = "pending_admin"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"


class ChangeType(str, PyEnum):
    """Categories of post-lock changes. Phase 2 will likely reshape this to
    match the action-pattern verbs (move_slot, drop_slot, etc.)."""
    UPDATE = "update"
    SWAP = "swap"
    ADMIN_OVERRIDE = "admin_override"
    AUTO_BUMP_FLAG = "auto_bump_flag"


# ─── Models ──────────────────────────────────────────────────────


class Event(Base):
    __tablename__ = "events"

    event_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    day1_date: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), unique=True, nullable=False
    )
    phase: Mapped[EventPhase] = mapped_column(
        Enum(EventPhase), default=EventPhase.COLLECTING, nullable=False
    )
    is_test: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    locked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    archived_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    submissions: Mapped[list["Submission"]] = relationship(
        back_populates="event", cascade="all, delete-orphan"
    )
    slots: Mapped[list["Slot"]] = relationship(
        back_populates="event", cascade="all, delete-orphan"
    )
    assignments: Mapped[list["Assignment"]] = relationship(
        back_populates="event", cascade="all, delete-orphan"
    )
    change_requests: Mapped[list["ChangeRequest"]] = relationship(
        back_populates="event", cascade="all, delete-orphan"
    )
    audit_logs: Mapped[list["AuditLog"]] = relationship(
        back_populates="event", cascade="all, delete-orphan"
    )
    sent_reminders: Mapped[list["SentReminder"]] = relationship(
        back_populates="event", cascade="all, delete-orphan"
    )


class Submission(Base):
    __tablename__ = "submissions"

    submission_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_id: Mapped[int] = mapped_column(Integer, ForeignKey("events.event_id"), nullable=False)
    discord_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    discord_name: Mapped[str] = mapped_column(String(100), nullable=False)

    # Raw resource numbers from the screenshot (x = construction, y = research,
    # z = troops, generic = wildcard split across the three at GENERIC_SPLIT).
    resource_x: Mapped[float | None] = mapped_column(Float, nullable=True)
    resource_y: Mapped[float | None] = mapped_column(Float, nullable=True)
    resource_z: Mapped[float | None] = mapped_column(Float, nullable=True)
    resource_generic: Mapped[float | None] = mapped_column(Float, nullable=True)

    # Resource value + share of generic, used by the optimizer.
    priority_x: Mapped[float | None] = mapped_column(Float, nullable=True)
    priority_y: Mapped[float | None] = mapped_column(Float, nullable=True)
    priority_z: Mapped[float | None] = mapped_column(Float, nullable=True)

    # Stored as a list of slot IDs the user is available for, e.g.
    # ["D1-CM-12", "D1-CM-13", ...].
    availability: Mapped[list | None] = mapped_column(JSON, nullable=True)

    screenshot_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    raw_availability_text: Mapped[str | None] = mapped_column(Text, nullable=True)

    has_screenshot: Mapped[bool] = mapped_column(Boolean, default=False)
    has_availability: Mapped[bool] = mapped_column(Boolean, default=False)

    submitted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    __table_args__ = (
        UniqueConstraint("event_id", "discord_id", name="uq_submission_user_event"),
    )

    event: Mapped["Event"] = relationship(back_populates="submissions")

    @property
    def is_complete(self) -> bool:
        return self.has_screenshot and self.has_availability

    def compute_priorities(self, generic_split: int = 3) -> None:
        """Distribute the generic resource pool across x/y/z."""
        generic_share = (self.resource_generic or 0) / generic_split
        self.priority_x = (self.resource_x or 0) + generic_share
        self.priority_y = (self.resource_y or 0) + generic_share
        self.priority_z = (self.resource_z or 0) + generic_share


class Slot(Base):
    __tablename__ = "slots"

    slot_id: Mapped[str] = mapped_column(String(20), primary_key=True)
    event_id: Mapped[int] = mapped_column(Integer, ForeignKey("events.event_id"), nullable=False)
    day: Mapped[int] = mapped_column(Integer, nullable=False)
    track: Mapped[str] = mapped_column(String(5), nullable=False)
    slot_index: Mapped[int] = mapped_column(Integer, nullable=False)
    start_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    end_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    event: Mapped["Event"] = relationship(back_populates="slots")
    assignment: Mapped["Assignment | None"] = relationship(
        back_populates="slot", uselist=False
    )


class Assignment(Base):
    __tablename__ = "assignments"

    assignment_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_id: Mapped[int] = mapped_column(Integer, ForeignKey("events.event_id"), nullable=False)
    slot_id: Mapped[str] = mapped_column(
        String(20), ForeignKey("slots.slot_id"), nullable=False
    )
    discord_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    assigned_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )

    __table_args__ = (
        UniqueConstraint("event_id", "slot_id", name="uq_assignment_slot"),
    )

    event: Mapped["Event"] = relationship(back_populates="assignments")
    slot: Mapped["Slot"] = relationship(back_populates="assignment")


class ChangeRequest(Base):
    __tablename__ = "change_requests"

    change_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_id: Mapped[int] = mapped_column(Integer, ForeignKey("events.event_id"), nullable=False)
    requested_by: Mapped[int] = mapped_column(BigInteger, nullable=False)

    change_type: Mapped[ChangeType] = mapped_column(Enum(ChangeType), nullable=False)
    status: Mapped[ChangeStatus] = mapped_column(
        Enum(ChangeStatus), default=ChangeStatus.PENDING_ADMIN, nullable=False
    )

    details: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    reason_for_user: Mapped[str | None] = mapped_column(Text, nullable=True)

    approval_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    swap_confirm_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    user_deadline: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    admin_deadline: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    resolved_by: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    event: Mapped["Event"] = relationship(back_populates="change_requests")


class AuditLog(Base):
    __tablename__ = "audit_log"

    log_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_id: Mapped[int] = mapped_column(Integer, ForeignKey("events.event_id"), nullable=False)
    timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    action: Mapped[str] = mapped_column(String(200), nullable=False)
    actor: Mapped[str] = mapped_column(String(100), nullable=False)
    details: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    event: Mapped["Event"] = relationship(back_populates="audit_logs")


class SentReminder(Base):
    """One row per reminder we've already delivered, keyed by (event, kind, key).

    Phase 3 replacement for the in-memory `_sent_reminders` set in the
    Reminders cog. Persisting these means a bot restart doesn't re-send
    daily reminders or personal 15-minute-warning DMs.

    `kind` values currently in use:
        "daily"    — daily channel announcement for game day N. key = str(N).
        "personal" — 15-minute DM warning. key = slot_id (e.g. "D2-CM-12").

    Rows cascade-delete with the parent Event, so /schedule reset wipes them.
    """
    __tablename__ = "sent_reminders"

    sent_reminder_id: Mapped[int] = mapped_column(
        Integer, primary_key=True, autoincrement=True
    )
    event_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("events.event_id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    key: Mapped[str] = mapped_column(String(64), nullable=False)
    sent_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )

    __table_args__ = (
        UniqueConstraint("event_id", "kind", "key", name="uq_sent_reminder"),
    )

    event: Mapped["Event"] = relationship(back_populates="sent_reminders")
