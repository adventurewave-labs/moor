"""Desired-state loader: docker-compose file -> DesiredState.

The parser deliberately covers the stable core subset of the compose
spec (image, deploy.replicas, environment, command, ports, networks,
labels) which is what the reconciler enforces. Anything outside that
subset is ignored loudly rather than silently (unknown keys are
reported to stderr by the caller if needed).
"""
from __future__ import annotations

import os

import yaml

from .models import DesiredService, DesiredState, PortMapping

LIBRARY_PREFIXES = ("docker.io/library/", "docker.io/")


def normalize_image(ref: str) -> str:
    """Normalize an image reference for comparison.

    Compose files write `nginx:1.27-alpine`; the Docker Engine reports
    whatever string was used at creation time, which is usually the
    same but can carry a registry prefix or lack a tag.
    """
    ref = ref.strip()
    for prefix in LIBRARY_PREFIXES:
        if ref.startswith(prefix):
            ref = ref[len(prefix):]
            break
    if "@" in ref:
        ref = ref.split("@", 1)[0]
    if ":" not in ref.rsplit("/", 1)[-1]:
        ref += ":latest"
    return ref


def _as_env(value) -> dict[str, str]:
    """Compose allows environment as a map or a list of K=V strings."""
    env: dict[str, str] = {}
    if not value:
        return env
    if isinstance(value, dict):
        return {str(k): str(v) for k, v in value.items() if v is not None}
    for item in value:
        if isinstance(item, str):
            if "=" in item:
                k, v = item.split("=", 1)
                env[k] = v
            else:
                env[item] = ""
    return env


def _as_labels(value) -> dict[str, str]:
    if not value:
        return {}
    if isinstance(value, dict):
        return {str(k): str(v) for k, v in value.items()}
    out: dict[str, str] = {}
    for item in value:
        if "=" in item:
            k, v = item.split("=", 1)
            out[k] = v
        else:
            out[item] = ""
    return out


def _as_command(value) -> tuple[str, ...] | None:
    if value is None:
        return None
    if isinstance(value, str):
        return tuple(value.split())
    return tuple(str(v) for v in value)


def _as_ports(value) -> tuple[PortMapping, ...]:
    if not value:
        return ()
    out: list[PortMapping] = []
    for item in value:
        if isinstance(item, dict):
            # long syntax
            target = int(item.get("target"))
            published = item.get("published")
            host = int(published) if published not in (None, "") else None
            proto = item.get("protocol", "tcp")
            out.append(PortMapping(target, host, proto))
            continue
        text = str(item)
        proto = "tcp"
        if "/" in text:
            text, proto = text.rsplit("/", 1)
        parts = text.split(":")
        if len(parts) == 1:
            out.append(PortMapping(int(parts[0]), None, proto))
        elif len(parts) == 2:
            out.append(PortMapping(int(parts[1]), int(parts[0]), proto))
        else:
            # ip:host:container
            out.append(PortMapping(int(parts[2]), int(parts[1]), proto))
    return tuple(out)


def load_desired_state(path: str, project: str, env: dict[str, str] | None = None) -> DesiredState:
    """Parse a compose file into a DesiredState of managed services.

    A service is managed unless it opts out with `moor.manage: "false"`.
    `${VAR}` substitution is resolved from the passed environment (for
    the container runtime this is os.environ), matching compose
    behaviour closely enough for declaration files.
    """
    with open(path, "r", encoding="utf-8") as fh:
        raw = fh.read()
    if env:
        for key, value in env.items():
            raw = raw.replace(f"${{{key}}}", value)
    doc = yaml.safe_load(raw) or {}
    services = doc.get("services") or {}
    if not isinstance(services, dict):
        raise ValueError(f"compose file {path} has no services mapping")

    out: list[DesiredService] = []
    for name, spec in services.items():
        if not isinstance(spec, dict):
            continue
        labels = _as_labels(spec.get("labels"))
        if labels.get("moor.manage", "true") == "false":
            continue
        deploy = spec.get("deploy") or {}
        replicas = int(deploy.get("replicas", 1))
        image = spec.get("image")
        if not image:
            # Services without an image (e.g. build-only) are not
            # runtime-manageable by Moor in v1; skip loudly.
            print(f"moor: service {name} has no image, skipping management")
            continue
        out.append(
            DesiredService(
                name=str(name),
                image=normalize_image(str(image)),
                replicas=max(0, replicas),
                env=_as_env(spec.get("environment")),
                command=_as_command(spec.get("command")),
                ports=_as_ports(spec.get("ports")),
                networks=tuple(str(n) for n in (spec.get("networks") or ())),
            )
        )
    return DesiredState(project=project, services=tuple(out))


class ComposeFile:
    """mtime-aware cache around load_desired_state."""

    def __init__(self, path: str, project: str, env: dict[str, str] | None = None):
        self.path = path
        self.project = project
        self.env = env
        self._mtime: float | None = None
        self._state: DesiredState | None = None

    def current(self) -> DesiredState:
        mtime = os.path.getmtime(self.path)
        if self._state is None or mtime != self._mtime:
            self._state = load_desired_state(self.path, self.project, self.env)
            self._mtime = mtime
        return self._state

    @property
    def changed_on_disk(self) -> bool:
        try:
            return self._mtime is not None and os.path.getmtime(self.path) != self._mtime
        except OSError:
            return False
