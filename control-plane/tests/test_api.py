"""API tests: routes, mode switching, reconcile trigger, alert payload."""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from fastapi.testclient import TestClient

from moor.api import create_app
from moor.compose import ComposeFile
from tests.conftest import COMPOSE_PATH, PROJECT


@pytest.fixture
def client(gateway, store, alert_calls, engine):
    alerter, _ = alert_calls
    app = create_app(
        gateway=gateway, store=store, alerter=alerter,
        engine=engine, start_engine=False,
    )
    with TestClient(app) as test_client:
        yield test_client


def test_health(client):
    r = client.get("/api/health")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["project"] == PROJECT
    assert body["docker"]["ok"] is True


def test_state_and_drift(client):
    r = client.get("/api/state")
    assert r.status_code == 200
    state = r.json()
    assert len(state["services"]) == 3
    assert state["drift"]["has_drift"] is False

    r = client.get("/api/drift")
    assert r.status_code == 200
    assert r.json()["has_drift"] is False


def test_plan_endpoint(client, gateway):
    gateway.kill("web")
    r = client.get("/api/plan")
    assert r.status_code == 200
    plan = r.json()
    assert plan["count"] == 1
    assert plan["actions"][0]["kind"] == "start"


def test_mode_switch(client, store):
    assert client.get("/api/state").json()["mode"] == "advise"
    r = client.post("/api/mode", json={"mode": "auto"})
    assert r.status_code == 200
    assert r.json()["mode"] == "auto"
    assert store.get_mode() == "auto"

    r = client.post("/api/mode", json={"mode": "bogus"})
    assert r.status_code == 400


def test_reconcile_endpoint_advises(client, gateway):
    gateway.kill("web")
    r = client.post("/api/reconcile")
    assert r.status_code == 200
    body = r.json()
    assert body["drift"] is True
    assert body["mode"] == "advise"
    assert body["actions"] is None
    # advise mode: container still dead
    assert not gateway.list_containers().live_for_service("web")


def test_reconcile_endpoint_in_auto_mode_fixes(client, store, gateway):
    store.set_mode("auto")
    gateway.kill("web")
    r = client.post("/api/reconcile")
    body = r.json()
    assert body["resolved"] is True
    assert gateway.list_containers().live_for_service("web")


def test_events_endpoint(client, gateway):
    gateway.kill("web")
    client.post("/api/reconcile")  # detection generates audit events
    r = client.get("/api/events", params={"limit": 50})
    assert r.status_code == 200
    events = r.json()["events"]
    assert events, "expected audit events"
    assert any(e["type"] == "drift.detected" for e in events)
    ids = [e["id"] for e in events]
    assert ids == sorted(ids)


def test_dashboard_served(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "MOOR" in r.text
    assert "desired-state control plane" in r.text


def test_alert_payload_is_slack_format(alert_calls, engine, gateway):
    alerter, calls = alert_calls
    gateway.kill("web")
    engine.run_once()
    assert calls
    payload = calls[0]["json"]
    assert set(payload) >= {"username", "text", "attachments"}
    att = payload["attachments"][0]
    assert set(att) >= {"color", "title", "text", "fields", "footer", "ts"}
    assert any(f["title"] == "project" for f in att["fields"])
