"""Security helpers, policy loading, AI triage and the real adapters (against mocked HTTP)."""

from __future__ import annotations

import json

import httpx
import pytest

from app.ai.llm import LLMClient, LLMError, extract_json
from app.ai.triage import Triager, rule_triage
from app.clock import FakeClock
from app.integrations.base import CallStatus, ChatMessage
from app.integrations.microsoft import TeamsGraphVoice, TeamsWebhookChat
from app.integrations.servicenow import ServiceNowClient
from app.integrations.uac import parse_task_instance
from app.policy import Policy, PolicyOverride, load_directory
from app.security import InvalidToken, mask_phone, redact, sign_ack_token, verify_ack_token
from tests.conftest import ROOT, failure

# ---------------------------------------------------------------- security


def test_redact_masks_secrets():
    text = (
        "connect password=Winter2026! token: abc123xyz Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.payload "
        "url=https://svc:hunter2@db.internal/x key AKIAABCDEFGHIJKLMNOP"
    )
    out = redact(text)
    for secret in ("Winter2026!", "abc123xyz", "eyJhbGciOiJIUzI1NiJ9", "hunter2", "AKIAABCDEFGHIJKLMNOP"):
        assert secret not in out
    assert "password=****" in out


def test_mask_phone():
    assert mask_phone("+919800000101") == "+********0101"
    assert mask_phone("12") == "****"


def test_ack_token_roundtrip_expiry_and_tamper():
    tok = sign_ack_token("k" * 32, "inc1", "teams", 60, now=1000)
    assert verify_ack_token("k" * 32, tok, now=1030)["i"] == "inc1"
    with pytest.raises(InvalidToken, match="expired"):
        verify_ack_token("k" * 32, tok, now=2000)
    with pytest.raises(InvalidToken):
        verify_ack_token("other-key" * 4, tok, now=1030)
    with pytest.raises(InvalidToken):
        verify_ack_token("k" * 32, "garbage", now=1030)


# ---------------------------------------------------------------- policy


def test_directory_routes_tasks(monkeypatch):
    monkeypatch.setenv("PAYMENTS_TEAMS_WEBHOOK", "https://example.test/hook")
    d = load_directory(ROOT / "config/teams.yaml")
    assert d.resolve("PAY_EOD").id == "payments"
    assert d.resolve("pay_eod").id == "payments"  # case-insensitive
    assert d.resolve("ETL_CUST_DAILY").id == "customer-data"
    assert d.resolve("anything", business_service="Payments").id == "payments"
    assert d.resolve("ZZZ").id == "batch-ops"
    assert d.get("payments").teams_webhook_url == "https://example.test/hook"


def test_policy_override():
    p = Policy().with_overrides(PolicyOverride(interval_minutes=10, primary_attempts=2))
    assert [lvl.interval_minutes for lvl in p.levels] == [10, 10, 10]
    assert p.levels[0].attempts == 2 and p.levels[1].attempts == 3


def test_policy_validation():
    with pytest.raises(ValueError):
        Policy(levels=[])
    with pytest.raises(ValueError):
        PolicyOverride(interval_minutes=500)


# ---------------------------------------------------------------- triage


@pytest.mark.parametrize(
    ("error", "category"),
    [
        ("No such file or directory: /in/x.csv", "missing_input"),
        ("ORA-01017: invalid username/password", "authentication"),
        ("java.net.ConnectException: Connection refused", "connectivity"),
        ("gzip: write error: No space left on device", "resources"),
        ("Permission denied: /data/out", "permissions"),
        ("something odd happened", "unknown"),
    ],
)
def test_rule_triage(error, category):
    assert rule_triage(failure(error_message=error), "P2").category == category


def _llm_returning(content: str, status: int = 200, seen: list | None = None) -> LLMClient:
    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(json.loads(request.content))
        return httpx.Response(status, json={"choices": [{"message": {"content": content}}]})

    return LLMClient(
        "key", "https://llm.test/v1", "m", client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )


async def test_llm_triage_masks_secrets_and_parses():
    seen: list = []
    answer = {
        "summary": "Settlement load failed: input file missing.",
        "probable_cause": "Card processor feed not received.",
        "category": "missing_input",
        "severity": "P2",
        "recommended_action": "Check the feed arrival, then rerun.",
        "confidence": 0.85,
    }
    triager = Triager(_llm_returning(f"```json\n{json.dumps(answer)}\n```", seen=seen))
    event = failure(log_excerpt="login password=TopSecret1 failed\nNo such file")
    t = await triager.triage(event, "Payments", "P3")
    assert t.source == "llm" and t.category == "missing_input" and t.severity == "P2"
    assert "TopSecret1" not in json.dumps(seen)


async def test_llm_garbage_falls_back_to_rules():
    t = await Triager(_llm_returning("I think it broke")).triage(failure(), "Payments", "P3")
    assert t.source == "rules" and t.category == "missing_input"


async def test_llm_invalid_schema_falls_back_to_rules():
    t = await Triager(_llm_returning('{"summary": "x", "category": "aliens"}')).triage(failure(), "P", "P3")
    assert t.source == "rules"


async def test_llm_http_error_falls_back_to_rules():
    t = await Triager(_llm_returning("", status=500)).triage(failure(), "Payments", "P3")
    assert t.source == "rules"


def test_extract_json():
    assert extract_json('noise {"a": 1} noise') == {"a": 1}
    with pytest.raises(LLMError):
        extract_json("[1, 2]")


# ---------------------------------------------------------------- UAC


def test_parse_uac_payload_variants():
    e = parse_task_instance(
        {"taskInstance": {"name": "PAY_X", "sysId": "s1", "status": "Start Failure", "exitCode": 8,
                          "businessServices": ["Payments"], "endTime": "2026-11-06 02:10:04 +0530"}},
        "uac_webhook",
    )  # fmt: skip
    assert (e.task_name, e.external_id, e.uac_status, e.exit_code) == ("PAY_X", "s1", "START_FAILURE", "8")
    assert e.business_service == "Payments" and e.failed_at.utcoffset().total_seconds() == 19800
    assert e.dedupe_key == "uac:s1"
    with pytest.raises(ValueError):
        parse_task_instance({"status": "Failed"}, "uac_webhook")


# ---------------------------------------------------------------- ServiceNow


async def test_servicenow_who_is_on_call():
    def handler(r: httpx.Request) -> httpx.Response:
        path = r.url.path
        if path.endswith("/sys_user_group"):
            return httpx.Response(200, json={"result": [{"sys_id": "g1", "name": "Pay", "manager": {"value": "u9"}}]})
        if path.endswith("/whoisoncall"):
            assert r.url.params["group_ids"] == "g1"
            return httpx.Response(200, json={"result": [{"userId": "u2", "order": 2}, {"userId": "u1", "order": 1}]})
        users = {
            "u1": {"name": "Prim", "mobile_phone": "+911"},
            "u2": {"name": "Sec", "phone": "+912"},
            "u9": {"name": "Boss", "mobile_phone": "+919"},
        }
        return httpx.Response(200, json={"result": users[path.rsplit("/", 1)[-1]]})

    sn = ServiceNowClient("https://sn.test", "u", "p", client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    oncall = await sn.who_is_on_call("Pay")
    assert {r: c.name for r, c in oncall.contacts.items()} == {"primary": "Prim", "secondary": "Sec", "manager": "Boss"}
    assert oncall.contacts["secondary"].phone == "+912"


# ---------------------------------------------------------------- Microsoft


async def test_teams_webhook_posts_adaptive_card_and_retries():
    calls = []

    def handler(r: httpx.Request) -> httpx.Response:
        calls.append(json.loads(r.content))
        return httpx.Response(503 if len(calls) == 1 else 202)

    chat = TeamsWebhookChat(client=httpx.AsyncClient(transport=httpx.MockTransport(handler)), retry_delay=0)
    await chat.post("https://hook.test", ChatMessage("t", "x", {"type": "AdaptiveCard"}))
    assert len(calls) == 2
    assert calls[1]["attachments"][0]["contentType"] == "application/vnd.microsoft.card.adaptive"


def test_graph_call_notifications_map_to_status():
    class _Tok:
        async def get(self):
            return "t"

    v = TeamsGraphVoice(_Tok(), "tenant", "app-instance", "OnCallPilot", "https://cb.test")
    v.states["c1"] = CallStatus.RINGING
    v.handle_notification({"resourceData": {"id": "c1", "state": "established"}})
    assert v.states["c1"] == CallStatus.ANSWERED
    v.states["c2"] = CallStatus.RINGING
    v.handle_notification({"resourceData": {"id": "c2", "state": "terminated", "resultInfo": {"code": 486}}})
    assert v.states["c2"] == CallStatus.BUSY


def test_fake_clock():
    c = FakeClock()
    t0 = c.now()
    assert (c.advance(minutes=5) - t0).total_seconds() == 300


def test_public_url_from_render(monkeypatch):
    from app.config import Settings

    monkeypatch.setenv("RENDER_EXTERNAL_URL", "https://oncallpilot.onrender.com/")
    assert Settings(_env_file=None).public_base_url == "https://oncallpilot.onrender.com"
    assert (
        Settings(_env_file=None, public_base_url="https://ops.example.com").public_base_url == "https://ops.example.com"
    )
