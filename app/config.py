"""Application settings, read from environment variables (and a .env file)."""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- Core -----------------------------------------------------------------------------------
    app_env: Literal["dev", "test", "prod"] = "dev"
    log_level: str = "INFO"
    database_url: str = "sqlite+aiosqlite:///./data/oncallpilot.db"
    teams_config_path: str = "config/teams.yaml"
    public_base_url: str = "http://localhost:8000"

    # Signs acknowledgement links. MUST be set in prod.
    secret_key: str = "dev-only-change-me"
    # Bearer token for the dashboard and API. Empty = no auth (dev only).
    api_token: str = ""
    ack_link_ttl_hours: int = 24

    # --- Worker ---------------------------------------------------------------------------------
    worker_enabled: bool = True
    worker_tick_seconds: float = 2.0
    worker_batch_size: int = 20
    # Divides every policy duration. 60 = one policy minute lasts one real second (demos only).
    time_scale: float = Field(default=1.0, gt=0)
    ring_timeout_seconds: int = 45
    call_poll_seconds: int = 5
    correlation_window_minutes: int = 30

    # --- Providers: "mock" for demo/testing, or the real adapter ---------------------------------
    chat_provider: Literal["mock", "teams_webhook"] = "mock"
    voice_provider: Literal["mock", "teams_graph"] = "mock"
    email_provider: Literal["mock", "smtp", "graph"] = "mock"
    oncall_provider: Literal["mock", "servicenow"] = "mock"

    # --- Stonebranch UAC ------------------------------------------------------------------------
    uac_base_url: str = ""  # e.g. https://uac.example.com/uc
    uac_username: str = ""
    uac_password: str = ""
    uac_webhook_secret: str = ""
    uac_poll_enabled: bool = False
    uac_poll_seconds: int = 60

    # --- ServiceNow -----------------------------------------------------------------------------
    servicenow_instance: str = ""  # e.g. https://acme.service-now.com
    servicenow_username: str = ""
    servicenow_password: str = ""
    # Where the failure's ServiceNow incident comes from:
    #   mock = demo stand-in, servicenow = real instance, off = don't touch incidents
    incident_provider: Literal["mock", "servicenow", "off"] = "mock"
    #   find = ServiceNow already raises an incident for UAC failures; link it and transfer it
    #   create = OnCallPilot creates the incident itself
    servicenow_incident_mode: Literal["find", "create"] = "find"
    servicenow_queue_group: str = ""  # SysOps queue where the UAC incidents arrive (narrows the search)
    servicenow_match_hours: int = 12
    # When to move the incident to the app team's queue: once the on-call acknowledges (or nobody
    # answers), or as soon as the Teams alert is posted.
    servicenow_transfer_when: Literal["acknowledged", "notified"] = "acknowledged"
    mock_oncall_path: str = "config/oncall_demo.yaml"

    # --- Microsoft Graph (Teams calls and/or mail) ----------------------------------------------
    graph_tenant_id: str = ""
    graph_client_id: str = ""
    graph_client_secret: str = ""
    # Teams Phone resource account (application instance) that places PSTN calls
    teams_call_source_id: str = ""
    teams_call_source_name: str = "OnCallPilot"

    # --- Email ----------------------------------------------------------------------------------
    mail_from: str = "oncallpilot@example.com"
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_username: str = ""
    smtp_password: str = ""
    smtp_starttls: bool = True

    # --- LLM (any OpenAI-compatible API) --------------------------------------------------------
    llm_api_key: str = ""
    llm_base_url: str = "https://api.groq.com/openai/v1"
    llm_model: str = "openai/gpt-oss-120b"
    vision_model: str = "meta-llama/llama-4-scout-17b-16e-instruct"
    llm_timeout_seconds: float = 30.0

    # --- Mock behaviour -------------------------------------------------------------------------
    mock_answer_probability: float = Field(default=0.0, ge=0, le=1)
    mock_ring_seconds: float = 6.0

    # --- Limits ---------------------------------------------------------------------------------
    max_log_chars: int = 6000
    max_screenshot_mb: int = 8

    @field_validator("llm_api_key", "graph_client_secret", "servicenow_password", "uac_password", mode="before")
    @classmethod
    def _strip(cls, v: str) -> str:
        return (v or "").strip()

    @property
    def llm_enabled(self) -> bool:
        return bool(self.llm_api_key)

    @property
    def demo_mode(self) -> bool:
        return "mock" in (
            self.chat_provider,
            self.voice_provider,
            self.email_provider,
            self.oncall_provider,
            self.incident_provider,
        )

    def check_production(self) -> list[str]:
        """Return problems that make this config unsafe for production."""
        problems = []
        if self.app_env == "prod":
            if self.secret_key in ("", "dev-only-change-me") or len(self.secret_key) < 32:
                problems.append("SECRET_KEY must be a random string of at least 32 characters")
            if not self.api_token:
                problems.append("API_TOKEN must be set")
            if self.time_scale != 1:
                problems.append("TIME_SCALE must be 1 in production")
        return problems


@lru_cache
def get_settings() -> Settings:
    return Settings()
