"""ServiceNow incident handling: link the incident ServiceNow raised, then transfer it to the app team's queue."""

from __future__ import annotations

import json

import httpx

from app.integrations.base import CallStatus
from app.integrations.servicenow import ServiceNowClient
from app.integrations.uac import parse_task_instance
from app.models import Status
from app.policy import PolicyOverride
from tests.conftest import failure, load, run_until_done


def _only(desk):
    assert len(desk.incidents) == 1
    return next(iter(desk.incidents.items()))


async def test_ack_transfers_incident_with_notified_work_note(engine, svc):
    svc.voice.scripts["+919800000101"] = [CallStatus.NO_ANSWER, CallStatus.ANSWERED]
    inc, _ = await engine.open(failure(clock=svc.clock))
    done = await run_until_done(engine, inc.id)
    assert done.status == Status.ACKNOWLEDGED

    number, record = _only(svc.incidents)
    assert done.servicenow_number == number
    assert record["group"] == "Payments App Support"  # moved from the SysOps queue
    note = record["notes"][-1]
    assert note.startswith("[OnCallPilot] Notified the on-call for UAC job failure PAY_EOD_SETTLEMENT_LOAD")
    assert "Teams alert posted to Payments Application Support" in note
    assert "Called primary Arjun Mehta (+********0101): no answer" in note
    assert "Called primary Arjun Mehta (+********0101): answered" in note
    assert "Outcome: Acknowledged by Arjun Mehta via call" in note
    assert "9800000101" not in note  # phone stays masked

    _, _, events, _ = await load(svc, inc.id)
    types = [e.type for e in events]
    assert types.index("servicenow_linked") < types.index("teams_posted")
    assert types[-1] == "servicenow_transferred"


async def test_teams_ack_before_calls_still_transfers(engine, svc):
    inc, _ = await engine.open(failure(clock=svc.clock))
    await engine.tick()
    await engine.acknowledge(inc.id, "Priya Nair", "teams")
    _, record = _only(svc.incidents)
    assert record["group"] == "Payments App Support"
    assert "Acknowledged by Priya Nair via teams" in record["notes"][-1]


async def test_exhausted_transfers_with_full_history(engine, svc):
    inc, _ = await engine.open(failure(clock=svc.clock), PolicyOverride(primary_attempts=1, secondary_attempts=1))
    await run_until_done(engine, inc.id)
    _, record = _only(svc.incidents)
    note = record["notes"][-1]
    assert record["group"] == "Payments App Support"
    assert "Nobody on the escalation ladder answered" in note
    assert "Emailed DL payments-appsupport@example.com" in note
    assert note.count("Called ") == 3


async def test_incident_arriving_late_is_found_at_transfer(engine, svc):
    svc.incidents.raise_for_failures = False  # ServiceNow hasn't raised it yet
    inc, _ = await engine.open(failure(clock=svc.clock))
    await engine.tick()
    _, _, events, _ = await load(svc, inc.id)
    assert any(e.type == "servicenow_pending" for e in events)

    svc.incidents.raise_for_failures = True  # now it exists
    await engine.acknowledge(inc.id, "Arjun Mehta", "dashboard")
    inc, _, events, _ = await load(svc, inc.id)
    assert inc.servicenow_number and events[-1].type == "servicenow_transferred"


async def test_missing_incident_tells_sysops_to_transfer_by_hand(engine, svc):
    svc.incidents.raise_for_failures = False
    inc, _ = await engine.open(failure(clock=svc.clock))
    await engine.tick()
    await engine.acknowledge(inc.id, "Arjun Mehta", "dashboard")
    _, _, events, _ = await load(svc, inc.id)
    missing = [e for e in events if e.type == "servicenow_missing"]
    assert missing and "Payments App Support by hand" in missing[0].message


async def test_incident_number_from_uac_payload_is_used(engine, svc):
    await svc.incidents.create_incident("SysOps", "PAY_X", "", "P3")  # INC0021001-style record
    number = next(iter(svc.incidents.incidents))
    event = parse_task_instance(
        {"taskName": "PAY_X", "sysId": "s9", "status": "Failed", "incidentNumber": number}, "uac_webhook"
    )
    inc, _ = await engine.open(event)
    await engine.tick()
    await engine.acknowledge(inc.id, "Arjun Mehta", "dashboard")
    assert svc.incidents.incidents[number]["group"] == "Payments App Support"
    assert len(svc.incidents.incidents) == 1  # no search, no new record


async def test_transfer_when_notified(engine, svc):
    svc.settings.servicenow_transfer_when = "notified"
    inc, _ = await engine.open(failure(clock=svc.clock))
    await engine.tick()
    _, record = _only(svc.incidents)
    assert record["group"] == "Payments App Support" and len(record["notes"]) == 1
    await engine.acknowledge(inc.id, "Arjun Mehta", "teams")
    assert len(record["notes"]) == 2 and "Acknowledged by Arjun Mehta" in record["notes"][1]


async def test_grouped_failure_is_transferred_with_its_parent(engine, svc):
    first, _ = await engine.open(failure("PAY_A", "ti-1", clock=svc.clock))
    await engine.tick()
    second, _ = await engine.open(failure("PAY_B", "ti-2", clock=svc.clock))
    await engine.tick()
    second, *_ = await load(svc, second.id)
    assert second.status == Status.GROUPED and second.servicenow_number  # linked straight away
    assert svc.incidents.incidents[second.servicenow_number]["group"] == "SysOps Batch Monitoring"  # not yet

    await engine.acknowledge(first.id, "Arjun Mehta", "teams")
    first, *_ = await load(svc, first.id)
    second, _, events, _ = await load(svc, second.id)
    record = svc.incidents.incidents[second.servicenow_number]
    assert record["group"] == "Payments App Support"
    assert f"Grouped under PAY_A ({first.servicenow_number})" in record["notes"][-1]
    assert events[-1].type == "servicenow_transferred"


async def test_grouped_failure_transferred_at_grouping_in_notified_mode(engine, svc):
    svc.settings.servicenow_transfer_when = "notified"
    first, _ = await engine.open(failure("PAY_A", "ti-1", clock=svc.clock))
    await engine.tick()
    second, _ = await engine.open(failure("PAY_B", "ti-2", clock=svc.clock))
    await engine.tick()
    second, *_ = await load(svc, second.id)
    record = svc.incidents.incidents[second.servicenow_number]
    assert record["group"] == "Payments App Support" and len(record["notes"]) == 1
    await engine.acknowledge(first.id, "Arjun Mehta", "teams")
    assert len(record["notes"]) == 2 and "Acknowledged by Arjun Mehta" in record["notes"][1]


async def test_grouped_failures_escalate_on_their_own_if_parent_is_resolved(engine, svc):
    """The parent job reran fine, but the grouped failures are still broken: someone must still be told."""
    first, _ = await engine.open(failure("PAY_A", "ti-1", clock=svc.clock))
    await engine.tick()
    svc.clock.advance(minutes=1)
    b, _ = await engine.open(failure("PAY_B", "ti-2", clock=svc.clock))
    await engine.tick()
    svc.clock.advance(minutes=1)
    c, _ = await engine.open(failure("PAY_C", "ti-3", clock=svc.clock))
    await engine.tick()

    await engine.resolve_task("PAY_A", "succeeded in UAC (rerun)")
    b, *_ = await load(svc, b.id)
    c, *_ = await load(svc, c.id)
    assert b.status == Status.NEW and b.parent_id is None  # oldest grouped failure takes over
    assert c.status == Status.GROUPED and c.parent_id == b.id  # the rest group under it

    svc.voice.scripts["+919800000101"] = [CallStatus.ANSWERED]
    done = await run_until_done(engine, b.id)
    _, attempts, events, _ = await load(svc, b.id)
    assert done.status == Status.ACKNOWLEDGED and [a.role for a in attempts] == ["primary"]
    assert any(e.type == "regrouped" for e in events)
    assert sum(e.type == "triaged" for e in events) == 1  # triage not repeated
    c, *_ = await load(svc, c.id)
    assert svc.incidents.incidents[c.servicenow_number]["group"] == "Payments App Support"


async def test_servicenow_client_find_and_transfer():
    seen: list[httpx.Request] = []

    def handler(r: httpx.Request) -> httpx.Response:
        seen.append(r)
        q = r.url.params.get("sysparm_query", "")
        if r.url.path.endswith("/sys_user_group"):
            return httpx.Response(200, json={"result": [{"sys_id": "grp-pay", "name": "Payments App Support"}]})
        if r.method == "PATCH":
            return httpx.Response(200, json={"result": {}})
        if "correlation_id=ti-77" in q:
            return httpx.Response(200, json={"result": []})
        if "short_descriptionLIKEPAY_EOD" in q:
            assert "assignment_group.name=SysOps Batch" in q and "ORDERBYDESCopened_at" in q
            return httpx.Response(200, json={"result": [{"number": "INC0099", "sys_id": "inc-sys"}]})
        if q == "number=INC0099":
            return httpx.Response(200, json={"result": [{"sys_id": "inc-sys"}]})
        return httpx.Response(404)

    sn = ServiceNowClient("https://sn.test", "u", "p", client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    await sn.find_incident("PAY_EOD^ORactive=false", "ti-77", "SysOps Batch")  # ^ can't add conditions
    number = await sn.find_incident("PAY_EOD", "ti-77", "SysOps Batch")
    assert number == "INC0099"
    await sn.transfer("INC0099", "Payments App Support", "notified")
    patch = next(r for r in seen if r.method == "PATCH")
    assert patch.url.path.endswith("/incident/inc-sys")
    assert json.loads(patch.content) == {"assignment_group": "grp-pay", "assigned_to": "", "work_notes": "notified"}
    assert all("^ORactive" not in r.url.params.get("sysparm_query", "") for r in seen)
