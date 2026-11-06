"""Stonebranch UAC adapter.

Two ways to learn about failures:

1. **Webhook (recommended)**: in UAC, create a Webhook on task instance status changes, filtered to
   Failed / Start Failure / Cancelled (and Success, so reruns can auto-resolve). Point it at
   ``POST /api/webhooks/uac`` with the ``X-Webhook-Token`` header. ``parse_webhook`` accepts the
   common field names, so the payload template can be simple (see docs/INTEGRATIONS.md).
2. **Poller (backup)**: lists recently failed task instances over the UAC REST API.

Endpoint paths follow the UAC 7.x REST API. Check them against your UAC version.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

import httpx

from app.integrations.base import FailureEvent, IntegrationError

log = logging.getLogger(__name__)

FAILED_STATUSES = {"FAILED", "START_FAILURE", "CANCELLED", "IN_DOUBT", "UNDELIVERABLE"}
SUCCESS_STATUSES = {"SUCCESS", "FINISHED"}


def _first(d: dict, *keys: str) -> str | None:
    for k in keys:
        v = d.get(k)
        if v not in (None, ""):
            return str(v)
    return None


def normalise_status(raw: str | None) -> str:
    return (raw or "").strip().upper().replace(" ", "_")


def parse_time(raw: str | None) -> datetime:
    if not raw:
        return datetime.now(UTC)
    for fmt in (None, "%Y-%m-%d %H:%M:%S %z", "%Y-%m-%d %H:%M:%S"):
        try:
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00")) if fmt is None else datetime.strptime(raw, fmt)
            return dt if dt.tzinfo else dt.replace(tzinfo=UTC)
        except ValueError:
            continue
    return datetime.now(UTC)


def parse_task_instance(d: dict, source: str) -> FailureEvent:
    """Turn a UAC task instance (webhook body or REST object) into a FailureEvent."""
    if isinstance(d.get("taskInstance"), dict):  # some templates nest the instance
        d = {**d, **d["taskInstance"]}
    name = _first(d, "taskName", "task_name", "name", "instanceName")
    if not name:
        raise ValueError("payload has no task name (taskName / name)")
    bs = d.get("businessServices") or d.get("businessService") or d.get("business_service")
    if isinstance(bs, list):
        bs = bs[0] if bs else None
    return FailureEvent(
        task_name=name,
        failed_at=parse_time(_first(d, "endTime", "updatedTime", "statusTime", "timestamp", "failed_at")),
        source=source,
        external_id=_first(d, "sysId", "taskInstanceId", "instanceId", "id", "sys_id"),
        workflow_name=_first(d, "workflowInstanceName", "workflowName", "workflow", "parentName"),
        agent=_first(d, "agent", "agentName", "agentHostname"),
        business_service=str(bs) if bs else None,
        uac_status=normalise_status(_first(d, "status", "statusName")),
        exit_code=_first(d, "exitCode", "exit_code"),
        error_message=_first(d, "statusDescription", "errorMessage", "error", "message"),
        log_excerpt=_first(d, "output", "stderr", "log", "log_excerpt"),
        servicenow_number=_first(d, "incidentNumber", "incident_number", "snIncident", "servicenowIncident"),
    )


class UacClient:
    def __init__(self, base_url: str, username: str, password: str, client: httpx.AsyncClient | None = None):
        if not base_url:
            raise ValueError("UAC_BASE_URL is required for the UAC poller")
        self._base = base_url.rstrip("/")
        self._auth = httpx.BasicAuth(username, password)
        self._client = client or httpx.AsyncClient(timeout=30)

    async def list_failed(self, since: str = "-15mn") -> list[FailureEvent]:
        body = {"status": "Failed,Start Failure", "updatedTime": since}
        try:
            r = await self._client.post(
                f"{self._base}/resources/taskinstance/list",
                json=body,
                auth=self._auth,
                headers={"Accept": "application/json"},
            )
            r.raise_for_status()
            items = r.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise IntegrationError(f"UAC list task instances failed: {exc}") from exc
        events = []
        for item in items if isinstance(items, list) else []:
            try:
                events.append(parse_task_instance(item, "uac_poll"))
            except ValueError:
                continue
        return events

    async def get_output(self, sys_id: str, max_chars: int = 6000) -> str:
        """Fetch STDERR (then STDOUT) of a task instance. Empty string if UAC has none."""
        for output_type in ("STDERR", "STDOUT"):
            try:
                r = await self._client.get(
                    f"{self._base}/resources/taskinstance/retrieveoutput",
                    params={"taskinstanceid": sys_id, "outputtype": output_type},
                    auth=self._auth,
                    headers={"Accept": "application/json"},
                )
                if r.status_code >= 400:
                    continue
                data = r.json()
                chunks = data if isinstance(data, list) else [data]
                text = "\n".join(str(c.get("outputData") or c.get("output") or "") for c in chunks).strip()
                if text:
                    return text[-max_chars:]  # the end of a log is where the error is
            except (httpx.HTTPError, ValueError, AttributeError) as exc:
                log.warning("UAC output fetch failed", extra={"sys_id": sys_id, "error": str(exc)})
        return ""
