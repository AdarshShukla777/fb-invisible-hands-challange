"""LLM access for specialist sub-agents.

A sub-agent here is a single bounded model call with an explicit contract (SubAgentSpec):
declared inputs/outputs, no side-effect permissions (it cannot touch the browser or the
database - the coordinator validates and persists its output), a per-call timeout and a
call budget per task. Nothing in v1 lets a model call tools or spawn further agents."""
from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from ..config import Settings


class LLMError(Exception):
    def __init__(self, kind: str, message: str):
        super().__init__(f"{kind}: {message}")
        self.kind = kind


@dataclass(frozen=True)
class SubAgentSpec:
    name: str
    purpose: str
    inputs: str
    outputs: str
    permissions: str
    timeout_s: float
    max_calls: int


class JsonLLM(Protocol):
    model: str

    def complete_json(self, *, system: str, user: str, schema: dict[str, Any], timeout_s: float) -> dict[str, Any]: ...


class SubAgentCall:
    """Enforces one task's call budget and records what was spent."""

    def __init__(self, spec: SubAgentSpec, llm: JsonLLM):
        self.spec, self.llm = spec, llm
        self.calls = 0
        self.elapsed_s = 0.0

    @property
    def remaining(self) -> int:
        return self.spec.max_calls - self.calls

    def __call__(self, *, system: str, user: str, schema: dict[str, Any]) -> dict[str, Any]:
        if self.remaining <= 0:
            raise LLMError("budget", f"{self.spec.name} exhausted its budget of {self.spec.max_calls} call(s)")
        self.calls += 1
        started = time.monotonic()
        try:
            return self.llm.complete_json(system=system, user=user, schema=schema, timeout_s=self.spec.timeout_s)
        finally:
            self.elapsed_s += time.monotonic() - started


_GEMINI_KEY_ENV = ("GEMINI_API_KEY", "GOOGLE_API_KEY", "GOOGLE_GENAI_USE_VERTEXAI")


def llm_configured(settings: "Settings") -> bool:
    """Whether a model is configured for the selected provider (used by planner 'auto' mode)."""
    if settings.llm_provider == "openai_compatible":
        return bool(settings.llm_base_url)
    if settings.llm_provider == "gemini":
        return bool(settings.llm_api_key) or any(os.environ.get(k) for k in _GEMINI_KEY_ENV)
    return any(os.environ.get(k) for k in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_PROFILE"))


def build_llm(settings: "Settings") -> JsonLLM | None:
    """The model client for the configured provider, or None if none is configured. Used by the
    planner (unless UITEST_PLANNER=rules) and by agent-mode tests."""
    if not llm_configured(settings):
        return None
    if settings.llm_provider == "openai_compatible":
        if not settings.llm_model:
            raise ValueError("UITEST_LLM_MODEL must name the served model, e.g. Qwen/Qwen3-14B")
        return OpenAICompatibleJsonLLM(settings.llm_base_url, settings.llm_model, api_key=settings.llm_api_key,
                                       max_tokens=settings.llm_max_tokens,
                                       disable_thinking=settings.llm_disable_thinking)
    if settings.llm_provider == "gemini":
        return GeminiJsonLLM(settings.llm_model, api_key=settings.llm_api_key, max_tokens=settings.llm_max_tokens)
    return AnthropicJsonLLM(settings.llm_model, settings.llm_effort, settings.llm_fallbacks)


_MAX_WHITESPACE_RUN = 200  # compact JSON never needs this much consecutive whitespace
_STALL_S = 30.0
_BOUNDS = frozenset({"maxLength", "minLength", "maxItems", "minItems"})


def schema_for(llm: JsonLLM, schema: Any) -> Any:
    """Keeps only the length limits a backend enforces (its `schema_bounds`): all of them for
    vLLM, item counts for Gemini, none for Anthropic structured outputs (which reject them)."""
    drop = _BOUNDS - getattr(llm, "schema_bounds", frozenset())
    if isinstance(schema, list):
        return [schema_for(llm, s) for s in schema]
    if isinstance(schema, dict):
        return {k: schema_for(llm, v) for k, v in schema.items() if k not in drop}
    return schema


def _parse_object(text: str) -> dict[str, Any]:
    """Parses a JSON object, tolerating a leading <think> block or a ```json fence."""
    text = re.sub(r"^\s*<think>.*?</think>", "", text, flags=re.DOTALL).strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text).strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise LLMError("invalid_output", "response was not valid JSON") from exc
    if not isinstance(data, dict):
        raise LLMError("invalid_output", "response JSON was not an object")
    return data


_GEMINI_BLOCKED = {"SAFETY", "PROHIBITED_CONTENT", "BLOCKLIST", "SPII", "RECITATION"}


class GeminiJsonLLM:
    """Gemini through Google's official google-genai SDK: models.generate_content with
    response_mime_type=application/json and a JSON Schema. The key comes from UITEST_LLM_API_KEY,
    else GEMINI_API_KEY / GOOGLE_API_KEY (or Vertex AI settings) as the SDK resolves them.
    SDK retries are off: the planner and agent retry transient failures once themselves."""

    schema_bounds = frozenset({"minItems", "maxItems"})  # Gemini supports item counts, not string lengths

    def __init__(self, model: str, *, api_key: str = "", max_tokens: int = 16384):
        from google import genai
        from google.genai import errors, types

        self._types, self._errors = types, errors
        self._client = genai.Client(api_key=api_key or None)
        self.model, self.max_tokens = model, max_tokens

    def complete_json(self, *, system: str, user: str, schema: dict[str, Any], timeout_s: float) -> dict[str, Any]:
        import httpx

        t, e = self._types, self._errors
        config = t.GenerateContentConfig(
            system_instruction=system,
            response_mime_type="application/json",
            response_json_schema=schema,
            temperature=0,
            max_output_tokens=self.max_tokens,  # includes thinking tokens on thinking models
            http_options=t.HttpOptions(timeout=int(timeout_s * 1000), retry_options=t.HttpRetryOptions(attempts=1)),
        )
        try:
            response = self._client.models.generate_content(model=self.model, contents=user, config=config)
        except e.ClientError as exc:
            if exc.code == 429:
                raise LLMError("rate_limited", "Gemini rate limit or quota exceeded") from exc
            if exc.code in (401, 403):
                raise LLMError("auth", "Gemini rejected the API key") from exc
            raise LLMError("api", f"Gemini returned HTTP {exc.code}: {str(exc.message)[:200]}") from exc
        except e.ServerError as exc:  # 5xx, e.g. 503 overloaded: transient
            raise LLMError("connection", f"Gemini is unavailable (HTTP {exc.code})") from exc
        except httpx.TimeoutException as exc:
            raise LLMError("timeout", f"no response within {timeout_s:.0f}s") from exc
        except httpx.TransportError as exc:
            raise LLMError("connection", "could not reach the Gemini API") from exc
        feedback = response.prompt_feedback
        if feedback is not None and feedback.block_reason:
            raise LLMError("refusal", f"Gemini blocked the prompt ({feedback.block_reason})")
        candidate = response.candidates[0] if response.candidates else None
        reason = getattr(candidate.finish_reason, "name", str(candidate.finish_reason)) if candidate else None
        if reason == "MAX_TOKENS":
            raise LLMError("truncated", f"output hit max_output_tokens ({self.max_tokens})")
        if reason in _GEMINI_BLOCKED:
            raise LLMError("refusal", f"Gemini stopped the response ({reason})")
        text = response.text if candidate else None
        if not text:
            raise LLMError("invalid_output", "empty response")
        return _parse_object(text)


class OpenAICompatibleJsonLLM:
    """Any OpenAI-compatible chat endpoint, e.g. a local vLLM server. Output is constrained with
    response_format=json_schema (vLLM enforces it with guided decoding). For Qwen3, thinking is
    switched off through the chat template so the reply is only the JSON object."""

    def __init__(self, base_url: str, model: str, *, api_key: str = "", max_tokens: int = 4096,
                 disable_thinking: bool = True, max_retries: int = 0):
        import openai

        self._openai = openai
        self._client = openai.OpenAI(base_url=base_url, api_key=api_key or "EMPTY", max_retries=max_retries)
        self.schema_bounds = _BOUNDS  # vLLM's grammar engine enforces item counts and string lengths
        self.model, self.max_tokens, self.disable_thinking = model, max_tokens, disable_thinking

    def complete_json(self, *, system: str, user: str, schema: dict[str, Any], timeout_s: float) -> dict[str, Any]:
        """At temperature 0 a whitespace runaway repeats for the same prompt, so a runaway is retried
        once with some sampling (within the same time budget)."""
        deadline = time.monotonic() + timeout_s
        try:
            return self._complete(system, user, schema, timeout_s, temperature=0)
        except LLMError as exc:
            left = deadline - time.monotonic()
            if exc.kind != "degenerate" or left < 5:
                raise
            return self._complete(system, user, schema, left, temperature=0.6)

    def _complete(self, system: str, user: str, schema: dict[str, Any], timeout_s: float,
                  temperature: float) -> dict[str, Any]:
        """Streams the reply so a runaway generation is cut off early: constrained JSON decoding
        still allows unlimited whitespace, and a model occasionally gets stuck emitting it."""
        import httpx

        o = self._openai
        extra = {"chat_template_kwargs": {"enable_thinking": False}} if self.disable_thinking else None
        deadline = time.monotonic() + timeout_s
        parts: list[str] = []
        finish, trailing_ws = None, 0
        try:
            # Callers retry once themselves (max_retries=0 here), and a server that stops producing output
            # for _STALL_S seconds counts as stalled: a hung server fails a call in about 30 s, not minutes.
            limits = httpx.Timeout(timeout_s, connect=5.0, read=min(timeout_s, _STALL_S))
            stream = self._client.with_options(timeout=limits).chat.completions.create(
                model=self.model,
                messages=[{"role": "system", "content": system + "\n\nReply with compact JSON on one line."},
                          {"role": "user", "content": user}],
                response_format={"type": "json_schema", "json_schema": {"name": "result", "schema": schema, "strict": True}},
                max_tokens=self.max_tokens,
                temperature=temperature,
                stream=True,
                extra_body=extra,
            )
            with stream:  # leaving the block closes the connection; vLLM then aborts the request
                for chunk in stream:
                    if chunk.choices:
                        delta = chunk.choices[0].delta.content or ""
                        parts.append(delta)
                        finish = chunk.choices[0].finish_reason or finish
                        for ch in delta:
                            trailing_ws = trailing_ws + 1 if ch.isspace() else 0
                        if trailing_ws > _MAX_WHITESPACE_RUN:
                            raise LLMError("degenerate", "the model started emitting endless whitespace")
                    if time.monotonic() > deadline:
                        raise LLMError("timeout", f"no complete response within {timeout_s:.0f}s")
        except o.APITimeoutError as exc:
            raise LLMError("timeout", f"no response within {timeout_s:.0f}s") from exc
        except o.RateLimitError as exc:
            raise LLMError("rate_limited", "rate limited by the model server") from exc
        except o.AuthenticationError as exc:
            raise LLMError("auth", "the model server rejected the API key") from exc
        except o.APIStatusError as exc:
            raise LLMError("api", f"model server returned HTTP {exc.status_code}: {str(exc.message)[:200]}") from exc
        except (o.APIConnectionError, httpx.TransportError) as exc:
            kind = "timeout" if isinstance(exc, httpx.TimeoutException) else "connection"
            raise LLMError(kind, "the model server stopped responding" if kind == "timeout"
                           else "could not reach the model server") from exc
        text = "".join(parts)
        if finish == "length":
            raise LLMError("truncated", f"output hit max_tokens ({self.max_tokens})")
        if not text.strip():
            raise LLMError("invalid_output", "empty response")
        return _parse_object(text)


class AnthropicJsonLLM:
    """Claude via the official SDK, constrained to a JSON schema with structured outputs.
    The SDK retries 408/409/429/5xx and connection errors (max_retries); the timeout is per attempt."""

    def __init__(self, model: str, effort: str, fallbacks: bool, max_retries: int = 2):
        import anthropic

        self._anthropic = anthropic
        self._client = anthropic.Anthropic(max_retries=max_retries)
        self.model, self.effort, self.fallbacks = model, effort, fallbacks

    def complete_json(self, *, system: str, user: str, schema: dict[str, Any], timeout_s: float) -> dict[str, Any]:
        a = self._anthropic
        client = self._client.with_options(timeout=timeout_s)
        kwargs: dict[str, Any] = {
            "model": self.model,
            "max_tokens": 16000,
            "system": system,
            "messages": [{"role": "user", "content": user}],
            "output_config": {"effort": self.effort, "format": {"type": "json_schema", "schema": schema}},
        }
        try:
            if self.fallbacks:
                # Server-side refusal fallback: the API re-runs a declined request on a fallback model.
                response = client.beta.messages.create(
                    betas=["server-side-fallback-2026-07-01"], fallbacks="default", **kwargs
                )
            else:
                response = client.messages.create(**kwargs)
        except a.APITimeoutError as exc:
            raise LLMError("timeout", f"no response within {timeout_s:.0f}s") from exc
        except a.RateLimitError as exc:
            raise LLMError("rate_limited", "rate limited by the model API") from exc
        except a.AuthenticationError as exc:
            raise LLMError("auth", "model API credentials were rejected") from exc
        except a.APIStatusError as exc:
            raise LLMError("api", f"model API returned HTTP {exc.status_code}") from exc
        except a.APIConnectionError as exc:
            raise LLMError("connection", "could not reach the model API") from exc
        if response.stop_reason == "refusal":
            raise LLMError("refusal", "the model declined this request")
        if response.stop_reason == "max_tokens":
            raise LLMError("truncated", "the model output was cut off")
        text = next((b.text for b in response.content if b.type == "text"), None)
        if text is None:
            raise LLMError("invalid_output", "no text block in the response")
        return _parse_object(text)
