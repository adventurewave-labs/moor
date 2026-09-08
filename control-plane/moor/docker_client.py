"""Docker Engine API gateway: the only module that touches the docker SDK.

The gateway converts engine responses into the ActualContainer domain
model and performs the mutations the reconciler plans. Keeping the SDK
at this boundary is what makes the control-plane core testable.
"""
from __future__ import annotations

import itertools
import time
from datetime import datetime

import docker

from .compose import normalize_image
from .models import (MANAGE_LABEL, OWNED_LABEL, PROJECT_LABEL,
                     ActualContainer, ActualState, DesiredService,
                     PortMapping)

_name_counter = itertools.count(1)


def _parse_created(value: object) -> float:
    """Docker Engine API >= 1.48 (Docker 29) returns `Created` as an RFC 3339
    string; older engines returned a unix epoch float. Accept both."""
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(
                value.replace("Z", "+00:00")
            ).timestamp()
        except ValueError:
            return 0.0
    return 0.0


class DockerGateway:
    """Reads actual state and executes mutations on the Docker Engine."""

    def __init__(self, project: str, client: docker.DockerClient | None = None):
        self.project = project
        self._client = client or docker.from_env(timeout=30)
        self._image_env_cache: dict[str, list[str]] = {}

    # ------------------------------------------------------------ observe

    def engine_info(self) -> dict:
        try:
            version = self._client.version()
            info = self._client.info()
            return {
                "ok": True,
                "version": version.get("Version"),
                "containers": info.get("Containers"),
                "images": info.get("Images"),
            }
        except docker.errors.DockerException:
            return {"ok": False}

    def list_containers(self) -> ActualState:
        containers = self._client.containers.list(
            all=True, filters={"label": f"{PROJECT_LABEL}={self.project}"}
        )
        observed: list[ActualContainer] = []
        for c in containers:
            attrs = c.attrs
            config = attrs.get("Config") or {}
            host_config = attrs.get("HostConfig") or {}
            network_settings = attrs.get("NetworkSettings") or {}
            state = (attrs.get("State") or {}).get("Status", "unknown")
            env = _parse_env(config.get("Env") or [])
            cmd = config.get("Cmd")
            ports = _parse_ports(host_config.get("PortBindings") or {})
            observed.append(
                ActualContainer(
                    id=c.id,
                    name=(c.name or "").lstrip("/"),
                    service=(c.labels or {}).get("com.docker.compose.service", ""),
                    image=normalize_image(str(config.get("Image") or "")),
                    env=env,
                    command=tuple(cmd) if cmd else None,
                    ports=frozenset(ports),
                    state=state,
                    labels=dict(c.labels or {}),
                    networks=tuple((network_settings.get("Networks") or {}).keys()),
                    created=_parse_created(attrs.get("Created", 0)),
                )
            )
        return ActualState(project=self.project, containers=tuple(observed))

    def image_env(self, image_ref: str) -> dict[str, str]:
        """Environment baked into the image itself (not compose env)."""
        ref = normalize_image(image_ref)
        if ref in self._image_env_cache:
            return _parse_env(self._image_env_cache[ref])
        try:
            image = self._client.images.get(image_ref)
            raw = (image.attrs.get("Config") or {}).get("Env") or []
        except docker.errors.ImageNotFound:
            try:
                pulled = self._client.images.pull(image_ref)
                raw = (pulled.attrs.get("Config") or {}).get("Env") or []
            except docker.errors.DockerException:
                raw = []
        except docker.errors.DockerException:
            raw = []
        self._image_env_cache[ref] = list(raw)
        return _parse_env(raw)

    # ------------------------------------------------------------ mutate

    def remove(self, container_id: str, force: bool = True) -> None:
        container = self._client.containers.get(container_id)
        try:
            container.stop(timeout=2)
        except docker.errors.APIError:
            pass  # already stopped / already gone
        container.remove(force=force)

    def start(self, container_id: str) -> None:
        self._client.containers.get(container_id).start()

    def create(
        self,
        service: DesiredService,
        network_names: tuple[str, ...] = (),
    ) -> ActualContainer:
        """Create (and start) one container for a desired service.

        The container carries the standard compose labels so that both
        `docker compose ps` and Moor agree on its ownership, plus a
        `moor.owned` marker for audit purposes.
        """
        name = self._next_container_name(service.name)
        ports_param = {}
        for pm in service.ports:
            if pm.host_port is not None:
                ports_param[pm.container_port] = pm.host_port
        primary_network = None
        extra_networks: list[str] = []
        if network_names:
            primary_network = network_names[0]
            extra_networks = list(network_names[1:])

        container = None
        try:
            container = self._client.containers.create(
                image=service.image,
                name=name,
                command=list(service.command) if service.command else None,
                environment=service.env_list(),
                labels={
                    PROJECT_LABEL: self.project,
                    "com.docker.compose.service": service.name,
                    "com.docker.compose.container-number": str(next(_name_counter)),
                    MANAGE_LABEL: "true",
                    OWNED_LABEL: "true",
                },
                ports=ports_param or None,
                network=primary_network,
                detach=True,
            )
        except docker.errors.ImageNotFound:
            # declared image not present locally (e.g. declaration moved
            # to a new tag): pull it, then retry — same behaviour as
            # `docker compose up`.
            self._client.images.pull(service.image)
            container = self._client.containers.create(
                image=service.image,
                name=name,
                command=list(service.command) if service.command else None,
                environment=service.env_list(),
                labels={
                    PROJECT_LABEL: self.project,
                    "com.docker.compose.service": service.name,
                    "com.docker.compose.container-number": str(next(_name_counter)),
                    MANAGE_LABEL: "true",
                    OWNED_LABEL: "true",
                },
                ports=ports_param or None,
                network=primary_network,
                detach=True,
            )
        for net in extra_networks:
            try:
                self._client.api.connect_container_to_network(container.id, net)
            except docker.errors.APIError:
                pass  # already attached
        container.start()

        refreshed = self._client.containers.get(container.id)
        attrs = refreshed.attrs
        config = attrs.get("Config") or {}
        host_config = attrs.get("HostConfig") or {}
        return ActualContainer(
            id=refreshed.id,
            name=refreshed.name.lstrip("/"),
            service=service.name,
            image=normalize_image(str(config.get("Image") or "")),
            env=_parse_env(config.get("Env") or []),
            command=tuple(config.get("Cmd") or ()) or None,
            ports=frozenset(_parse_ports(host_config.get("PortBindings") or {})),
            state=(attrs.get("State") or {}).get("Status", "created"),
            labels=dict(refreshed.labels or {}),
            networks=tuple((attrs.get("NetworkSettings") or {}).get("Networks", {}).keys()),
            created=float(attrs.get("Created", 0) or 0),
        )

    def resolve_networks(self, service: DesiredService) -> tuple[str, ...]:
        """Map declared network names to engine network names."""
        resolved: list[str] = []
        for net in service.networks or ("default",):
            candidates = [net, f"{self.project}_{net}"]
            for candidate in candidates:
                try:
                    found = self._client.networks.list(names=[candidate])
                    if found:
                        resolved.append(found[0].name)
                        break
                except docker.errors.DockerException:
                    continue
        if not resolved:
            try:
                found = self._client.networks.list(names=[f"{self.project}_default"])
                if found:
                    resolved.append(found[0].name)
            except docker.errors.DockerException:
                pass
        return tuple(dict.fromkeys(resolved))

    def next_free_index(self, service: str) -> int:
        """Lowest compose-style container index not in use for a service."""
        taken = {
            c.name
            for c in self.list_containers().for_service(service)
        }
        idx = 1
        while f"{self.project}-{service}-{idx}" in taken:
            idx += 1
        return idx

    def _next_container_name(self, service: str) -> str:
        return f"{self.project}-{service}-{self.next_free_index(service)}"


# ------------------------------------------------------------------ helpers

def _parse_env(raw: list[str]) -> dict[str, str]:
    env: dict[str, str] = {}
    for item in raw or []:
        if "=" in item:
            k, v = item.split("=", 1)
            env[k] = v
        elif item:
            env[item] = ""
    return env


def _parse_ports(bindings: dict) -> list[PortMapping]:
    """PortBindings: {'80/tcp': [{'HostIp': '', 'HostPort': '8081'}]}."""
    out: list[PortMapping] = []
    for key, hosts in (bindings or {}).items():
        if "/" in key:
            port_s, proto = key.split("/", 1)
        else:
            port_s, proto = key, "tcp"
        host_port = None
        if hosts:
            host_port = int(hosts[0].get("HostPort", 0) or 0) or None
        out.append(PortMapping(int(port_s), host_port, proto))
    return out


def now() -> float:
    return time.time()
