"""Request and response models for the REST API."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from app.engine.messages import masked_oncall
from app.models import Incident
from app.policy import PolicyOverride


class ManualAlert(BaseModel):
    task_name: str = Field(min_length=1, max_length=200)
    team_id: str | None = None
    workflow_name: str | None = Field(default=None, max_length=200)
    agent: str | None = Field(default=None, max_length=200)
    exit_code: str | None = Field(default=None, max_length=20)
    error_message: str | None = Field(default=None, max_length=4000)
    log_excerpt: str | None = Field(default=None, max_length=20000)
    failed_at: datetime | None = None
    override: PolicyOverride | None = None


class AckRequest(BaseModel):
    by: str = Field(default="SysOps operator", min_length=1, max_length=200)


class CloseRequest(BaseModel):
    by: str = Field(default="SysOps operator", min_length=1, max_length=200)
    reason: str = Field(default="Closed by operator", max_length=500)


class DemoFailure(BaseModel):
    scenario: str | None = None
    override: PolicyOverride | None = None


def incident_summary(inc: Incident) -> dict:
    triage = inc.triage or {}
    return {
        "id": inc.id,
        "task_name": inc.task_name,
        "workflow_name": inc.workflow_name,
        "team_id": inc.team_id,
        "severity": inc.severity,
        "status": inc.status,
        "source": inc.source,
        "summary": triage.get("summary") or inc.error_message,
        "level_index": inc.level_index,
        "attempt_no": inc.attempt_no,
        "next_action_at": inc.next_action_at,
        "failed_at": inc.failed_at,
        "created_at": inc.created_at,
        "closed_at": inc.closed_at,
        "acknowledged_by": inc.acknowledged_by,
        "ack_channel": inc.ack_channel,
        "parent_id": inc.parent_id,
        "servicenow_number": inc.servicenow_number,
    }


def incident_detail(inc: Incident, attempts, events, notifications) -> dict:
    return {
        **incident_summary(inc),
        "agent": inc.agent,
        "business_service": inc.business_service,
        "uac_status": inc.uac_status,
        "exit_code": inc.exit_code,
        "error_message": inc.error_message,
        "log_excerpt": inc.log_excerpt,
        "triage": inc.triage,
        "policy": inc.policy,
        "oncall": masked_oncall(inc.oncall),
        "email_sent": inc.email_sent,
        "attempts": [
            {
                "id": a.id, "role": a.role, "attempt_no": a.attempt_no, "level_index": a.level_index,
                "contact_name": a.contact_name, "phone": a.phone_masked, "outcome": a.outcome,
                "started_at": a.started_at, "ended_at": a.ended_at, "detail": a.detail,
            }
            for a in attempts
        ],
        "events": [{"at": e.at, "type": e.type, "message": e.message} for e in events],
        "notifications": [
            {
                "channel": n.channel, "kind": n.kind, "target": n.target, "subject": n.subject, "body": n.body,
                "status": n.status, "error": n.error, "created_at": n.created_at,
            }
            for n in notifications
        ],
    }  # fmt: skip
