"""main.py provider flags: exactly one provider, and no other provider's credentials in the process."""
from __future__ import annotations

import pytest

import main

KEYS = {"GOOGLE_API_KEY": "g-key", "ANTHROPIC_API_KEY": "a-key", "UITEST_LLM_PROVIDER": "anthropic",
        "UITEST_LLM_MODEL": "leftover-model", "UITEST_LLM_BASE_URL": "http://leftover/v1"}


def configure(argv: list[str], env: dict[str, str], served: list[str] | None = None) -> tuple[dict, list[str]]:
    env = dict(env)
    notes = main.apply_provider(main.parse_args(argv), env, probe=lambda url: served)
    return env, notes


def test_default_is_local_vllm_with_served_model_and_no_cloud_keys():
    env, notes = configure([], KEYS, served=["Qwen/Qwen3-14B"])
    assert env["UITEST_LLM_PROVIDER"] == "openai_compatible"
    assert env["UITEST_LLM_BASE_URL"] == "http://localhost:8001/v1" and env["UITEST_LLM_MODEL"] == "Qwen/Qwen3-14B"
    assert "GOOGLE_API_KEY" not in env and "ANTHROPIC_API_KEY" not in env
    assert notes == ["LLM provider: openai_compatible | model: Qwen/Qwen3-14B | http://localhost:8001/v1 "
                     "| other providers' credentials are not loaded"]


def test_default_warns_when_vllm_is_down():
    env, notes = configure(["--vllm-url", "http://gpu-box:9000/v1"], {}, served=None)
    assert env["UITEST_LLM_BASE_URL"] == "http://gpu-box:9000/v1" and env["UITEST_LLM_MODEL"] == "Qwen/Qwen3-14B"
    assert "not responding" in notes[1]


def test_gemini_uses_only_the_gemini_key():
    env, _ = configure(["--gemini"], KEYS)
    assert env["UITEST_LLM_PROVIDER"] == "gemini" and env["UITEST_LLM_API_KEY"] == "g-key"
    assert env["UITEST_LLM_MODEL"] == "gemini-2.5-flash"
    assert "ANTHROPIC_API_KEY" not in env and "GOOGLE_API_KEY" not in env and "UITEST_LLM_BASE_URL" not in env


def test_anthropic_uses_only_the_anthropic_key():
    env, _ = configure(["--anthropic", "--model", "claude-sonnet-5"], KEYS)
    assert env["UITEST_LLM_PROVIDER"] == "anthropic" and env["UITEST_LLM_MODEL"] == "claude-sonnet-5"
    assert env["ANTHROPIC_API_KEY"] == "a-key" and "GOOGLE_API_KEY" not in env and "UITEST_LLM_API_KEY" not in env


@pytest.mark.parametrize("flag", ["--gemini", "--anthropic"])
def test_missing_key_stops_startup(flag):
    with pytest.raises(SystemExit):
        configure([flag], {})


def test_flags_are_mutually_exclusive():
    with pytest.raises(SystemExit):
        main.parse_args(["--gemini", "--anthropic"])


def test_browser_flags(monkeypatch):
    for var in ("UITEST_HEADLESS", "UITEST_SLOW_MO_MS", "UITEST_VIDEO", "UITEST_DATA_DIR", "UITEST_RUNNER_CONCURRENCY", "UITEST_HIGHLIGHT"):
        monkeypatch.delenv(var, raising=False)
    env: dict[str, str] = {}
    monkeypatch.setattr(main.os, "environ", env)
    notes = main.apply_browser_options(main.parse_args(["--headed", "--slow-mo", "250", "--video"]), env)
    assert env == {"UITEST_HEADLESS": "false", "UITEST_RUNNER_CONCURRENCY": "1", "UITEST_SLOW_MO_MS": "250", "UITEST_VIDEO": "true"}
    assert notes[0] == ("Browser: visible window (1 at a time), +250 ms before each action, recording video, "
                        "pointer and element boxes shown")
    assert notes[1].startswith("Data: ") and "backend" in notes[1]
