"""Engine tests: the full control loop behaviour.

Covers the four core behaviours of the PRD:
advise mode (detect, alert, never mutate), auto mode (converge),
drift-vs-intended classification, and crash-safe persistence.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from moor.compose import ComposeFile
from moor.diff import diff_states
from moor.engine import (CONVERGE_PLANNED, DRIFT_DETECTED,
                         DRIFT_PERSISTENT, DRIFT_RESOLVED, Reconciler,
                         actual_hash, desired_hash)
from moor.compose import load_desired_state
from tests.conftest import (COMPOSE_PATH, COMPOSE_SCALED_PATH, PROJECT,
                            FakeGateway)


def _types(store):
    return [e["type"] for e in store.list(limit=500)]


def test_first_cycle_is_baseline(engine, store, alert_calls):
    result = engine.run_once()
    assert result["drift"] is False
    alerter, calls = alert_calls
    assert calls == []  # healthy startup sends no alerts
    snap = store.get_snapshot()
    assert snap["desired_hash"] == desired_hash(load_desired_state(COMPOSE_PATH, PROJECT))


def test_advise_mode_detects_and_alerts_without_mutating(engine, store, alert_calls):
    engine.run_once()  # baseline

    gateway: FakeGateway = engine.gateway
    gateway.kill("web")
    gateway.add_rogue("cache", 3)
    gateway.mutate_env("db", "POSTGRES_PASSWORD", "rogue")

    result = engine.run_once()
    assert result["drift"] is True
    assert result["classification"] == "drift"
    assert result["mode"] == "advise"
    assert result["actions"] is None  # advise never mutates

    types = _types(store)
    assert types.count(DRIFT_DETECTED) == 3  # one per service
    alerter, calls = alert_calls
    assert calls, "webhook alert expected"
    payload = calls[0]["json"]
    assert payload["username"] == "Moor"
    assert payload["attachments"][0]["color"] == "#e01b24"  # critical
    assert "drift" in payload["text"].lower()

    # environment untouched: drift persists visibly
    live_cache = engine.gateway.list_containers().live_for_service("cache")
    assert len(live_cache) == 4
    web_live = engine.gateway.list_containers().live_for_service("web")
    assert len(web_live) == 0


def test_auto_mode_remediates_all_drift(engine, store, alert_calls):
    engine.run_once()
    store.set_mode("auto")

    gateway: FakeGateway = engine.gateway
    gateway.kill("web")
    gateway.add_rogue("cache", 3)
    gateway.mutate_env("db", "POSTGRES_PASSWORD", "rogue")

    result = engine.run_once()
    assert result["drift"] is True
    assert result["resolved"] is True
    # 3 rogue-removes + db remove/create + web start
    assert result["actions"]["ok"] == 6
    assert result["actions"]["failed"] == 0

    # convergence verified by re-observation through the real diff path
    desired = load_desired_state(COMPOSE_PATH, PROJECT)
    report = diff_states(desired, gateway.list_containers(), gateway.image_env)
    assert not report.has_drift

    types = _types(store)
    assert DRIFT_RESOLVED in types
    alerter, calls = alert_calls
    assert any("remediated" in c["json"]["text"].lower() for c in calls)


def test_intended_change_classified_and_converged(engine, store, alert_calls):
    engine.run_once()
    store.set_mode("auto")

    # the declaration moves: cache 1 -> 3 replicas
    engine.compose = ComposeFile(COMPOSE_SCALED_PATH, PROJECT)

    result = engine.run_once()
    assert result["classification"] == "intended"
    assert result["drift"] is True  # environment lags the new declaration
    assert result["resolved"] is True

    types = _types(store)
    assert CONVERGE_PLANNED in types
    assert DRIFT_DETECTED not in types  # intended change is not drift
    live = engine.gateway.list_containers().live_for_service("cache")
    assert len(live) == 3


def test_resolved_event_when_drift_disappears(engine, store):
    engine.run_once()
    gateway: FakeGateway = engine.gateway
    gateway.kill("web")
    engine.run_once()  # detected in advise mode

    # someone repairs by hand; next cycle must emit drift.resolved
    for record in gateway.containers.values():
        if record["service"] == "web":
            record["state"] = "running"
    result = engine.run_once()
    assert result["drift"] is False
    assert DRIFT_RESOLVED in _types(store)


def test_persistent_drift_backs_off(engine, store):
    engine.run_once()
    store.set_mode("auto")
    gateway: FakeGateway = engine.gateway
    gateway.fail_on_create = True
    gateway.kill("web")
    # kill leaves an exited container whose spec still matches -> the
    # planner would emit a START action; force CREATE by removing it.
    for cid in [r["id"] for r in gateway.containers.values() if r["service"] == "web"]:
        gateway.containers.pop(cid)

    result = engine.run_once()
    assert result["drift"] is True
    assert result["resolved"] is False
    assert DRIFT_PERSISTENT in _types(store)
    assert store.in_backoff("web") is True

    # while in backoff, a new cycle does not retry remediation
    gateway.fail_on_create = False
    result = engine.run_once()
    assert result["actions"] is None


def test_no_duplicate_alerts_for_unchanged_drift(engine, store, alert_calls):
    engine.run_once()
    gateway: FakeGateway = engine.gateway
    gateway.kill("web")
    engine.run_once()
    engine.run_once()  # same drift, second cycle

    types = _types(store)
    assert types.count(DRIFT_DETECTED) == 1  # deduplicated
    alerter, calls = alert_calls
    assert len([c for c in calls if "drift detected" in c["json"]["text"].lower()]) == 1


def test_engine_thread_lifecycle(engine, store):
    engine.start()
    try:
        assert engine._thread.is_alive()
    finally:
        engine.stop()
    assert not engine._thread.is_alive()


def test_current_state_shape(engine):
    state = engine.current_state()
    assert state["project"] == PROJECT
    assert {s["name"] for s in state["services"]} == {"web", "cache", "db"}
    assert all(s["status"] == "compliant" for s in state["services"])
    assert state["mode"] == "advise"


def test_planned_actions_empty_when_compliant(engine):
    plan = engine.planned_actions()
    assert plan["count"] == 0
    assert plan["mode"] == "advise"
