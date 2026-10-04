"""Shared application state handed to route handlers."""
from __future__ import annotations

import shutil
from dataclasses import dataclass
from datetime import timedelta

from fastapi import Request

from ..config import Settings
from ..db import Database
from ..execution.worker import WorkerPool
from ..planning.service import PlanningService
from ..util import iso, utcnow


@dataclass
class AppState:
    settings: Settings
    db: Database
    planning: PlanningService
    pool: WorkerPool | None = None

    def stale_before(self) -> str:
        return iso(utcnow() - timedelta(days=self.settings.history_stale_days))

    def notify_workers(self) -> None:
        if self.pool:
            self.pool.notify()

    def remove_artifacts(self, project_id: str) -> None:
        target = (self.settings.artifacts_dir / project_id).resolve()
        if target.parent == self.settings.artifacts_dir.resolve() and target.exists():
            shutil.rmtree(target, ignore_errors=True)


def get_state(request: Request) -> AppState:
    return request.app.state.uitest
