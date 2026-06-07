"""
Database models.

Phase changes summary:
  - EventPhase gains PUBLISHED; ARCHIVED is removed.
    Lifecycle: COLLECTING → LOCKED (admin-private review) → PUBLISHED (players notified).
  - Submission gains ttg, tg, dust (premium resource counts) and
    player_ingame_id / has_player_id for the in-game numeric player ID.
  - Event gains published_at timestamp.
  - PlayerProfile (new) maps discord_id → ingame_player_id across events.
  - compute_priorities() updated: Day 1 and Day 2 use points
    (speedup minutes × 30 + premium-resource bonuses); Day 4 uses speedup days.
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

    Admin-driven transitions (no auto-transitions):
        (new)       → COLLECTING   admin opens submissions (/schedule create)
        COLLECTING  → LOCKED       admin locks (/schedule lock); optimizer runs;
                                   schedule is visible to admins only.
        LOCKED      → PUBLISHED    admin publishes (/schedule publish); player
                                   DMs sent; public change-request flow begins.
    """
    COLLECTING = "collecting"
    LOCKED = "locked"
    PUBLISHED = "published"


class ChangeStatus(str, PyEnum):
    PENDING_CONFIRMATION = "pending_confirmation"
    PENDING_ADMIN = "pending_admin"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"


class ChangeType(str, PyEnum):
    UPDATE = "update"
    SWAP = "swap"
    ADD = "add"
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
    published_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # archived_at kept in DB for existing rows; never written going forward.
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


class PlayerProfile(Base):
    """Persists the discord_id ↔ ingame_player_id mapping across events.

    When a player submits their in-game ID during COLLECTING, this row is
    upserted so the bot can pre-populate it for their next event without
    asking again. Admin-injected assignments never write here.
    """
    __tablename__ = "player_profiles"

    discord_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    ingame_player_id: Mapped[str] = mapped_column(String(20), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )


class Submission(Base):
    __tablename__ = "submissions"

    submission_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_id: Mapped[int] = mapped_column(Integer, ForeignKey("events.event_id"), nullable=False)
    discord_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    discord_name: Mapped[str] = mapped_column(String(100), nullable=False)

    # In-game numeric player ID (8–10 digits). Submitted via text.
    player_ingame_id: Mapped[str | None] = mapped_column(String(20), nullable=True)
    has_player_id: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    # Premium resource counts. has_resources becomes True only when all three
    # (ttg, tg, dust) have been explicitly set — even if the values are 0.
    has_resources: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    # Speedup durations in days (from screenshot).
    speedup_construction: Mapped[float | None] = mapped_column(Float, nullable=True)
    speedup_research: Mapped[float | None] = mapped_column(Float, nullable=True)
    speedup_training: Mapped[float | None] = mapped_column(Float, nullable=True)
    speedup_general: Mapped[float | None] = mapped_column(Float, nullable=True)

    # Premium resource counts (submitted via text).
    # TTG (Tempered Truegold) and TG (Truegold) boost Day 1 priority.
    # Dust (Truegold Dust) boosts Day 2 priority.
    ttg: Mapped[float | None] = mapped_column(Float, nullable=True)
    tg: Mapped[float | None] = mapped_column(Float, nullable=True)
    dust: Mapped[float | None] = mapped_column(Float, nullable=True)

    # Optimizer priority scores.
    #   priority_x — Day 1, in points  (speedup-mins×30 + TTG×30000 + TG×2000)
    #   priority_y — Day 2, in points  (speedup-mins×30 + Dust×1000)
    #   priority_z — Day 4, in speedup days (training + general/3)
    priority_x: Mapped[float | None] = mapped_column(Float, nullable=True)
    priority_y: Mapped[float | None] = mapped_column(Float, nullable=True)
    priority_z: Mapped[float | None] = mapped_column(Float, nullable=True)

    # List of slot IDs the user is available for.
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
        return self.has_screenshot and self.has_availability and self.has_player_id and self.has_resources

    def compute_priorities(self, generic_split: int = 3) -> None:
        """Compute optimizer priority scores from stored resource values.

        Day 1 (priority_x) and Day 2 (priority_y) are in points:
            speedup_minutes × 30  +  premium-resource bonuses
            (TTG = 30 000 pts, TG = 2 000 pts, Dust = 1 000 pts)

        General speedups are split equally across all three days before
        conversion, then added to each day's pool.

        Day 4 (priority_z) remains in speedup days (no premium resources).
        """
        general_share_days = (self.speedup_general or 0) / generic_split
        minutes_per_day = 1440

        # Day 1 points
        d1_speedup_min = ((self.speedup_construction or 0) + general_share_days) * minutes_per_day
        self.priority_x = (
            d1_speedup_min * 30
            + (self.ttg or 0) * 30_000
            + (self.tg or 0) * 2_000
        )

        # Day 2 points
        d2_speedup_min = ((self.speedup_research or 0) + general_share_days) * minutes_per_day
        self.priority_y = (
            d2_speedup_min * 30
            + (self.dust or 0) * 1_000
        )

        # Day 4 speedup days
        self.priority_z = (self.speedup_training or 0) + general_share_days


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
    """Deduplication table so reminders aren't re-sent after a bot restart.

    `kind` values:
        "daily"    — daily channel announcement for game day N. key = str(N).
        "personal" — 15-minute DM warning. key = slot_id (e.g. "D2-CM-12").
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
