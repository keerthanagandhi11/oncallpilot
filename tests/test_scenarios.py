"""The seven escalation scenarios, checked from both sides.

SysOps side: incident status, call ladder, timeline.
App support side: Teams messages, which phones rang, DL email, ServiceNow queue and work note.
Team policy (payments): wait 5 min, primary x3 every 7 min, secondary x3, manager x1, DL email before manager.
"""

from __future__ import annotations

from datetime import timedelta

from app.integrations.base import CallStatus
from app.models import Status
from tests.conftest import failure, load, run_until_done

ARJUN, PRIYA, RAHUL = "+919800000101", "+919800000102", "+919800000103"
TEAM_QUEUE = "Payments App Support"


def teams_titles(svc) -> list[str]:
    return [m.title for _, m in svc.chat.sent]


def phones_called(svc) -> list[str]:
    return [c.name for c in svc.voice.placed]


def snow(svc) -> dict:
    assert len(svc.incidents.incidents) == 1
    return next(iter(svc.incidents.incidents.values()))


async def until(engine, inc_id, predicate):
    async def stop(i):
        return predicate(i)

    return await run_until_done(engine, inc_id, stop=stop)


async def test_1_acknowledged_in_teams(engine, svc):
    inc, _ = await engine.open(failure(clock=svc.clock))
    await engine.tick()
    svc.clock.advance(minutes=2)
    await engine.acknowledge(inc.id, "Arjun Mehta", "teams")
    svc.clock.advance(minutes=60)
    await engine.tick()

    inc, attempts, _, _ = await load(svc, inc.id)
    # SysOps side
    assert inc.status == Status.ACKNOWLEDGED and inc.ack_channel == "teams" and attempts == []
    # App support side
    assert teams_titles(svc) == [
        "🔴 P2 UAC job failure: PAY_EOD_SETTLEMENT_LOAD",
        "✅ Acknowledged: PAY_EOD_SETTLEMENT_LOAD",
    ]
    assert phones_called(svc) == [] and svc.email.sent == []
    record = snow(svc)
    assert record["group"] == TEAM_QUEUE and "Acknowledged by Arjun Mehta via teams" in record["notes"][-1]


async def test_2_primary_answers_second_call(engine, svc):
    svc.voice.scripts[ARJUN] = [CallStatus.NO_ANSWER, CallStatus.ANSWERED]
    inc, _ = await engine.open(failure(clock=svc.clock))
    start = svc.clock.now()
    done = await run_until_done(engine, inc.id)
    _, attempts, _, _ = await load(svc, inc.id)

    assert done.status == Status.ACKNOWLEDGED and done.acknowledged_by == "Arjun Mehta" and done.ack_channel == "call"
    assert [(a.role, a.outcome) for a in attempts] == [("primary", "no_answer"), ("primary", "answered")]
    assert attempts[0].started_at - start == timedelta(minutes=5)  # 02:15
    assert attempts[1].started_at - attempts[0].ended_at == timedelta(minutes=7)  # 02:22
    assert phones_called(svc) == ["Arjun Mehta", "Arjun Mehta"]
    assert svc.email.sent == []
    note = snow(svc)["notes"][-1]
    assert "no answer" in note and "answered" in note and "Acknowledged by Arjun Mehta via call" in note


async def test_3_secondary_takes_it(engine, svc):
    svc.voice.scripts[PRIYA] = [CallStatus.ANSWERED]
    inc, _ = await engine.open(failure(clock=svc.clock))
    done = await run_until_done(engine, inc.id)
    _, attempts, _, _ = await load(svc, inc.id)

    assert done.status == Status.ACKNOWLEDGED and done.acknowledged_by == "Priya Nair"
    assert [a.role for a in attempts] == ["primary"] * 3 + ["secondary"]
    assert attempts[3].started_at == attempts[2].ended_at  # straight to secondary, no extra wait
    assert phones_called(svc) == ["Arjun Mehta"] * 3 + ["Priya Nair"]
    assert "Rahul Iyer" not in phones_called(svc) and svc.email.sent == []
    assert snow(svc)["group"] == TEAM_QUEUE


async def test_4_nobody_answers(engine, svc):
    inc, _ = await engine.open(failure(clock=svc.clock))
    start = svc.clock.now()
    done = await run_until_done(engine, inc.id)
    _, attempts, events, _ = await load(svc, inc.id)

    assert done.status == Status.EXHAUSTED
    assert phones_called(svc) == ["Arjun Mehta"] * 3 + ["Priya Nair"] * 3 + ["Rahul Iyer"]
    assert all(a.outcome == "no_answer" for a in attempts)
    assert len(svc.email.sent) == 1 and "did not respond" in svc.email.sent[0]["subject"]
    types = [e.type for e in events]
    manager_call = next(i for i, e in enumerate(events) if e.type == "calling" and "manager" in e.message)
    assert types.index("email_sent") < manager_call
    assert teams_titles(svc)[-1] == "⛔ No response: PAY_EOD_SETTLEMENT_LOAD"
    record = snow(svc)
    assert record["group"] == TEAM_QUEUE and record["notes"][-1].count("Called ") == 7
    assert done.closed_at - start < timedelta(minutes=45)  # whole ladder ~38 min


async def test_5_failure_storm_is_grouped(engine, svc):
    svc.voice.scripts[ARJUN] = [CallStatus.ANSWERED]
    first, _ = await engine.open(failure("PAY_EOD_SETTLEMENT_LOAD", "ti-1", clock=svc.clock))
    await engine.tick()
    svc.clock.advance(minutes=2)
    second, _ = await engine.open(failure("SETTLE_ARCHIVE_COMPRESS", "ti-2", clock=svc.clock))
    await engine.tick()
    done = await run_until_done(engine, first.id)
    second, second_attempts, _, _ = await load(svc, second.id)

    assert second.status == Status.GROUPED and second.parent_id == first.id and second_attempts == []
    assert done.status == Status.ACKNOWLEDGED
    assert phones_called(svc) == ["Arjun Mehta"]  # one call for two failures
    titles = teams_titles(svc)
    assert "🟠 Related failure: SETTLE_ARCHIVE_COMPRESS" in titles and len(titles) == 3
    # Both ServiceNow incidents move to the team's queue: the grouped one with a note pointing at the first.
    assert second.servicenow_number and second.servicenow_number != done.servicenow_number
    records = svc.incidents.incidents
    assert {records[n]["group"] for n in (done.servicenow_number, second.servicenow_number)} == {TEAM_QUEUE}
    child_note = records[second.servicenow_number]["notes"][-1]
    assert "Grouped under PAY_EOD_SETTLEMENT_LOAD" in child_note and "related-failure card" in child_note
    assert "Acknowledged by Arjun Mehta via call" in child_note


async def test_6_rerun_fixes_it(engine, svc):
    inc, _ = await engine.open(failure(clock=svc.clock))
    await engine.tick()
    svc.clock.advance(minutes=3)
    assert await engine.resolve_task("PAY_EOD_SETTLEMENT_LOAD", "succeeded in UAC (rerun)") == [inc.id]
    svc.clock.advance(minutes=60)
    await engine.tick()

    inc, attempts, _, _ = await load(svc, inc.id)
    assert inc.status == Status.RESOLVED and attempts == [] and phones_called(svc) == []
    assert teams_titles(svc)[-1] == "🟢 Resolved: PAY_EOD_SETTLEMENT_LOAD"
    record = snow(svc)
    assert record["group"] == "SysOps Batch Monitoring"  # stays with SysOps to close
    assert "rerun" in record["notes"][-1]


async def test_7_teams_is_down(engine, svc):
    svc.chat.fail = True
    svc.voice.scripts[ARJUN] = [CallStatus.ANSWERED]
    inc, _ = await engine.open(failure(clock=svc.clock))
    done = await run_until_done(engine, inc.id)
    _, attempts, events, notes = await load(svc, inc.id)

    assert done.status == Status.ACKNOWLEDGED and done.acknowledged_by == "Arjun Mehta"
    assert svc.chat.sent == []  # nothing reached Teams
    assert all(n.status == "failed" for n in notes if n.channel == "teams")
    assert any(e.type == "teams_failed" for e in events)
    assert phones_called(svc) == ["Arjun Mehta"]
    record = snow(svc)
    assert record["group"] == TEAM_QUEUE
    assert "Teams alert posted" not in record["notes"][-1]  # the note doesn't claim a post that failed
