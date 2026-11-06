"""Microsoft adapters: Teams channel alerts, Teams PSTN calls and Outlook mail through Microsoft Graph.

* Teams alerts use a **Workflows (Power Automate) webhook** for the channel: "Post to a channel when a
  webhook request is received". It takes an Adaptive Card and needs no app registration.
* Calls use the Graph **Cloud Communications API** (POST /communications/calls). This needs an Entra
  app with the Calls.Initiate.All application permission, a Teams Phone **resource account** with a
  phone number (its id goes in TEAMS_CALL_SOURCE_ID), and a public HTTPS callback URL.
* Mail uses Graph sendMail with the Mail.Send application permission (limit it to the sender
  mailbox with an application access policy).
"""

from __future__ import annotations

import asyncio
import logging
import time

import httpx

from app.integrations.base import CallStatus, ChatMessage, Contact, IntegrationError

log = logging.getLogger(__name__)
GRAPH = "https://graph.microsoft.com/v1.0"


async def _with_retries(fn, attempts: int = 3, base_delay: float = 1.0):
    """Retry on network errors, HTTP 429 and 5xx with exponential backoff."""
    for i in range(attempts):
        try:
            return await fn()
        except httpx.HTTPStatusError as exc:
            retriable = exc.response.status_code == 429 or exc.response.status_code >= 500
            if not retriable or i == attempts - 1:
                raise
        except httpx.TransportError:
            if i == attempts - 1:
                raise
        await asyncio.sleep(base_delay * 2**i)


class TeamsWebhookChat:
    def __init__(self, client: httpx.AsyncClient | None = None, retry_delay: float = 1.0):
        self._client = client or httpx.AsyncClient(timeout=15)
        self._retry_delay = retry_delay

    async def post(self, webhook_url: str, message: ChatMessage) -> None:
        if not webhook_url:
            raise IntegrationError("this team has no teams_webhook_url configured")
        payload = {
            "type": "message",
            "attachments": [{"contentType": "application/vnd.microsoft.card.adaptive", "content": message.card}],
        }

        async def send():
            r = await self._client.post(webhook_url, json=payload)
            r.raise_for_status()

        try:
            await _with_retries(send, base_delay=self._retry_delay)
        except httpx.HTTPError as exc:
            raise IntegrationError(f"Teams webhook failed: {exc}") from exc


class GraphToken:
    """Client-credentials token, cached until shortly before it expires."""

    def __init__(self, tenant: str, client_id: str, secret: str, client: httpx.AsyncClient | None = None):
        if not (tenant and client_id and secret):
            raise ValueError("GRAPH_TENANT_ID, GRAPH_CLIENT_ID and GRAPH_CLIENT_SECRET are required")
        self._client = client or httpx.AsyncClient(timeout=15)
        self._url = f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"
        self._form = {
            "client_id": client_id,
            "client_secret": secret,
            "scope": "https://graph.microsoft.com/.default",
            "grant_type": "client_credentials",
        }
        self._token = ""
        self._expires = 0.0
        self._lock = asyncio.Lock()

    async def get(self) -> str:
        async with self._lock:
            if time.time() < self._expires - 120:
                return self._token
            try:
                r = await self._client.post(self._url, data=self._form)
                r.raise_for_status()
                data = r.json()
            except httpx.HTTPError as exc:
                raise IntegrationError(f"Graph token request failed: {exc}") from exc
            self._token = data["access_token"]
            self._expires = time.time() + int(data.get("expires_in", 3600))
            return self._token


class TeamsGraphVoice:
    """Places outbound PSTN calls from a Teams resource account and reads their state.

    Graph pushes call state changes to ``callback_url`` (handled by /api/webhooks/teams-calls),
    which stores them in ``self.states``. ``get_status`` also reads the call from Graph as a backup.
    """

    def __init__(
        self,
        token: GraphToken,
        tenant_id: str,
        source_id: str,
        source_name: str,
        callback_url: str,
        client: httpx.AsyncClient | None = None,
    ):
        if not source_id:
            raise ValueError("TEAMS_CALL_SOURCE_ID (resource account id) is required for Teams calls")
        self._token = token
        self._tenant = tenant_id
        self._source = {"id": source_id, "displayName": source_name}
        self._callback = callback_url
        self._client = client or httpx.AsyncClient(timeout=20)
        self.states: dict[str, CallStatus] = {}

    async def _headers(self) -> dict:
        return {"Authorization": f"Bearer {await self._token.get()}"}

    async def place_call(self, contact: Contact, script: str, incident_id: str) -> str:
        body = {
            "@odata.type": "#microsoft.graph.call",
            "callbackUri": self._callback,
            "tenantId": self._tenant,
            "source": {"@odata.type": "#microsoft.graph.participantInfo",
                       "identity": {"applicationInstance": self._source}},
            "targets": [{"@odata.type": "#microsoft.graph.invitationParticipantInfo",
                         "identity": {"phone": {"id": contact.phone}}}],
            "requestedModalities": ["audio"],
            "mediaConfig": {"@odata.type": "#microsoft.graph.serviceHostedMediaConfig"},
            "callOptions": {"@odata.type": "#microsoft.graph.outgoingCallOptions"},
        }  # fmt: skip

        async def call():
            r = await self._client.post(f"{GRAPH}/communications/calls", json=body, headers=await self._headers())
            r.raise_for_status()
            return r.json()["id"]

        try:
            call_id = await _with_retries(call, attempts=2)
        except (httpx.HTTPError, KeyError) as exc:
            raise IntegrationError(f"Teams call failed: {exc}") from exc
        self.states[call_id] = CallStatus.RINGING
        # ``script`` is not spoken: Graph playPrompt only plays pre-recorded audio files. The details
        # reach the person through the Teams card and email; a recorded prompt is on the roadmap.
        return call_id

    def handle_notification(self, notification: dict) -> None:
        """Apply one Graph call-state notification (from the callback webhook)."""
        resource = notification.get("resourceData") or {}
        call_id = (resource.get("id") or notification.get("resourceUrl", "").rsplit("/", 1)[-1]) or None
        state = resource.get("state")
        if not call_id or not state:
            return
        if state == "established":
            self.states[call_id] = CallStatus.ANSWERED
        elif state == "terminated" and self.states.get(call_id) != CallStatus.ANSWERED:
            code = ((resource.get("resultInfo") or {}).get("code")) or 0
            self.states[call_id] = CallStatus.BUSY if code == 486 else CallStatus.NO_ANSWER

    async def get_status(self, call_id: str) -> CallStatus:
        if self.states.get(call_id, CallStatus.RINGING) != CallStatus.RINGING:
            return self.states[call_id]
        try:
            r = await self._client.get(f"{GRAPH}/communications/calls/{call_id}", headers=await self._headers())
            if r.status_code == 404:  # call already gone and we missed the callback
                return CallStatus.NO_ANSWER
            r.raise_for_status()
            self.handle_notification({"resourceData": {"id": call_id, **r.json()}})
        except httpx.HTTPError as exc:
            log.warning("could not read call state", extra={"call_id": call_id, "error": str(exc)})
        return self.states.get(call_id, CallStatus.RINGING)

    async def hang_up(self, call_id: str) -> None:
        try:
            await self._client.delete(f"{GRAPH}/communications/calls/{call_id}", headers=await self._headers())
        except httpx.HTTPError as exc:
            log.warning("hang up failed", extra={"call_id": call_id, "error": str(exc)})


class GraphMail:
    def __init__(self, token: GraphToken, sender: str, client: httpx.AsyncClient | None = None):
        self._token = token
        self._sender = sender
        self._client = client or httpx.AsyncClient(timeout=20)

    async def send(self, to: str, subject: str, text: str, html: str) -> None:
        body = {
            "message": {
                "subject": subject,
                "body": {"contentType": "HTML", "content": html},
                "toRecipients": [{"emailAddress": {"address": a.strip()}} for a in to.split(",") if a.strip()],
                "importance": "high",
            },
            "saveToSentItems": True,
        }

        async def send():
            r = await self._client.post(
                f"{GRAPH}/users/{self._sender}/sendMail",
                json=body,
                headers={"Authorization": f"Bearer {await self._token.get()}"},
            )
            r.raise_for_status()

        try:
            await _with_retries(send)
        except httpx.HTTPError as exc:
            raise IntegrationError(f"Graph sendMail failed: {exc}") from exc
