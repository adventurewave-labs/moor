"""Compose parser tests: the desired-state contract."""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from moor.compose import ComposeFile, load_desired_state, normalize_image
from tests.conftest import COMPOSE_PATH, COMPOSE_SCALED_PATH, PROJECT


def test_parses_managed_services_and_skips_control_plane():
    state = load_desired_state(COMPOSE_PATH, PROJECT)
    assert state.managed_names == ["web", "cache", "db"]
    assert state.service("moor") is None  # opted out via moor.manage=false


def test_service_fields():
    state = load_desired_state(COMPOSE_PATH, PROJECT)
    web = state.service("web")
    assert web.image == "nginx:1.27-alpine"
    assert web.replicas == 1
    assert web.env == {"MOOR_TIER": "frontend"}
    assert web.ports[0].container_port == 80
    assert web.ports[0].host_port == 8081
    assert web.networks == ("frontend",)

    db = state.service("db")
    assert db.env["POSTGRES_PASSWORD"] == "demo"
    assert db.ports == ()


def test_replicas_from_deploy():
    state = load_desired_state(COMPOSE_SCALED_PATH, PROJECT)
    assert state.service("cache").replicas == 3


def test_normalize_image():
    assert normalize_image("nginx") == "nginx:latest"
    assert normalize_image("nginx:1.27") == "nginx:1.27"
    assert normalize_image("docker.io/library/nginx:1.27") == "nginx:1.27"
    assert normalize_image("docker.io/redis") == "redis:latest"
    assert normalize_image("redis@sha256:abc") == "redis:latest"


def test_compose_file_cache_reloads_on_change():
    cf = ComposeFile(COMPOSE_PATH, PROJECT)
    first = cf.current()
    assert cf.current() is first  # cached
    cf2 = ComposeFile(COMPOSE_SCALED_PATH, PROJECT)
    assert cf2.current().service("cache").replicas == 3
