"""REST API, webhooks and the acknowledgement pages."""

from __future__ import annotations

import html
import logging

from fastapi import APIRouter, Depends, File, Form, Header, HTTPException, Query, Request, UploadFile
from fastapi.responses import HTMLResponse
from sqlalchemy import select

from app.ai.llm import LLMError
from app.ai.vision import extract_from_screenshot
from app.demo import SCENARIOS, demo_failure
from app.engine.escalation import EscalationEngine
from app.integrations.base import FailureEvent
from app.integrations.microsoft import TeamsGraphVoice
from app.integrations.mock import MockVoice
from app.integrations.uac import FAILED_STATUSES, SUCCESS_STATUSES, normalise_status, parse_task_instance
from app.models import CallAttempt, Event, Incident, Notification, Status
from app.schemas import AckRequest, CloseRequest, DemoFailure, ManualAlert, incident_detail, incident_summary
from app.security import InvalidToken, calls_webhook_token, constant_time_equals, verify_ack_token

log = logging.getLogger(__name__)


def engine(request: Request) -> EscalationEngine:
    return request.app.state.engine


def require_token(request: Request, authorization: str = Header(default="")) -> None:
    """Bearer token check for the dashboard API. Disabled when API_TOKEN is empty (dev)."""
    token = request.app.state.settings.api_token
    if token and not constant_time_equals(authorization.removeprefix("Bearer ").strip(), token):
        raise HTTPException(401, "missing or wrong API token", headers={"WWW-Authenticate": "Bearer"})


public = APIRouter()
api = APIRouter(prefix="/api", dependencies=[Depends(require_token)])


# ---------------------------------------------------------------- health
@public.get("/api/health", tags=["ops"])
async def health():
    return {"status": "ok"}


@public.get("/api/ready", tags=["ops"])
async def ready(request: Request):
    db_ok = await request.app.state.services.db.ping()
    worker = request.app.state.worker
    body = {"database": db_ok, "worker": bool(worker) and worker.last_tick_ok is not None}
    if not db_ok:
        raise HTTPException(503, body)
    return body


# ---------------------------------------------------------------- config & incidents
@api.get("/config", tags=["incidents"])
async def config(request: Request):
    svc = request.app.state.services
    s = svc.settings
    return {
        "demo_mode": s.demo_mode,
        "time_scale": s.time_scale,
        "llm_enabled": s.llm_enabled,
        "providers": {"chat": s.chat_provider, "voice": s.voice_provider, "email": s.email_provider,
                      "oncall": s.oncall_provider},
        "teams": [{"id": t.id, "name": t.name, "email_dl": t.email_dl, "servicenow_group": t.servicenow_group,
                   "policy": t.policy.model_dump()} for t in svc.directory.teams],
        "scenarios": list(SCENARIOS),
    }  # fmt: skip


@api.get("/incidents", tags=["incidents"])
async def list_incidents(request: Request, status: str | None = None, limit: int = Query(default=50, ge=1, le=500)):
    async with request.app.state.services.db.sessions() as s:
        q = select(Incident).order_by(Incident.created_at.desc()).limit(limit)
        if status == "active":
            q = q.where(Incident.status.in_(["new", "notified", "calling", "waiting"]))
        elif status:
            q = q.where(Incident.status == status)
        return [incident_summary(i) for i in (await s.scalars(q)).all()]


async def _detail(request: Request, incident_id: str) -> dict:
    async with request.app.state.services.db.sessions() as s:
        inc = await s.get(Incident, incident_id)
        if inc is None:
            raise HTTPException(404, "incident not found")
        attempts = (await s.scalars(select(CallAttempt).where(CallAttempt.incident_id == inc.id).order_by(
            CallAttempt.id))).all()  # fmt: skip
        events = (await s.scalars(select(Event).where(Event.incident_id == inc.id).order_by(Event.id))).all()
        notes = (
            await s.scalars(select(Notification).where(Notification.incident_id == inc.id).order_by(Notification.id))
        ).all()
        return incident_detail(inc, attempts, events, notes)


@api.get("/incidents/{incident_id}", tags=["incidents"])
async def get_incident(request: Request, incident_id: str):
    return await _detail(request, incident_id)


@api.post("/incidents", status_code=201, tags=["incidents"])
async def create_incident(request: Request, body: ManualAlert):
    eng = engine(request)
    if body.team_id and eng.svc.directory.get(body.team_id) is None:
        raise HTTPException(422, f"unknown team '{body.team_id}'")
    event = FailureEvent(
        task_name=body.task_name.strip(), failed_at=body.failed_at or eng.clock.now(), source="manual",
        workflow_name=body.workflow_name, agent=body.agent, exit_code=body.exit_code,
        error_message=body.error_message, log_excerpt=body.log_excerpt, team_id=body.team_id,
    )  # fmt: skip
    inc, created = await eng.open(event, body.override)
    return {"id": inc.id, "created": created, "status": inc.status, "team_id": inc.team_id}


@api.post("/incidents/extract", tags=["incidents"])
async def extract_screenshot(request: Request, file: UploadFile = File(...)):
    """Read a UAC screenshot with the vision model. Returns fields to pre-fill the alert form."""
    svc = request.app.state.services
    llm = svc.extras.get("llm")
    if llm is None:
        raise HTTPException(503, "Screenshot reading needs an LLM key (LLM_API_KEY).")
    if (file.content_type or "") not in ("image/png", "image/jpeg", "image/webp"):
        raise HTTPException(415, "upload a PNG, JPEG or WebP screenshot")
    data = await file.read(svc.settings.max_screenshot_mb * 1024 * 1024 + 1)
    if len(data) > svc.settings.max_screenshot_mb * 1024 * 1024:
        raise HTTPException(413, f"screenshot is larger than {svc.settings.max_screenshot_mb} MB")
    try:
        result = await extract_from_screenshot(llm, data, file.content_type, svc.settings.vision_model)
    except (LLMError, ValueError) as exc:
        raise HTTPException(502, f"Could not read the screenshot: {exc}") from exc
    return result.model_dump()


@api.post("/incidents/{incident_id}/ack", tags=["incidents"])
async def ack(request: Request, incident_id: str, body: AckRequest):
    inc = await engine(request).acknowledge(incident_id, body.by, "dashboard")
    if inc is None:
        raise HTTPException(404, "incident not found")
    return {"id": inc.id, "status": inc.status}


@api.post("/incidents/{incident_id}/resolve", tags=["incidents"])
async def resolve(request: Request, incident_id: str, body: CloseRequest):
    inc = await engine(request).close(incident_id, Status.RESOLVED, body.by, body.reason)
    if inc is None:
        raise HTTPException(404, "incident not found")
    return {"id": inc.id, "status": inc.status}


@api.post("/incidents/{incident_id}/cancel", tags=["incidents"])
async def cancel(request: Request, incident_id: str, body: CloseRequest):
    inc = await engine(request).close(incident_id, Status.CANCELLED, body.by, body.reason)
    if inc is None:
        raise HTTPException(404, "incident not found")
    return {"id": inc.id, "status": inc.status}


# ---------------------------------------------------------------- demo helpers (mock providers only)
@api.post("/demo/failure", status_code=201, tags=["demo"])
async def demo(request: Request, body: DemoFailure):
    eng = engine(request)
    if not eng.s.demo_mode:
        raise HTTPException(404, "demo endpoints are off when all providers are real")
    inc, _ = await eng.open(demo_failure(body.scenario, eng.clock.now()), body.override)
    return {"id": inc.id, "task_name": inc.task_name, "team_id": inc.team_id}


@api.post("/demo/answer/{incident_id}", tags=["demo"])
async def demo_answer(request: Request, incident_id: str):
    """Pretend the person being called picked up (mock voice only)."""
    eng = engine(request)
    if not isinstance(eng.svc.voice, MockVoice):
        raise HTTPException(404, "only available with VOICE_PROVIDER=mock")
    async with eng.svc.db.sessions() as s:
        attempt = await s.scalar(
            select(CallAttempt)
            .where(CallAttempt.incident_id == incident_id, CallAttempt.outcome == "ringing")
            .order_by(CallAttempt.id.desc())
        )
    if attempt is None or not eng.svc.voice.force_answer(attempt.provider_call_id or ""):
        raise HTTPException(409, "no call is ringing for this incident")
    return {"answered": attempt.contact_name}


# ---------------------------------------------------------------- webhooks
@public.post("/api/webhooks/uac", tags=["webhooks"])
async def uac_webhook(request: Request, x_webhook_token: str = Header(default="")):
    """Receives UAC task instance status changes. Failed → escalate; Success → resolve."""
    eng = engine(request)
    secret = eng.s.uac_webhook_secret
    if not secret and eng.s.app_env == "prod":
        raise HTTPException(503, "UAC_WEBHOOK_SECRET is not configured")
    if secret and not constant_time_equals(x_webhook_token, secret):
        raise HTTPException(401, "bad webhook token")
    try:
        payload = await request.json()
    except ValueError as exc:
        raise HTTPException(400, "body must be JSON") from exc
    items = payload if isinstance(payload, list) else [payload]
    results = []
    for item in items[:100]:
        if not isinstance(item, dict):
            continue
        try:
            event = parse_task_instance(item, "uac_webhook")
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        status = normalise_status(event.uac_status)
        if status in SUCCESS_STATUSES:
            resolved = await eng.resolve_task(event.task_name, f"{event.task_name} succeeded in UAC (rerun)")
            results.append({"task": event.task_name, "action": "resolved", "incidents": resolved})
        elif status in FAILED_STATUSES or not status:
            inc, created = await eng.open(event)
            results.append({"task": event.task_name, "action": "opened" if created else "duplicate", "id": inc.id})
        else:
            results.append({"task": event.task_name, "action": "ignored", "status": status})
    return {"results": results}


@public.post("/api/webhooks/teams-calls", tags=["webhooks"])
async def teams_calls_webhook(request: Request, token: str = ""):
    """Graph call state notifications (answered, terminated...)."""
    eng = engine(request)
    if not constant_time_equals(token, calls_webhook_token(eng.s.secret_key)):
        raise HTTPException(401, "bad token")
    voice = eng.svc.voice
    if isinstance(voice, TeamsGraphVoice):
        body = await request.json()
        for n in body.get("value", []):
            voice.handle_notification(n)
    return {"ok": True}


# ---------------------------------------------------------------- acknowledgement pages
def _page(title: str, body: str, status: int = 200) -> HTMLResponse:
    return HTMLResponse(
        f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{html.escape(title)}</title>
<meta name="robots" content="noindex"><style>
body{{font-family:system-ui,sans-serif;background:#f4f5f7;color:#1d2330;margin:0;padding:24px}}
main{{max-width:520px;margin:40px auto;background:#fff;border-radius:12px;padding:28px;box-shadow:0 2px 12px #0001}}
h1{{font-size:20px;margin:0 0 12px}}input{{width:100%;padding:10px;border:1px solid #ccd;border-radius:8px;
box-sizing:border-box;font-size:15px}}button{{margin-top:14px;width:100%;padding:12px;border:0;border-radius:8px;
background:#1f7a4d;color:#fff;font-size:16px;cursor:pointer}}.muted{{color:#667;font-size:14px}}
@media(prefers-color-scheme:dark){{body{{background:#111418;color:#e6e8eb}}main{{background:#1b1f26}}
input{{background:#111418;color:#e6e8eb;border-color:#334}}}}</style></head>
<body><main>{body}</main></body></html>""",
        status_code=status,
    )


@public.get("/ack/{token}", response_class=HTMLResponse, include_in_schema=False)
async def ack_page(request: Request, token: str):
    """Confirmation page. GET never acknowledges: Teams/Outlook link previews would trigger it."""
    eng = engine(request)
    try:
        payload = verify_ack_token(eng.s.secret_key, token)
    except InvalidToken as exc:
        return _page("Link not valid", f"<h1>Link not valid</h1><p>{html.escape(str(exc))}.</p>", 400)
    async with eng.svc.db.sessions() as s:
        inc = await s.get(Incident, payload["i"])
    if inc is None:
        return _page("Not found", "<h1>Incident not found</h1>", 404)
    e = html.escape
    if not inc.is_active:
        by = f" by {e(inc.acknowledged_by)}" if inc.acknowledged_by else ""
        return _page(
            "Already handled",
            f"<h1>{e(inc.task_name)}</h1><p>This escalation is already <b>{e(inc.status)}</b>{by}.</p>",
        )
    summary = e((inc.triage or {}).get("summary") or inc.error_message or "")
    return _page(
        "Acknowledge failure",
        f"""<h1>Acknowledge {e(inc.task_name)}?</h1><p class="muted">{e(inc.severity)} · {summary}</p>
<form method="post"><label for="by">Your name</label><input id="by" name="by" required maxlength="200"
autocomplete="name"><button type="submit">✅ I'm on it, stop the calls</button></form>""",
    )


@public.post("/ack/{token}", response_class=HTMLResponse, include_in_schema=False)
async def ack_submit(request: Request, token: str, by: str = Form(..., max_length=200)):
    eng = engine(request)
    try:
        payload = verify_ack_token(eng.s.secret_key, token)
    except InvalidToken as exc:
        return _page("Link not valid", f"<h1>Link not valid</h1><p>{html.escape(str(exc))}.</p>", 400)
    channel = "email" if payload.get("w") == "email" else "teams"
    inc = await eng.acknowledge(payload["i"], by.strip() or "unknown", channel)
    if inc is None:
        return _page("Not found", "<h1>Incident not found</h1>", 404)
    return _page(
        "Acknowledged",
        f"<h1>Thanks, {html.escape(inc.acknowledged_by or by)}</h1><p>{html.escape(inc.task_name)} is "
        f"<b>{html.escape(inc.status)}</b>. No more calls will be made.</p>",
    )
