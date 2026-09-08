"""Test fixtures: an in-memory Docker gateway implementing the same
interface as moor.docker_client.DockerGateway.

The product code path is identical — the engine, diff, planner and
executor only know the gateway interface. Tests substitute this fake
so the reconciliation logic is verified without a Docker daemon.
"""
from __future__ import annotations

import itertools
import os
import time

import pytest
import yaml

from moor.models import (MANAGE_LABEL, OWNED_LABEL, PROJECT_LABEL,
                         ActualContainer, ActualState, DesiredService,
                         PortMapping)

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
TESTDATA = os.path.join(TESTS_DIR, "testdata")
COMPOSE_PATH = os.path.join(TESTDATA, "compose.yml")
COMPOSE_SCALED_PATH = os.path.join(TESTDATA, "compose_scaled.yml")

PROJECT = "moordemo"

# Image-baked environment (what the image itself provides).
IMAGE_ENVS = {
    "nginx:1.27-alpine": ["PATH=/usr/local/sbin:/usr/local/bin", "NGINX_VERSION=1.27"],
    "redis:7.2-alpine": ["PATH=/usr/local/bin", "REDIS_VERSION=7.2"],
    "redis:6.2-alpine": ["PATH=/usr/local/bin", "REDIS_VERSION=6.2"],
    "postgres:16-alpine": ["PATH=/usr/local/bin", "PGDATA=/var/lib/postgresql/data"],
    "postgres:15-alpine": ["PATH=/usr/local/bin", "PGDATA=/var/lib/postgresql/data"],
}

# Image default commands (what a container runs when compose declares none).
IMAGE_CMDS = {
    "nginx:1.27-alpine": ("nginx", "-g", "daemon off;"),
    "redis:7.2-alpine": ("redis-server",),
    "redis:6.2-alpine": ("redis-server",),
    "postgres:16-alpine": ("postgres",),
    "postgres:15-alpine": ("postgres",),
}

_ids = itertools.count(1)


def _next_id() -> str:
    return f"id{next(_ids):08d}"


class FakeGateway:
    """In-memory Docker Engine for tests."""

    def __init__(self, compose_path: str = COMPOSE_PATH, project: str = PROJECT):
        self.project = project
        self.compose_path = compose_path
        self.containers: dict[str, dict] = {}
        self.fail_on_create = False
        self.created_events: list[dict] = []
        self.removed_events: list[str] = []

    # ------------------------------------------------------- seed helpers

    def seed_from_compose(self, compose_path: str | None = None) -> None:
        """Create the containers docker compose would have created."""
        from moor.compose import load_desired_state

        state = load_desired_state(compose_path or self.compose_path, self.project)
        for svc in state.services:
            for idx in range(1, svc.replicas + 1):
                self.add_container(svc, idx)

    def add_container(
        self,
        svc: DesiredService,
        index: int = 1,
        *,
        state: str = "running",
        image: str | None = None,
        env_extra: dict[str, str] | None = None,
        env_override: dict[str, str] | None = None,
        name: str | None = None,
    ) -> dict:
        env = dict(svc.env)
        if env_extra:
            env.update(env_extra)
        if env_override:
            env.update(env_override)
        record = {
            "id": _next_id(),
            "name": name or f"{self.project}-{svc.name}-{index}",
            "service": svc.name,
            "image": image or svc.image,
            "env": env,
            "command": list(svc.command) if svc.command else None,
            "ports": {pm.key: pm.host_port for pm in svc.ports},
            "state": state,
            "labels": {
                PROJECT_LABEL: self.project,
                "com.docker.compose.service": svc.name,
                MANAGE_LABEL: "true",
            },
            "networks": (f"{self.project}_{n}" for n in svc.networks) if svc.networks else (f"{self.project}_default",),
            "created": time.time() - 100 + index,
        }
        record["networks"] = tuple(record["networks"])
        self.containers[record["id"]] = record
        return record

    # ------------------------------------------------ gateway interface

    def list_containers(self) -> ActualState:
        observed = []
        for record in self.containers.values():
            if record["labels"].get(PROJECT_LABEL) != self.project:
                continue
            ports = frozenset(
                PortMapping(int(key.split("/")[0]), host, key.split("/")[1] if "/" in key else "tcp")
                for key, host in record["ports"].items()
            )
            observed.append(
                ActualContainer(
                    id=record["id"],
                    name=record["name"],
                    service=record["service"],
                    image=record["image"],
                    env=dict(record["env"]),
                    command=tuple(record["command"]) if record["command"] else None,
                    ports=ports,
                    state=record["state"],
                    labels=dict(record["labels"]),
                    networks=tuple(record["networks"]),
                    created=record["created"],
                )
            )
        return ActualState(project=self.project, containers=tuple(observed))

    def image_env(self, image_ref: str) -> dict[str, str]:
        raw = IMAGE_ENVS.get(image_ref, [])
        out: dict[str, str] = {}
        for item in raw:
            k, v = item.split("=", 1)
            out[k] = v
        return out

    def image_cmd(self, image_ref: str) -> tuple[str, ...] | None:
        return IMAGE_CMDS.get(image_ref)

    def engine_info(self) -> dict:
        return {"ok": True, "version": "test", "containers": len(self.containers), "images": len(IMAGE_ENVS)}

    def remove(self, container_id: str, force: bool = True) -> None:
        self.removed_events.append(container_id)
        self.containers.pop(container_id, None)

    def start(self, container_id: str) -> None:
        record = self.containers[container_id]
        record["state"] = "running"

    def create(self, service: DesiredService, network_names: tuple[str, ...] = ()) -> ActualContainer:
        if self.fail_on_create:
            raise RuntimeError("simulated docker create failure")
        taken = {r["name"] for r in self.containers.values() if r["service"] == service.name}
        idx = 1
        while f"{self.project}-{service.name}-{idx}" in taken:
            idx += 1
        env_base = dict(service.env)
        baked = self.image_env(service.image)
        env_full = {**baked, **env_base}
        record = {
            "id": _next_id(),
            "name": f"{self.project}-{service.name}-{idx}",
            "service": service.name,
            "image": service.image,
            "env": env_full,
            "command": list(service.command) if service.command else None,
            "ports": {pm.key: pm.host_port for pm in service.ports},
            "state": "running",
            "labels": {
                PROJECT_LABEL: self.project,
                "com.docker.compose.service": service.name,
                "com.docker.compose.config-hash": f"moor-fake-{service.name}",
                MANAGE_LABEL: "true",
                OWNED_LABEL: "true",
            },
            "networks": network_names or (f"{self.project}_default",),
            "created": time.time(),
        }
        self.containers[record["id"]] = record
        self.created_events.append(record)
        ports = frozenset(
            PortMapping(int(key.split("/")[0]), host, key.split("/")[1] if "/" in key else "tcp")
            for key, host in record["ports"].items()
        )
        return ActualContainer(
            id=record["id"], name=record["name"], service=record["service"],
            image=record["image"], env=dict(record["env"]),
            command=tuple(record["command"]) if record["command"] else None,
            ports=ports, state=record["state"], labels=dict(record["labels"]),
            networks=tuple(record["networks"]), created=record["created"],
        )

    def resolve_networks(self, service: DesiredService) -> tuple[str, ...]:
        return tuple(f"{self.project}_{n}" for n in (service.networks or ("default",)))

    def next_free_index(self, service: str) -> int:
        taken = {r["name"] for r in self.containers.values() if r["service"] == service}
        idx = 1
        while f"{self.project}-{service}-{idx}" in taken:
            idx += 1
        return idx

    # --------------------------------------------------- scenario helpers

    def kill(self, service: str) -> None:
        for record in self.containers.values():
            if record["service"] == service and record["state"] == "running":
                record["state"] = "exited"
                return
        raise AssertionError(f"no running container for {service}")

    def mutate_env(self, service: str, key: str, value: str) -> None:
        for record in self.containers.values():
            if record["service"] == service:
                record["env"][key] = value
                return
        raise AssertionError(f"no container for {service}")

    def swap_image(self, service: str, image: str) -> None:
        for record in self.containers.values():
            if record["service"] == service:
                record["image"] = image
                return
        raise AssertionError(f"no container for {service}")

    def add_rogue(self, service: str, count: int = 3) -> list[dict]:
        """Extra containers with the same labels (rogue scaling)."""
        base = next(
            (r for r in self.containers.values() if r["service"] == service), None
        )
        assert base is not None, f"service {service} not running"
        out = []
        for i in range(1, count + 1):
            record = dict(base)
            record["id"] = _next_id()
            record["name"] = f"{self.project}-{service}-rogue{i}"
            record["created"] = time.time() + i
            record["ports"] = {}
            self.containers[record["id"]] = record
            out.append(record)
        return out


@pytest.fixture
def gateway() -> FakeGateway:
    gw = FakeGateway()
    gw.seed_from_compose()
    return gw


@pytest.fixture
def store(tmp_path):
    from moor.events import EventStore

    return EventStore(f"sqlite:///{tmp_path}/moor.db")


@pytest.fixture
def alert_calls():
    """Collect webhook deliveries via a mock HTTP transport."""
    import httpx

    calls: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        calls.append({"url": str(request.url), "json": json.loads(request.content)})
        return httpx.Response(200, json={"ok": True})

    transport = httpx.MockTransport(handler)
    from moor.events import WebhookAlerter

    alerter = WebhookAlerter("http://sink.test/webhook", transport=transport)
    return alerter, calls


@pytest.fixture
def engine(gateway, store, alert_calls):
    from moor.config import MoorConfig
    from moor.engine import Reconciler
    from moor.compose import ComposeFile

    alerter, _ = alert_calls
    config = MoorConfig(
        project=PROJECT, compose_path=gateway.compose_path, mode="advise",
        interval=0.05, webhook_url="http://sink.test/webhook",
        db_url="sqlite://", api_host="x", api_port=1, cooldown=0.1,
    )
    reconciler = Reconciler(
        config, gateway, store, alerter,
        compose_file=ComposeFile(gateway.compose_path, PROJECT),
    )
    return reconciler
