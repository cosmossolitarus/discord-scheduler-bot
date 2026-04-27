"""
Database models.
"""

from datetime import datetime, timezone
from enum import Enum as PyEnum

from sqlalchemy import (
    String, Integer, Float, Boolean, DateTime, Text, JSON,
    ForeignKey, UniqueConstraint, Enum, BigInteger,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from bot.database import Base


class EventPhase(str, PyEnum):
    COLLECTING = "collecting"
    LOCKED = "locked"
    ACTIVE = "active"
    ARCHIVED = "archived"


class AssignmentStatus(str, PyEnum):
    ASSIGNED = "assigned"
    PENDING_CHANGE = "pending_change"
    UNASSIGNED = "unassigned"


class ChangeStatus(str, PyEnum):
    PENDING_CONFIRMATION = "pending_confirmation"
    PENDING_ADMIN = "pending_admin"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"


class ChangeType(str, PyEnum):
    UPDATE = "update"
    SWAP = "swap"
    ADMIN_OVERRIDE = "admin_override"
    AUTO_BUMP_FLAG = "auto_bump_flag"


class Event(Base):
    __tablename__ = "events"

    event_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    day1_date: Mapped[datetime] = mapped_column(DateTime(timezone=True), unique=True, nullable=False)
    phase: Mapped[str] = mapped_column(
        Enum(EventPhase), default=EventPhase.COLLECTING, nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )

    submissions: Mapped[list["Submission"]] = relationship(back_populates="event", cascade="all, delete-orphan")
    slots: Mapped[list["Slot"]] = relationship(back_populates="event", cascade="all, delete-orphan")
    assignments: Mapped[list["Assignment"]] = relationship(back_populates="event", cascade="all, delete-orphan")
    change_requests: Mapped[list["ChangeRequest"]] = relationship(back_populates="event", cascade="all, delete-orphan")
    audit_logs: Mapped[list["AuditLog"]] = relationship(back_populates="event", cascade="all, delete-orphan")


class Submission(Base):
    __tablename__ = "submissions"

    submission_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_id: Mapped[int] = mapped_column(Integer, ForeignKey("events.event_id"), nullable=False)
    discord_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    discord_name: Mapped[str] = mapped_column(String(100), nullable=False)

    resource_x: Mapped[float | None] = mapped_column(Float, nullable=True)
    resource_y: Mapped[float | None] = mapped_column(Float, nullable=True)
    resource_z: Mapped[float | None] = mapped_column(Float, nullable=True)
    resource_generic: Mapped[float | None] = mapped_column(Float, nullable=True)

    priority_x: Mapped[float | None] = mapped_column(Float, nullable=True)
    priority_y: Mapped[float | None] = mapped_column(Float, nullable=True)
    priority_z: Mapped[float | None] = mapped_column(Float, nullable=True)

    availability: Mapped[dict | None] = mapped_column(JSON, nullable=True)

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

    def compute_priorities(self, generic_split: int = 3):
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
    assignment: Mapped["Assignment | None"] = relationship(back_populates="slot", uselist=False)


class Assignment(Base):
    __tablename__ = "assignments"

    assignment_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_id: Mapped[int] = mapped_column(Integer, ForeignKey("events.event_id"), nullable=False)
    slot_id: Mapped[str] = mapped_column(String(20), ForeignKey("slots.slot_id"), nullable=False)
    discord_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    status: Mapped[str] = mapped_column(
        Enum(AssignmentStatus), default=AssignmentStatus.ASSIGNED, nullable=False
    )
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

    change_type: Mapped[str] = mapped_column(Enum(ChangeType), nullable=False)
    status: Mapped[str] = mapped_column(
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
