"""Live end-to-end check of the escalation scenarios against a running OnCallPilot.

It drives the real HTTP API, worker and timers (demo mode, mock providers, TIME_SCALE=60) and checks
both sides: what SysOps sees (status, calls) and what app support gets (Teams, phones, email, ServiceNow).

    python scripts/e2e_scenarios.py --base http://localhost:8022 --teams-down-base http://localhost:8023

The second server must run with CHAT_PROVIDER=teams_webhook and a Teams webhook URL that can't be reached.
"""

from __future__ import annotations

import argparse
import re
import sys
import time

import httpx

TEAM_QUEUE = "Payments App Support"


class Check:
    def __init__(self, name: str):
        self.name, self.ok, self.notes = name, True, []

    def that(self, cond: bool, what: str) -> None:
        self.notes.append(("✓" if cond else "✗") + " " + what)
        self.ok &= bool(cond)


class Api:
    def __init__(self, base: str):
        self.c = httpx.Client(base_url=base, timeout=20)

    def simulate(self, scenario: str = "missing_file") -> str:
        r = self.c.post("/api/demo/failure", json={"scenario": scenario})
        r.raise_for_status()
        return r.json()["id"]

    def get(self, inc_id: str) -> dict:
        return self.c.get(f"/api/incidents/{inc_id}").json()

    def wait(self, inc_id: str, pred, what: str, timeout: float = 240) -> dict:
        end = time.time() + timeout
        while time.time() < end:
            d = self.get(inc_id)
            if pred(d):
                return d
            time.sleep(0.4)
        raise TimeoutError(f"timed out waiting for {what}; last status {d['status']}")

    def answer_call(self, inc_id: str, n: int) -> dict:
        """Wait until call number n is ringing, then pick it up."""
        d = self.wait(inc_id, lambda d: len(d["attempts"]) == n and d["attempts"][-1]["outcome"] == "ringing",
                      f"call {n} to ring")  # fmt: skip
        self.c.post(f"/api/demo/answer/{inc_id}").raise_for_status()
        return d

    def finished(self, inc_id: str) -> dict:
        return self.wait(inc_id, lambda d: d["status"] not in ("new", "notified", "calling", "waiting"), "the end")


def sides(c: Check, d: dict, teams_kinds: list[str], callees: list[str], emails: int, snow_moved: bool) -> None:
    sent_teams = [n["kind"] for n in d["notifications"] if n["channel"] == "teams" and n["status"] == "sent"]
    c.that(sent_teams == teams_kinds, f"Teams messages {sent_teams} (expected {teams_kinds})")
    names = [a["contact_name"] for a in d["attempts"]]
    c.that(names == callees, f"phones rang: {names or 'none'}")
    mails = [n for n in d["notifications"] if n["channel"] == "email" and n["status"] == "sent"]
    c.that(len(mails) == emails, f"DL emails sent: {len(mails)} (expected {emails})")
    transfers = [n for n in d["notifications"] if n["channel"] == "servicenow" and n["kind"] == "transfer"]
    if snow_moved:
        ok = bool(transfers) and transfers[-1]["status"] == "sent" and transfers[-1]["target"].endswith(TEAM_QUEUE)
        c.that(ok, f"ServiceNow {d['servicenow_number']} moved to {TEAM_QUEUE} with work note")
        noted = bool(transfers) and "Notified the on-call" in transfers[-1]["body"]
        c.that(noted, "work note says 'Notified the on-call'")
    else:
        c.that(not transfers, "ServiceNow incident not transferred (stays with SysOps)")


def s1_teams_ack(api: Api) -> Check:
    c = Check("1 Acknowledged in Teams")
    inc = api.simulate()
    d = api.wait(inc, lambda d: d["status"] == "notified", "Teams alert")
    card = next(n for n in d["notifications"] if n["kind"] == "alert")
    url = re.search(r"Acknowledge: (\S+)", card["body"]).group(1)
    path = url.split("://", 1)[1].split("/", 1)[1]
    page = api.c.get("/" + path)
    c.that(page.status_code == 200 and "<form" in page.text, "ack link from the Teams card opens a confirm page")
    c.that(api.get(inc)["status"] == "notified", "opening the link alone does not acknowledge (link previews)")
    done = api.c.post("/" + path, data={"by": "Arjun Mehta"})
    c.that("Thanks, Arjun Mehta" in done.text, "pressing the button acknowledges")
    d = api.finished(inc)
    ok = d["status"] == "acknowledged" and d["ack_channel"] == "teams"
    c.that(ok, f"SysOps sees: {d['status']} via {d['ack_channel']}")
    sides(c, d, ["alert", "ack"], [], 0, True)
    return c


def s2_primary_second(api: Api) -> Check:
    c = Check("2 Primary answers 2nd call")
    inc = api.simulate()
    api.answer_call(inc, 2)
    d = api.finished(inc)
    ok = d["status"] == "acknowledged" and d["acknowledged_by"] == "Arjun Mehta"
    c.that(ok, f"SysOps sees: {d['status']} by {d['acknowledged_by']}")
    c.that([a["outcome"] for a in d["attempts"]] == ["no_answer", "answered"], "call 1 missed, call 2 answered")
    sides(c, d, ["alert", "ack"], ["Arjun Mehta"] * 2, 0, True)
    return c


def s3_secondary(api: Api) -> Check:
    c = Check("3 Secondary takes it")
    inc = api.simulate()
    api.answer_call(inc, 4)
    d = api.finished(inc)
    ok = d["status"] == "acknowledged" and d["acknowledged_by"] == "Priya Nair"
    c.that(ok, f"SysOps sees: {d['status']} by {d['acknowledged_by']}")
    sides(c, d, ["alert", "ack"], ["Arjun Mehta"] * 3 + ["Priya Nair"], 0, True)
    return c


def s4_nobody(api: Api) -> Check:
    c = Check("4 Nobody answers")
    inc = api.simulate()
    d = api.finished(inc)
    c.that(d["status"] == "exhausted", f"SysOps sees: {d['status']} (follow up by hand)")
    c.that(all(a["outcome"] == "no_answer" for a in d["attempts"]), "all 7 calls unanswered")
    types = [e["type"] for e in d["events"]]
    mgr = next(i for i, e in enumerate(d["events"]) if e["type"] == "calling" and "manager" in e["message"])
    c.that(types.index("email_sent") < mgr, "DL emailed before the manager was called")
    sides(c, d, ["alert", "exhausted"], ["Arjun Mehta"] * 3 + ["Priya Nair"] * 3 + ["Rahul Iyer"], 1, True)
    return c


def s5_storm(api: Api) -> Check:
    c = Check("5 Failure storm")
    first = api.simulate("missing_file")
    api.wait(first, lambda d: d["status"] == "notified", "first alert")
    second = api.simulate("disk_full")
    d2 = api.finished(second)
    c.that(d2["status"] == "grouped" and d2["parent_id"] == first, f"second failure {d2['status']} under the first")
    c.that(d2["attempts"] == [], "no calls for the second failure")
    c.that([n["kind"] for n in d2["notifications"]] == ["grouped"], "Teams got a 'Related failure' card")
    api.answer_call(first, 1)
    d = api.finished(first)
    c.that(d["status"] == "acknowledged", f"first failure: {d['status']}")
    sides(c, d, ["alert", "ack"], ["Arjun Mehta"], 0, True)
    d2 = api.get(second)
    moved = [n for n in d2["notifications"] if n["channel"] == "servicenow" and n["kind"] == "transfer"]
    ok = bool(moved) and moved[-1]["status"] == "sent" and moved[-1]["target"].endswith(TEAM_QUEUE)
    c.that(ok, f"grouped failure's ServiceNow {d2['servicenow_number']} also moved to {TEAM_QUEUE}")
    c.that(
        bool(moved) and "Grouped under PAY_EOD_SETTLEMENT_LOAD" in moved[-1]["body"], "its work note names the first"
    )
    return c


def s8_parent_rerun(api: Api) -> Check:
    c = Check("8 Storm, then the first job reruns fine")
    first = api.simulate("missing_file")
    api.wait(first, lambda d: d["status"] == "notified", "first alert")
    second = api.simulate("disk_full")
    api.wait(second, lambda d: d["status"] == "grouped", "grouping")
    body = {"taskName": "PAY_EOD_SETTLEMENT_LOAD", "sysId": f"rerun-{first}", "status": "Success"}
    api.c.post("/api/webhooks/uac", json=body).raise_for_status()
    c.that(api.get(first)["status"] == "resolved", "first failure resolved by the rerun")
    d2 = api.wait(second, lambda d: d["status"] in ("notified", "calling", "waiting"), "second to escalate")
    c.that(d2["parent_id"] is None, f"grouped failure now escalates on its own ({d2['status']})")
    api.answer_call(second, 1)
    d2 = api.finished(second)
    c.that(d2["status"] == "acknowledged", f"SysOps sees: {d2['task_name']} {d2['status']} by {d2['acknowledged_by']}")
    sides(c, d2, ["grouped", "alert", "ack"], ["Arjun Mehta"], 0, True)
    return c


def s6_rerun(api: Api) -> Check:
    c = Check("6 Rerun fixes it")
    inc = api.simulate()
    api.wait(inc, lambda d: d["status"] == "notified", "Teams alert")
    body = {"taskName": "PAY_EOD_SETTLEMENT_LOAD", "sysId": f"rerun-{inc}", "status": "Success"}
    r = api.c.post("/api/webhooks/uac", json=body)
    c.that(r.status_code == 200 and r.json()["results"][0]["action"] == "resolved", "UAC Success webhook accepted")
    time.sleep(8)  # past the time the first call would have gone out
    d = api.get(inc)
    c.that(d["status"] == "resolved", f"SysOps sees: {d['status']}")
    sides(c, d, ["alert", "resolved"], [], 0, False)
    return c


def s7_teams_down(api: Api) -> Check:
    c = Check("7 Teams is down")
    inc = api.simulate()
    api.answer_call(inc, 1)
    d = api.finished(inc)
    c.that(d["status"] == "acknowledged", f"SysOps sees: {d['status']} by {d['acknowledged_by']}")
    failed = [n for n in d["notifications"] if n["channel"] == "teams" and n["status"] == "failed"]
    c.that(len(failed) == 2, f"both Teams posts recorded as failed ({len(failed)})")
    c.that(any(e["type"] == "teams_failed" for e in d["events"]), "timeline shows the Teams failure")
    sides(c, d, [], ["Arjun Mehta"], 0, True)
    return c


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://localhost:8022")
    ap.add_argument("--teams-down-base", default="http://localhost:8023")
    args = ap.parse_args()
    api, down = Api(args.base), Api(args.teams_down_base)
    runs = [(s1_teams_ack, api), (s2_primary_second, api), (s3_secondary, api), (s4_nobody, api),
            (s5_storm, api), (s6_rerun, api), (s7_teams_down, down), (s8_parent_rerun, api)]  # fmt: skip
    results = []
    for fn, target in runs:
        t0 = time.time()
        try:
            c = fn(target)
        except Exception as exc:  # report and carry on with the next scenario
            c = Check(fn.__name__)
            c.that(False, f"crashed: {type(exc).__name__}: {exc}")
        results.append(c)
        print(f"\n{'PASS' if c.ok else 'FAIL'}  {c.name}  ({time.time() - t0:.0f}s)")
        for n in c.notes:
            print("   ", n)
        sys.stdout.flush()
    passed = sum(c.ok for c in results)
    print(f"\n{passed}/{len(results)} scenarios passed")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
