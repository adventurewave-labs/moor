"""Chaos injection: the dashboard's "Inject drift" button, made real.

Moor's value proposition is that drift gets detected and repaired. To
prove it on demand — from the GUI, the CLI, or any API client — the
control plane exposes a small chaos surface identical in spirit to the
drift-injector CLI: kill a container, launch rogue replicas, recreate
a container with a mutated environment, or swap its image.

Every mutation is a REAL Docker Engine call performed by the control
plane itself; nothing is simulated. The reconciler then has to notice
and (in auto mode) repair it, exactly as it would for drift caused by
a tired human. The mutations route through the DockerGateway so the
"only docker_client touches the SDK" boundary holds and the whole
flow stays testable against the in-memory gateway.
"""
from __future__ import annotations

import random

from .compose import normalize_image

#: Valid chaos actions (``random`` picks one of the other four).
CHAOS_ACTIONS = ("kill", "scale", "env", "image", "random")

#: Event type recorded in the audit trail for every injection.
CHAOS_INJECTED = "chaos.injected"

#: Value written over a declared env var (and used as the rogue value).
ROGUE_VALUE = "rogue"

#: How many undeclared replicas `scale` launches.
ROGUE_COUNT = 2


class ChaosError(Exception):
    """A chaos request that cannot be performed.

    Carries the HTTP status the API should answer with.
    """

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


class ChaosInjector:
    """Performs one real drift injection per call, audited as events."""

    def __init__(self, gateway, store, compose, project: str):
        self.gateway = gateway
        self.store = store
        self.compose = compose  # ComposeFile — the desired-state source
        self.project = project

    # ------------------------------------------------------------- public

    def inject(self, action: str = "random", service: str | None = None) -> dict:
        """Inject one drift. Returns a dict describing what was done."""
        if action not in CHAOS_ACTIONS:
            raise ChaosError(
                f"unknown action '{action}': use one of {', '.join(CHAOS_ACTIONS)}"
            )
        if action == "random":
            action = random.choice(("kill", "scale", "env", "image"))
        result = getattr(self, f"_{action}")(service)
        result["action"] = action
        self._audit(result)
        return result

    # ------------------------------------------------------------ actions

    def _kill(self, service: str | None) -> dict:
        """docker kill: container stays dead until Moor restores it."""
        target = self._pick_live(service)
        self.gateway.kill_container(target.id)
        return {
            "service": target.service,
            "container_name": target.name,
            "detail": f"docker kill {target.name} — container left dead",
        }

    def _scale(self, service: str | None) -> dict:
        """Launch undeclared rogue replicas of a service."""
        target = self._pick_live(service)
        taken = {c.name for c in self.gateway.list_containers().for_service(target.service)}
        spawned: list[str] = []
        n = 1
        while len(spawned) < ROGUE_COUNT:
            name = f"{self.project}-{target.service}-rogue{n}"
            n += 1
            if name in taken:
                continue
            spawned.append(self.gateway.spawn_replica(target.id, name).name)
        return {
            "service": target.service,
            "container_name": target.name,
            "containers": spawned,
            "detail": f"spawned rogue replicas {', '.join(spawned)} (undeclared)",
        }

    def _env(self, service: str | None) -> dict:
        """Recreate a container with one environment variable wrong."""
        target = self._pick_live(service)
        svc = self._require_service(target.service)
        if svc and svc.env:
            key = sorted(svc.env)[0]
            declared = svc.env[key]
        else:
            key, declared = "MOOR_CHAOS", None
        new = self.gateway.recreate_container(
            target.id, env_overrides={key: ROGUE_VALUE}
        )
        origin = f"declared '{declared}'" if declared is not None else "key not declared"
        return {
            "service": target.service,
            "container_name": new.name,
            "detail": f"recreated {new.name} with {key}='{ROGUE_VALUE}' ({origin})",
        }

    def _image(self, service: str | None) -> dict:
        """Recreate a container with a different image tag."""
        target = self._pick_live(service)
        svc = self._require_service(target.service)
        declared = normalize_image(svc.image) if svc else ""
        candidates = [i for i in self.gateway.list_local_images() if i != declared]
        if not candidates:
            raise ChaosError("no alternative image available locally", 502)
        # Preference order: a different tag of the same repository, then
        # any image that keeps running, then anything. Base images whose
        # default command is a shell (busybox & friends) exit instantly,
        # which degrades image drift into replica drift and leaves a dead
        # container behind — avoid them when possible.
        repo = declared.rsplit(":", 1)[0]
        same_repo = [i for i in candidates if i.rsplit(":", 1)[0] == repo]
        long_running = [i for i in candidates if not self._is_shell_image(i)]
        pick = (same_repo or long_running or candidates)[0]
        new = self.gateway.recreate_container(target.id, image=pick)
        return {
            "service": target.service,
            "container_name": new.name,
            "detail": f"recreated {new.name} with image {pick} (declared {declared})",
        }

    # ------------------------------------------------------------ helpers

    def _desired(self):
        return self.compose.current()

    def _require_service(self, service: str):
        svc = self._desired().service(service)
        if svc is None:
            raise ChaosError(f"service '{service}' is not managed by moor", 400)
        return svc

    def _pick_live(self, service: str | None):
        """Choose a live container to hurt: of `service`, or any managed one."""
        actual = self.gateway.list_containers()
        if service is not None:
            self._require_service(service)
            live = actual.live_for_service(service)
            if not live:
                raise ChaosError(f"no running container for service '{service}'", 409)
        else:
            live = [
                c
                for name in self._desired().managed_names
                for c in actual.live_for_service(name)
            ]
            if not live:
                raise ChaosError("no running managed containers to target", 409)
        return random.choice(live)

    def _is_shell_image(self, image_ref: str) -> bool:
        """True when the image's default command is a bare shell."""
        try:
            cmd = self.gateway.image_cmd(image_ref)
        except Exception:
            return False
        if not cmd:
            return False
        first = str(cmd[0]).rsplit("/", 1)[-1]
        return first in ("sh", "bash", "ash", "dash")

    def _audit(self, result: dict) -> None:
        try:
            self.store.append(
                CHAOS_INJECTED,
                result.get("service"),
                "warning",
                {
                    "action": result["action"],
                    # summary is self-contained (names the container) —
                    # renderers prepend container_name, so it is omitted
                    # here to avoid double-printing it.
                    "summary": result["detail"],
                    "containers": result.get("containers", []),
                    "origin": "control-plane",
                },
            )
        except Exception:
            pass  # an event-store outage must not fail the chaos call
