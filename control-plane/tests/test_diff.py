"""Diff engine tests: every drift kind must be detected, and only that."""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from moor.diff import diff_states
from moor.models import (DRIFT_COMMAND, DRIFT_ENV, DRIFT_IMAGE, DRIFT_PORTS,
                         DRIFT_REPLICAS_EXTRA, DRIFT_REPLICAS_MISSING,
                         DRIFT_SERVICE_ORPHAN)
from moor.compose import load_desired_state
from tests.conftest import COMPOSE_PATH, PROJECT


def _desired():
    return load_desired_state(COMPOSE_PATH, PROJECT)


def test_baseline_compliant(gateway):
    report = diff_states(_desired(), gateway.list_containers(), gateway.image_env)
    assert not report.has_drift
    assert report.hash


def test_killed_container_is_missing_replica(gateway):
    gateway.kill("web")
    report = diff_states(_desired(), gateway.list_containers(), gateway.image_env)
    kinds = {i.kind for i in report.for_service("web")}
    assert kinds == {DRIFT_REPLICAS_MISSING}


def test_rogue_scaling_is_extra_replicas(gateway):
    gateway.add_rogue("cache", 3)
    report = diff_states(_desired(), gateway.list_containers(), gateway.image_env)
    items = report.for_service("cache")
    assert len(items) == 1
    assert items[0].kind == DRIFT_REPLICAS_EXTRA
    assert len(items[0].details["extra_containers"]) == 3


def test_image_swap_detected(gateway):
    gateway.swap_image("cache", "redis:6.2-alpine")
    report = diff_states(_desired(), gateway.list_containers(), gateway.image_env)
    kinds = {i.kind for i in report.for_service("cache")}
    assert DRIFT_IMAGE in kinds


def test_env_value_change_detected(gateway):
    gateway.mutate_env("db", "POSTGRES_PASSWORD", "rogue")
    report = diff_states(_desired(), gateway.list_containers(), gateway.image_env)
    items = report.for_service("db")
    assert {i.kind for i in items} == {DRIFT_ENV}
    changed = items[0].details["changed"]
    assert "POSTGRES_PASSWORD" in changed
    # secrets are masked in the report
    assert changed["POSTGRES_PASSWORD"]["declared"] == "****"
    assert changed["POSTGRES_PASSWORD"]["actual"] == "****"


def test_injected_extra_env_detected(gateway):
    # an env key that is neither declared nor baked into the image
    gateway.mutate_env("db", "ROGUE_KEY", "1")
    report = diff_states(_desired(), gateway.list_containers(), gateway.image_env)
    items = report.for_service("db")
    assert {i.kind for i in items} == {DRIFT_ENV}
    assert "ROGUE_KEY" in items[0].details["extra_keys"]


def test_image_baked_env_is_not_drift(gateway):
    # POSTGRES_DB is declared; PATH/PGDATA come baked in the image.
    # Adding a baked key to a container must not raise env drift.
    db = next(
        r for r in gateway.containers.values() if r["service"] == "db"
    )
    db["env"]["PATH"] = "/usr/local/bin"
    report = diff_states(_desired(), gateway.list_containers(), gateway.image_env)
    assert not report.for_service("db")


def test_command_drift_detected(gateway):
    db = next(r for r in gateway.containers.values() if r["service"] == "db")
    db["command"] = ["postgres", "--wrong-flag"]
    report = diff_states(_desired(), gateway.list_containers(), gateway.image_env)
    kinds = {i.kind for i in report.for_service("db")}
    assert DRIFT_COMMAND in kinds


def test_port_drift_detected(gateway):
    web = next(r for r in gateway.containers.values() if r["service"] == "web")
    web["ports"] = {"80/tcp": 9999}
    report = diff_states(_desired(), gateway.list_containers(), gateway.image_env)
    kinds = {i.kind for i in report.for_service("web")}
    assert DRIFT_PORTS in kinds


def test_orphaned_service_detected(gateway):
    from tests.conftest import IMAGE_ENVS, _next_id
    import time

    record = {
        "id": _next_id(),
        "name": f"{PROJECT}-legacy-1",
        "service": "legacy",
        "image": "nginx:1.27-alpine",
        "env": {},
        "command": None,
        "ports": {},
        "state": "running",
        "labels": {
            "com.docker.compose.project": PROJECT,
            "com.docker.compose.service": "legacy",
            "moor.manage": "true",
        },
        "networks": (f"{PROJECT}_default",),
        "created": time.time(),
    }
    gateway.containers[record["id"]] = record
    report = diff_states(_desired(), gateway.list_containers(), gateway.image_env)
    items = report.for_service("legacy")
    assert len(items) == 1
    assert items[0].kind == DRIFT_SERVICE_ORPHAN


def test_unmanaged_containers_ignored(gateway):
    from tests.conftest import _next_id
    import time

    record = {
        "id": _next_id(),
        "name": f"{PROJECT}-tooling-1",
        "service": "tooling",
        "image": "nginx:1.27-alpine",
        "env": {},
        "command": None,
        "ports": {},
        "state": "running",
        "labels": {
            "com.docker.compose.project": PROJECT,
            "com.docker.compose.service": "tooling",
            "moor.manage": "false",
        },
        "networks": (f"{PROJECT}_control",),
        "created": time.time(),
    }
    gateway.containers[record["id"]] = record
    report = diff_states(_desired(), gateway.list_containers(), gateway.image_env)
    assert not report.for_service("tooling")
