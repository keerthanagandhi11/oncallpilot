"""Builds the engine's dependencies from settings: picks a real adapter or a mock for each system."""

from __future__ import annotations

import httpx

from app.ai.llm import LLMClient
from app.ai.triage import Triager
from app.clock import Clock
from app.config import Settings
from app.db import Database
from app.engine.escalation import Services
from app.integrations.microsoft import GraphMail, GraphToken, TeamsGraphVoice, TeamsWebhookChat
from app.integrations.mock import MockChat, MockEmail, MockIncidentDesk, MockOnCallDirectory, MockVoice
from app.integrations.servicenow import ServiceNowClient
from app.integrations.smtp_email import SmtpEmail
from app.integrations.uac import UacClient
from app.policy import load_directory
from app.security import calls_webhook_token


def build_services(settings: Settings, clock: Clock | None = None, db: Database | None = None) -> Services:
    clock = clock or Clock()
    db = db or Database(settings.database_url)
    http = httpx.AsyncClient(timeout=20, headers={"User-Agent": "OnCallPilot/1.0"})

    graph_token = None
    if settings.voice_provider == "teams_graph" or settings.email_provider == "graph":
        graph_token = GraphToken(
            settings.graph_tenant_id, settings.graph_client_id, settings.graph_client_secret, client=http
        )

    servicenow = None
    if settings.servicenow_instance:
        servicenow = ServiceNowClient(
            settings.servicenow_instance, settings.servicenow_username, settings.servicenow_password, client=http
        )

    if settings.incident_provider == "servicenow":
        if servicenow is None:
            raise ValueError("INCIDENT_PROVIDER=servicenow needs SERVICENOW_INSTANCE")
        incidents = servicenow
    elif settings.incident_provider == "mock":
        incidents = MockIncidentDesk()
    else:
        incidents = None

    if settings.oncall_provider == "servicenow":
        if servicenow is None:
            raise ValueError("ONCALL_PROVIDER=servicenow needs SERVICENOW_INSTANCE")
        oncall = servicenow
    else:
        oncall = MockOnCallDirectory(settings.mock_oncall_path)

    chat = TeamsWebhookChat(client=http) if settings.chat_provider == "teams_webhook" else MockChat()

    if settings.voice_provider == "teams_graph":
        voice = TeamsGraphVoice(
            graph_token,  # type: ignore[arg-type]
            settings.graph_tenant_id,
            settings.teams_call_source_id,
            settings.teams_call_source_name,
            f"{settings.public_base_url.rstrip('/')}/api/webhooks/teams-calls"
            f"?token={calls_webhook_token(settings.secret_key)}",
            client=http,
        )
    else:
        voice = MockVoice(clock, settings.mock_answer_probability, settings.mock_ring_seconds)

    if settings.email_provider == "smtp":
        email = SmtpEmail(
            settings.smtp_host, settings.smtp_port, settings.smtp_username, settings.smtp_password,
            settings.mail_from, settings.smtp_starttls,
        )  # fmt: skip
    elif settings.email_provider == "graph":
        email = GraphMail(graph_token, settings.mail_from, client=http)  # type: ignore[arg-type]
    else:
        email = MockEmail()

    llm = None
    if settings.llm_enabled:
        llm = LLMClient(
            settings.llm_api_key,
            settings.llm_base_url,
            settings.llm_model,
            timeout=settings.llm_timeout_seconds,
            client=httpx.AsyncClient(timeout=settings.llm_timeout_seconds),
        )

    uac = (
        UacClient(settings.uac_base_url, settings.uac_username, settings.uac_password, client=http)
        if (settings.uac_base_url)
        else None
    )

    return Services(
        settings=settings, db=db, clock=clock, directory=load_directory(settings.teams_config_path),
        oncall=oncall, chat=chat, voice=voice, email=email, triager=Triager(llm, settings.max_log_chars),
        incidents=incidents, uac=uac, extras={"llm": llm, "http": http},
    )  # fmt: skip
