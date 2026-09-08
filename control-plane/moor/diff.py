"""The diff engine: desired state vs actual state -> drift report.

Comparison semantics:

* replicas: live containers (running/restarting/created) are counted and
  matched against the declared count. Older containers win (compose
  convention: index 1 is the original); excess containers are "extra".
* image: normalized references must match exactly (tag included).
* environment: every declared K=V must be present with the same value.
  Extra keys that are neither declared nor baked into the image count
  as injected environment drift.
* command: declared command must match the container command.
* ports: declared host bindings must match actual bindings.
* services present in reality but not declared (with managed labels)
  are orphaned drift.
"""
from __future__ import annotations

from .compose import normalize_image
from .models import (DRIFT_COMMAND, DRIFT_ENV, DRIFT_IMAGE, DRIFT_PORTS,
                     DRIFT_REPLICAS_EXTRA, DRIFT_REPLICAS_MISSING,
                     DRIFT_SERVICE_ORPHAN, ActualContainer, ActualState,
                     DesiredState, DriftItem, DriftReport)

SEVERITY = {
    DRIFT_REPLICAS_MISSING: "critical",
    DRIFT_REPLICAS_EXTRA: "warning",
    DRIFT_IMAGE: "critical",
    DRIFT_ENV: "critical",
    DRIFT_COMMAND: "warning",
    DRIFT_PORTS: "warning",
    DRIFT_SERVICE_ORPHAN: "critical",
}


def diff_states(
    desired: DesiredState,
    actual: ActualState,
    image_env_fn=None,
) -> DriftReport:
    """Compare desired against actual and produce a drift report.

    ``image_env_fn(ref) -> dict`` supplies image-baked environment so
    injected extra variables can be distinguished from image defaults.
    """
    items: list[DriftItem] = []

    for svc in desired.services:
        containers = sorted(
            actual.for_service(svc.name), key=lambda c: (c.created, c.name)
        )
        live = [c for c in containers if c.is_live]
        dead = [c for c in containers if not c.is_live]

        # ---- replicas
        if len(live) > svc.replicas:
            extra = live[svc.replicas:]
            items.append(
                DriftItem(
                    service=svc.name,
                    kind=DRIFT_REPLICAS_EXTRA,
                    message=f"replicas {svc.replicas} declared, {len(live)} running",
                    severity=SEVERITY[DRIFT_REPLICAS_EXTRA],
                    details={"declared": svc.replicas, "running": len(live),
                             "extra_containers": [c.name for c in extra]},
                )
            )
        elif len(live) < svc.replicas:
            items.append(
                DriftItem(
                    service=svc.name,
                    kind=DRIFT_REPLICAS_MISSING,
                    message=f"replicas {svc.replicas} declared, {len(live)} running",
                    severity=SEVERITY[DRIFT_REPLICAS_MISSING],
                    details={"declared": svc.replicas, "running": len(live),
                             "dead_containers": [c.name for c in dead]},
                )
            )

        # ---- per-container spec comparison (kept live containers only)
        kept = live[: svc.replicas]
        for container in kept:
            items.extend(_diff_container(svc, container, image_env_fn))

    # ---- orphaned services: containers exist, no declaration
    for service_name in actual.services():
        if desired.service(service_name) is not None:
            continue
        managed_containers = [
            c for c in actual.for_service(service_name) if c.managed
        ]
        if managed_containers:
            items.append(
                DriftItem(
                    service=service_name,
                    kind=DRIFT_SERVICE_ORPHAN,
                    message=f"service not declared in compose, {len(managed_containers)} container(s) running",
                    severity=SEVERITY[DRIFT_SERVICE_ORPHAN],
                    details={"containers": [c.name for c in managed_containers]},
                )
            )

    return DriftReport(items=tuple(items))


def _diff_container(svc, container: ActualContainer, image_env_fn) -> list[DriftItem]:
    items: list[DriftItem] = []

    # image
    if normalize_image(svc.image) != normalize_image(container.image):
        items.append(
            DriftItem(
                service=svc.name,
                kind=DRIFT_IMAGE,
                message=f"image {svc.image} declared, {container.image} running",
                severity=SEVERITY[DRIFT_IMAGE],
                details={"declared": svc.image, "actual": container.image,
                         "container": container.name},
            )
        )

    # environment
    env_problems: dict = {}
    for key, want in svc.env.items():
        got = container.env.get(key)
        if got != want:
            env_problems[key] = {"declared": _mask(key, want), "actual": _mask(key, got)}
    extra_keys: list[str] = []
    if image_env_fn is not None:
        try:
            baked = image_env_fn(container.image)
        except Exception:
            baked = {}
        for key in container.env:
            if key not in svc.env and key not in baked:
                extra_keys.append(key)
    if env_problems or extra_keys:
        items.append(
            DriftItem(
                service=svc.name,
                kind=DRIFT_ENV,
                message=_env_message(env_problems, extra_keys),
                severity=SEVERITY[DRIFT_ENV],
                details={"declared": _mask_map(svc.env), "actual": _mask_map(container.env),
                         "changed": env_problems, "extra_keys": extra_keys,
                         "container": container.name},
            )
        )

    # command
    desired_cmd = list(svc.command) if svc.command else None
    actual_cmd = list(container.command) if container.command else None
    if desired_cmd != actual_cmd:
        items.append(
            DriftItem(
                service=svc.name,
                kind=DRIFT_COMMAND,
                message="command does not match declaration",
                severity=SEVERITY[DRIFT_COMMAND],
                details={"declared": desired_cmd, "actual": actual_cmd,
                         "container": container.name},
            )
        )

    # ports
    desired_ports = {pm.key: pm.host_port for pm in svc.ports}
    actual_ports = {pm.key: pm.host_port for pm in container.ports}
    if desired_ports != actual_ports:
        items.append(
            DriftItem(
                service=svc.name,
                kind=DRIFT_PORTS,
                message="port bindings do not match declaration",
                severity=SEVERITY[DRIFT_PORTS],
                details={"declared": desired_ports, "actual": actual_ports,
                         "container": container.name},
            )
        )

    return items


def _env_message(changed: dict, extra: list[str]) -> str:
    parts: list[str] = []
    if changed:
        parts.append("env " + ", ".join(sorted(changed)) + " differs from declaration")
    if extra:
        parts.append("env " + ", ".join(sorted(extra)) + " injected (not declared)")
    return "; ".join(parts)


_SENSITIVE = ("PASSWORD", "SECRET", "TOKEN", "KEY", "CREDENTIAL")


def _mask(key: str, value: str | None) -> str:
    if value is None:
        return "(unset)"
    if any(marker in key.upper() for marker in _SENSITIVE) and value:
        return "****"
    if len(str(value)) > 48:
        return str(value)[:45] + "..."
    return str(value)


def _mask_map(env: dict) -> dict:
    return {k: _mask(k, v) for k, v in env.items()}
