"""Start the API server with one model provider.

    uv run python main.py               # local vLLM (default): http://localhost:8001/v1
    uv run python main.py --gemini      # Gemini API only   (GEMINI_API_KEY or GOOGLE_API_KEY)
    uv run python main.py --anthropic   # Anthropic API only (ANTHROPIC_API_KEY)

Options: --model NAME, --vllm-url URL (default mode only), --host, --port,
         --headed (show the browser window), --slow-mo MS (slow it down to follow along),
         --video (record every test run; shown in the Execution tab).
Keys are read from the environment and backend/.env. The chosen provider is the only one the
server can use: the other providers' credentials are removed from this process at startup."""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request
from collections.abc import MutableMapping
from pathlib import Path

DOTENV = Path(__file__).with_name(".env")
DEFAULT_VLLM_URL = "http://localhost:8001/v1"
DEFAULT_LOCAL_MODEL = "Qwen/Qwen3-14B"
DEFAULT_MODELS = {"gemini": "gemini-2.5-flash", "anthropic": "claude-opus-5"}
_ANTHROPIC_VARS = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_PROFILE")
_GEMINI_VARS = ("GEMINI_API_KEY", "GOOGLE_API_KEY", "GOOGLE_GENAI_USE_VERTEXAI")
_UITEST_LLM_VARS = ("UITEST_LLM_PROVIDER", "UITEST_LLM_BASE_URL", "UITEST_LLM_API_KEY", "UITEST_LLM_MODEL")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="UI Test Agent API server")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--gemini", dest="provider", action="store_const", const="gemini",
                       help="use only the Gemini API (GEMINI_API_KEY or GOOGLE_API_KEY)")
    group.add_argument("--anthropic", dest="provider", action="store_const", const="anthropic",
                       help="use only the Anthropic API (ANTHROPIC_API_KEY)")
    parser.set_defaults(provider="local")
    parser.add_argument("--model", help="model name (defaults: the model served by vLLM, gemini-2.5-flash, claude-opus-5)")
    parser.add_argument("--vllm-url", help=f"OpenAI-compatible base URL for the default mode (default {DEFAULT_VLLM_URL})")
    parser.add_argument("--headed", action="store_true", help="show the browser window while tests run")
    parser.add_argument("--slow-mo", type=int, metavar="MS", help="pause MS milliseconds between browser operations")
    parser.add_argument("--video", action="store_true", help="record a video of every test run")
    parser.add_argument("--host", default=os.environ.get("UITEST_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("UITEST_PORT", "8000")))
    return parser.parse_args(argv)


def served_models(base_url: str, timeout: float = 3.0) -> list[str] | None:
    """Model ids from an OpenAI-compatible /models endpoint, or None if the server does not answer."""
    try:
        with urllib.request.urlopen(base_url.rstrip("/") + "/models", timeout=timeout) as r:
            return [m["id"] for m in json.load(r).get("data", [])]
    except Exception:
        return None


def apply_provider(args: argparse.Namespace, env: MutableMapping[str, str] = os.environ,
                   probe=served_models) -> list[str]:
    """Sets UITEST_LLM_* for the chosen provider and removes every other provider's credentials.
    Returns startup messages; raises SystemExit if the chosen provider has no credentials."""
    for var in _UITEST_LLM_VARS:  # the flag decides, not leftovers from .env or the shell
        env.pop(var, None)
    notes: list[str] = []
    if args.provider == "gemini":
        key = env.get("GEMINI_API_KEY") or env.get("GOOGLE_API_KEY")
        if not key:
            raise SystemExit("--gemini needs GEMINI_API_KEY or GOOGLE_API_KEY (environment or backend/.env).")
        for var in _ANTHROPIC_VARS + _GEMINI_VARS:
            env.pop(var, None)
        env.update(UITEST_LLM_PROVIDER="gemini", UITEST_LLM_API_KEY=key,
                   UITEST_LLM_MODEL=args.model or DEFAULT_MODELS["gemini"])
    elif args.provider == "anthropic":
        if not any(env.get(v) for v in _ANTHROPIC_VARS):
            raise SystemExit("--anthropic needs ANTHROPIC_API_KEY (environment or backend/.env).")
        for var in _GEMINI_VARS:
            env.pop(var, None)
        env.update(UITEST_LLM_PROVIDER="anthropic", UITEST_LLM_MODEL=args.model or DEFAULT_MODELS["anthropic"])
    else:
        for var in _ANTHROPIC_VARS + _GEMINI_VARS:
            env.pop(var, None)
        url = args.vllm_url or DEFAULT_VLLM_URL
        models = probe(url)
        if models is None:
            notes.append(f"WARNING: the local model server at {url} is not responding; LLM planning and "
                         "agent-mode tests will fail until it is back.")
        model = args.model or (models[0] if models and len(models) == 1 else DEFAULT_LOCAL_MODEL)
        if models and model not in models:
            notes.append(f"WARNING: {url} serves {', '.join(models)}, not {model}.")
        env.update(UITEST_LLM_PROVIDER="openai_compatible", UITEST_LLM_BASE_URL=url, UITEST_LLM_MODEL=model)
    where = env.get("UITEST_LLM_BASE_URL", "")
    notes.insert(0, f"LLM provider: {env['UITEST_LLM_PROVIDER']} | model: {env['UITEST_LLM_MODEL']}"
                    + (f" | {where}" if where else "") + " | other providers' credentials are not loaded")
    return notes


def apply_browser_options(args: argparse.Namespace, env: MutableMapping[str, str] = os.environ) -> list[str]:
    if args.headed:
        env["UITEST_HEADLESS"] = "false"
        # one visible window at a time: easier to follow, and heavy pages (canvas, WebGL) don't compete
        env.setdefault("UITEST_RUNNER_CONCURRENCY", "1")
    if args.slow_mo is not None:
        env["UITEST_SLOW_MO_MS"] = str(args.slow_mo)
    if args.video:
        env["UITEST_VIDEO"] = "true"
    from app.config import Settings  # after .env and flags are applied

    s = Settings.from_env()
    visible = not s.headless
    overlay = s.highlight == "on" or (s.highlight == "auto" and (visible or s.video))
    browser = f"visible window ({s.runner_concurrency} at a time)" if visible else "headless (use --headed to watch)"
    return [f"Browser: {browser}{f', +{s.slow_mo_ms} ms before each action' if s.slow_mo_ms else ''}"
            f"{', recording video' if s.video else ''}"
            f"{', pointer and element boxes shown' if overlay else ''}",
            f"Data: {s.db_path} | screenshots, traces, videos: {s.artifacts_dir}"]


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    try:
        from dotenv import load_dotenv

        load_dotenv(DOTENV, override=False)  # real environment variables win over .env
    except ImportError:
        pass
    for note in apply_provider(args):
        print(note, flush=True)
    for note in apply_browser_options(args):
        print(note, flush=True)
    import uvicorn

    uvicorn.run("app.server:app", host=args.host, port=args.port)


if __name__ == "__main__":
    main(sys.argv[1:])
