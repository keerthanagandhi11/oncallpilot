"""Interfaces every integration implements, plus the plain data they exchange.

The escalation engine only talks to these interfaces, so each system (UAC, ServiceNow, Teams, voice,
email) can be swapped between a real adapter and a mock without touching the engine.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Protocol


@dataclass
class FailureEvent:
    """A job failure, normalised from whatever source reported it."""

    task_name: str
    failed_at: datetime
    source: str = "manual"
    external_id: str | None = None  # UAC task instance sys_id
    workflow_name: str | None = None
    agent: str | None = None
    business_service: str | None = None
    uac_status: str | None = "FAILED"
    exit_code: str | None = None
    error_message: str | None = None
    log_excerpt: str | None = None
    team_id: str | None = None  # operator may force a team
    servicenow_number: str | None = None  # if the source already knows the ServiceNow incident

    @property
    def dedupe_key(self) -> str:
        if self.external_id:
            return f"{self.source.split('_')[0]}:{self.external_id}"
        return f"{self.task_name}:{self.failed_at.isoformat()}"


@dataclass
class Contact:
    role: str
    name: str
    phone: str
    email: str | None = None

    def to_dict(self) -> dict:
        return {"role": self.role, "name": self.name, "phone": self.phone, "email": self.email}


@dataclass
class OnCall:
    group: str
    contacts: dict[str, Contact] = field(default_factory=dict)  # role -> contact

    def to_dict(self) -> dict:
        return {"group": self.group, "contacts": {r: c.to_dict() for r, c in self.contacts.items()}}


class CallStatus(StrEnum):
    RINGING = "ringing"
    ANSWERED = "answered"
    NO_ANSWER = "no_answer"
    BUSY = "busy"
    FAILED = "failed"


@dataclass
class ChatMessage:
    title: str
    text: str
    card: dict  # Adaptive Card JSON


class OnCallDirectory(Protocol):
    async def who_is_on_call(self, group: str) -> OnCall: ...


class ChatProvider(Protocol):
    async def post(self, webhook_url: str, message: ChatMessage) -> None: ...


class VoiceProvider(Protocol):
    async def place_call(self, contact: Contact, script: str, incident_id: str) -> str: ...
    async def get_status(self, call_id: str) -> CallStatus: ...
    async def hang_up(self, call_id: str) -> None: ...


class EmailProvider(Protocol):
    async def send(self, to: str, subject: str, text: str, html: str) -> None: ...


class IncidentDesk(Protocol):
    """The ticketing system (ServiceNow): find, create, transfer and annotate incidents."""

    async def find_incident(
        self, task_name: str, external_id: str | None, queue_group: str | None, hours: int = 12
    ) -> str | None: ...
    async def create_incident(self, group: str, short: str, description: str, severity: str) -> str: ...
    async def transfer(self, number: str, group: str, note: str) -> None: ...
    async def add_work_note(self, number: str, note: str) -> None: ...


class IntegrationError(Exception):
    """A call to an external system failed."""
