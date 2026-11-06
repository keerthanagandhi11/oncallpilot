"""Mock adapters for demos and tests. They behave like the real systems but call nobody."""

from __future__ import annotations

import logging
import random
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

import yaml

from app.clock import Clock
from app.integrations.base import CallStatus, ChatMessage, Contact, IntegrationError, OnCall

log = logging.getLogger(__name__)


class MockOnCallDirectory:
    """Reads a YAML roster that stands in for ServiceNow's on-call schedule."""

    def __init__(self, path: str | Path):
        self.path = Path(path)

    async def who_is_on_call(self, group: str) -> OnCall:
        data = yaml.safe_load(self.path.read_text(encoding="utf-8")) or {}
        roster = (data.get("groups") or {}).get(group)
        if not roster:
            raise IntegrationError(f"no on-call roster for group '{group}'")
        contacts = {
            role: Contact(role=role, name=c["name"], phone=str(c["phone"]), email=c.get("email"))
            for role, c in roster.items()
            if c
        }
        return OnCall(group=group, contacts=contacts)


class MockChat:
    def __init__(self) -> None:
        self.sent: list[tuple[str, ChatMessage]] = []
        self.fail = False  # tests flip this to simulate an outage

    async def post(self, webhook_url: str, message: ChatMessage) -> None:
        if self.fail:
            raise IntegrationError("Teams webhook unavailable (simulated)")
        self.sent.append((webhook_url, message))
        log.info("mock teams post", extra={"title": message.title})


class MockEmail:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send(self, to: str, subject: str, text: str, html: str) -> None:
        self.sent.append({"to": to, "subject": subject, "text": text})
        log.info("mock email", extra={"to": to, "subject": subject})


@dataclass
class _MockCall:
    contact: Contact
    resolves_at: datetime
    outcome: CallStatus
    hung_up: bool = False


class MockVoice:
    """Pretends to ring a phone. After ``ring_seconds`` the call is answered or not.

    The outcome comes from, in order: a scripted list for that phone number (tests), a forced outcome
    from the dashboard's "simulate answer" button, or ``answer_probability``.
    """

    def __init__(self, clock: Clock, answer_probability: float = 0.0, ring_seconds: float = 6.0):
        self.clock = clock
        self.answer_probability = answer_probability
        self.ring_seconds = ring_seconds
        self.calls: dict[str, _MockCall] = {}
        self.scripts: dict[str, list[CallStatus]] = {}
        self.placed: list[Contact] = []

    async def place_call(self, contact: Contact, script: str, incident_id: str) -> str:
        if contact.phone in self.scripts and self.scripts[contact.phone]:
            outcome = self.scripts[contact.phone].pop(0)
        else:
            outcome = CallStatus.ANSWERED if random.random() < self.answer_probability else CallStatus.NO_ANSWER
        call_id = f"mock-{uuid.uuid4().hex[:10]}"
        self.calls[call_id] = _MockCall(contact, self.clock.now() + timedelta(seconds=self.ring_seconds), outcome)
        self.placed.append(contact)
        return call_id

    async def get_status(self, call_id: str) -> CallStatus:
        call = self.calls.get(call_id)
        if call is None:
            return CallStatus.FAILED
        if call.hung_up:
            return CallStatus.NO_ANSWER
        return call.outcome if self.clock.now() >= call.resolves_at else CallStatus.RINGING

    async def hang_up(self, call_id: str) -> None:
        if call_id in self.calls:
            self.calls[call_id].hung_up = True

    def force_answer(self, call_id: str) -> bool:
        call = self.calls.get(call_id)
        if call is None:
            return False
        call.outcome = CallStatus.ANSWERED
        call.resolves_at = self.clock.now()
        return True


class MockIncidentDesk:
    """Stands in for ServiceNow incidents. Pretends ServiceNow already raised an incident for each
    UAC failure (assigned to the SysOps queue), and records transfers and work notes."""

    def __init__(self, start: int = 4021001) -> None:
        self._next = start
        self.incidents: dict[str, dict] = {}  # number -> {task, external_id, group, notes}
        self.raise_for_failures = True  # tests set False to simulate "no incident in ServiceNow yet"

    def _new(self, task: str, external_id: str | None, group: str) -> str:
        number = f"INC{self._next:07d}"
        self._next += 1
        self.incidents[number] = {"task": task, "external_id": external_id, "group": group, "notes": []}
        return number

    async def find_incident(
        self, task_name: str, external_id: str | None, queue_group: str | None, hours: int = 12
    ) -> str | None:
        for number, inc in self.incidents.items():
            if (external_id and inc["external_id"] == external_id) or (not external_id and inc["task"] == task_name):
                return number
        if not self.raise_for_failures:
            return None
        return self._new(task_name, external_id, queue_group or "SysOps Batch Monitoring")

    async def create_incident(self, group: str, short: str, description: str, severity: str) -> str:
        return self._new(short, None, group)

    async def transfer(self, number: str, group: str, note: str) -> None:
        if number not in self.incidents:
            raise IntegrationError(f"incident {number} not found")
        self.incidents[number]["group"] = group
        self.incidents[number]["notes"].append(note)

    async def add_work_note(self, number: str, note: str) -> None:
        if number not in self.incidents:
            raise IntegrationError(f"incident {number} not found")
        self.incidents[number]["notes"].append(note)
