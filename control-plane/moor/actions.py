"""Action planner and executor.

The planner turns a drift report into an ordered, idempotent action
list: removals first (free ports and names), then starts (cheap
recovery for dead-but-correct containers), then creates. The executor
runs each action through the Docker gateway and emits an event per
outcome.
"""
from __future__ import annotations

import hashlib
import json
from typing import Callable

from .models import (ACTION_CREATE, ACTION_REMOVE, ACTION_START,
                     DRIFT_IMAGE, DRIFT_ENV, DRIFT_COMMAND, DRIFT_PORTS,
                     DRIFT_REPLICAS_EXTRA, DRIFT_REPLICAS_MISSING,
                     DRIFT_SERVICE_ORPHAN, Action, ActionPlan,
                     ActualContainer, ActualState, DesiredState, DriftReport)

# Drift kinds that are repaired by recreating the container with the
# declared spec.
SPEC_DRIFTS = (DRIFT_IMAGE, DRIFT_ENV, DRIFT_COMMAND, DRIFT_PORTS)

EventEmitter = Callable[[str, str | None, str, dict], None]


def build_plan(
    desired: DesiredState,
    actual: ActualState,
    report: DriftReport,
    image_cmd_fn=None,
) -> ActionPlan:
    actions: list[Action] = []

    affected = set(report.services_affected)

    for svc in desired.services:
        if svc.name not in affected:
            continue
        containers = sorted(
            actual.for_service(svc.name), key=lambda c: (c.created, c.name)
        )
        live = [c for c in containers if c.is_live]

        # 1. remove extra replicas
        if len(live) > svc.replicas:
            for extra in live[svc.replicas:]:
                actions.append(Action(
                    kind=ACTION_REMOVE, service=svc.name,
                    reason=DRIFT_REPLICAS_EXTRA,
                    container_id=extra.id, container_name=extra.name,
                ))

        # 2. spec drift on kept containers -> recreate
        kept = live[: svc.replicas]
        spec_drift_containers = {
            item.details.get("container")
            for item in report.for_service(svc.name)
            if item.kind in SPEC_DRIFTS
        }
        to_recreate = [c for c in kept if c.name in spec_drift_containers]
        for bad in to_recreate:
            actions.append(Action(
                kind=ACTION_REMOVE, service=svc.name,
                reason=next(
                    item.kind for item in report.for_service(svc.name)
                    if item.kind in SPEC_DRIFTS and item.details.get("container") == bad.name
                ),
                container_id=bad.id, container_name=bad.name,
            ))
            actions.append(Action(
                kind=ACTION_CREATE, service=svc.name,
                reason="recreate", spec=svc,
            ))

        # 3. missing replicas -> start a matching dead container, or create
        #    (recreate actions from step 2 already count toward the target)
        removes = sum(
            1 for a in actions if a.service == svc.name and a.kind == ACTION_REMOVE
        )
        creates = sum(
            1 for a in actions if a.service == svc.name and a.kind == ACTION_CREATE
        )
        live_count = len(live) - removes + creates
        missing = max(0, svc.replicas - live_count)
        if missing > 0:
            dead = [c for c in containers if not c.is_live]
            matching_dead = [
                c for c in dead
                if _spec_matches(svc, c, image_cmd_fn)
            ]
            for c in matching_dead[:missing]:
                actions.append(Action(
                    kind=ACTION_START, service=svc.name,
                    reason=DRIFT_REPLICAS_MISSING,
                    container_id=c.id, container_name=c.name,
                ))
                missing -= 1
            for _ in range(max(0, missing)):
                actions.append(Action(
                    kind=ACTION_CREATE, service=svc.name,
                    reason=DRIFT_REPLICAS_MISSING, spec=svc,
                ))

    # 4. orphaned services -> remove all managed containers
    for item in report.items:
        if item.kind != DRIFT_SERVICE_ORPHAN:
            continue
        for c in actual.for_service(item.service):
            if c.managed:
                actions.append(Action(
                    kind=ACTION_REMOVE, service=item.service,
                    reason=DRIFT_SERVICE_ORPHAN,
                    container_id=c.id, container_name=c.name,
                ))

    return ActionPlan(actions=tuple(actions))


def _spec_matches(svc, container: ActualContainer, image_cmd_fn=None) -> bool:
    """Would restarting this (stopped) container satisfy the declaration?

    An undeclared command means "the image default": a container created
    by compose carries the image's default Cmd, and that must still count
    as matching so the planner restarts it instead of stacking new ones.
    """
    from .compose import normalize_image

    if normalize_image(svc.image) != normalize_image(container.image):
        return False
    for key, want in svc.env.items():
        if container.env.get(key) != want:
            return False
    desired_cmd = list(svc.command) if svc.command else None
    actual_cmd = list(container.command) if container.command else None
    if desired_cmd != actual_cmd:
        if desired_cmd is None and image_cmd_fn is not None:
            try:
                image_default = list(image_cmd_fn(container.image) or [])
            except Exception:
                image_default = None
            if image_default is not None and actual_cmd == image_default:
                return True  # image default command: matches
        return False
    return True


def spec_config_hash(service: DesiredService) -> str:
    """Stable config-hash label for containers Moor creates.

    `docker compose` recognizes its service containers by the
    com.docker.compose.config-hash label: without it, compose ps/down
    ignore the container and the next `up` fails on the name conflict.
    Moor stamps a deterministic hash of the desired spec; a mismatch with
    compose's own hash simply means compose recreates the container on
    the next `up` — i.e. compose keeps reconciling its own bookkeeping.
    """
    canonical = json.dumps(
        {
            "image": service.image,
            "command": list(service.command) if service.command else None,
            "environment": sorted(service.env_list()),
            "ports": sorted(
                (pm.container_port, pm.host_port, pm.protocol)
                for pm in service.ports
            ),
        },
        sort_keys=True,
    )
    return "moor-" + hashlib.sha1(canonical.encode()).hexdigest()[:12]


def execute_plan(
    gateway,
    plan: ActionPlan,
    emit: EventEmitter,
) -> dict:
    """Execute actions in order; returns a summary of outcomes."""
    ok, failed = 0, 0
    failures: list[dict] = []
    for action in plan.actions:
        try:
            if action.kind == ACTION_REMOVE:
                gateway.remove(action.container_id)
            elif action.kind == ACTION_START:
                gateway.start(action.container_id)
            elif action.kind == ACTION_CREATE:
                networks = gateway.resolve_networks(action.spec)
                gateway.create(action.spec, networks)
            else:
                raise ValueError(f"unknown action kind {action.kind}")
            ok += 1
            emit(
                "action.ok",
                action.service,
                "info",
                action.to_json(),
            )
        except Exception as exc:  # noqa: BLE001 — engine must survive any action failure
            failed += 1
            record = {**action.to_json(), "error": str(exc)}
            failures.append(record)
            emit(
                "action.failed",
                action.service,
                "error",
                record,
            )
    return {"ok": ok, "failed": failed, "failures": failures}
