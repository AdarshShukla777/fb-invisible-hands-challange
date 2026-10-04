"""OpenAI-compatible (e.g. vLLM) planner backend, tested against a fake streaming server."""
from __future__ import annotations

import json
import threading
import time
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from app.planning.llm import LLMError, OpenAICompatibleJsonLLM, build_llm
from app.planning.llm_planner import _prune
from app.planning.rules import PlanContext
from app.planning.service import PlanningService

SCHEMA = {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"], "additionalProperties": False}


class FakeServer:
    """Streams the configured chunks as server-sent events and records each request body."""

    def __init__(self):
        self.requests: list[dict] = []
        self.chunks: list[str] = []
        self.finish = "stop"
        self.status = 200
        self.delay = 0.0
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                fake.requests.append(body)
                if fake.status != 200:
                    data = json.dumps({"error": {"message": "boom"}}).encode()
                    self.send_response(fake.status)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                    return
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                self.wfile.flush()
                time.sleep(fake.delay)  # a server that accepted the request but produces nothing
                try:
                    for i, text in enumerate(fake.chunks + [""]):
                        last = i == len(fake.chunks)
                        chunk = {"id": "c", "object": "chat.completion.chunk", "created": 0, "model": body["model"],
                                 "choices": [{"index": 0, "delta": {"content": text} if text else {},
                                              "finish_reason": fake.finish if last else None}]}
                        self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
                        self.wfile.flush()
                    self.wfile.write(b"data: [DONE]\n\n")
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                    pass  # the client hung up (runaway guard)

            def log_message(self, *args) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/v1"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()


@pytest.fixture
def fake():
    f = FakeServer()
    yield f
    f.server.shutdown()


def client(fake: FakeServer, **kw) -> OpenAICompatibleJsonLLM:
    return OpenAICompatibleJsonLLM(fake.url, "Qwen/Qwen3-14B", max_retries=0, **kw)


def test_request_shape_and_streamed_json(fake):
    fake.chunks = ["<think>\n\n</think>", '{"ok"', ":true}"]
    assert client(fake).complete_json(system="sys", user="u", schema=SCHEMA, timeout_s=10) == {"ok": True}
    body = fake.requests[0]
    assert body["model"] == "Qwen/Qwen3-14B" and body["stream"] is True and body["temperature"] == 0
    assert body["response_format"]["type"] == "json_schema" and body["response_format"]["json_schema"]["schema"] == SCHEMA
    assert body["chat_template_kwargs"] == {"enable_thinking": False}
    assert "compact JSON" in body["messages"][0]["content"]


def test_thinking_flag_can_be_left_to_the_server(fake):
    fake.chunks = ['{"ok":true}']
    client(fake, disable_thinking=False).complete_json(system="s", user="u", schema=SCHEMA, timeout_s=10)
    assert "chat_template_kwargs" not in fake.requests[0]


@pytest.mark.parametrize("setup,kind", [
    (lambda f: setattr(f, "chunks", ['{"ok":'] + [" " * 50] * 40), "degenerate"),
    (lambda f: (setattr(f, "chunks", ['{"ok":']), setattr(f, "finish", "length")), "truncated"),
    (lambda f: setattr(f, "chunks", ["not json"]), "invalid_output"),
    (lambda f: setattr(f, "status", 500), "api"),
])
def test_failures_become_typed_llm_errors(fake, setup, kind):
    setup(fake)
    with pytest.raises(LLMError) as exc:
        client(fake).complete_json(system="s", user="u", schema=SCHEMA, timeout_s=10)
    assert exc.value.kind == kind


def test_unreachable_server_is_a_connection_error():
    llm = OpenAICompatibleJsonLLM("http://127.0.0.1:9/v1", "m", max_retries=0)
    with pytest.raises(LLMError) as exc:
        llm.complete_json(system="s", user="u", schema=SCHEMA, timeout_s=5)
    assert exc.value.kind == "connection"


def test_build_llm_from_settings(settings, fake):
    local = replace(settings, planner_mode="auto", llm_provider="openai_compatible", llm_base_url=fake.url,
                    llm_model="Qwen/Qwen3-14B")
    assert isinstance(build_llm(local), OpenAICompatibleJsonLLM)
    # rules mode still builds the client (agent-mode tests need it) but the planner never calls it
    rules = replace(local, planner_mode="rules")
    PlanningService(rules, build_llm(rules)).plan("vague", PlanContext(base_url="http://a.test/"))
    assert fake.requests == []
    assert build_llm(replace(local, llm_base_url="")) is None
    with pytest.raises(ValueError):
        build_llm(replace(local, llm_model=""))


def test_local_model_plans_through_the_same_validator(settings, fake):
    plan = {"clarifications": [], "steps": [
        {"action": "navigate", "target": None, "value": None, "url": "/login", "key": None, "assertion": None},
        {"action": "click", "target": {"description": "Log in", "role": "button", "name": "Log in", "label": None,
                                       "placeholder": None, "testid": None, "text": None, "css": None, "exact": False},
         "value": None, "url": None, "key": None, "assertion": None},
        {"action": "assert", "target": None, "value": None, "url": None, "key": None,
         "assertion": {"type": "text_visible", "expected": "Welcome back", "target": None, "source": "shows"}},
    ]}
    fake.chunks = [json.dumps(plan)]
    local = replace(settings, planner_mode="llm", llm_provider="openai_compatible", llm_base_url=fake.url,
                    llm_model="Qwen/Qwen3-14B")
    svc = PlanningService(local, build_llm(local))
    ctx = PlanContext(base_url="http://app.test/")
    ok = svc.plan('Go to /login, click Log in and check the page shows "Welcome back"', ctx)
    assert ok.status == "ready" and ok.planner == "llm:Qwen/Qwen3-14B"
    unquoted = svc.plan("Go to /login, click Log in and check the page shows Welcome back", ctx)
    assert unquoted.status == "needs_clarification"
    assert {i["code"] for i in unquoted.issues} == {"ungrounded_assertion"}


def test_prune_keeps_only_fields_that_apply():
    step = {"action": "fill", "url": "/login", "key": "email", "value": '"ada@example.com"',
            "target": {"description": "email", "role": "page", "name": '"Email"', "label": None, "placeholder": None,
                       "testid": None, "text": None, "css": None, "exact": False},
            "assertion": {"type": "text_visible", "expected": "ada@example.com", "target": None, "source": "x"}}
    assert _prune(step) == {"action": "fill", "value": "ada@example.com",
                            "target": {"description": "email", "role": None, "name": "Email", "label": None,
                                       "placeholder": None, "testid": None, "text": None, "css": None, "exact": False}}
    empty_target = {"description": "greeting", "role": None, "name": None, "label": None, "placeholder": None,
                    "testid": None, "text": None, "css": None, "exact": False}
    assertion = _prune({"action": "assert", "assertion": {"type": "text_visible", "expected": "Hi",
                                                          "target": empty_target, "source": "Hi"}})
    assert assertion["assertion"]["target"] is None


def test_retry_once_on_transient_failure(settings):
    class Flaky:
        model = "flaky"
        calls = 0

        def complete_json(self, **_):
            self.calls += 1
            if self.calls == 1:
                raise LLMError("degenerate", "whitespace")
            return {"clarifications": ["Which page?"], "steps": []}

    llm = Flaky()
    o = PlanningService(replace(settings, planner_mode="llm"), llm).plan("do the thing", PlanContext(base_url="http://a.test/"))
    assert llm.calls == 2 and o.issues[0]["message"] == "Which page?"


def test_invented_secret_placeholders_are_rejected(settings):
    from app.planning.plan import validate_plan

    raw = {"steps": [{"action": "navigate", "url": "/login"},
                     {"action": "fill", "value": "{{secret:PASSWORD}}", "target": {"label": "Password"}},
                     {"action": "assert", "assertion": {"type": "url_contains", "expected": "/home"}}]}
    result = validate_plan(raw, base_url="http://a.test/", instruction="Log in and check the URL contains /home",
                           max_steps=50)
    assert [i.code for i in result.issues] == ["ungrounded_value"]


def test_local_backend_gets_a_bounded_schema(settings, fake):
    """Without limits a small model can loop on clarification questions until the timeout."""
    from app.planning.llm_planner import MAX_CLARIFICATIONS, PLAN_SCHEMA

    fake.chunks = [json.dumps({"clarifications": ["Which page?"], "steps": []})]
    local = replace(settings, planner_mode="llm", llm_provider="openai_compatible", llm_base_url=fake.url,
                    llm_model="Qwen/Qwen3-14B")
    PlanningService(local, build_llm(local)).plan("do something", PlanContext(base_url="http://a.test/"))
    sent = fake.requests[0]["response_format"]["json_schema"]["schema"]["properties"]
    assert sent["clarifications"]["maxItems"] == MAX_CLARIFICATIONS
    assert sent["clarifications"]["items"]["maxLength"] == 300
    assert "maxItems" not in sent["steps"]  # unbounded: Gemini rejects large array limits
    assert "maxItems" not in PLAN_SCHEMA["properties"]["clarifications"]  # Anthropic schema unchanged


def test_stalled_server_fails_fast(fake, monkeypatch):
    import app.planning.llm as llm_module

    monkeypatch.setattr(llm_module, "_STALL_S", 1.0)
    fake.delay, fake.chunks = 4.0, ['{"ok":true}']
    started = time.monotonic()
    with pytest.raises(LLMError) as exc:
        client(fake).complete_json(system="s", user="u", schema=SCHEMA, timeout_s=60)
    assert exc.value.kind == "timeout" and time.monotonic() - started < 3.5
