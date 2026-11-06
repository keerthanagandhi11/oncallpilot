"""Text of every outbound message: Teams Adaptive Cards and emails.

All failure text is passed through ``redact`` and HTML-escaped before it leaves the service.
"""

from __future__ import annotations

import html
from datetime import datetime

from app.integrations.base import ChatMessage
from app.models import CallAttempt, Incident, Notification
from app.policy import Team
from app.security import mask_phone, redact

SEV_COLOR = {"P1": "attention", "P2": "warning", "P3": "accent", "P4": "default"}


def _fmt(dt: datetime | None) -> str:
    return dt.strftime("%d %b %Y %H:%M UTC") if dt else "-"


def _triage(inc: Incident) -> dict:
    return inc.triage or {}


def _facts(inc: Incident) -> list[dict]:
    facts = [
        ("Task", inc.task_name),
        ("Workflow", inc.workflow_name),
        ("Agent", inc.agent),
        ("Status", inc.uac_status),
        ("Exit code", inc.exit_code),
        ("Failed at", _fmt(inc.failed_at)),
        ("Severity", inc.severity),
        ("ServiceNow", inc.servicenow_number),
        ("OnCallPilot ID", inc.id),
    ]
    return [{"title": k, "value": str(v)} for k, v in facts if v]


def _ladder_text(inc: Incident) -> str:
    parts = []
    contacts = (inc.oncall or {}).get("contacts", {})
    for lvl in inc.policy.get("levels", []):
        who = contacts.get(lvl["role"], {}).get("name", "not on rota")
        parts.append(f"{lvl['role'].title()} ({who}): {lvl['attempts']}x every {lvl['interval_minutes']:g} min")
    return " → ".join(parts)


def alert_card(inc: Incident, team: Team, ack_url: str, dashboard_url: str) -> ChatMessage:
    t = _triage(inc)
    title = f"🔴 {inc.severity} UAC job failure: {inc.task_name}"
    log_tail = redact(inc.log_excerpt or "")[-1200:]
    body: list[dict] = [
        {"type": "TextBlock", "text": title, "weight": "Bolder", "size": "Medium", "wrap": True,
         "color": SEV_COLOR.get(inc.severity, "attention")},
        {"type": "TextBlock", "text": f"For **{team.name}**. Please acknowledge, or the on-call will be phoned.",
         "wrap": True, "spacing": "Small"},
        {"type": "FactSet", "facts": _facts(inc)},
    ]  # fmt: skip
    if inc.error_message:
        body.append({"type": "TextBlock", "text": f"**Error:** {redact(inc.error_message)[:500]}", "wrap": True})
    if t:
        label = "AI triage" if t.get("source") == "llm" else "Triage (rules)"
        body += [
            {"type": "TextBlock", "text": f"**{label}** · confidence {float(t.get('confidence', 0)):.0%}",
             "wrap": True, "separator": True},
            {"type": "TextBlock", "text": t.get("summary", ""), "wrap": True},
            {"type": "TextBlock", "text": f"**Probable cause:** {t.get('probable_cause', '')}", "wrap": True},
            {"type": "TextBlock", "text": f"**First steps:** {t.get('recommended_action', '')}", "wrap": True},
        ]  # fmt: skip
    if log_tail:
        body.append({"type": "TextBlock", "text": "**Log (last lines)**", "wrap": True, "separator": True})
        body.append({"type": "TextBlock", "text": log_tail, "wrap": True, "fontType": "Monospace", "size": "Small"})
    body.append(
        {"type": "TextBlock", "text": f"**Escalation:** {_ladder_text(inc)}", "wrap": True, "size": "Small",
         "isSubtle": True}
    )  # fmt: skip
    card = {
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "type": "AdaptiveCard",
        "version": "1.4",
        "msteams": {"width": "Full"},
        "body": body,
        "actions": [
            {"type": "Action.OpenUrl", "title": "✅ Acknowledge", "url": ack_url},
            {"type": "Action.OpenUrl", "title": "Open in OnCallPilot", "url": dashboard_url},
        ],
    }
    text = f"{title}\n{t.get('summary', inc.error_message or '')}\nAcknowledge: {ack_url}"
    return ChatMessage(title=title, text=text, card=card)


def simple_card(title: str, lines: list[str], color: str = "default") -> ChatMessage:
    body = [{"type": "TextBlock", "text": title, "weight": "Bolder", "wrap": True, "color": color}]
    body += [{"type": "TextBlock", "text": line, "wrap": True, "spacing": "Small"} for line in lines]
    card = {"$schema": "http://adaptivecards.io/schemas/adaptive-card.json", "type": "AdaptiveCard",
            "version": "1.4", "body": body}  # fmt: skip
    return ChatMessage(title=title, text="\n".join([title, *lines]), card=card)


def ack_card(inc: Incident) -> ChatMessage:
    return simple_card(
        f"✅ Acknowledged: {inc.task_name}",
        [f"By **{inc.acknowledged_by}** via {inc.ack_channel}. Escalation stopped. (ID {inc.id})"],
        "good",
    )


def resolved_card(inc: Incident, reason: str) -> ChatMessage:
    return simple_card(f"🟢 Resolved: {inc.task_name}", [f"{reason}. Escalation stopped. (ID {inc.id})"], "good")


def grouped_card(inc: Incident, parent: Incident) -> ChatMessage:
    return simple_card(
        f"🟠 Related failure: {inc.task_name}",
        [
            f"{_triage(inc).get('summary') or inc.error_message or 'Job failed.'}",
            f"Not phoning separately: the on-call is already being escalated for **{parent.task_name}** "
            f"(ID {parent.id}).",
        ],
        "warning",
    )


def exhausted_card(inc: Incident) -> ChatMessage:
    return simple_card(
        f"⛔ No response: {inc.task_name}",
        [
            f"Everyone on the escalation ladder was called and nobody answered. (ID {inc.id})",
            "The team DL has been emailed. **SysOps: please follow up by hand.**",
        ],
        "attention",
    )


def call_script(inc: Incident) -> str:
    summary = _triage(inc).get("summary") or inc.error_message or "a batch job failed"
    return (
        f"This is OnCallPilot from SysOps. Severity {inc.severity}. UAC job {inc.task_name} failed. "
        f"{summary}. Please check Microsoft Teams and acknowledge."
    )


def email(inc: Incident, team: Team, ack_url: str, reason: str, attempts: list[CallAttempt]) -> tuple[str, str, str]:
    t = _triage(inc)
    subject = f"[{inc.severity}] UAC job failure: {inc.task_name} ({reason})"
    call_lines = "\n".join(
        f"- {_fmt(a.started_at)} {a.role} {a.contact_name} ({a.phone_masked}): {a.outcome}" for a in attempts
    )
    facts = "\n".join(f"{f['title']}: {f['value']}" for f in _facts(inc))
    text = (
        f"Hello {team.name},\n\nA UAC job failed and needs your attention ({reason}).\n\n{facts}\n\n"
        f"Error: {redact(inc.error_message) or '-'}\n\n"
        f"Summary: {t.get('summary', '-')}\nProbable cause: {t.get('probable_cause', '-')}\n"
        f"First steps: {t.get('recommended_action', '-')}\n\n"
        f"Calls so far:\n{call_lines or '- none yet'}\n\nAcknowledge: {ack_url}\n\n-- OnCallPilot (SysOps)"
    )
    e = html.escape
    rows = "".join(f"<tr><td><b>{e(f['title'])}</b></td><td>{e(f['value'])}</td></tr>" for f in _facts(inc))
    att_rows = "".join(
        f"<li>{e(_fmt(a.started_at))} · {e(a.role)} {e(a.contact_name)} ({e(a.phone_masked)}): {e(a.outcome)}</li>"
        for a in attempts
    )
    html_body = (
        f"<p>Hello {e(team.name)},</p><p>A UAC job failed and needs your attention (<b>{e(reason)}</b>).</p>"
        f"<table cellpadding='4'>{rows}</table>"
        f"<p><b>Error:</b> {e(redact(inc.error_message) or '-')}</p>"
        f"<p><b>Summary:</b> {e(t.get('summary', '-'))}<br><b>Probable cause:</b> {e(t.get('probable_cause', '-'))}"
        f"<br><b>First steps:</b> {e(t.get('recommended_action', '-'))}</p>"
        f"<p><b>Calls so far:</b></p><ul>{att_rows or '<li>none yet</li>'}</ul>"
        f"<p><a href='{e(ack_url)}'>Acknowledge this failure</a></p><p>-- OnCallPilot (SysOps)</p>"
    )
    return subject, text, html_body


def transfer_note(
    inc: Incident, team: Team, attempts: list[CallAttempt], notes: list[Notification], outcome: str
) -> str:
    """ServiceNow work note written when the incident is moved to the app team's queue."""
    t = _triage(inc)
    hm = lambda d: d.strftime("%H:%M") if d else "--:--"  # noqa: E731
    steps: list[tuple] = []
    for n in notes:
        if n.status != "sent":
            continue
        if n.channel == "teams" and n.kind == "alert":
            steps.append((n.created_at, f"Teams alert posted to {team.name}"))
        elif n.channel == "teams" and n.kind == "grouped":
            steps.append((n.created_at, f"Teams related-failure card posted to {team.name}"))
        elif n.channel == "email":
            steps.append((n.created_at, f"Emailed DL {n.target}"))
    for a in attempts:
        result = {"answered": "answered", "no_answer": "no answer", "busy": "busy", "failed": "call failed",
                  "cancelled": "cancelled (acknowledged)", "ringing": "ringing"}.get(a.outcome, a.outcome)  # fmt: skip
        steps.append((a.started_at, f"Called {a.role} {a.contact_name} ({a.phone_masked}): {result}"))
    steps.sort(key=lambda x: x[0])
    lines = [
        f"[OnCallPilot] Notified the on-call for UAC job failure {inc.task_name}.",
        f"Failed: {_fmt(inc.failed_at)} | workflow {inc.workflow_name or '-'} | agent {inc.agent or '-'}"
        f" | exit code {inc.exit_code or '-'}",
        f"Error: {redact(inc.error_message) or '-'}",
    ]
    if t:
        summary = (t.get("summary") or "").rstrip(".")
        lines.append(f"Summary: {summary}. Probable cause: {t.get('probable_cause', '')}")
    lines.append("Notifications (UTC):")
    lines += [f"- {hm(at)} {text}" for at, text in steps] or ["- none"]
    lines += [f"Outcome: {outcome}", f"Transferred to {team.queue} for investigation.", f"OnCallPilot ID: {inc.id}"]
    return "\n".join(lines)


def masked_oncall(oncall: dict | None) -> dict:
    """On-call snapshot with phone numbers masked, safe for the UI and API."""
    if not oncall:
        return {}
    contacts = {r: {**c, "phone": mask_phone(c.get("phone"))} for r, c in (oncall.get("contacts") or {}).items()}
    return {**oncall, "contacts": contacts}
