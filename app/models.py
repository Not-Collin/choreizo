"""SQLAlchemy ORM models for Choreizo."""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Users + invites
# ---------------------------------------------------------------------------


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    telegram_chat_id: Mapped[int | None] = mapped_column(Integer, unique=True)
    telegram_username: Mapped[str | None] = mapped_column(String(64))
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_escalation: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    send_time: Mapped[str] = mapped_column(String(5), default="08:00", nullable=False)
    password_hash: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )

    eligibilities: Mapped[list[ChoreEligibility]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    assignments: Mapped[list[Assignment]] = relationship(
        back_populates="user", foreign_keys="Assignment.user_id"
    )
    chores_created: Mapped[list[Chore]] = relationship(
        back_populates="created_by", foreign_keys="Chore.created_by_user_id"
    )
    invite_codes_used: Mapped[list[InviteCode]] = relationship(
        back_populates="used_by_user", foreign_keys="InviteCode.used_by_user_id"
    )

    __table_args__ = (
        CheckConstraint(
            "send_time GLOB '[0-2][0-9]:[0-5][0-9]'", name="ck_users_send_time_format"
        ),
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<User id={self.id} name={self.name!r}>"


class InviteCode(Base):
    __tablename__ = "invite_codes"

    code: Mapped[str] = mapped_column(String(32), primary_key=True)
    intended_name: Mapped[str | None] = mapped_column(String(100))
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    used_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )

    used_by_user: Mapped[User | None] = relationship(
        back_populates="invite_codes_used", foreign_keys=[used_by_user_id]
    )

    @property
    def is_expired(self) -> bool:
        """True iff the code has an expiry that is already in the past."""
        if self.expires_at is None:
            return False
        exp = self.expires_at
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=timezone.utc)
        return exp < datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Chores + eligibility
# ---------------------------------------------------------------------------


class Chore(Base):
    __tablename__ = "chores"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    frequency_days: Mapped[int] = mapped_column(Integer, nullable=False)
    priority: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    estimated_minutes: Mapped[int | None] = mapped_column(Integer)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )

    created_by: Mapped[User | None] = relationship(
        back_populates="chores_created", foreign_keys=[created_by_user_id]
    )
    eligibilities: Mapped[list[ChoreEligibility]] = relationship(
        back_populates="chore", cascade="all, delete-orphan"
    )
    assignments: Mapped[list[Assignment]] = relationship(back_populates="chore")

    __table_args__ = (
        CheckConstraint("frequency_days > 0", name="ck_chores_frequency_positive"),
        CheckConstraint("priority IN (0, 1)", name="ck_chores_priority_valid"),
        Index("ix_chores_enabled", "enabled"),
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Chore id={self.id} name={self.name!r} freq={self.frequency_days}d>"


class ChoreEligibility(Base):
    __tablename__ = "chore_eligibility"

    chore_id: Mapped[int] = mapped_column(
        ForeignKey("chores.id", ondelete="CASCADE"), primary_key=True
    )
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    mode: Mapped[str] = mapped_column(String(8), nullable=False)

    chore: Mapped[Chore] = relationship(back_populates="eligibilities")
    user: Mapped[User] = relationship(back_populates="eligibilities")

    __table_args__ = (
        CheckConstraint("mode IN ('allow', 'deny')", name="ck_eligibility_mode_valid"),
    )


# ---------------------------------------------------------------------------
# Assignments + reminders
# ---------------------------------------------------------------------------


class Assignment(Base):
    __tablename__ = "assignments"

    id: Mapped[int] = mapped_column(primary_key=True)
    chore_id: Mapped[int] = mapped_column(
        ForeignKey("chores.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    assigned_date: Mapped[str] = mapped_column(String(10), nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="pending", nullable=False)
    assigned_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    responded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    escalated_from_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    rolled_over_from_assignment_id: Mapped[int | None] = mapped_column(
        ForeignKey("assignments.id")
    )
    notes: Mapped[str | None] = mapped_column(Text)

    chore: Mapped[Chore] = relationship(back_populates="assignments")
    user: Mapped[User] = relationship(back_populates="assignments", foreign_keys=[user_id])
    escalated_from: Mapped[User | None] = relationship(foreign_keys=[escalated_from_user_id])
    rolled_over_from: Mapped[Assignment | None] = relationship(
        remote_side="Assignment.id", foreign_keys=[rolled_over_from_assignment_id]
    )
    reminders: Mapped[list[ReminderEvent]] = relationship(
        back_populates="assignment", cascade="all, delete-orphan"
    )

    __table_args__ = (
        CheckConstraint(
            "status IN ('pending','completed','skipped','ignored','overdue','escalated')",
            name="ck_assignments_status_valid",
        ),
        Index("ix_assignments_user_date", "user_id", "assigned_date"),
        Index("ix_assignments_chore_date", "chore_id", "assigned_date"),
        Index("ix_assignments_status", "status"),
    )

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"<Assignment id={self.id} chore={self.chore_id} "
            f"user={self.user_id} date={self.assigned_date} status={self.status}>"
        )


class ReminderEvent(Base):
    __tablename__ = "reminder_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    assignment_id: Mapped[int] = mapped_column(
        ForeignKey("assignments.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    sent_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )

    assignment: Mapped[Assignment] = relationship(back_populates="reminders")

    __table_args__ = (
        CheckConstraint(
            "kind IN ('daily_send','hourly_reminder','escalation','admin_notify')",
            name="ck_reminders_kind_valid",
        ),
        Index("ix_reminders_assignment", "assignment_id"),
    )


# ---------------------------------------------------------------------------
# Magic links + key/value settings
# ---------------------------------------------------------------------------


class MagicLinkToken(Base):
    __tablename__ = "magic_link_tokens"

    token: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (Index("ix_magic_link_expires", "expires_at"),)


class AppSetting(Base):
    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False
    )


__all__ = [
    "Base",
    "User",
    "InviteCode",
    "Chore",
    "ChoreEligibility",
    "Assignment",
    "ReminderEvent",
    "MagicLinkToken",
    "AppSetting",
]
