"""FastAPI application factory."""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from ..config import Settings
from ..db import Database, migrate, pending_migrations
from ..errors import AppError
from ..execution.worker import WorkerPool
from ..planning.llm import JsonLLM, build_llm
from ..planning.service import PlanningService
from . import routes_bugs, routes_history, routes_projects, routes_runs
from .state import AppState

log = logging.getLogger(__name__)
_AUTO = object()


def create_app(settings: Settings | None = None, llm: Any = _AUTO) -> FastAPI:
    settings = settings or Settings.from_env()
    settings.ensure_dirs()
    db = Database(settings.db_path)
    if llm is _AUTO:
        llm = build_llm(settings)
    state = AppState(settings=settings, db=db, planning=PlanningService(settings, llm))

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if settings.auto_migrate:
            applied = migrate(db)
            if applied:
                log.info("applied migrations: %s", ", ".join(applied))
        elif pending_migrations(db):
            raise RuntimeError("The database has pending migrations. Run: uv run python -m app.cli migrate")
        if settings.start_workers:
            state.pool = WorkerPool(db, settings, llm)
            state.pool.start()
        try:
            yield
        finally:
            if state.pool:
                state.pool.stop()

    app = FastAPI(title="UI Test Agent API", version="1.0.0", lifespan=lifespan)
    app.state.uitest = state
    app.add_middleware(CORSMiddleware, allow_origins=list(settings.cors_origins), allow_methods=["*"],
                       allow_headers=["*"])

    @app.exception_handler(AppError)
    async def app_error(_: Request, exc: AppError) -> JSONResponse:
        return JSONResponse({"error": {"code": exc.code, "message": exc.message, "details": exc.details}},
                            status_code=exc.status_code)

    @app.get("/api/health")
    def health() -> dict[str, Any]:
        llm_obj: JsonLLM | None = state.planning.llm
        return {
            "status": "ok",
            "pendingMigrations": pending_migrations(db),
            "workers": {"enabled": state.pool is not None, "alive": state.pool.alive if state.pool else 0},
            "planner": {"mode": settings.planner_mode, "llm": llm_obj.model if llm_obj else None},
            "liveView": False,
            "video": settings.video,
        }

    for router in (routes_projects.router, routes_runs.router, routes_history.router, routes_bugs.router):
        app.include_router(router, prefix="/api")
    return app
