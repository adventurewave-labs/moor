"""FastAPI application: REST API, SSE event stream, dashboard.

The API is the product's integration surface: the dashboard, the CLI,
and any external tooling all talk to these endpoints.
"""
from __future__ import annotations

import asyncio
import os
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

from .config import MoorConfig
from .docker_client import DockerGateway
from .engine import Reconciler
from .events import EventStore, WebhookAlerter

UI_DIR = os.path.join(os.path.dirname(__file__), "ui")


class ModeRequest(BaseModel):
    mode: str


def create_app(
    config: MoorConfig | None = None,
    gateway: DockerGateway | None = None,
    store: EventStore | None = None,
    alerter: WebhookAlerter | None = None,
    engine: Reconciler | None = None,
    start_engine: bool = True,
) -> FastAPI:
    """Assemble the app. All dependencies are injectable for tests."""
    config = config or MoorConfig.from_env()
    store = store or EventStore(config.db_url)
    if store.get_mode() not in ("advise", "auto"):
        store.set_mode(config.mode)
    gateway = gateway or DockerGateway(config.project)
    alerter = alerter or WebhookAlerter(config.webhook_url)
    engine = engine or Reconciler(config, gateway, store, alerter)

    @asynccontextmanager
    async def _lifespan(app: FastAPI):
        if start_engine:
            engine.start()
        yield
        engine.stop()

    app = FastAPI(
        title="Moor control plane",
        version="1.0.0",
        description="Desired-state reconciliation for docker-compose environments",
        lifespan=_lifespan,
    )
    app.add_middleware(
        CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"]
    )
    app.state.config = config
    app.state.store = store
    app.state.engine = engine
    app.state.gateway = gateway

    # ------------------------------------------------------------------ ui

    @app.get("/", include_in_schema=False)
    async def dashboard() -> FileResponse:
        return FileResponse(os.path.join(UI_DIR, "index.html"))

    # ----------------------------------------------------------------- api

    @app.get("/api/health")
    async def health() -> dict:
        return {
            "ok": True,
            "project": config.project,
            "mode": store.get_mode(),
            "interval": config.interval,
            "compose": config.compose_path,
            "docker": gateway.engine_info(),
            "version": "1.0.0",
        }

    @app.get("/api/state")
    async def state() -> dict:
        return engine.current_state()

    @app.get("/api/drift")
    async def drift() -> dict:
        return engine.current_state()["drift"]

    @app.get("/api/plan")
    async def plan() -> dict:
        return engine.planned_actions()

    @app.get("/api/events")
    async def events(limit: int = 100, since: int = 0) -> dict:
        return {"events": store.list(limit=limit, since_id=since)}

    @app.post("/api/mode")
    async def set_mode(body: ModeRequest) -> dict:
        mode = body.mode.lower()
        if mode not in config.valid_modes:
            raise HTTPException(status_code=400, detail="mode must be advise or auto")
        previous = store.get_mode()
        store.set_mode(mode)
        engine.emit("mode.changed", None, "info", {"from": previous, "to": mode})
        return {"mode": mode, "previous": previous}

    @app.post("/api/reconcile")
    async def reconcile() -> dict:
        result = await asyncio.to_thread(engine.run_once)
        return result

    @app.get("/api/stream")
    async def stream() -> StreamingResponse:
        async def event_stream():
            last_id = store.last_id()
            yield "retry: 3000\n\n"
            idle = 0
            while True:
                rows = store.list(limit=50, since_id=last_id)
                if rows:
                    for row in rows:
                        last_id = row["id"]
                        yield (
                            f"id: {row['id']}\n"
                            f"event: {row['type']}\n"
                            f"data: {row}\n\n"
                        )
                    idle = 0
                else:
                    idle += 1
                    if idle % 5 == 0:  # ~5s keepalive for proxies
                        yield ": ping\n\n"
                await asyncio.sleep(1.0)

        return StreamingResponse(
            event_stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    return app


def serve() -> None:
    """Entrypoint for `moor serve`."""
    import uvicorn

    config = MoorConfig.from_env()
    app = create_app(config)
    uvicorn.run(app, host=config.api_host, port=config.api_port, log_level="info")
