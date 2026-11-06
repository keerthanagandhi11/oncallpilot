"""FastAPI application: wires settings, services, the escalation engine, the worker and the UI."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from app.api.routes import api, public
from app.clock import Clock
from app.config import Settings, get_settings
from app.engine.escalation import EscalationEngine
from app.engine.worker import Worker
from app.logging_setup import setup_logging
from app.services import build_services

log = logging.getLogger(__name__)
STATIC = Path(__file__).parent / "static"


def create_app(settings: Settings | None = None, clock: Clock | None = None, services=None) -> FastAPI:
    settings = settings or get_settings()
    setup_logging(settings.log_level)
    problems = settings.check_production()
    if problems:
        raise RuntimeError("Unsafe production config: " + "; ".join(problems))

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        svc = services or build_services(settings, clock)
        await svc.db.create_all()
        engine = EscalationEngine(svc)
        app.state.settings, app.state.services, app.state.engine = settings, svc, engine
        app.state.worker = None
        if settings.worker_enabled:
            app.state.worker = Worker(engine, svc.uac)
            app.state.worker.start()
        log.info(
            "OnCallPilot started",
            extra={"demo_mode": settings.demo_mode, "time_scale": settings.time_scale, "llm": settings.llm_enabled},
        )
        try:
            yield
        finally:
            if app.state.worker:
                await app.state.worker.stop()
            http = svc.extras.get("http")
            if http is not None:
                await http.aclose()
            await svc.db.dispose()

    app = FastAPI(
        title="OnCallPilot",
        version="1.0.0",
        description="Automated on-call escalation for Stonebranch UAC job failures.",
        lifespan=lifespan,
    )
    app.include_router(public)
    app.include_router(api)

    @app.get("/metrics", include_in_schema=False)
    async def metrics():
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    @app.get("/", include_in_schema=False)
    async def index():
        return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})

    @app.middleware("http")
    async def security_headers(request, call_next):
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault("X-Frame-Options", "DENY")
        return response

    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    return app


# Run with: uvicorn app.main:create_app --factory
