"""Moor CLI: terraform-style verbs against the control plane API.

    moor serve        run the control plane (API + reconciler + dashboard)
    moor status       compliance snapshot
    moor plan         drift diff + planned actions
    moor apply        reconcile now, tail the outcome
    moor watch        follow live events
    moor mode         show or set advise/auto
    moor events       recent audit trail
"""
from __future__ import annotations

import json
import os
import time
from typing import Optional

import httpx
import typer
from rich.box import SIMPLE
from rich.console import Console
from rich.table import Table

app = typer.Typer(
    name="moor",
    help="Moor — desired-state control plane for Docker environments.",
    no_args_is_help=True,
    add_completion=False,
)
console = Console()

API_BASE = os.environ.get("MOOR_API", "http://127.0.0.1:8080")
CLIENT = httpx.Client(base_url=API_BASE, timeout=30.0)

STATUS_COLORS = {
    "compliant": "green",
    "drifting": "red",
    "orphaned": "yellow",
    "reconciling": "yellow",
}
SEVERITY_COLORS = {
    "critical": "red",
    "warning": "yellow",
    "error": "red",
    "ok": "green",
    "info": "cyan",
}


def _get(path: str, params: dict | None = None) -> dict:
    try:
        response = CLIENT.get(path, params=params)
        response.raise_for_status()
        return response.json()
    except httpx.HTTPError as exc:
        console.print(f"[red]error talking to control plane at {API_BASE}[/red]")
        console.print(f"  {exc}")
        raise typer.Exit(code=1)


def _post(path: str, payload: dict | None = None) -> dict:
    try:
        response = CLIENT.post(path, json=payload or {})
        response.raise_for_status()
        return response.json()
    except httpx.HTTPError as exc:
        console.print(f"[red]request failed: {exc}")
        raise typer.Exit(code=1)


@app.command()
def serve() -> None:
    """Run the control plane (API server + reconciler loop + dashboard)."""
    from .api import serve as run_server

    run_server()


@app.command()
def status() -> None:
    """Show the compliance snapshot of every managed service."""
    state = _get("/api/state")
    header = Table.grid(padding=(0, 2))
    header.add_row(
        f"[bold cyan]Moor[/bold cyan] — project [b]{state['project']}[/b]"
        f"  ·  mode [b]{'[green]auto[/green]' if state['mode'] == 'auto' else '[yellow]advise[/yellow]'}[/b]"
        f"  ·  loop {state['interval']:.0f}s"
    )
    console.print(header)

    table = Table(box=SIMPLE, show_header=True, header_style="bold")
    table.add_column("service", style="bold")
    table.add_column("image (declared)", overflow="fold")
    table.add_column("image (actual)", overflow="fold")
    table.add_column("replicas", justify="center")
    table.add_column("state", justify="center")

    for svc in state["services"]:
        color = STATUS_COLORS.get(svc["status"], "white")
        actual_images = ", ".join(svc["images_actual"]) or "-"
        drift_note = ""
        if svc["status"] != "compliant":
            kinds = ",".join(sorted({i["kind"] for i in svc["drift_items"]}))
            drift_note = f" ({kinds})"
        table.add_row(
            svc["name"],
            svc["image_desired"] or "-",
            actual_images,
            f"{svc['replicas_actual']}/{svc['replicas_desired']}",
            f"[{color}]{svc['status']}{drift_note}[/{color}]",
        )
    console.print(table)
    drifting = [s for s in state["services"] if s["status"] != "compliant"]
    if drifting:
        console.print(
            f"[red]{len(drifting)} service(s) drifting[/red] — run [b]moor plan[/b] for the diff"
        )
    else:
        console.print("[green]All managed services compliant.[/green]")


@app.command()
def plan() -> None:
    """Show the drift diff and the actions auto mode would execute."""
    state = _get("/api/state")
    plan_data = _get("/api/plan")

    console.print(
        f"\n[bold cyan]Moor — desired state review[/bold cyan] "
        f"(project: {state['project']}, mode: {state['mode'].upper()})\n"
    )

    for svc in state["services"]:
        color = STATUS_COLORS.get(svc["status"], "white")
        images = ", ".join(svc["images_actual"]) or "none"
        console.print(
            f"  [bold]{svc['name']:<8}[/bold] {svc['image_desired'] or '-':<24} "
            f"replicas {svc['replicas_actual']}/{svc['replicas_desired']}   "
            f"[{color}]{svc['status'].upper()}[/{color}]"
        )
        for item in svc["drift_items"]:
            console.print(f"          [red]~[/red] {item['message']}")
            details = item.get("details") or {}
            if item["kind"] == "replicas_extra":
                for name in details.get("extra_containers", []):
                    console.print(f"            [dim]- {name}[/dim]")

    actions = plan_data.get("actions", [])
    if actions:
        console.print(f"\n[bold]Plan: {plan_data['count']} action(s)[/bold]")
        for action in actions:
            verb = {
                "remove": "[red]remove [/red]",
                "create": "[green]create [/green]",
                "start": "[yellow]start  [/yellow]",
            }[action["kind"]]
            target = action.get("container_name") or f"{state['project']}-{action['service']}-N"
            console.print(
                f"  {verb} container {target}  "
                f"[dim]({action['service']} · {action['reason']})[/dim]"
            )
    else:
        console.print("\n[green]Plan: no actions — environment matches declaration.[/green]")


@app.command()
def apply(timeout: int = 90) -> None:
    """Trigger a reconcile cycle now and tail its outcome."""
    state = _get("/api/state")
    if state["mode"] != "auto":
        console.print(
            "[yellow]mode is ADVISE — apply will detect and log, not remediate.[/yellow]\n"
            "Switch with: [b]moor mode auto[/b]\n"
        )
    since = _get("/api/events", params={"limit": 1})["events"]
    last_id = since[-1]["id"] if since else 0

    console.print("[bold cyan]moor apply[/bold cyan] — running reconcile cycle...\n")
    result = _post("/api/reconcile")

    deadline = time.time() + timeout
    shown = 0
    while time.time() < deadline:
        rows = _get("/api/events", params={"limit": 100, "since": last_id})["events"]
        for row in rows[shown:]:
            last_id = row["id"]
            shown += 1
            _print_event(row, compact=True)
        if rows and any(r["type"] in ("drift.resolved", "drift.persistent") for r in rows):
            break
        time.sleep(0.5)

    if result.get("resolved"):
        console.print("\n[green]Declared state restored.[/green]")
    elif result.get("drift"):
        console.print("\n[red]Drift remains — see events above.[/red]")
    else:
        console.print("\n[green]No drift detected.[/green]")


@app.command()
def watch() -> None:
    """Follow the live event stream (Ctrl+C to stop)."""
    console.print("[bold cyan]moor watch[/bold cyan] — streaming events (Ctrl+C to stop)\n")
    try:
        with CLIENT.stream("GET", "/api/stream") as response:
            event_type = "event"
            for line in response.iter_lines():
                if line.startswith("event:"):
                    event_type = line.split(":", 1)[1].strip()
                elif line.startswith("data:"):
                    try:
                        row = json.loads(line.split(":", 1)[1].strip())
                    except json.JSONDecodeError:
                        continue
                    _print_event(row, event_type=event_type)
    except httpx.HTTPError as exc:
        console.print(f"[red]stream failed: {exc}")
        raise typer.Exit(code=1)
    except KeyboardInterrupt:
        console.print("\n[dim]stopped.[/dim]")


@app.command()
def mode(new_mode: Optional[str] = typer.Argument(None)) -> None:
    """Show or set the reconciliation mode (advise / auto)."""
    if new_mode is None:
        state = _get("/api/state")
        console.print(f"mode: [bold]{state['mode']}[/bold]")
        return
    new_mode = new_mode.lower()
    if new_mode not in ("advise", "auto"):
        console.print("[red]mode must be 'advise' or 'auto'")
        raise typer.Exit(code=1)
    result = _post("/api/mode", {"mode": new_mode})
    color = "green" if new_mode == "auto" else "yellow"
    console.print(f"mode: [bold]{result['previous']}[/bold] -> [bold {color}]{result['mode']}[/bold {color}]")


@app.command()
def events(limit: int = typer.Argument(30, help="Number of events to show")) -> None:
    """Show recent audit-trail events."""
    rows = _get("/api/events", params={"limit": limit})["events"]
    for row in rows:
        _print_event(row)


# ------------------------------------------------------------------ output

def _print_event(row: dict, compact: bool = False, event_type: str | None = None) -> None:
    etype = event_type or row.get("type", "event")
    severity = row.get("severity", "info")
    color = SEVERITY_COLORS.get(severity, "cyan")
    ts = time.strftime("%H:%M:%S", time.localtime(row.get("ts", time.time())))
    service = row.get("service") or "-"
    payload = row.get("payload") or {}
    note = payload.get("summary") or payload.get("message") or payload.get("title") or ""
    container = payload.get("container_name")
    if container:
        note = f"{container}" + (f" — {note}" if note else "")
    if compact:
        console.print(f"  [{color}]·[/{color}] [{ts}] {etype:<18} {service:<10} {note}")
    else:
        console.print(f"[{color}]·[/{color}] [{ts}] {etype:<18} {service:<10} {note}")


if __name__ == "__main__":
    app()
