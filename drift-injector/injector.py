"""Moor drift-injector — a real chaos tool, not a simulation.

Every action performs the exact Docker Engine API mutations a tired
engineer (or a rogue script) would: kill a container, scale a service
by launching extra replicas, swap an image, or mutate environment
variables by recreating a container by hand.

The injected containers carry the same compose labels as the real
stack, so from the control plane's point of view they are
indistinguishable from legitimate members of the environment — which
is precisely the drift Moor must catch.
"""
from __future__ import annotations

import os
import random

import docker
import typer
from rich.console import Console

main = typer.Typer(help="Drift injector — chaos tool for the Moor demo.", no_args_is_help=True)
app = typer.Typer(help="Inject one drift action.", no_args_is_help=True)
main.add_typer(app, name="inject")
console = Console()

PROJECT = os.environ.get("MOOR_PROJECT", "moordemo")
PROJECT_LABEL = "com.docker.compose.project"
SERVICE_LABEL = "com.docker.compose.service"


def get_client() -> docker.DockerClient:
    return docker.from_env(timeout=30)


def containers_for(client: docker.DockerClient, service: str):
    return [
        c for c in client.containers.list(
            all=True,
            filters={"label": f"{PROJECT_LABEL}={PROJECT}"},
        )
        if (c.labels or {}).get(SERVICE_LABEL) == service
    ]


def pick_running(service: str):
    client = get_client()
    found = [c for c in containers_for(client, service) if c.status == "running"]
    if not found:
        console.print(f"[red]no running container for service '{service}'[/red]")
        raise typer.Exit(code=1)
    return client, found[0]


def recreate_like(container, *, image=None, env_overrides=None):
    """Stop+remove a container, then recreate it with modified config.

    This is the classic hand-fix: the operator 'replaces' the container
    themselves, slightly wrong, and walks away.
    """
    client = get_client()
    attrs = container.attrs
    config = attrs.get("Config") or {}
    labels = dict(config.get("Labels") or {})
    networks = list((attrs.get("NetworkSettings") or {}).get("Networks", {}).keys())
    name = (container.name or "").lstrip("/")

    env = list(config.get("Env") or [])
    if env_overrides:
        for key, value in env_overrides.items():
            env = [e for e in env if not e.startswith(key + "=")]
            env.append(f"{key}={value}")

    primary = networks[0] if networks else None
    container.stop(timeout=2)
    container.remove(force=True)

    new = client.containers.create(
        image=image or config.get("Image"),
        name=name,
        command=config.get("Cmd"),
        environment=env,
        labels=labels,
        detach=True,
        network=primary,
    )
    for net in networks[1:]:
        try:
            client.api.connect_container_to_network(new.id, net)
        except docker.errors.APIError:
            pass
    new.start()
    return new


# ------------------------------------------------------------------ actions

@app.command()
def kill(service: str = typer.Option("web", help="Service to kill")):
    """Kill one running container of a service (stays dead: no restart policy)."""
    client, container = pick_running(service)
    console.print(
        f"[yellow]inject>[/yellow] killing [b]{container.name}[/b] "
        f"({container.image.tags[0] if container.image.tags else container.image.id[:15]})"
    )
    container.kill()
    console.print("[green]done — the container is now dead. Watch Moor detect it.[/green]")


@app.command()
def scale(
    service: str = typer.Option("cache", help="Service to rogue-scale"),
    count: int = typer.Option(4, help="Total replica count after injection"),
):
    """Launch extra rogue replicas of a service (same labels, no host ports)."""
    client = get_client()
    found = containers_for(client, service)
    if not found:
        console.print(f"[red]no container for service '{service}'[/red]")
        raise typer.Exit(code=1)
    source = found[0]
    attrs = source.attrs
    config = attrs.get("Config") or {}
    labels = dict(config.get("Labels") or {})
    networks = list((attrs.get("NetworkSettings") or {}).get("Networks", {}).keys())

    to_add = count - len([c for c in found if c.status == "running"])
    if to_add <= 0:
        console.print(f"service already has {len(found)} containers")
        return
    for i in range(to_add):
        name = f"{PROJECT}-{service}-rogue{i + 1}"
        try:
            existing = client.containers.get(name)
            existing.remove(force=True)
        except docker.errors.NotFound:
            pass
        console.print(
            f"[yellow]inject>[/yellow] creating rogue replica [b]{name}[/b] "
            f"({config.get('Image')})"
        )
        new = client.containers.create(
            image=config.get("Image"),
            name=name,
            command=config.get("Cmd"),
            environment=list(config.get("Env") or []),
            labels=labels,
            detach=True,
            network=networks[0] if networks else None,
        )
        for net in networks[1:]:
            try:
                client.api.connect_container_to_network(new.id, net)
            except docker.errors.APIError:
                pass
        new.start()
    console.print(f"[green]done — {to_add} rogue replica(s) running. Watch Moor detect it.[/green]")


@app.command()
def env(
    service: str = typer.Option("db", help="Service to mutate"),
    key: str = typer.Option("POSTGRES_PASSWORD", help="Environment key to change"),
    value: str = typer.Option("rogue", help="New (wrong) value"),
):
    """Recreate a container with a mutated environment variable."""
    client, container = pick_running(service)
    console.print(
        f"[yellow]inject>[/yellow] recreating [b]{container.name}[/b] with "
        f"{key} changed to '{value}'"
    )
    recreate_like(container, env_overrides={key: value})
    console.print("[green]done — environment drift injected. Watch Moor detect it.[/green]")


@app.command()
def image(
    service: str = typer.Option("cache", help="Service to swap"),
    new_image: str = typer.Option("redis:6.2-alpine", "--image", help="Replacement image"),
):
    """Recreate a container with a different image tag."""
    client, container = pick_running(service)
    console.print(
        f"[yellow]inject>[/yellow] recreating [b]{container.name}[/b] with image {new_image}"
    )
    recreate_like(container, image=new_image)
    console.print("[green]done — image drift injected. Watch Moor detect it.[/green]")


@app.command(name="random")
def random_action():
    """Pick one drift at random and inject it."""
    choice = random.choice(["kill", "scale", "env", "image"])
    console.print(f"[dim]choosing action: {choice}[/dim]")
    if choice == "kill":
        kill(service="web")
    elif choice == "scale":
        scale(service="cache", count=4)
    elif choice == "env":
        env(service="db")
    else:
        image(service="cache")


if __name__ == "__main__":
    main()
