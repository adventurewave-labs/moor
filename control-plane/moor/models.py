"""Domain models shared across the control plane.

Everything in here is a plain dataclass: the diff engine, the action
planner and the API all speak in these terms, which keeps the Docker
SDK at the edges of the system and the core logic testable.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Iterable

# Container states that count toward a live replica.
LIVE_STATES = frozenset({"running", "restarting", "created"})

# Label contract between the compose file and the control plane.
PROJECT_LABEL = "com.docker.compose.project"
SERVICE_LABEL = "com.docker.compose.service"
MANAGE_LABEL = "moor.manage"
OWNED_LABEL = "moor.owned"


@dataclass(frozen=True)
class PortMapping:
    """A published port: container port (optionally bound to a host port)."""

    container_port: int
    host_port: int | None = None
    protocol: str = "tcp"

    @property
    def key(self) -> str:
        return f"{self.container_port}/{self.protocol}"


@dataclass(frozen=True)
class DesiredService:
    """What the compose file declares one managed service should be."""

    name: str
    image: str
    replicas: int = 1
    env: dict[str, str] = field(default_factory=dict)
    command: tuple[str, ...] | None = None
    ports: tuple[PortMapping, ...] = ()
    networks: tuple[str, ...] = ()

    def env_list(self) -> list[str]:
        return [f"{k}={v}" for k, v in sorted(self.env.items())]


@dataclass(frozen=True)
class DesiredState:
    project: str
    services: tuple[DesiredService, ...]

    @property
    def managed_names(self) -> list[str]:
        return [s.name for s in self.services]

    def service(self, name: str) -> DesiredService | None:
        for s in self.services:
            if s.name == name:
                return s
        return None


@dataclass(frozen=True)
class ActualContainer:
    """A live observation of one container owned by the compose project."""

    id: str
    name: str
    service: str
    image: str
    env: dict[str, str]
    command: tuple[str, ...] | None
    ports: frozenset[PortMapping]
    state: str
    labels: dict[str, str]
    networks: tuple[str, ...]
    created: float

    @property
    def is_live(self) -> bool:
        return self.state in LIVE_STATES

    @property
    def managed(self) -> bool:
        return self.labels.get(MANAGE_LABEL, "true") != "false"


@dataclass(frozen=True)
class ActualState:
    project: str
    containers: tuple[ActualContainer, ...]

    def for_service(self, service: str) -> list[ActualContainer]:
        return [c for c in self.containers if c.service == service]

    def live_for_service(self, service: str) -> list[ActualContainer]:
        return [c for c in self.for_service(service) if c.is_live]

    def services(self) -> list[str]:
        seen: list[str] = []
        for c in self.containers:
            if c.service not in seen:
                seen.append(c.service)
        return seen


# ----------------------------------------------------------------- drift

# Drift kinds, as persisted in events and the API.
DRIFT_REPLICAS_EXTRA = "replicas_extra"
DRIFT_REPLICAS_MISSING = "replicas_missing"
DRIFT_IMAGE = "image_changed"
DRIFT_ENV = "env_changed"
DRIFT_COMMAND = "command_changed"
DRIFT_PORTS = "ports_changed"
DRIFT_SERVICE_ORPHAN = "service_orphaned"


@dataclass(frozen=True)
class DriftItem:
    service: str
    kind: str
    message: str
    severity: str = "warning"
    details: dict = field(default_factory=dict)

    def to_json(self) -> dict:
        return {
            "service": self.service,
            "kind": self.kind,
            "message": self.message,
            "severity": self.severity,
            "details": self.details,
        }


@dataclass(frozen=True)
class DriftReport:
    items: tuple[DriftItem, ...]

    @property
    def has_drift(self) -> bool:
        return bool(self.items)

    @property
    def services_affected(self) -> list[str]:
        seen: list[str] = []
        for i in self.items:
            if i.service not in seen:
                seen.append(i.service)
        return seen

    def for_service(self, service: str) -> list[DriftItem]:
        return [i for i in self.items if i.service == service]

    @property
    def hash(self) -> str:
        canonical = json.dumps(
            [i.to_json() for i in self.items], sort_keys=True, separators=(",", ":")
        )
        return hashlib.sha256(canonical.encode()).hexdigest()[:16]

    def to_json(self) -> dict:
        return {
            "has_drift": self.has_drift,
            "hash": self.hash,
            "services": self.services_affected,
            "items": [i.to_json() for i in self.items],
        }


# ----------------------------------------------------------------- actions

ACTION_REMOVE = "remove"
ACTION_START = "start"
ACTION_CREATE = "create"


@dataclass(frozen=True)
class Action:
    kind: str
    service: str
    reason: str  # the drift kind that produced this action
    container_id: str | None = None
    container_name: str | None = None
    spec: DesiredService | None = None

    def to_json(self) -> dict:
        return {
            "kind": self.kind,
            "service": self.service,
            "reason": self.reason,
            "container_id": self.container_id,
            "container_name": self.container_name,
            "image": self.spec.image if self.spec else None,
        }


@dataclass(frozen=True)
class ActionPlan:
    actions: tuple[Action, ...]

    @property
    def empty(self) -> bool:
        return not self.actions

    def to_json(self) -> dict:
        return {"count": len(self.actions), "actions": [a.to_json() for a in self.actions]}


@dataclass
class EventRecord:
    id: int
    ts: float
    type: str
    service: str | None
    severity: str
    payload: dict

    def to_json(self) -> dict:
        return {
            "id": self.id,
            "ts": self.ts,
            "type": self.type,
            "service": self.service,
            "severity": self.severity,
            "payload": self.payload,
        }


def summarize(items: Iterable[DriftItem]) -> str:
    """One-line human summary of a set of drift items."""
    items = list(items)
    if not items:
        return "all services compliant"
    parts = [f"{i.service}: {i.message}" for i in items]
    return "; ".join(parts)
