"""Team directory and escalation policies, loaded from config/teams.yaml.

Values like ``${PAYMENTS_TEAMS_WEBHOOK}`` are replaced with environment variables, so secrets
(webhook URLs) stay out of the file.
"""

from __future__ import annotations

import fnmatch
import os
import re
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator

Role = Literal["primary", "secondary", "manager"]
EmailTrigger = Literal["at_start", "before_manager", "on_exhausted"]


class Level(BaseModel):
    role: Role
    attempts: int = Field(default=3, ge=1, le=10)
    interval_minutes: float = Field(default=7, ge=0.5, le=120)


class Policy(BaseModel):
    initial_wait_minutes: float = Field(default=5, ge=0, le=120)
    email_dl: EmailTrigger = "before_manager"
    answered_counts_as_ack: bool = True
    levels: list[Level] = Field(
        default_factory=lambda: [
            Level(role="primary", attempts=3, interval_minutes=7),
            Level(role="secondary", attempts=3, interval_minutes=7),
            Level(role="manager", attempts=1, interval_minutes=5),
        ]
    )

    @field_validator("levels")
    @classmethod
    def _non_empty(cls, v: list[Level]) -> list[Level]:
        if not v:
            raise ValueError("a policy needs at least one level")
        return v

    def with_overrides(self, o: PolicyOverride | None) -> Policy:
        if o is None:
            return self
        p = self.model_copy(deep=True)
        if o.initial_wait_minutes is not None:
            p.initial_wait_minutes = o.initial_wait_minutes
        if o.email_dl is not None:
            p.email_dl = o.email_dl
        for lvl in p.levels:
            if o.interval_minutes is not None:
                lvl.interval_minutes = o.interval_minutes
            if lvl.role == "primary" and o.primary_attempts is not None:
                lvl.attempts = o.primary_attempts
            if lvl.role == "secondary" and o.secondary_attempts is not None:
                lvl.attempts = o.secondary_attempts
        return p


class PolicyOverride(BaseModel):
    """Per-alert changes the operator can make (for example a shorter interval)."""

    interval_minutes: float | None = Field(default=None, ge=0.5, le=120)
    initial_wait_minutes: float | None = Field(default=None, ge=0, le=120)
    primary_attempts: int | None = Field(default=None, ge=1, le=10)
    secondary_attempts: int | None = Field(default=None, ge=1, le=10)
    email_dl: EmailTrigger | None = None


class Match(BaseModel):
    task_patterns: list[str] = Field(default_factory=list)
    business_services: list[str] = Field(default_factory=list)


class Team(BaseModel):
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$")
    name: str
    servicenow_group: str  # on-call rota group
    assignment_group: str = ""  # queue the incident is transferred to; defaults to servicenow_group
    email_dl: str
    teams_webhook_url: str = ""
    default_severity: Literal["P1", "P2", "P3", "P4"] = "P3"
    match: Match = Field(default_factory=Match)
    policy: Policy = Field(default_factory=Policy)

    @property
    def queue(self) -> str:
        return self.assignment_group or self.servicenow_group

    @field_validator("teams_webhook_url", mode="before")
    @classmethod
    def _none_to_empty(cls, v):  # an unset ${VAR} expands to YAML null
        return v or ""

    def matches(self, task_name: str, business_service: str | None) -> bool:
        if business_service and business_service.lower() in (s.lower() for s in self.match.business_services):
            return True
        return any(fnmatch.fnmatchcase(task_name.upper(), p.upper()) for p in self.match.task_patterns)


class TeamDirectory(BaseModel):
    fallback_team: str
    teams: list[Team]

    @model_validator(mode="after")
    def _check(self) -> TeamDirectory:
        ids = [t.id for t in self.teams]
        if len(ids) != len(set(ids)):
            raise ValueError("team ids must be unique")
        if self.fallback_team not in ids:
            raise ValueError(f"fallback_team '{self.fallback_team}' is not a team id")
        return self

    def get(self, team_id: str) -> Team | None:
        return next((t for t in self.teams if t.id == team_id), None)

    def resolve(self, task_name: str, business_service: str | None = None) -> Team:
        """Pick the team that owns a task. First match wins; otherwise the fallback team."""
        for team in self.teams:
            if team.matches(task_name, business_service):
                return team
        return self.get(self.fallback_team)  # type: ignore[return-value]


_ENV = re.compile(r"\$\{([A-Z0-9_]+)(?::-([^}]*))?\}")


def _expand(text: str) -> str:
    return _ENV.sub(lambda m: os.environ.get(m.group(1), m.group(2) or ""), text)


def load_directory(path: str | Path) -> TeamDirectory:
    raw = yaml.safe_load(_expand(Path(path).read_text(encoding="utf-8")))
    return TeamDirectory.model_validate(raw)
