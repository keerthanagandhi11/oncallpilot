"""The escalation engine: a durable state machine stored in the database.

Every incident carries ``next_action_at``. The worker calls ``tick()``; each due incident gets one
step inside its own transaction (with a row lock on Postgres), then a new ``next_action_at``.
Nothing is held only in memory, so a restart carries on exactly where it stopped.

    NEW ──notify──► NOTIFIED ──initial wait──► CALLING ──answered──► ACKNOWLEDGED
                                                 │  ▲
                                       no answer │  │ interval
                                                 ▼  │
                                               WAITING ──ladder done──► EXHAUSTED

The LLM never influences this module's decisions; triage is only attached to messages.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import timedelta

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app import metrics
from app.ai.triage import Triager
from app.clock import Clock
from app.config import Settings
from app.db import Database
from app.engine import messages
from app.integrations.base import (
    CallStatus,
    ChatMessage,
    ChatProvider,
    Contact,
    EmailProvider,
    FailureEvent,
    IncidentDesk,
    IntegrationError,
    OnCallDirectory,
    VoiceProvider,
)
from app.integrations.uac import UacClient
from app.models import ACTIVE, CallAttempt, Event, Incident, Notification, Status
from app.policy import Level, Policy, PolicyOverride, Team, TeamDirectory
from app.security import mask_phone, redact, sign_ack_token

log = logging.getLogger(__name__)
ACTIVE_VALUES = [s.value for s in ACTIVE]


@dataclass
class Services:
    settings: Settings
    db: Database
    clock: Clock
    directory: TeamDirectory
    oncall: OnCallDirectory
    chat: ChatProvider
    voice: VoiceProvider
    email: EmailProvider
    triager: Triager
    incidents: IncidentDesk | None = None  # ServiceNow incidents (find / create / transfer)
    uac: UacClient | None = None
    extras: dict = field(default_factory=dict)


class EscalationEngine:
    def __init__(self, svc: Services):
        self.svc = svc
        self.s = svc.settings
        self.clock = svc.clock

    # ------------------------------------------------------------------ helpers
    def duration(self, minutes: float) -> timedelta:
        """A policy duration in real time (TIME_SCALE speeds up demos)."""
        return timedelta(seconds=minutes * 60 / self.s.time_scale)

    def team_of(self, inc: Incident) -> Team:
        return self.svc.directory.get(inc.team_id) or self.svc.directory.resolve(inc.task_name)

    def ack_url(self, inc: Incident, who: str = "teams") -> str:
        token = sign_ack_token(self.s.secret_key, inc.id, who, self.s.ack_link_ttl_hours * 3600)
        return f"{self.s.public_base_url.rstrip('/')}/ack/{token}"

    def dashboard_url(self, inc: Incident) -> str:
        return f"{self.s.public_base_url.rstrip('/')}/#/incident/{inc.id}"

    def _event(self, s: AsyncSession, inc: Incident, type_: str, message: str, **data) -> None:
        s.add(Event(incident_id=inc.id, at=self.clock.now(), type=type_, message=message, data=data or None))
        log.info(message, extra={"incident": inc.id, "event": type_, "team": inc.team_id})

    async def _post_teams(self, s: AsyncSession, inc: Incident, kind: str, msg: ChatMessage) -> bool:
        team = self.team_of(inc)
        target = f"Teams · {team.name}"
        try:
            await self.svc.chat.post(team.teams_webhook_url, msg)
            status, error = "sent", None
        except IntegrationError as exc:
            status, error = "failed", str(exc)
            self._event(s, inc, "teams_failed", f"Teams post failed: {exc}")
        s.add(Notification(incident_id=inc.id, channel="teams", kind=kind, target=target, subject=msg.title,
                           body=msg.text, status=status, error=error, created_at=self.clock.now()))  # fmt: skip
        metrics.NOTIFICATIONS.labels("teams", status).inc()
        return status == "sent"

    async def _send_email(self, s: AsyncSession, inc: Incident, reason: str) -> None:
        team = self.team_of(inc)
        attempts = (await s.scalars(select(CallAttempt).where(CallAttempt.incident_id == inc.id))).all()
        subject, text, html = messages.email(inc, team, self.ack_url(inc, "email"), reason, list(attempts))
        try:
            await self.svc.email.send(team.email_dl, subject, text, html)
            status, error = "sent", None
            inc.email_sent = True
            self._event(s, inc, "email_sent", f"Emailed {team.email_dl} ({reason})")
        except IntegrationError as exc:
            status, error = "failed", str(exc)
            self._event(s, inc, "email_failed", f"Email to {team.email_dl} failed: {exc}")
        s.add(Notification(incident_id=inc.id, channel="email", kind="dl", target=team.email_dl, subject=subject,
                           body=text, status=status, error=error, created_at=self.clock.now()))  # fmt: skip
        metrics.NOTIFICATIONS.labels("email", status).inc()

    async def _sn_note(self, s: AsyncSession, inc: Incident, note: str) -> None:
        if self.svc.incidents and inc.servicenow_number:
            status, error = "sent", None
            try:
                await self.svc.incidents.add_work_note(inc.servicenow_number, f"[OnCallPilot] {note}")
            except IntegrationError as exc:
                status, error = "failed", str(exc)
                self._event(s, inc, "servicenow_failed", f"ServiceNow work note failed: {exc}")
            s.add(Notification(incident_id=inc.id, channel="servicenow", kind="note", target=inc.servicenow_number,
                               subject="Work note", body=f"[OnCallPilot] {note}", status=status, error=error,
                               created_at=self.clock.now()))  # fmt: skip

    async def _link_incident(self, s: AsyncSession, inc: Incident, team: Team) -> None:
        """Find (or create) the ServiceNow incident for this failure and remember its number."""
        desk = self.svc.incidents
        if desk is None or inc.servicenow_number:
            return
        try:
            if self.s.servicenow_incident_mode == "create":
                inc.servicenow_number = await desk.create_incident(
                    team.queue,
                    f"UAC job failure: {inc.task_name}",
                    f"{(inc.triage or {}).get('summary', '')}\n\nError: {inc.error_message or '-'}",
                    inc.severity,
                )
                self._event(s, inc, "servicenow_linked", f"Created ServiceNow {inc.servicenow_number}")
                return
            inc.servicenow_number = await desk.find_incident(
                inc.task_name, inc.external_id, self.s.servicenow_queue_group or None, self.s.servicenow_match_hours
            )
        except IntegrationError as exc:
            self._event(s, inc, "servicenow_failed", f"ServiceNow lookup failed: {exc}")
            return
        if inc.servicenow_number:
            self._event(s, inc, "servicenow_linked", f"Linked ServiceNow incident {inc.servicenow_number}")
        else:
            self._event(s, inc, "servicenow_pending", "No ServiceNow incident for this failure yet; will look again")

    async def _sn_transfer(self, s: AsyncSession, inc: Incident, outcome: str) -> None:
        """Move the ServiceNow incident to the app team's queue with a 'notified the on-call' work note."""
        desk = self.svc.incidents
        if desk is None:
            return
        team = self.team_of(inc)
        await self._link_incident(s, inc, team)  # it may have arrived after the UAC webhook
        if not inc.servicenow_number:
            missing = f"No ServiceNow incident found for this failure; transfer it to {team.queue} by hand"
            self._event(s, inc, "servicenow_missing", missing)
            return
        attempts = (await s.scalars(select(CallAttempt).where(CallAttempt.incident_id == inc.id))).all()
        notes = (await s.scalars(select(Notification).where(Notification.incident_id == inc.id))).all()
        note = messages.transfer_note(inc, team, list(attempts), list(notes), outcome)
        status, error = "sent", None
        try:
            await desk.transfer(inc.servicenow_number, team.queue, note)
            done = f"Transferred {inc.servicenow_number} to {team.queue} with a work note"
            self._event(s, inc, "servicenow_transferred", done)
        except IntegrationError as exc:
            status, error = "failed", str(exc)
            failed = f"Transfer of {inc.servicenow_number} failed: {exc}. Transfer it by hand."
            self._event(s, inc, "servicenow_failed", failed)
        s.add(Notification(incident_id=inc.id, channel="servicenow", kind="transfer",
                           target=f"{inc.servicenow_number} → {team.queue}", subject="Transfer + work note",
                           body=note, status=status, error=error, created_at=self.clock.now()))  # fmt: skip

    async def _sn_finish(self, s: AsyncSession, inc: Incident, outcome: str) -> None:
        """At the end of an escalation: transfer now, or (if already transferred at notify) add a note."""
        if self.s.servicenow_transfer_when == "acknowledged":
            await self._sn_transfer(s, inc, outcome)
        else:
            await self._sn_note(s, inc, outcome)
        await self._sn_children(s, inc, outcome)

    async def _grouped_children(self, s: AsyncSession, parent: Incident) -> list[Incident]:
        q = (
            select(Incident)
            .where(Incident.parent_id == parent.id, Incident.status == Status.GROUPED.value)
            .order_by(Incident.created_at)
        )
        return list((await s.scalars(q)).all())

    async def _sn_children(self, s: AsyncSession, parent: Incident, outcome: str) -> None:
        """Failures grouped under this escalation were covered by the same notification: hand their
        ServiceNow incidents to the team too."""
        for child in await self._grouped_children(s, parent):
            ref = parent.servicenow_number or parent.id
            child_outcome = f"Grouped under {parent.task_name} ({ref}); covered by the same escalation. {outcome}"
            if self.s.servicenow_transfer_when == "acknowledged":
                await self._sn_transfer(s, child, child_outcome)
            else:
                await self._sn_note(s, child, child_outcome)

    async def _release_children(self, s: AsyncSession, parent: Incident) -> None:
        """The parent was resolved or cancelled, but its grouped failures are still broken. The oldest one
        starts its own escalation; any others are grouped under it instead."""
        children = await self._grouped_children(s, parent)
        if not children:
            return
        lead, rest = children[0], children[1:]
        now = self.clock.now()
        lead.parent_id, lead.status, lead.next_action_at, lead.closed_at = None, Status.NEW.value, now, None
        self._event(s, lead, "regrouped",
                    f"{parent.task_name} was {parent.status}; this failure now escalates on its own")  # fmt: skip
        self._event(s, parent, "related", f"Related failure {lead.task_name} ({lead.id}) now escalates on its own")
        for child in rest:
            child.parent_id = lead.id
            self._event(s, child, "regrouped", f"Now grouped under {lead.task_name} ({lead.id})")

    def _close(self, inc: Incident, status: Status) -> None:
        now = self.clock.now()
        inc.status = status.value
        inc.next_action_at = None
        inc.call_deadline = None
        inc.closed_at = now
        inc.updated_at = now

    # ------------------------------------------------------------------ open
    async def open(self, event: FailureEvent, override: PolicyOverride | None = None) -> tuple[Incident, bool]:
        """Record a failure. Returns (incident, created). Duplicates return the existing incident."""
        now = self.clock.now()
        async with self.svc.db.sessions() as s:
            existing = await s.scalar(select(Incident).where(Incident.dedupe_key == event.dedupe_key))
            if existing:
                metrics.DUPLICATES.labels(event.source).inc()
                return existing, False

            team = (self.svc.directory.get(event.team_id) if event.team_id else None) or self.svc.directory.resolve(
                event.task_name, event.business_service
            )
            policy = team.policy.with_overrides(override)
            parent = await s.scalar(
                select(Incident)
                .where(
                    Incident.team_id == team.id,
                    Incident.status.in_(ACTIVE_VALUES),
                    Incident.parent_id.is_(None),
                    Incident.created_at >= now - timedelta(minutes=self.s.correlation_window_minutes),
                )
                .order_by(Incident.created_at)
                .limit(1)
            )
            inc = Incident(
                dedupe_key=event.dedupe_key, source=event.source, external_id=event.external_id,
                task_name=event.task_name, workflow_name=event.workflow_name, agent=event.agent,
                business_service=event.business_service, uac_status=event.uac_status, exit_code=event.exit_code,
                error_message=redact(event.error_message)[:4000] or None,
                log_excerpt=redact(event.log_excerpt)[-self.s.max_log_chars :] or None,
                failed_at=event.failed_at, team_id=team.id, severity=team.default_severity,
                status=Status.NEW.value, policy=policy.model_dump(), next_action_at=now,
                parent_id=parent.id if parent else None, created_at=now, updated_at=now,
                servicenow_number=event.servicenow_number,
            )  # fmt: skip
            s.add(inc)
            try:
                await s.flush()
            except IntegrityError:  # another request inserted the same failure first
                await s.rollback()
                existing = await s.scalar(select(Incident).where(Incident.dedupe_key == event.dedupe_key))
                metrics.DUPLICATES.labels(event.source).inc()
                return existing, False  # type: ignore[return-value]
            self._event(s, inc, "opened", f"Failure of {inc.task_name} received from {event.source}; team {team.name}")
            if override:
                self._event(s, inc, "policy_override", "Operator changed the escalation policy for this alert",
                            **override.model_dump(exclude_none=True))  # fmt: skip
            await s.commit()
            metrics.INCIDENTS.labels(team.id, event.source).inc()
            return inc, True

    # ------------------------------------------------------------------ worker entry points
    async def tick(self) -> int:
        """Process every due incident once. Returns how many steps ran."""
        now = self.clock.now()
        async with self.svc.db.sessions() as s:
            ids = (
                await s.scalars(
                    select(Incident.id)
                    .where(Incident.status.in_(ACTIVE_VALUES), Incident.next_action_at <= now)
                    .order_by(Incident.next_action_at)
                    .limit(self.s.worker_batch_size)
                )
            ).all()
            active = await s.scalar(select(func.count()).where(Incident.status.in_(ACTIVE_VALUES)))
            metrics.ACTIVE.set(active or 0)
        done = 0
        for incident_id in ids:
            try:
                if await self.step(incident_id):
                    done += 1
            except Exception:
                metrics.WORKER_ERRORS.inc()
                log.exception("escalation step failed", extra={"incident": incident_id})
                await self._back_off(incident_id)
        return done

    async def _back_off(self, incident_id: str) -> None:
        """After an unexpected error, retry the step in 30 s rather than spinning."""
        async with self.svc.db.sessions() as s:
            await s.execute(
                update(Incident)
                .where(Incident.id == incident_id)
                .values(next_action_at=self.clock.now() + timedelta(seconds=30))
            )
            await s.commit()

    async def _lock(self, s: AsyncSession, incident_id: str, wait: bool = False) -> Incident | None:
        """Load an incident with a row lock (Postgres). The worker skips locked rows; operators wait."""
        q = select(Incident).where(Incident.id == incident_id)
        if not self.svc.db.is_sqlite:
            q = q.with_for_update(skip_locked=not wait)
        return await s.scalar(q)

    async def step(self, incident_id: str) -> bool:
        async with self.svc.db.sessions() as s:
            inc = await self._lock(s, incident_id)
            now = self.clock.now()
            if inc is None or not inc.is_active or inc.next_action_at is None or inc.next_action_at > now:
                return False
            status = Status(inc.status)
            if status == Status.NEW:
                await self._notify(s, inc)
            elif status in (Status.NOTIFIED, Status.WAITING):
                await self._place_next_call(s, inc)
            elif status == Status.CALLING:
                await self._check_call(s, inc)
            inc.updated_at = self.clock.now()
            await s.commit()
            return True

    # ------------------------------------------------------------------ steps
    async def _notify(self, s: AsyncSession, inc: Incident) -> None:
        team = self.team_of(inc)
        event = FailureEvent(
            task_name=inc.task_name, failed_at=inc.failed_at, source=inc.source, external_id=inc.external_id,
            workflow_name=inc.workflow_name, agent=inc.agent, business_service=inc.business_service,
            uac_status=inc.uac_status, exit_code=inc.exit_code, error_message=inc.error_message,
            log_excerpt=inc.log_excerpt,
        )  # fmt: skip

        if self.svc.uac and inc.external_id and not inc.log_excerpt:
            output = await self.svc.uac.get_output(inc.external_id, self.s.max_log_chars)
            if output:
                inc.log_excerpt = event.log_excerpt = redact(output)
                self._event(s, inc, "log_fetched", "Fetched job output from UAC")

        if not inc.triage:  # a released grouped failure already has one
            triage = await self.svc.triager.triage(event, team.name, team.default_severity)
            inc.triage = triage.model_dump()
            inc.severity = triage.severity
            metrics.TRIAGE.labels(triage.source).inc()
            self._event(s, inc, "triaged", f"Triage ({triage.source}): {triage.summary}", category=triage.category)

        if inc.parent_id:
            parent = await s.get(Incident, inc.parent_id)
            if parent is not None and parent.is_active:
                await self._post_teams(s, inc, "grouped", messages.grouped_card(inc, parent))
                self._event(s, inc, "grouped", f"Grouped under active escalation {parent.id}; no separate calls")
                self._event(s, parent, "related", f"Related failure {inc.task_name} ({inc.id}) grouped here")
                self._close(inc, Status.GROUPED)
                await self._link_incident(s, inc, team)
                if self.s.servicenow_transfer_when == "notified":
                    await self._sn_transfer(
                        s, inc, f"Grouped under {parent.task_name}; the on-call is already being notified for it."
                    )
                return
            inc.parent_id = None  # parent finished meanwhile: escalate this one on its own

        await self._lookup_oncall(s, inc, team)

        await self._link_incident(s, inc, team)

        await self._post_teams(
            s, inc, "alert", messages.alert_card(inc, team, self.ack_url(inc), self.dashboard_url(inc))
        )
        self._event(s, inc, "teams_posted", f"Alert posted to {team.name} in Teams")
        if self.s.servicenow_transfer_when == "notified":
            await self._sn_transfer(s, inc, "Teams alert posted; the on-call will be phoned if nobody acknowledges.")

        policy = Policy.model_validate(inc.policy)
        if policy.email_dl == "at_start":
            await self._send_email(s, inc, "new failure")
        inc.status = Status.NOTIFIED.value
        inc.next_action_at = self.clock.now() + self.duration(policy.initial_wait_minutes)
        wait = f"{policy.initial_wait_minutes:g}"
        self._event(s, inc, "waiting_ack", f"Waiting {wait} min for a Teams acknowledgement before calling")

    async def _lookup_oncall(self, s: AsyncSession, inc: Incident, team: Team) -> None:
        try:
            oncall = await self.svc.oncall.who_is_on_call(team.servicenow_group)
            inc.oncall = oncall.to_dict()
            names = ", ".join(f"{r}: {c.name}" for r, c in oncall.contacts.items())
            self._event(s, inc, "oncall", f"On call for {team.servicenow_group}: {names}")
        except IntegrationError as exc:
            inc.oncall = inc.oncall or None
            self._event(s, inc, "oncall_failed", f"On-call lookup failed: {exc}")

    async def _place_next_call(self, s: AsyncSession, inc: Incident) -> None:
        policy = Policy.model_validate(inc.policy)
        team = self.team_of(inc)
        if not (inc.oncall or {}).get("contacts"):
            await self._lookup_oncall(s, inc, team)  # retry once before giving up on calls

        while inc.level_index < len(policy.levels):
            level = policy.levels[inc.level_index]
            raw = ((inc.oncall or {}).get("contacts") or {}).get(level.role)
            if raw:
                break
            self._event(s, inc, "level_skipped", f"No {level.role} on call; skipping that level")
            inc.level_index += 1
            inc.attempt_no = 1
        else:
            await self._exhaust(s, inc)
            return

        contact = Contact(**raw)
        if inc.attempt_no == 1 and not inc.email_sent and policy.email_dl == "before_manager":
            if self._is_manager_level(policy, inc.level_index):
                await self._send_email(s, inc, "primary and secondary on-call did not respond")

        now = self.clock.now()
        attempt = CallAttempt(
            incident_id=inc.id, level_index=inc.level_index, role=level.role, attempt_no=inc.attempt_no,
            contact_name=contact.name, phone_masked=mask_phone(contact.phone), started_at=now,
        )  # fmt: skip
        label = f"{level.role} {contact.name} ({attempt.phone_masked}), attempt {inc.attempt_no}/{level.attempts}"
        try:
            attempt.provider_call_id = await self.svc.voice.place_call(contact, messages.call_script(inc), inc.id)
        except IntegrationError as exc:
            attempt.outcome, attempt.detail, attempt.ended_at = CallStatus.FAILED.value, str(exc), now
            s.add(attempt)
            metrics.CALLS.labels(inc.team_id, level.role, "failed").inc()
            self._event(s, inc, "call_failed", f"Could not call {label}: {exc}")
            self._advance(s, inc, policy)
            return
        s.add(attempt)
        inc.status = Status.CALLING.value
        inc.call_deadline = now + timedelta(seconds=self.s.ring_timeout_seconds)
        inc.next_action_at = now + timedelta(seconds=min(self.s.call_poll_seconds, self.s.ring_timeout_seconds))
        self._event(s, inc, "calling", f"Calling {label}", role=level.role, attempt=inc.attempt_no)

    @staticmethod
    def _is_manager_level(policy: Policy, index: int) -> bool:
        roles = [lvl.role for lvl in policy.levels]
        if "manager" in roles:
            return policy.levels[index].role == "manager"
        return index == len(policy.levels) - 1 and index > 0

    async def _check_call(self, s: AsyncSession, inc: Incident) -> None:
        attempt = await s.scalar(
            select(CallAttempt)
            .where(CallAttempt.incident_id == inc.id, CallAttempt.outcome == CallStatus.RINGING.value)
            .order_by(CallAttempt.id.desc())
            .limit(1)
        )
        policy = Policy.model_validate(inc.policy)
        now = self.clock.now()
        if attempt is None:
            inc.status, inc.next_action_at = Status.WAITING.value, now
            return
        status = await self.svc.voice.get_status(attempt.provider_call_id or "")
        if status == CallStatus.RINGING:
            if inc.call_deadline and now < inc.call_deadline:
                inc.next_action_at = now + timedelta(seconds=self.s.call_poll_seconds)
                return
            await self.svc.voice.hang_up(attempt.provider_call_id or "")
            status = CallStatus.NO_ANSWER

        attempt.outcome, attempt.ended_at = status.value, now
        metrics.CALLS.labels(inc.team_id, attempt.role, status.value).inc()
        who = f"{attempt.role} {attempt.contact_name}"
        if status == CallStatus.ANSWERED:
            self._event(s, inc, "call_answered", f"{who} answered the call")
            if policy.answered_counts_as_ack:
                await self._ack_locked(s, inc, attempt.contact_name, "call")
                return
        else:
            self._event(s, inc, "call_unanswered", f"{who} did not answer ({status.value.replace('_', ' ')})")
        self._advance(s, inc, policy)

    def _advance(self, s: AsyncSession, inc: Incident, policy: Policy) -> None:
        """Move the ladder pointer after a call and schedule the next one."""
        level: Level = policy.levels[inc.level_index]
        now = self.clock.now()
        inc.call_deadline = None
        inc.status = Status.WAITING.value
        if inc.attempt_no < level.attempts:
            inc.attempt_no += 1
            inc.next_action_at = now + self.duration(level.interval_minutes)
            self._event(s, inc, "scheduled", f"Next call to {level.role} in {level.interval_minutes:g} min")
        else:
            inc.level_index += 1
            inc.attempt_no = 1
            inc.next_action_at = now  # escalate straight to the next level
            if inc.level_index < len(policy.levels):
                self._event(s, inc, "escalated", f"Escalating to {policy.levels[inc.level_index].role}")

    async def _exhaust(self, s: AsyncSession, inc: Incident) -> None:
        self._close(inc, Status.EXHAUSTED)
        self._event(s, inc, "exhausted", "Nobody on the escalation ladder responded. SysOps must follow up.")
        if not inc.email_sent:
            await self._send_email(s, inc, "nobody on the escalation ladder responded")
        await self._post_teams(s, inc, "exhausted", messages.exhausted_card(inc))
        await self._sn_finish(
            s, inc, "Nobody on the escalation ladder answered. Team DL emailed. Team to pick up from the queue."
        )
        metrics.EXHAUSTED.labels(inc.team_id).inc()

    # ------------------------------------------------------------------ operator actions
    async def acknowledge(self, incident_id: str, by: str, channel: str) -> Incident | None:
        """Stop the escalation. Returns the incident, or None if it doesn't exist."""
        async with self.svc.db.sessions() as s:
            inc = await self._lock(s, incident_id, wait=True)
            if inc is None:
                return None
            if inc.is_active:
                await self._ack_locked(s, inc, by, channel)
                await s.commit()
            return inc

    async def _ack_locked(self, s: AsyncSession, inc: Incident, by: str, channel: str) -> None:
        if inc.status == Status.CALLING.value:
            ringing = await s.scalar(
                select(CallAttempt)
                .where(CallAttempt.incident_id == inc.id, CallAttempt.outcome == CallStatus.RINGING.value)
                .order_by(CallAttempt.id.desc())
                .limit(1)
            )
            if ringing and channel != "call":
                await self.svc.voice.hang_up(ringing.provider_call_id or "")
                ringing.outcome, ringing.ended_at, ringing.detail = "cancelled", self.clock.now(), "acknowledged"
        inc.acknowledged_by = by[:200]
        inc.ack_channel = channel
        self._close(inc, Status.ACKNOWLEDGED)
        self._event(s, inc, "acknowledged", f"Acknowledged by {by} via {channel}; escalation stopped")
        await self._post_teams(s, inc, "ack", messages.ack_card(inc))
        when = self.clock.now().strftime("%H:%M UTC")
        await self._sn_finish(s, inc, f"Acknowledged by {by} via {channel} at {when}.")
        metrics.ACKS.labels(inc.team_id, channel).inc()
        metrics.TIME_TO_ACK.labels(inc.team_id).observe((self.clock.now() - inc.created_at).total_seconds())

    async def close(self, incident_id: str, status: Status, by: str, reason: str) -> Incident | None:
        """Resolve or cancel an active incident."""
        assert status in (Status.RESOLVED, Status.CANCELLED)
        async with self.svc.db.sessions() as s:
            inc = await self._lock(s, incident_id, wait=True)
            if inc is None or not inc.is_active:
                return inc
            if inc.status == Status.CALLING.value:
                ringing = await s.scalar(
                    select(CallAttempt).where(
                        CallAttempt.incident_id == inc.id, CallAttempt.outcome == CallStatus.RINGING.value
                    )
                )
                if ringing:
                    await self.svc.voice.hang_up(ringing.provider_call_id or "")
                    ringing.outcome, ringing.ended_at = "cancelled", self.clock.now()
            self._close(inc, status)
            self._event(s, inc, status.value, f"{status.value.title()} by {by}: {reason}")
            if status == Status.RESOLVED and inc.oncall:  # only tell Teams if the alert went out
                await self._post_teams(s, inc, "resolved", messages.resolved_card(inc, reason))
            await self._sn_note(s, inc, f"{status.value.title()} by {by}: {reason}")
            await self._release_children(s, inc)
            await s.commit()
            return inc

    async def resolve_task(self, task_name: str, reason: str) -> list[str]:
        """A task succeeded (for example on rerun): resolve its active incidents."""
        async with self.svc.db.sessions() as s:
            ids = (
                await s.scalars(
                    select(Incident.id).where(Incident.task_name == task_name, Incident.status.in_(ACTIVE_VALUES))
                )
            ).all()
        for incident_id in ids:
            await self.close(incident_id, Status.RESOLVED, "UAC", reason)
        return list(ids)
