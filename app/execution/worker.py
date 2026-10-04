"""In-process worker pool.

Each worker thread owns one Playwright instance and one browser; every test run gets a
fresh isolated browser context (independent executions never share cookies/storage).
Claims are atomic, so a test run is executed by exactly one worker."""
from __future__ import annotations

import logging
import threading
import time
import uuid

from playwright.sync_api import Browser, Playwright, sync_playwright

from ..config import Settings
from ..db import Database
from ..planning.llm import JsonLLM
from ..repo import runs as runs_repo
from .executor import TestRunExecutor
from .finalize import finalize_test_run, recover_interrupted

log = logging.getLogger(__name__)
_STALE_CHECK_EVERY_S = 30


def launch_browser(pw: Playwright, settings: Settings, attempts: int = 2) -> Browser:
    last: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            return pw.chromium.launch(headless=settings.headless)
        except Exception as exc:  # bounded retry, then surface as browser_launch_failed
            last = exc
            log.warning("browser launch attempt %s failed: %s", attempt, exc)
            time.sleep(1)
    raise RuntimeError(f"Could not launch Chromium: {last}")


def claim(db: Database, worker_id: str) -> dict | None:
    with db.tx() as conn:
        return runs_repo.claim_next(conn, worker_id)


class WorkerPool:
    def __init__(self, db: Database, settings: Settings, llm: JsonLLM | None = None):
        self.db, self.settings, self.llm = db, settings, llm
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._threads: list[threading.Thread] = []

    def start(self) -> None:
        recovered = recover_interrupted(self.db, None)
        if recovered:
            log.warning("marked %s interrupted test run(s) from a previous process as error", recovered)
        for i in range(self.settings.runner_concurrency):
            t = threading.Thread(target=self._loop, args=(f"worker-{i}-{uuid.uuid4().hex[:6]}",),
                                 name=f"uitest-worker-{i}", daemon=True)
            t.start()
            self._threads.append(t)

    def notify(self) -> None:
        self._wake.set()

    def stop(self, timeout: float = 15) -> None:
        self._stop.set()
        self._wake.set()
        for t in self._threads:
            t.join(timeout)

    @property
    def alive(self) -> int:
        return sum(t.is_alive() for t in self._threads)

    def _loop(self, worker_id: str) -> None:
        executor = TestRunExecutor(self.db, self.settings, llm=self.llm)
        pw: Playwright | None = None
        browser: Browser | None = None
        last_check = 0.0
        try:
            while not self._stop.is_set():
                if time.monotonic() - last_check > _STALE_CHECK_EVERY_S:
                    last_check = time.monotonic()
                    try:
                        recover_interrupted(self.db, self.settings.heartbeat_stale_s)
                    except Exception:
                        log.exception("stale-run check failed")
                try:
                    tr = claim(self.db, worker_id)
                except Exception:
                    log.exception("claim failed")
                    tr = None
                if tr is None:
                    self._wake.wait(1.0)
                    self._wake.clear()
                    continue
                try:
                    if browser is None or not browser.is_connected():
                        pw = pw or sync_playwright().start()
                        browser = launch_browser(pw, self.settings)
                except Exception as exc:
                    finalize_test_run(self.db, tr["project_id"], tr["id"], "error", "browser_launch_failed", str(exc))
                    continue
                try:
                    executor.execute(browser, tr["project_id"], tr["id"])
                except Exception:  # keep the worker alive; the stale-run check finalizes the run
                    log.exception("executor failed for test run %s", tr["id"])
        finally:
            for closer in (browser and browser.close, pw and pw.stop):
                try:
                    if closer:
                        closer()
                except Exception:
                    pass


def run_pending(db: Database, settings: Settings, max_runs: int | None = None, llm: JsonLLM | None = None) -> int:
    """Executes queued test runs in the calling thread until the queue is empty (CLI/tests)."""
    executor = TestRunExecutor(db, settings, llm=llm)
    done = 0
    with sync_playwright() as pw:
        browser: Browser | None = None
        try:
            while max_runs is None or done < max_runs:
                tr = claim(db, "inline")
                if tr is None:
                    break
                if browser is None or not browser.is_connected():
                    browser = launch_browser(pw, settings)
                executor.execute(browser, tr["project_id"], tr["id"])
                done += 1
        finally:
            if browser is not None:
                browser.close()
    return done
