from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).parent))

from app.api.app import create_app  # noqa: E402
from app.config import Settings  # noqa: E402
from app.execution.worker import run_pending  # noqa: E402
from demo_site import DemoSite  # noqa: E402


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings.from_env(
        data_dir=tmp_path, db_path=tmp_path / "test.db", artifacts_dir=tmp_path / "artifacts",
        start_workers=False, auto_migrate=True, planner_mode="rules", headless=True,
        action_timeout_ms=1500, navigation_timeout_ms=5000, test_timeout_s=60, trace_mode="off", video=False,
        healing_enabled=True, healing_max_per_run=3, history_stale_days=30,
    )


class Api:
    """Thin helper over TestClient that raises on unexpected status codes."""

    def __init__(self, client: TestClient):
        self.client = client

    @property
    def state(self):
        return self.client.app.state.uitest

    def call(self, method: str, path: str, expect: int | tuple[int, ...] = 200, **kw: Any) -> Any:
        r = self.client.request(method, "/api" + path, **kw)
        expected = (expect,) if isinstance(expect, int) else expect
        assert r.status_code in expected, f"{method} {path} -> {r.status_code}: {r.text}"
        return r.json() if r.content else None

    def project(self, name: str, url: str) -> dict:
        return self.call("POST", "/projects", 201, json={"name": name, "url": url})

    def add_tests(self, pid: str, *instructions: str) -> list[dict]:
        return self.call("POST", f"/projects/{pid}/tests", 201, json={"instructions": list(instructions)})["tests"]

    def run(self, pid: str, test_ids: list[str] | None = None, key: str | None = None, expect: Any = 201) -> dict:
        headers = {"Idempotency-Key": key} if key else {}
        return self.call("POST", f"/projects/{pid}/runs", expect, json={"testIds": test_ids} if test_ids else {},
                         headers=headers)

    def drain(self) -> int:
        return run_pending(self.state.db, self.state.settings, llm=self.state.planning.llm)

    def run_detail(self, pid: str, run_id: str) -> dict:
        return self.call("GET", f"/projects/{pid}/runs/{run_id}")


@pytest.fixture
def api(settings: Settings):
    app = create_app(settings, llm=None)
    with TestClient(app) as client:
        yield Api(client)


@pytest.fixture(scope="session")
def site():
    s = DemoSite().start()
    yield s
    s.stop()


@pytest.fixture(autouse=True)
def _reset_site_variant(request):
    if "site" in request.fixturenames:
        request.getfixturevalue("site").variant = "original"
    yield
