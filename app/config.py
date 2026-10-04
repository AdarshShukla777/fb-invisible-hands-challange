"""Runtime settings, read once from environment variables (prefix UITEST_)."""
from __future__ import annotations

import os
from dataclasses import dataclass, replace
from pathlib import Path

# Default data location: backend/data, independent of the directory the server is started from.
BACKEND_DIR = Path(__file__).resolve().parents[1]


def _str(name: str, default: str) -> str:
    return os.environ.get(name, default).strip() or default


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    return default if raw is None else raw.strip().lower() in {"1", "true", "yes", "on"}


def _int(name: str, default: int, lo: int, hi: int) -> int:
    try:
        value = int(os.environ.get(name, default))
    except ValueError:
        value = default
    return max(lo, min(hi, value))


def _float(name: str, default: float, lo: float, hi: float) -> float:
    try:
        value = float(os.environ.get(name, default))
    except ValueError:
        value = default
    return max(lo, min(hi, value))


def _choice(name: str, default: str, allowed: set[str]) -> str:
    value = _str(name, default).lower()
    return value if value in allowed else default


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    db_path: Path
    artifacts_dir: Path
    cors_origins: tuple[str, ...]
    auto_migrate: bool
    start_workers: bool
    # Execution
    runner_concurrency: int
    headless: bool
    slow_mo_ms: int  # extra pause before each visible action, to follow a run by eye
    highlight: str  # auto | on | off: pointer and element boxes (auto: when headed or recording)
    action_timeout_ms: int
    navigation_timeout_ms: int
    test_timeout_s: int
    max_steps: int
    trace_mode: str  # off | on_failure | always
    video: bool
    screenshot_mode: str  # failures | steps
    heartbeat_stale_s: int
    # Healing
    healing_enabled: bool
    healing_min_confidence: float
    healing_min_margin: float
    healing_plausible: float
    healing_max_per_run: int
    history_stale_days: int
    # Planning
    planner_mode: str  # auto | rules | llm
    llm_provider: str  # anthropic | openai_compatible
    llm_base_url: str
    llm_api_key: str
    llm_max_tokens: int
    llm_disable_thinking: bool
    llm_model: str
    llm_effort: str
    llm_timeout_s: float
    llm_max_calls_per_plan: int
    llm_fallbacks: bool
    planner_concurrency: int
    # Agent mode
    agent_max_steps: int
    agent_replay: bool
    agent_decision_timeout_s: float
    agent_test_timeout_s: int
    # Layout tracking: pixels an acted-on element may move or resize before it is reported
    layout_tolerance_px: int = 3

    @classmethod
    def from_env(cls, **overrides) -> "Settings":
        data_dir = Path(overrides.pop("data_dir", None) or _str("UITEST_DATA_DIR", str(BACKEND_DIR / "data"))).resolve()
        provider = _choice("UITEST_LLM_PROVIDER", "anthropic", {"anthropic", "openai_compatible", "gemini"})
        local = provider == "openai_compatible"
        default_model = {"anthropic": "claude-opus-5", "gemini": "gemini-2.5-flash"}.get(provider, "")
        settings = cls(
            data_dir=data_dir,
            db_path=Path(_str("UITEST_DB_PATH", str(data_dir / "uitest.db"))),
            artifacts_dir=Path(_str("UITEST_ARTIFACTS_DIR", str(data_dir / "artifacts"))),
            cors_origins=tuple(
                o.strip() for o in _str("UITEST_CORS_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173").split(",")
                if o.strip()
            ),
            auto_migrate=_bool("UITEST_AUTO_MIGRATE", True),
            start_workers=_bool("UITEST_START_WORKERS", True),
            runner_concurrency=_int("UITEST_RUNNER_CONCURRENCY", 2, 1, 8),
            headless=_bool("UITEST_HEADLESS", True),
            slow_mo_ms=_int("UITEST_SLOW_MO_MS", 0, 0, 5000),
            highlight=_choice("UITEST_HIGHLIGHT", "auto", {"auto", "on", "off"}),
            action_timeout_ms=_int("UITEST_ACTION_TIMEOUT_MS", 5000, 500, 60000),
            navigation_timeout_ms=_int("UITEST_NAVIGATION_TIMEOUT_MS", 15000, 1000, 120000),
            test_timeout_s=_int("UITEST_TEST_TIMEOUT_S", 180, 10, 1800),
            max_steps=_int("UITEST_MAX_STEPS", 50, 1, 200),
            trace_mode=_choice("UITEST_TRACE", "on_failure", {"off", "on_failure", "always"}),
            video=_bool("UITEST_VIDEO", False),
            screenshot_mode=_choice("UITEST_SCREENSHOTS", "failures", {"failures", "steps"}),
            heartbeat_stale_s=_int("UITEST_HEARTBEAT_STALE_S", 300, 30, 7200),
            healing_enabled=_bool("UITEST_HEALING", True),
            healing_min_confidence=_float("UITEST_HEALING_MIN_CONFIDENCE", 0.75, 0.5, 1.0),
            healing_min_margin=_float("UITEST_HEALING_MIN_MARGIN", 0.15, 0.0, 1.0),
            healing_plausible=_float("UITEST_HEALING_PLAUSIBLE", 0.5, 0.1, 1.0),
            healing_max_per_run=_int("UITEST_HEALING_MAX_PER_RUN", 3, 0, 20),
            history_stale_days=_int("UITEST_HISTORY_STALE_DAYS", 30, 1, 3650),
            planner_mode=_choice("UITEST_PLANNER", "auto", {"auto", "rules", "llm"}),
            llm_provider=provider,
            llm_base_url=os.environ.get("UITEST_LLM_BASE_URL", "").strip(),
            llm_api_key=os.environ.get("UITEST_LLM_API_KEY", "").strip(),
            # Gemini counts thinking tokens against the output limit, so it needs more room.
            llm_max_tokens=_int("UITEST_LLM_MAX_TOKENS", 16384 if provider == "gemini" else 4096, 256, 65536),
            llm_disable_thinking=_bool("UITEST_LLM_DISABLE_THINKING", True),
            # anthropic: claude-opus-5, gemini: gemini-2.5-flash; a local server must name its model explicitly
            llm_model=os.environ.get("UITEST_LLM_MODEL", "").strip() or default_model,
            llm_effort=_choice("UITEST_LLM_EFFORT", "medium", {"low", "medium", "high", "xhigh", "max"}),
            llm_timeout_s=_float("UITEST_LLM_TIMEOUT_S", 180.0 if local else 60.0, 5.0, 600.0),
            llm_max_calls_per_plan=_int("UITEST_LLM_MAX_CALLS_PER_PLAN", 2, 1, 5),
            llm_fallbacks=_bool("UITEST_LLM_FALLBACKS", True),
            planner_concurrency=_int("UITEST_PLANNER_CONCURRENCY", 4, 1, 16),
            agent_max_steps=_int("UITEST_AGENT_MAX_STEPS", 25, 3, 100),
            agent_replay=_bool("UITEST_AGENT_REPLAY", True),
            agent_decision_timeout_s=_float("UITEST_AGENT_DECISION_TIMEOUT_S", 90.0, 5.0, 600.0),
            # agent runs make a model call per step, so they get more time than plan runs
            agent_test_timeout_s=_int("UITEST_AGENT_TEST_TIMEOUT_S", 600, 30, 7200),
            layout_tolerance_px=_int("UITEST_LAYOUT_TOLERANCE_PX", 3, 0, 200),
        )
        return replace(settings, **overrides) if overrides else settings

    def ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.artifacts_dir.mkdir(parents=True, exist_ok=True)
