"""The escalation state machine, driven end to end with a fake clock and mock providers."""

from __future__ import annotations

from datetime import timedelta

from app.engine.escalation import EscalationEngine
from app.integrations.base import CallStatus, IntegrationError
from app.models import Status
from app.policy import PolicyOverride
from tests.conftest import failure, load, run_until_done


async def test_nobody_answers_full_ladder_then_exhausted(engine: EscalationEngine, svc):
    inc, created = await engine.open(failure(clock=svc.clock))
    assert created and inc.team_id == "payments" and inc.status == "new"
    start = svc.clock.now()

    done = await run_until_done(engine, inc.id)
    inc, attempts, events, notes = await load(svc, inc.id)

    assert done.status == Status.EXHAUSTED
    # Payments policy: primary x3, secondary x3, manager x1
    assert [(a.role, a.attempt_no) for a in attempts] == [
        ("primary", 1), ("primary", 2), ("primary", 3),
        ("secondary", 1), ("secondary", 2), ("secondary", 3),
        ("manager", 1),
    ]  # fmt: skip
    assert all(a.outcome == "no_answer" for a in attempts)
    assert [a.contact_name for a in attempts][:1] == ["Arjun Mehta"]

    # First call after the 5 min initial wait; next primary calls 7 min after the previous ended
    assert attempts[0].started_at - start == timedelta(minutes=5)
    gap = attempts[1].started_at - attempts[0].ended_at
    assert gap == timedelta(minutes=7)

    # DL email sent exactly once, before the manager was called (policy: before_manager)
    assert len(svc.email.sent) == 1
    assert svc.email.sent[0]["to"] == "payments-appsupport@example.com"
    email_event = next(e for e in events if e.type == "email_sent")
    manager_call = next(e for e in events if e.type == "calling" and "manager" in e.message)
    assert email_event.id < manager_call.id

    kinds = [n.kind for n in notes if n.channel == "teams"]
    assert kinds == ["alert", "exhausted"]
    assert inc.closed_at is not None and inc.next_action_at is None


async def test_ack_from_teams_before_first_call_means_no_calls(engine, svc):
    inc, _ = await engine.open(failure(clock=svc.clock))
    await engine.tick()  # notify
    inc, *_ = await load(svc, inc.id)
    assert inc.status == Status.NOTIFIED

    await engine.acknowledge(inc.id, "Arjun Mehta", "teams")
    svc.clock.advance(minutes=30)
    await engine.tick()
    inc, attempts, _, notes = await load(svc, inc.id)
    assert inc.status == Status.ACKNOWLEDGED and inc.acknowledged_by == "Arjun Mehta"
    assert attempts == []
    assert [n.kind for n in notes if n.channel == "teams"] == ["alert", "ack"]


async def test_secondary_answers_on_second_try(engine, svc):
    svc.voice.scripts["+919800000102"] = [CallStatus.NO_ANSWER, CallStatus.ANSWERED]
    inc, _ = await engine.open(failure(clock=svc.clock))
    done = await run_until_done(engine, inc.id)
    _, attempts, _, _ = await load(svc, inc.id)
    assert done.status == Status.ACKNOWLEDGED
    assert done.acknowledged_by == "Priya Nair" and done.ack_channel == "call"
    assert [a.role for a in attempts] == ["primary"] * 3 + ["secondary"] * 2
    assert svc.email.sent == []  # manager never reached, so no DL email yet


async def test_operator_override_changes_interval_and_attempts(engine, svc):
    override = PolicyOverride(interval_minutes=5, primary_attempts=2, initial_wait_minutes=1)
    inc, _ = await engine.open(failure(clock=svc.clock), override)
    await run_until_done(engine, inc.id)
    _, attempts, events, _ = await load(svc, inc.id)
    assert [a.role for a in attempts].count("primary") == 2
    assert attempts[1].started_at - attempts[0].ended_at == timedelta(minutes=5)
    assert any(e.type == "policy_override" for e in events)


async def test_ring_timeout_counts_as_no_answer(engine, svc):
    svc.voice.ring_seconds = 10_000  # phone never stops ringing
    inc, _ = await engine.open(failure(clock=svc.clock), PolicyOverride(primary_attempts=1, secondary_attempts=1))
    await run_until_done(engine, inc.id)
    _, attempts, _, _ = await load(svc, inc.id)
    assert attempts[0].outcome == "no_answer"
    assert attempts[0].ended_at - attempts[0].started_at >= timedelta(seconds=45)


async def test_ack_while_ringing_hangs_up(engine, svc):
    inc, _ = await engine.open(failure(clock=svc.clock))
    await run_until_done(engine, inc.id, stop=lambda i: _async(i.status == Status.CALLING))
    await engine.acknowledge(inc.id, "SysOps operator", "dashboard")
    inc, attempts, _, _ = await load(svc, inc.id)
    assert inc.status == Status.ACKNOWLEDGED
    assert attempts[-1].outcome == "cancelled"
    assert svc.voice.calls[attempts[-1].provider_call_id].hung_up


async def test_duplicate_failure_is_ignored(engine, svc):
    a, created_a = await engine.open(failure(clock=svc.clock))
    b, created_b = await engine.open(failure(clock=svc.clock))
    assert created_a and not created_b and a.id == b.id


async def test_failure_storm_is_grouped_under_one_escalation(engine, svc):
    first, _ = await engine.open(failure("PAY_A", "ti-1", clock=svc.clock))
    await engine.tick()
    second, _ = await engine.open(failure("PAY_B", "ti-2", clock=svc.clock))
    await engine.tick()
    second, attempts, _, notes = await load(svc, second.id)
    assert second.status == Status.GROUPED and second.parent_id == first.id
    assert attempts == [] and [n.kind for n in notes] == ["grouped"]
    # a different team still escalates on its own
    other, _ = await engine.open(failure("CDP_LOAD", "ti-3", clock=svc.clock))
    assert other.parent_id is None and other.team_id == "customer-data"


async def test_successful_rerun_resolves_and_stops_calls(engine, svc):
    inc, _ = await engine.open(failure(clock=svc.clock))
    await run_until_done(engine, inc.id, stop=lambda i: _async(i.status == Status.WAITING))
    resolved = await engine.resolve_task("PAY_EOD_SETTLEMENT_LOAD", "rerun succeeded")
    assert resolved == [inc.id]
    calls_before = len(svc.voice.placed)
    svc.clock.advance(hours=2)
    await engine.tick()
    inc, *_ = await load(svc, inc.id)
    assert inc.status == Status.RESOLVED and len(svc.voice.placed) == calls_before


async def test_teams_outage_does_not_stop_the_calls(engine, svc):
    svc.chat.fail = True
    inc, _ = await engine.open(failure(clock=svc.clock), PolicyOverride(primary_attempts=1, secondary_attempts=1))
    done = await run_until_done(engine, inc.id)
    _, attempts, events, notes = await load(svc, inc.id)
    assert done.status == Status.EXHAUSTED and len(attempts) == 3
    assert any(e.type == "teams_failed" for e in events)
    assert all(n.status == "failed" for n in notes if n.channel == "teams")


async def test_failed_call_moves_on(engine, svc):
    async def broken(*_a, **_k):
        raise IntegrationError("SIP trunk down")

    svc.voice.place_call = broken
    inc, _ = await engine.open(failure(clock=svc.clock))
    done = await run_until_done(engine, inc.id)
    _, attempts, _, _ = await load(svc, inc.id)
    assert done.status == Status.EXHAUSTED
    assert len(attempts) == 7 and all(a.outcome == "failed" for a in attempts)


async def test_missing_role_is_skipped(engine, svc, tmp_path):
    roster = tmp_path / "roster.yaml"
    roster.write_text(
        "groups:\n  Payments App Support:\n    primary: {name: A, phone: '+911111111111'}\n"
        "    manager: {name: M, phone: '+912222222222'}\n"
    )
    svc.oncall.path = roster
    inc, _ = await engine.open(failure(clock=svc.clock))
    await run_until_done(engine, inc.id)
    _, attempts, events, _ = await load(svc, inc.id)
    assert [a.role for a in attempts] == ["primary"] * 3 + ["manager"]
    assert any(e.type == "level_skipped" for e in events)


async def test_email_at_start_policy(engine, svc):
    inc, _ = await engine.open(failure(clock=svc.clock), PolicyOverride(email_dl="at_start"))
    await engine.tick()
    assert len(svc.email.sent) == 1


async def test_escalation_survives_restart(engine, svc):
    """A new engine on the same database carries on where the old one stopped."""
    inc, _ = await engine.open(failure(clock=svc.clock))
    await run_until_done(engine, inc.id, stop=lambda i: _async(i.attempt_no == 2 and i.status == Status.WAITING))
    restarted = EscalationEngine(svc)
    done = await run_until_done(restarted, inc.id)
    _, attempts, _, _ = await load(svc, inc.id)
    assert done.status == Status.EXHAUSTED and len(attempts) == 7


async def test_time_scale_speeds_up_policy(engine, svc):
    svc.settings.time_scale = 60
    assert engine.duration(7) == timedelta(seconds=7)


async def test_unknown_task_goes_to_fallback_team(engine, svc):
    inc, _ = await engine.open(failure("HR_PAYSLIP_PRINT", clock=svc.clock))
    assert inc.team_id == "batch-ops"


async def _async(value):
    return value
