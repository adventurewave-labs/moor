"""The reconciliation engine: observe -> diff -> plan -> act, on a loop.

Design notes
------------
* Crash-safe: all cross-cycle memory (last hashes, last report, mode,
  backoffs) lives in the event store, so restarting the control plane
  resumes without losing history or misclassifying drift.
* Drift vs intended change: if the *desired* hash moved since the last
  cycle the divergence is an intended change (declaration moved) and
  the loop converges forward; if reality moved while the declaration
  is unchanged, it is drift and the loop restores the declaration.
* Advise mode never mutates. Auto mode executes plans for services not
  in backoff; services that fail to converge are rate-limited so a
  broken container can never trigger a restart storm.
"""
from __future__ import annotations

import hashlib
import json
import threading
import time

from .actions import build_plan, execute_plan
from .compose import ComposeFile
from .diff import diff_states
from .events import EventStore, WebhookAlerter
from .models import ActualState, DesiredState, DriftReport
from .config import MoorConfig

ENGINE_STARTED = "engine.started"
ENGINE_ERROR = "engine.error"
DRIFT_DETECTED = "drift.detected"
DRIFT_PERSISTENT = "drift.persistent"
DRIFT_RESOLVED = "drift.resolved"
CONVERGE_PLANNED = "converge.planned"
ALERT_SENT = "alert.sent"
ALERT_FAILED = "alert.failed"


def _hash_obj(obj) -> str:
    canonical = json.dumps(obj, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()[:16]


def desired_hash(desired: DesiredState) -> str:
    return _hash_obj([
        {
            "name": s.name, "image": s.image, "replicas": s.replicas,
            "env": s.env, "command": list(s.command or []),
            "ports": [(p.container_port, p.host_port, p.protocol) for p in s.ports],
            "networks": list(s.networks),
        }
        for s in desired.services
    ])


def actual_hash(actual: ActualState) -> str:
    return _hash_obj([
        {
            "name": c.name, "service": c.service, "image": c.image,
            "state": c.state, "env": c.env,
            "command": list(c.command or []),
            "ports": sorted((p.container_port, p.host_port, p.protocol) for p in c.ports),
        }
        for c in sorted(actual.containers, key=lambda c: c.name)
    ])


class Reconciler:
    """One reconciliation loop for one compose project."""

    def __init__(
        self,
        config: MoorConfig,
        gateway,
        store: EventStore,
        alerter: WebhookAlerter,
        compose_file: ComposeFile | None = None,
    ):
        self.config = config
        self.gateway = gateway
        self.store = store
        self.alerter = alerter
        self.compose = compose_file or ComposeFile(config.compose_path, config.project)
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.last_cycle: dict = {}

    # ------------------------------------------------------------ control

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="moor-reconciler", daemon=True)
        self._thread.start()
        self.emit(ENGINE_STARTED, None, "info", {
            "project": self.config.project,
            "mode": self.store.get_mode(),
            "interval": self.config.interval,
            "compose": self.config.compose_path,
        })

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.run_once()
            except Exception as exc:  # noqa: BLE001 — the loop must never die
                try:
                    self.emit(ENGINE_ERROR, None, "error", {"error": str(exc)})
                except Exception:
                    pass
            self._stop.wait(self.config.interval)

    # ------------------------------------------------------------ observe

    def _observe(self) -> tuple[DesiredState, ActualState, DriftReport]:
        desired = self.compose.current()
        actual = self.gateway.list_containers()
        report = diff_states(
            desired, actual,
            image_env_fn=self.gateway.image_env,
            image_cmd_fn=getattr(self.gateway, "image_cmd", None),
        )
        return desired, actual, report

    def current_state(self) -> dict:
        """Fresh observation for the API/UI. No side effects."""
        desired, actual, report = self._observe()
        services = []
        for svc in desired.services:
            containers = actual.for_service(svc.name)
            live = [c for c in containers if c.is_live]
            items = report.for_service(svc.name)
            images = sorted({c.image for c in live})
            services.append({
                "name": svc.name,
                "image_desired": svc.image,
                "images_actual": images,
                "replicas_desired": svc.replicas,
                "replicas_actual": len(live),
                "env_desired": sorted(svc.env.keys()),
                "drift_items": [i.to_json() for i in items],
                "status": "drifting" if items else "compliant",
            })
        actual_services = [s for s in actual.services() if desired.service(s) is None]
        for orphan in actual_services:
            items = report.for_service(orphan)
            services.append({
                "name": orphan,
                "image_desired": None,
                "images_actual": sorted({c.image for c in actual.for_service(orphan)}),
                "replicas_desired": 0,
                "replicas_actual": len(actual.live_for_service(orphan)),
                "env_desired": [],
                "drift_items": [i.to_json() for i in items],
                "status": "orphaned",
            })
        return {
            "project": self.config.project,
            "mode": self.store.get_mode(),
            "interval": self.config.interval,
            "compose": self.config.compose_path,
            "services": services,
            "drift": report.to_json(),
            "generated_at": time.time(),
        }

    def planned_actions(self) -> dict:
        """What auto mode *would* do right now (terraform-style plan)."""
        desired, actual, report = self._observe()
        plan = build_plan(desired, actual, report)
        return {**plan.to_json(), "mode": self.store.get_mode()}

    # ------------------------------------------------------------ the loop

    def run_once(self) -> dict:
        with self._lock:
            return self._cycle()

    def _cycle(self) -> dict:
        desired, actual, report = self._observe()
        dhash = desired_hash(desired)
        ahash = actual_hash(actual)
        snapshot = self.store.get_snapshot()
        first_cycle = snapshot is None
        snap = snapshot or {
            "desired_hash": None, "actual_hash": None,
            "report_hash": None, "drift": False, "updated": 0.0,
        }
        mode = self.store.get_mode()
        # Who moved first? If the declaration hash changed since the last
        # cycle, the divergence is an intended change. On the first cycle
        # there is no history: classify as drift (conservative default).
        intended = (not first_cycle) and (dhash != snap["desired_hash"])
        report_is_new = report.hash != snap["report_hash"]

        result = {
            "cycle_at": time.time(),
            "mode": mode,
            "drift": report.has_drift,
            "classification": None,
            "services": report.services_affected,
            "actions": None,
            "resolved": False,
        }

        # ---- all clear
        if not report.has_drift:
            if snap["drift"]:
                self.emit(DRIFT_RESOLVED, None, "ok", {
                    "summary": "all managed services compliant",
                })
                self._alert(
                    "State restored",
                    "All managed services match the declared compose state.",
                    "ok",
                    [("project", self.config.project)],
                )
                for service in list(desired.managed_names):
                    self.store.clear_backoff(service)
            self.store.save_snapshot(dhash, ahash, report.hash, False)
            self.last_cycle = result
            return result

        # ---- divergence exists
        result["classification"] = "intended" if intended else "drift"

        if intended:
            self.emit(CONVERGE_PLANNED, None, "info", {
                "summary": "desired state changed; converging environment",
                "services": report.services_affected,
                "items": [i.to_json() for i in report.items],
            })
            self._alert(
                "Intended change detected",
                "The compose declaration changed. Moor is converging the "
                "live environment to match it.",
                "info",
                [("project", self.config.project),
                 ("services", ", ".join(report.services_affected))],
            )
        else:
            if report_is_new or not snap["drift"]:
                for service in report.services_affected:
                    items = [i.to_json() for i in report.for_service(service)]
                    self.emit(DRIFT_DETECTED, service, "critical", {
                        "items": items,
                        "summary": items[0]["message"] if items else "",
                    })
                self._alert(
                    "Configuration drift detected",
                    "\n".join(
                        f"*{i.service}*: {i.message}" for i in report.items[:8]
                    ) or "drift detected",
                    "critical",
                    [("project", self.config.project),
                     ("services", ", ".join(report.services_affected)),
                     ("mode", mode)],
                )

        # ---- remediation
        if mode == "auto":
            actionable = [
                s for s in report.services_affected
                if not self.store.in_backoff(s)
            ]
            if actionable:
                plan = self._filter_plan(build_plan(desired, actual, report), actionable)
                summary = execute_plan(self.gateway, plan, self.emit)
                result["actions"] = summary

                # re-observe after acting
                _, actual2, report2 = self._observe()
                if report2.has_drift:
                    now = time.time()
                    for service in report2.services_affected:
                        prev_until = self.store.backoff_until(service)
                        attempts = 1
                        self.store.set_backoff(service, now + self.config.cooldown, attempts)
                        self.emit(DRIFT_PERSISTENT, service, "error", {
                            "message": "drift persists after remediation; backing off",
                            "cooldown": self.config.cooldown,
                            "prev_until": prev_until,
                        })
                    self._alert(
                        "Drift persists after remediation",
                        "Moor applied its plan but drift remains. "
                        "Remediation is backing off; manual attention may be required.",
                        "warning",
                        [("services", ", ".join(report2.services_affected)),
                         ("cooldown", f"{self.config.cooldown:.0f}s")],
                    )
                    self.store.save_snapshot(dhash, ahash, report2.hash, True)
                    self.last_cycle = result
                    return result
                self.emit(DRIFT_RESOLVED, None, "ok", {
                    "summary": "declared state restored",
                    "actions": summary,
                })
                self._alert(
                    "Drift remediated",
                    "Moor restored the declared state automatically.",
                    "ok",
                    [("project", self.config.project),
                     ("actions_ok", str(summary.get("ok", 0))),
                     ("actions_failed", str(summary.get("failed", 0)))],
                )
                for service in list(desired.managed_names):
                    self.store.clear_backoff(service)
                result["resolved"] = True

        self.store.save_snapshot(
            dhash, ahash, report.hash, report.has_drift
        )
        self.last_cycle = result
        return result

    def _filter_plan(self, plan, services: list[str]):
        from .models import ActionPlan
        return ActionPlan(actions=tuple(
            a for a in plan.actions if a.service in services
        ))

    # ------------------------------------------------------------ helpers

    def emit(self, type_: str, service: str | None, severity: str, payload: dict) -> None:
        try:
            self.store.append(type_, service, severity, payload)
        except Exception:
            pass  # event store outage must not stop reconciliation

    def _alert(self, title: str, text: str, color: str, fields) -> None:
        if not self.alerter.url:
            return
        ok, error = self.alerter.send(title, text, color, fields)
        if ok:
            self.emit(ALERT_SENT, None, "info", {"title": title, "color": color})
        else:
            self.emit(ALERT_FAILED, None, "error", {"title": title, "error": error})
