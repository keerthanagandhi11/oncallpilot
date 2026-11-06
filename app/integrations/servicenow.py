"""ServiceNow adapter: who is on call (On-Call Scheduling API) and incident records (Table API).

Uses basic auth with an integration user that can read sys_user, sys_user_group and the on-call
rota, and (optionally) create incidents. Check field names against your instance; customised
instances sometimes store mobile numbers in a different field.
"""

from __future__ import annotations

import logging

import httpx

from app.integrations.base import Contact, IntegrationError, OnCall

log = logging.getLogger(__name__)

_SEVERITY_TO_IMPACT_URGENCY = {"P1": (1, 1), "P2": (1, 2), "P3": (2, 2), "P4": (3, 3)}


class ServiceNowClient:
    def __init__(self, instance: str, username: str, password: str, client: httpx.AsyncClient | None = None):
        if not instance:
            raise ValueError("SERVICENOW_INSTANCE is required for the ServiceNow adapter")
        self._client = client or httpx.AsyncClient(timeout=20)
        self._base = instance.rstrip("/")
        self._auth = httpx.BasicAuth(username, password)
        self._group_cache: dict[str, dict] = {}

    async def _get(self, path: str, params: dict | None = None) -> dict:
        try:
            r = await self._client.get(
                f"{self._base}{path}", params=params, auth=self._auth, headers={"Accept": "application/json"}
            )
            r.raise_for_status()
            return r.json()
        except httpx.HTTPError as exc:
            raise IntegrationError(f"ServiceNow GET {path} failed: {exc}") from exc

    async def _group(self, name: str) -> dict:
        if name not in self._group_cache:
            data = await self._get(
                "/api/now/table/sys_user_group",
                {"sysparm_query": f"name={name}", "sysparm_fields": "sys_id,name,manager,email", "sysparm_limit": 1},
            )
            if not data.get("result"):
                raise IntegrationError(f"ServiceNow group '{name}' not found")
            self._group_cache[name] = data["result"][0]
        return self._group_cache[name]

    async def _user(self, sys_id: str, role: str) -> Contact | None:
        data = await self._get(
            f"/api/now/table/sys_user/{sys_id}", {"sysparm_fields": "sys_id,name,email,mobile_phone,phone"}
        )
        u = data.get("result") or {}
        phone = u.get("mobile_phone") or u.get("phone")
        if not phone:
            log.warning("on-call user has no phone number", extra={"user": u.get("name"), "role": role})
            return None
        return Contact(role=role, name=u.get("name") or sys_id, phone=phone, email=u.get("email") or None)

    async def who_is_on_call(self, group: str) -> OnCall:
        grp = await self._group(group)
        data = await self._get("/api/now/on_call_rota/whoisoncall", {"group_ids": grp["sys_id"]})
        entries = data.get("result") or []

        def order(e: dict) -> int:
            for key in ("order", "escalation_level", "escalationLevel"):
                if str(e.get(key, "")).isdigit():
                    return int(e[key])
            return 99

        users: list[str] = []
        for e in sorted(entries, key=order):
            uid = e.get("userId") or e.get("user_id") or (e.get("user") or {}).get("value")
            if uid and uid not in users:
                users.append(uid)

        oncall = OnCall(group=group)
        for role, uid in zip(("primary", "secondary"), users, strict=False):
            if contact := await self._user(uid, role):
                oncall.contacts[role] = contact
        manager = grp.get("manager")
        manager_id = manager.get("value") if isinstance(manager, dict) else manager
        if manager_id and (contact := await self._user(manager_id, "manager")):
            oncall.contacts["manager"] = contact
        if not oncall.contacts:
            raise IntegrationError(f"nobody is on call for '{group}'")
        return oncall

    async def create_incident(self, group: str, short: str, description: str, severity: str) -> str:
        grp = await self._group(group)
        impact, urgency = _SEVERITY_TO_IMPACT_URGENCY.get(severity, (2, 2))
        body = {
            "short_description": short[:160],
            "description": description,
            "assignment_group": grp["sys_id"],
            "impact": impact,
            "urgency": urgency,
            "category": "software",
        }
        try:
            r = await self._client.post(f"{self._base}/api/now/table/incident", json=body, auth=self._auth)
            r.raise_for_status()
            return r.json()["result"]["number"]
        except (httpx.HTTPError, KeyError) as exc:
            raise IntegrationError(f"ServiceNow incident create failed: {exc}") from exc

    async def _incident_sys_id(self, number: str) -> str:
        data = await self._get(
            "/api/now/table/incident",
            {"sysparm_query": f"number={_q(number)}", "sysparm_fields": "sys_id", "sysparm_limit": 1},
        )
        if not data.get("result"):
            raise IntegrationError(f"incident {number} not found")
        return data["result"][0]["sys_id"]

    async def _patch(self, sys_id: str, body: dict, what: str) -> None:
        try:
            r = await self._client.patch(f"{self._base}/api/now/table/incident/{sys_id}", json=body, auth=self._auth)
            r.raise_for_status()
        except httpx.HTTPError as exc:
            raise IntegrationError(f"ServiceNow {what} failed: {exc}") from exc

    async def add_work_note(self, number: str, note: str) -> None:
        await self._patch(await self._incident_sys_id(number), {"work_notes": note}, "work note")

    async def find_incident(
        self, task_name: str, external_id: str | None, queue_group: str | None, hours: int = 12
    ) -> str | None:
        """Find the incident ServiceNow already raised for this UAC failure.

        Tries, in order: correlation_id = the UAC task instance id, then an active incident whose short
        description mentions the task name, opened in the last ``hours``, in the SysOps queue if one is
        set. Returns the newest match's number, or None.
        """
        base = "active=true"
        if queue_group:
            base += f"^assignment_group.name={_q(queue_group)}"
        queries = []
        if external_id:
            queries.append(f"active=true^correlation_id={_q(external_id)}")
        queries.append(
            f"{base}^short_descriptionLIKE{_q(task_name)}"
            f"^opened_at>=javascript:gs.hoursAgoStart({int(hours)})^ORDERBYDESCopened_at"
        )
        for query in queries:
            data = await self._get(
                "/api/now/table/incident",
                {"sysparm_query": query, "sysparm_fields": "number,sys_id", "sysparm_limit": 1},
            )
            if data.get("result"):
                return data["result"][0]["number"]
        return None

    async def transfer(self, number: str, group: str, note: str) -> None:
        """Move the incident to the application support team's queue and add a work note."""
        grp = await self._group(group)
        body = {"assignment_group": grp["sys_id"], "assigned_to": "", "work_notes": note}
        await self._patch(await self._incident_sys_id(number), body, "transfer")


def _q(value: str) -> str:
    """Make a value safe inside a ServiceNow encoded query (^ separates conditions)."""
    return value.replace("^", "").replace("\n", " ").strip()[:200]
