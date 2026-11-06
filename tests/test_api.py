"""REST API, webhooks, auth and the acknowledgement pages."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.security import sign_ack_token


@pytest.fixture
def client(settings, svc):
    settings.uac_webhook_secret = "hook-secret"
    app = create_app(settings, services=svc)
    with TestClient(app) as c:
        c.svc = svc
        yield c


def test_health_and_ready(client):
    assert client.get("/api/health").json() == {"status": "ok"}
    assert client.get("/api/ready").json()["database"] is True


def test_dashboard_served_with_security_headers(client):
    r = client.get("/")
    assert r.status_code == 200 and "OnCallPilot" in r.text
    assert r.headers["x-frame-options"] == "DENY"


def test_manual_alert_and_detail(client):
    r = client.post("/api/incidents", json={"task_name": "CDP_CUSTOMER_MERGE", "error_message": "ORA-01017"})
    assert r.status_code == 201 and r.json()["team_id"] == "customer-data"
    detail = client.get(f"/api/incidents/{r.json()['id']}").json()
    assert detail["status"] == "new" and detail["policy"]["levels"][0]["role"] == "primary"


def test_manual_alert_rejects_bad_override(client):
    r = client.post("/api/incidents", json={"task_name": "X", "override": {"interval_minutes": 0}})
    assert r.status_code == 422
    r = client.post("/api/incidents", json={"task_name": "X", "team_id": "nope"})
    assert r.status_code == 422


def test_uac_webhook_requires_token(client):
    body = {"taskName": "PAY_X", "sysId": "abc", "status": "Failed"}
    assert client.post("/api/webhooks/uac", json=body).status_code == 401
    r = client.post("/api/webhooks/uac", json=body, headers={"X-Webhook-Token": "hook-secret"})
    assert r.status_code == 200 and r.json()["results"][0]["action"] == "opened"
    again = client.post("/api/webhooks/uac", json=body, headers={"X-Webhook-Token": "hook-secret"})
    assert again.json()["results"][0]["action"] == "duplicate"


def test_uac_success_webhook_resolves(client):
    h = {"X-Webhook-Token": "hook-secret"}
    opened = client.post("/api/webhooks/uac", json={"taskName": "PAY_Y", "sysId": "1", "status": "Failed"}, headers=h)
    inc_id = opened.json()["results"][0]["id"]
    r = client.post("/api/webhooks/uac", json={"taskName": "PAY_Y", "sysId": "2", "status": "Success"}, headers=h)
    assert r.json()["results"][0]["incidents"] == [inc_id]
    assert client.get(f"/api/incidents/{inc_id}").json()["status"] == "resolved"


def test_ack_link_get_does_not_ack_but_post_does(client, settings):
    inc_id = client.post("/api/incidents", json={"task_name": "PAY_Z"}).json()["id"]
    token = sign_ack_token(settings.secret_key, inc_id, "teams", 3600)

    page = client.get(f"/ack/{token}")  # what a Teams link preview would do
    assert page.status_code == 200 and "<form" in page.text
    assert client.get(f"/api/incidents/{inc_id}").json()["status"] == "new"

    done = client.post(f"/ack/{token}", data={"by": "Arjun"})
    assert "Thanks, Arjun" in done.text
    detail = client.get(f"/api/incidents/{inc_id}").json()
    assert detail["status"] == "acknowledged" and detail["ack_channel"] == "teams"


def test_tampered_ack_link_rejected(client, settings):
    inc_id = client.post("/api/incidents", json={"task_name": "PAY_Q"}).json()["id"]
    token = sign_ack_token(settings.secret_key, inc_id, "teams", 3600)
    bad = token[:-2] + ("aa" if not token.endswith("aa") else "bb")
    assert client.post(f"/ack/{bad}", data={"by": "x"}).status_code == 400


def test_api_token_enforced(settings, svc):
    settings.api_token = "s3cret"
    with TestClient(create_app(settings, services=svc)) as c:
        assert c.get("/api/incidents").status_code == 401
        assert c.get("/api/incidents", headers={"Authorization": "Bearer s3cret"}).status_code == 200
        assert c.get("/api/health").status_code == 200  # health stays open for load balancers


def test_demo_flow_and_phone_numbers_masked(client):
    r = client.post("/api/demo/failure", json={"scenario": "missing_file"})
    inc_id = r.json()["id"]
    client.portal.call(client.app.state.engine.tick)  # the worker is off in tests; run one step by hand
    d = client.get(f"/api/incidents/{inc_id}").json()
    assert d["status"] == "notified"
    phone = d["oncall"]["contacts"]["primary"]["phone"]
    assert phone.endswith("0101") and "*" in phone and "9800000101" not in phone
    assert d["triage"]["category"] == "missing_input"


def test_screenshot_needs_llm(client):
    r = client.post("/api/incidents/extract", files={"file": ("s.png", b"\x89PNG", "image/png")})
    assert r.status_code == 503


def test_prod_refuses_unsafe_config(settings):
    settings.app_env = "prod"
    settings.secret_key = "short"
    with pytest.raises(RuntimeError, match="SECRET_KEY"):
        create_app(settings)


def test_metrics_endpoint(client):
    client.post("/api/incidents", json={"task_name": "PAY_M"})
    assert "oncallpilot_incidents_total" in client.get("/metrics").text
