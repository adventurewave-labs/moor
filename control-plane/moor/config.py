"""Runtime configuration, sourced from environment variables."""
from __future__ import annotations

import os
from dataclasses import dataclass

DEFAULT_COMPOSE_PATH = "/desired-state/docker-compose.yml"
DEFAULT_PROJECT = "moordemo"
DEFAULT_INTERVAL = 5.0
DEFAULT_MODE = "advise"
DEFAULT_COOLDOWN = 20.0


@dataclass
class MoorConfig:
    project: str
    compose_path: str
    mode: str
    interval: float
    webhook_url: str | None
    db_url: str
    api_host: str
    api_port: int
    cooldown: float

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "MoorConfig":
        env = dict(os.environ if env is None else env)
        return cls(
            project=env.get("MOOR_PROJECT", DEFAULT_PROJECT),
            compose_path=env.get("MOOR_COMPOSE", DEFAULT_COMPOSE_PATH),
            mode=(env.get("MOOR_MODE", DEFAULT_MODE) or DEFAULT_MODE).lower(),
            interval=float(env.get("MOOR_INTERVAL", str(DEFAULT_INTERVAL))),
            webhook_url=env.get("MOOR_ALERT_WEBHOOK") or None,
            db_url=env.get(
                "MOOR_DB_URL",
                "postgresql+psycopg://moor:moor@moor-db:5432/moor",
            ),
            api_host=env.get("MOOR_API_HOST", "0.0.0.0"),
            api_port=int(env.get("MOOR_API_PORT", "8080")),
            cooldown=float(env.get("MOOR_COOLDOWN", str(DEFAULT_COOLDOWN))),
        )

    @property
    def valid_modes(self) -> tuple[str, ...]:
        return ("advise", "auto")
