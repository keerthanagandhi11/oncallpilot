from __future__ import annotations

import os
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import select

from app.ai.triage import Triager
from app.clock import FakeClock
from app.config import Settings
from app.db import Database
from app.engine.escalation import EscalationEngine, Services
from app.integrations.base import FailureEvent
from app.integrations.mock import MockChat, MockEmail, MockIncidentDesk, MockOnCallDirectory, MockVoice
from app.models import CallAttempt, Event, Incident, Notification
from app.policy import load_directory

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        _env_file=None,
        app_env="test",
        database_url=os.environ.get("TEST_DATABASE_URL") or f"sqlite+aiosqlite:///{tmp_path}/test.db",
        teams_config_path=str(ROOT / "config/teams.yaml"),
        mock_oncall_path=str(ROOT / "config/oncall_demo.yaml"),
        secret_key="test-secret-key-test-secret-key-123",
        worker_enabled=False,
        llm_api_key="",
        ring_timeout_seconds=45,
        call_poll_seconds=5,
        mock_ring_seconds=6,
    )


@pytest_asyncio.fixture
async def svc(settings) -> Services:
    clock = FakeClock()
    db = Database(settings.database_url)
    if not db.is_sqlite:  # shared Postgres: start every test from empty tables
        from app.db import Base

        async with db.engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
    await db.create_all()
    services = Services(
        settings=settings,
        db=db,
        clock=clock,
        directory=load_directory(settings.teams_config_path),
        oncall=MockOnCallDirectory(settings.mock_oncall_path),
        chat=MockChat(),
        voice=MockVoice(clock, answer_probability=0.0, ring_seconds=settings.mock_ring_seconds),
        email=MockEmail(),
        triager=Triager(None),
        incidents=MockIncidentDesk(),
    )
    yield services
    await db.dispose()


@pytest.fixture
def engine(svc) -> EscalationEngine:
    return EscalationEngine(svc)


def failure(task="PAY_EOD_SETTLEMENT_LOAD", ext="ti-001", clock: FakeClock | None = None, **kw) -> FailureEvent:
    return FailureEvent(
        task_name=task,
        failed_at=(clock.now() if clock else FakeClock().now()),
        source="uac_webhook",
        external_id=ext,
        error_message=kw.pop("error_message", "No such file or directory: /data/in/settle.csv"),
        exit_code="8",
        **kw,
    )


async def load(svc: Services, incident_id: str):
    async with svc.db.sessions() as s:
        inc = await s.get(Incident, incident_id)
        attempts = (
            await s.scalars(select(CallAttempt).where(CallAttempt.incident_id == incident_id).order_by(CallAttempt.id))
        ).all()
        events = (await s.scalars(select(Event).where(Event.incident_id == incident_id).order_by(Event.id))).all()
        notes = (
            await s.scalars(
                select(Notification).where(Notification.incident_id == incident_id).order_by(Notification.id)
            )
        ).all()
        return inc, attempts, events, notes


async def run_until_done(engine: EscalationEngine, incident_id: str, max_steps: int = 500, stop=None):
    """Drive the worker, jumping the fake clock to each next due time, until the incident closes."""
    clock: FakeClock = engine.clock  # type: ignore[assignment]
    for _ in range(max_steps):
        inc, *_ = await load(engine.svc, incident_id)
        if not inc.is_active or (stop and await stop(inc)):
            return inc
        if inc.next_action_at and inc.next_action_at > clock.now():
            clock.advance(seconds=(inc.next_action_at - clock.now()).total_seconds())
        await engine.tick()
    raise AssertionError("escalation did not finish")
