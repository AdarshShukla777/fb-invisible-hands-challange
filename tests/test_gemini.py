"""Gemini provider (google-genai SDK), with the SDK call stubbed: real SDK request/response types,
no network and no API key needed."""
from __future__ import annotations

import json
from dataclasses import replace
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from google.genai import errors, types

from app.agent.prompt import DECISION_SCHEMA
from app.planning.llm import GeminiJsonLLM, LLMError, build_llm, schema_for
from app.planning.rules import PlanContext
from app.planning.service import PlanningService


def response(text: str | None = None, finish: str = "STOP", blocked: str | None = None) -> types.GenerateContentResponse:
    if blocked:
        return types.GenerateContentResponse(prompt_feedback=types.GenerateContentResponsePromptFeedback(
            block_reason=types.BlockedReason(blocked)))
    parts = [types.Part(text=text)] if text is not None else []
    return types.GenerateContentResponse(candidates=[types.Candidate(
        content=types.Content(role="model", parts=parts), finish_reason=types.FinishReason(finish))])


class Stub:
    """Stands in for client.models: records requests and returns (or raises) the scripted results."""

    def __init__(self, *results: Any):
        self.results, self.requests = list(results), []

    def generate_content(self, *, model: str, contents: Any, config: types.GenerateContentConfig):
        self.requests.append({"model": model, "contents": contents, "config": config})
        r = self.results.pop(0)
        if isinstance(r, BaseException):
            raise r
        return r


def gemini(*results: Any) -> tuple[GeminiJsonLLM, Stub]:
    llm = GeminiJsonLLM("gemini-2.5-flash", api_key="test-key")
    stub = Stub(*results)
    llm._client = SimpleNamespace(models=stub)
    return llm, stub


SCHEMA = {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"], "additionalProperties": False}


def test_request_uses_json_schema_output_and_parses_the_reply():
    llm, stub = gemini(response('{"ok": true}'))
    assert llm.complete_json(system="be brief", user="hello", schema=SCHEMA, timeout_s=12) == {"ok": True}
    req = stub.requests[0]
    cfg = req["config"]
    assert req["model"] == "gemini-2.5-flash" and req["contents"] == "hello"
    assert cfg.system_instruction == "be brief" and cfg.response_mime_type == "application/json"
    assert cfg.response_json_schema == SCHEMA and cfg.temperature == 0
    assert cfg.http_options.timeout == 12000 and cfg.http_options.retry_options.attempts == 1


def test_gemini_keeps_item_limits_but_not_string_limits():
    llm, _ = gemini()
    decision = schema_for(llm, DECISION_SCHEMA)
    assert decision["properties"]["missing"]["maxItems"] == 5
    assert "maxLength" not in json.dumps(decision)


@pytest.mark.parametrize("result,kind", [
    (response('{"ok":', finish="MAX_TOKENS"), "truncated"),
    (response("", finish="SAFETY"), "refusal"),
    (response(blocked="PROHIBITED_CONTENT"), "refusal"),
    (response("not json"), "invalid_output"),
    (response(None), "invalid_output"),
    (errors.ClientError(429, {"error": {"code": 429, "message": "quota", "status": "RESOURCE_EXHAUSTED"}}), "rate_limited"),
    (errors.ClientError(403, {"error": {"code": 403, "message": "bad key", "status": "PERMISSION_DENIED"}}), "auth"),
    (errors.ClientError(400, {"error": {"code": 400, "message": "bad schema", "status": "INVALID_ARGUMENT"}}), "api"),
    (errors.ServerError(503, {"error": {"code": 503, "message": "overloaded", "status": "UNAVAILABLE"}}), "connection"),
    (httpx.ReadTimeout("slow"), "timeout"),
])
def test_failures_map_to_llm_error_kinds(result, kind):
    llm, _ = gemini(result)
    with pytest.raises(LLMError) as exc:
        llm.complete_json(system="s", user="u", schema=SCHEMA, timeout_s=5)
    assert exc.value.kind == kind


def test_build_llm_selects_gemini(settings, monkeypatch):
    for key in ("GEMINI_API_KEY", "GOOGLE_API_KEY", "GOOGLE_GENAI_USE_VERTEXAI"):
        monkeypatch.delenv(key, raising=False)
    g = replace(settings, llm_provider="gemini", llm_model="gemini-2.5-flash", llm_api_key="")
    assert build_llm(g) is None  # no key anywhere
    monkeypatch.setenv("GEMINI_API_KEY", "from-env")
    llm = build_llm(g)
    assert isinstance(llm, GeminiJsonLLM) and llm.model == "gemini-2.5-flash"


def test_settings_default_model_for_gemini(monkeypatch, tmp_path):
    from app.config import Settings

    monkeypatch.setenv("UITEST_LLM_PROVIDER", "gemini")
    monkeypatch.delenv("UITEST_LLM_MODEL", raising=False)
    monkeypatch.delenv("UITEST_LLM_MAX_TOKENS", raising=False)
    s = Settings.from_env(data_dir=tmp_path)
    assert s.llm_model == "gemini-2.5-flash" and s.llm_max_tokens == 16384 and s.llm_timeout_s == 60


def test_planner_retries_once_on_gemini_overload(settings):
    plan = {"clarifications": [], "steps": [
        {"action": "navigate", "target": None, "value": None, "url": "/login", "key": None, "assertion": None},
        {"action": "assert", "target": None, "value": None, "url": None, "key": None,
         "assertion": {"type": "text_visible", "expected": "Sign in", "target": None, "source": "shows"}},
    ]}
    llm, stub = gemini(errors.ServerError(503, {"error": {"code": 503, "message": "overloaded"}}),
                       response(json.dumps(plan)))
    svc = PlanningService(replace(settings, planner_mode="llm", llm_provider="gemini"), llm)
    outcome = svc.plan('Open the login page and check it shows "Sign in"', PlanContext(base_url="http://a.test/"))
    assert outcome.status == "ready" and outcome.planner == "llm:gemini-2.5-flash" and len(stub.requests) == 2
    sent = stub.requests[1]["config"].response_json_schema
    assert sent["properties"]["clarifications"]["maxItems"] == 5 and "maxLength" not in json.dumps(sent)
