"""Planner + executor tests: remediation must restore declared state."""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from moor.actions import build_plan, execute_plan
from moor.compose import load_desired_state
from moor.diff import diff_states
from moor.models import (ACTION_CREATE, ACTION_REMOVE, ACTION_START,
                         DRIFT_REPLICAS_EXTRA, DRIFT_REPLICAS_MISSING)
from tests.conftest import COMPOSE_PATH, PROJECT


def _desired():
    return load_desired_state(COMPOSE_PATH, PROJECT)


def _events():
    out: list[tuple] = []

    def emit(type_, service, severity, payload):
        out.append((type_, service, severity, payload))

    return emit, out


def test_baseline_plan_is_empty(gateway):
    desired = _desired()
    actual = gateway.list_containers()
    report = diff_states(desired, actual, gateway.image_env)
    assert build_plan(desired, actual, report).empty


def test_kill_produces_start_action_for_matching_dead_container(gateway):
    gateway.kill("web")
    desired = _desired()
    actual = gateway.list_containers()
    report = diff_states(desired, actual, gateway.image_env)
    plan = build_plan(desired, actual, report)
    assert [a.kind for a in plan.actions] == [ACTION_START]
    assert plan.actions[0].reason == DRIFT_REPLICAS_MISSING

    emit, out = _events()
    execute_plan(gateway, plan, emit)
    assert gateway.list_containers().live_for_service("web")  # alive again
    assert [e[0] for e in out] == ["action.ok"]


def test_killed_container_with_image_default_command_is_restarted(gateway):
    # Real Docker records the image default command on every container;
    # the planner must restart the killed container instead of stacking a
    # new one (caught live in a Codespace against Docker Engine 29).
    for r in gateway.containers.values():
        if not r["command"]:
            r["command"] = list(gateway.image_cmd(r["image"]) or ())
    gateway.kill("web")
    desired = _desired()
    actual = gateway.list_containers()
    report = diff_states(
        desired, actual, gateway.image_env, image_cmd_fn=gateway.image_cmd
    )
    plan = build_plan(desired, actual, report, image_cmd_fn=gateway.image_cmd)
    assert [a.kind for a in plan.actions] == [ACTION_START]
    assert plan.actions[0].service == "web"


def test_rogue_replicas_produce_removals(gateway):
    gateway.add_rogue("cache", 3)
    desired = _desired()
    actual = gateway.list_containers()
    report = diff_states(desired, actual, gateway.image_env)
    plan = build_plan(desired, actual, report)
    removes = [a for a in plan.actions if a.kind == ACTION_REMOVE]
    assert len(removes) == 3
    assert all(a.reason == DRIFT_REPLICAS_EXTRA for a in removes)

    emit, out = _events()
    execute_plan(gateway, plan, emit)
    assert len(gateway.list_containers().live_for_service("cache")) == 1
    assert len([e for e in out if e[0] == "action.ok"]) == 3


def test_env_drift_produces_recreate(gateway):
    gateway.mutate_env("db", "POSTGRES_PASSWORD", "rogue")
    desired = _desired()
    actual = gateway.list_containers()
    report = diff_states(desired, actual, gateway.image_env)
    plan = build_plan(desired, actual, report)
    kinds = [a.kind for a in plan.actions]
    assert kinds == [ACTION_REMOVE, ACTION_CREATE]

    emit, _ = _events()
    execute_plan(gateway, plan, emit)
    report_after = diff_states(
        desired, gateway.list_containers(), gateway.image_env
    )
    assert not report_after.has_drift
    # recreated container carries the declared env, not the mutated one
    db = gateway.list_containers().for_service("db")[0]
    assert db.env["POSTGRES_PASSWORD"] == "demo"


def test_image_swap_produces_recreate_with_declared_image(gateway):
    gateway.swap_image("cache", "redis:6.2-alpine")
    desired = _desired()
    actual = gateway.list_containers()
    report = diff_states(desired, actual, gateway.image_env)
    plan = build_plan(desired, actual, report)
    emit, _ = _events()
    execute_plan(gateway, plan, emit)
    cache = gateway.list_containers().for_service("cache")[0]
    assert cache.image == "redis:7.2-alpine"


def test_missing_service_created_from_scratch(gateway):
    # simulate 'the whole service vanished'
    for cid in [r["id"] for r in gateway.containers.values() if r["service"] == "db"]:
        gateway.containers.pop(cid)
    desired = _desired()
    actual = gateway.list_containers()
    report = diff_states(desired, actual, gateway.image_env)
    plan = build_plan(desired, actual, report)
    assert [a.kind for a in plan.actions] == [ACTION_CREATE]
    emit, _ = _events()
    execute_plan(gateway, plan, emit)
    assert len(gateway.list_containers().live_for_service("db")) == 1


def test_created_container_gets_compose_labels(gateway):
    for cid in [r["id"] for r in gateway.containers.values() if r["service"] == "db"]:
        gateway.containers.pop(cid)
    desired = _desired()
    actual = gateway.list_containers()
    report = diff_states(desired, actual, gateway.image_env)
    plan = build_plan(desired, actual, report)
    emit, _ = _events()
    execute_plan(gateway, plan, emit)
    created = gateway.created_events[0]
    assert created["labels"]["com.docker.compose.project"] == PROJECT
    assert created["labels"]["com.docker.compose.service"] == "db"
    assert created["labels"]["moor.owned"] == "true"


def test_failed_action_is_reported_not_raised(gateway):
    gateway.fail_on_create = True
    for cid in [r["id"] for r in gateway.containers.values() if r["service"] == "db"]:
        gateway.containers.pop(cid)
    desired = _desired()
    actual = gateway.list_containers()
    report = diff_states(desired, actual, gateway.image_env)
    plan = build_plan(desired, actual, report)
    emit, out = _events()
    summary = execute_plan(gateway, plan, emit)
    assert summary["failed"] == 1
    assert out[-1][0] == "action.failed"
    assert "simulated docker create failure" in out[-1][3]["error"]
