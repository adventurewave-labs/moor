"""Chaos injection tests: the dashboard button's whole path.

Every test drives the real API endpoint against the in-memory gateway:
mutation -> chaos.injected event -> drift classification -> (in auto
mode) remediation. The fake gateway implements the same mutation
interface as DockerGateway, so these verify the wiring, not the Docker
SDK.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from fastapi.testclient import TestClient

from moor.api import create_app
from moor.chaos import CHAOS_ACTIONS, CHAOS_INJECTED
from tests.conftest import PROJECT


@pytest.fixture
def client(gateway, store, alert_calls, engine):
    alerter, _ = alert_calls
    app = create_app(
        gateway=gateway, store=store, alerter=alerter,
        engine=engine, start_engine=False,
    )
    with TestClient(app) as test_client:
        yield test_client


def _kinds(client) -> set[str]:
    return {i["kind"] for i in client.get("/api/drift").json()["items"]}


def _types(client, limit: int = 30) -> list[str]:
    events = client.get("/api/events", params={"limit": limit}).json()["events"]
    return [e["type"] for e in events]


# ------------------------------------------------------------- each action

@pytest.mark.parametrize("action,expected_kind", [
    ("kill", "replicas_missing"),
    ("scale", "replicas_extra"),
    ("env", "env_changed"),
    ("image", "image_changed"),
])
def test_each_action_produces_its_drift(client, action, expected_kind):
    r = client.post("/api/chaos", json={"action": action})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["action"] == action
    assert body["service"] in ("web", "cache", "db")
    assert body["detail"]
    drift = client.get("/api/drift").json()
    assert drift["has_drift"] is True
    assert expected_kind in _kinds(client)


def test_kill_is_a_real_dead_container(client, gateway):
    r = client.post("/api/chaos", json={"action": "kill", "service": "web"})
    assert r.status_code == 200
    assert r.json()["container_name"] == f"{PROJECT}-web-1"
    # the mutation was real: nothing is left running for web
    assert gateway.list_containers().live_for_service("web") == []


def test_scale_names_rogue_containers(client):
    r = client.post("/api/chaos", json={"action": "scale", "service": "cache"})
    body = r.json()
    assert len(body["containers"]) == 2
    assert all(name.endswith("-rogue1") or name.endswith("-rogue2")
               for name in body["containers"])
    names = {c.name for c in client.app.state.gateway.list_containers().for_service("cache")}
    assert set(body["containers"]) <= names


def test_env_mutates_a_declared_variable(client):
    r = client.post("/api/chaos", json={"action": "env", "service": "db"})
    detail = r.json()["detail"]
    # mutates a declared db variable (POSTGRES_DB or POSTGRES_PASSWORD)
    assert "POSTGRES_" in detail
    assert "='rogue'" in detail
    assert "(declared " in detail


def test_image_prefers_same_repo_tag(client):
    r = client.post("/api/chaos", json={"action": "image", "service": "cache"})
    detail = r.json()["detail"]
    assert "redis:6.2-alpine" in detail  # same repo, different tag, present locally


# ---------------------------------------------------------- full lifecycle

def test_chaos_event_lands_in_audit_trail(client):
    client.post("/api/chaos", json={"action": "kill", "service": "web"})
    events = client.get("/api/events", params={"limit": 5}).json()["events"]
    chaos_events = [e for e in events if e["type"] == CHAOS_INJECTED]
    assert chaos_events
    assert chaos_events[-1]["severity"] == "warning"
    assert chaos_events[-1]["service"] == "web"
    assert "docker kill" in chaos_events[-1]["payload"]["summary"]


def test_auto_mode_detects_and_repairs_chaos(client, store, gateway):
    store.set_mode("auto")
    r = client.post("/api/chaos", json={"action": "kill", "service": "web"})
    assert r.status_code == 200
    result = client.post("/api/reconcile").json()
    assert result["resolved"] is True
    assert gateway.list_containers().live_for_service("web")
    types = _types(client)
    assert CHAOS_INJECTED in types
    assert "drift.detected" in types
    assert "drift.resolved" in types


def test_random_action_is_one_of_the_four(client):
    r = client.post("/api/chaos", json={"action": "random"})
    assert r.status_code == 200
    assert r.json()["action"] in ("kill", "scale", "env", "image")
    assert client.get("/api/drift").json()["has_drift"] is True


def test_post_without_body_defaults_to_random(client):
    r = client.post("/api/chaos")
    assert r.status_code == 200
    assert r.json()["action"] in ("kill", "scale", "env", "image")


# -------------------------------------------------------------- rejections

def test_unknown_action_is_rejected(client):
    r = client.post("/api/chaos", json={"action": "bogus"})
    assert r.status_code == 400
    assert "bogus" in r.json()["detail"]


def test_unmanaged_service_is_rejected(client):
    # control-plane services opt out of management; chaos must refuse
    r = client.post("/api/chaos", json={"action": "kill", "service": "moor"})
    assert r.status_code == 400


def test_no_running_container_is_rejected(client, gateway):
    gateway.kill("web")  # scenario helper: the container is already dead
    r = client.post("/api/chaos", json={"action": "kill", "service": "web"})
    assert r.status_code == 409
