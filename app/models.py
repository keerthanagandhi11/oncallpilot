"""Database tables: incidents, call attempts, notifications and the audit timeline."""

from __future__ import annotations

import enum
import uuid
from datetime import UTC, datetime

from sqlalchemy import JSON, DateTime, ForeignKey, Index, Integer, String, Text, TypeDecorator
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


def new_id() -> str:
    return uuid.uuid4().hex[:12]


class Status(enum.StrEnum):
    NEW = "new"  # created, not yet triaged or posted
    NOTIFIED = "notified"  # Teams alert sent, waiting before the first call
    CALLING = "calling"  # a call is ringing
    WAITING = "waiting"  # between calls
    ACKNOWLEDGED = "acknowledged"
    EXHAUSTED = "exhausted"  # everyone was tried, nobody answered
    RESOLVED = "resolved"  # job fixed (for example a successful rerun)
    CANCELLED = "cancelled"
    GROUPED = "grouped"  # attached to another active escalation for the same team


ACTIVE = (Status.NEW, Status.NOTIFIED, Status.CALLING, Status.WAITING)
TERMINAL = (Status.ACKNOWLEDGED, Status.EXHAUSTED, Status.RESOLVED, Status.CANCELLED, Status.GROUPED)


class UTCDateTime(TypeDecorator):
    """Timezone-aware UTC datetimes. SQLite drops the zone, so it is restored on read."""

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect):
        if value is not None and value.tzinfo is None:
            raise ValueError("naive datetime; use timezone-aware UTC")
        return value.astimezone(UTC) if value is not None else None

    def process_result_value(self, value: datetime | None, dialect):
        if value is not None and value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value


class Incident(Base):
    __tablename__ = "incidents"

    id: Mapped[str] = mapped_column(String(16), primary_key=True, default=new_id)
    dedupe_key: Mapped[str] = mapped_column(String(200), unique=True)
    source: Mapped[str] = mapped_column(String(20))  # uac_webhook | uac_poll | manual | screenshot | demo
    external_id: Mapped[str | None] = mapped_column(String(100))

    # The failure
    task_name: Mapped[str] = mapped_column(String(200), index=True)
    workflow_name: Mapped[str | None] = mapped_column(String(200))
    agent: Mapped[str | None] = mapped_column(String(200))
    business_service: Mapped[str | None] = mapped_column(String(200))
    uac_status: Mapped[str | None] = mapped_column(String(50))
    exit_code: Mapped[str | None] = mapped_column(String(20))
    error_message: Mapped[str | None] = mapped_column(Text)
    log_excerpt: Mapped[str | None] = mapped_column(Text)
    failed_at: Mapped[datetime] = mapped_column(UTCDateTime())

    # Routing and AI triage
    team_id: Mapped[str] = mapped_column(String(64), index=True)
    severity: Mapped[str] = mapped_column(String(4), default="P3")
    triage: Mapped[dict | None] = mapped_column(JSON)

    # Escalation state
    status: Mapped[str] = mapped_column(String(20), default=Status.NEW.value, index=True)
    policy: Mapped[dict] = mapped_column(JSON)  # snapshot at open time
    oncall: Mapped[dict | None] = mapped_column(JSON)  # snapshot at notify time
    level_index: Mapped[int] = mapped_column(Integer, default=0)  # next level to call
    attempt_no: Mapped[int] = mapped_column(Integer, default=1)  # next attempt at that level
    next_action_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    call_deadline: Mapped[datetime | None] = mapped_column(UTCDateTime())
    email_sent: Mapped[bool] = mapped_column(default=False)
    parent_id: Mapped[str | None] = mapped_column(ForeignKey("incidents.id"))
    servicenow_number: Mapped[str | None] = mapped_column(String(40))

    acknowledged_by: Mapped[str | None] = mapped_column(String(200))
    ack_channel: Mapped[str | None] = mapped_column(String(20))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime())
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime())
    closed_at: Mapped[datetime | None] = mapped_column(UTCDateTime())

    attempts: Mapped[list[CallAttempt]] = relationship(
        back_populates="incident", order_by="CallAttempt.started_at", cascade="all, delete-orphan"
    )
    events: Mapped[list[Event]] = relationship(
        back_populates="incident", order_by="Event.id", cascade="all, delete-orphan"
    )
    notifications: Mapped[list[Notification]] = relationship(
        back_populates="incident", order_by="Notification.id", cascade="all, delete-orphan"
    )

    __table_args__ = (Index("ix_incidents_due", "status", "next_action_at"),)

    @property
    def is_active(self) -> bool:
        return self.status in {s.value for s in ACTIVE}


class CallAttempt(Base):
    __tablename__ = "call_attempts"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    incident_id: Mapped[str] = mapped_column(ForeignKey("incidents.id", ondelete="CASCADE"), index=True)
    level_index: Mapped[int] = mapped_column(Integer)
    role: Mapped[str] = mapped_column(String(20))
    attempt_no: Mapped[int] = mapped_column(Integer)
    contact_name: Mapped[str] = mapped_column(String(200))
    phone_masked: Mapped[str] = mapped_column(String(40))
    provider_call_id: Mapped[str | None] = mapped_column(String(200), index=True)
    outcome: Mapped[str] = mapped_column(String(20), default="ringing")  # ringing|answered|no_answer|busy|failed
    detail: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(UTCDateTime())
    ended_at: Mapped[datetime | None] = mapped_column(UTCDateTime())

    incident: Mapped[Incident] = relationship(back_populates="attempts")


class Notification(Base):
    __tablename__ = "notifications"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    incident_id: Mapped[str] = mapped_column(ForeignKey("incidents.id", ondelete="CASCADE"), index=True)
    channel: Mapped[str] = mapped_column(String(20))  # teams | email
    kind: Mapped[str] = mapped_column(String(30))  # alert | ack | exhausted | grouped | resolved | dl
    target: Mapped[str] = mapped_column(String(300))
    subject: Mapped[str | None] = mapped_column(String(300))
    body: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(10))  # sent | failed
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime())

    incident: Mapped[Incident] = relationship(back_populates="notifications")


class Event(Base):
    __tablename__ = "events"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    incident_id: Mapped[str] = mapped_column(ForeignKey("incidents.id", ondelete="CASCADE"), index=True)
    at: Mapped[datetime] = mapped_column(UTCDateTime())
    type: Mapped[str] = mapped_column(String(40))
    message: Mapped[str] = mapped_column(Text)
    data: Mapped[dict | None] = mapped_column(JSON)

    incident: Mapped[Incident] = relationship(back_populates="events")
