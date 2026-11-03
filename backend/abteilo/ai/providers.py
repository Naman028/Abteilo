"""Model providers behind one protocol.

A provider takes a system prompt, chat messages and a JSON schema and returns
the model's JSON text plus token counts. Providers never see URLs or keys from
the database; those come from server configuration only.

Adding a provider type = implement `ModelProvider` and add it to `PROVIDERS`.
"""

import json
import time
from collections import deque
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Protocol

import anthropic
import httpx

from abteilo.config import get_settings

EXTERNAL_PROVIDERS = frozenset({"openai_compatible", "anthropic"})


class ProviderError(Exception):
    """Carries a stable, safe code only; never provider response bodies."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class ChatMessage:
    role: str  # "user" | "assistant"
    content: str


@dataclass(frozen=True)
class Completion:
    text: str
    input_tokens: int
    output_tokens: int
    latency_ms: int


class ModelProvider(Protocol):
    def complete_json(
        self, *, model: str, system: str, messages: list[ChatMessage], schema: dict, max_tokens: int, temperature: float
    ) -> Completion: ...


class StreamingProvider(Protocol):
    """Optional: yields text chunks as they are generated, then exactly one Completion (full text and usage).
    Closing the iterator early must stop generation (the HTTP stream is closed)."""

    def stream_json(
        self, *, model: str, system: str, messages: list[ChatMessage], schema: dict, max_tokens: int, temperature: float
    ) -> Iterator[str | Completion]: ...


def _elapsed_ms(start: float) -> int:
    return int((time.monotonic() - start) * 1000)


class OllamaProvider:
    """Local open-source models via Ollama's native chat API with schema-constrained output."""

    def complete_json(self, *, model, system, messages, schema, max_tokens, temperature):
        settings = get_settings()
        start = time.monotonic()
        try:
            response = httpx.post(
                f"{settings.ollama_url.rstrip('/')}/api/chat",
                json={
                    "model": model,
                    "stream": False,
                    "format": schema,
                    "options": {"temperature": temperature, "num_predict": max_tokens},
                    "messages": [{"role": "system", "content": system}]
                    + [{"role": m.role, "content": m.content} for m in messages],
                },
                timeout=settings.model_timeout_seconds,
            )
        except httpx.TimeoutException as exc:
            raise ProviderError("MODEL_TIMEOUT") from exc
        except httpx.HTTPError as exc:
            raise ProviderError("MODEL_UNAVAILABLE") from exc
        if response.status_code == 404:
            raise ProviderError("MODEL_NOT_INSTALLED")
        if response.status_code != 200:
            raise ProviderError("MODEL_UNAVAILABLE")
        body = response.json()
        return Completion(
            text=body.get("message", {}).get("content", ""),
            input_tokens=int(body.get("prompt_eval_count") or 0),
            output_tokens=int(body.get("eval_count") or 0),
            latency_ms=_elapsed_ms(start),
        )


    def stream_json(self, *, model, system, messages, schema, max_tokens, temperature):
        settings = get_settings()
        start = time.monotonic()
        parts: list[str] = []
        try:
            with httpx.stream(
                "POST", f"{settings.ollama_url.rstrip('/')}/api/chat",
                json={
                    "model": model,
                    "stream": True,
                    "format": schema,
                    "options": {"temperature": temperature, "num_predict": max_tokens},
                    "messages": [{"role": "system", "content": system}]
                    + [{"role": m.role, "content": m.content} for m in messages],
                },
                timeout=settings.model_timeout_seconds,
            ) as response:
                if response.status_code == 404:
                    raise ProviderError("MODEL_NOT_INSTALLED")
                if response.status_code != 200:
                    raise ProviderError("MODEL_UNAVAILABLE")
                for line in response.iter_lines():
                    if not line:
                        continue
                    chunk = json.loads(line)
                    if chunk.get("error"):
                        raise ProviderError("MODEL_UNAVAILABLE")
                    piece = (chunk.get("message") or {}).get("content") or ""
                    if piece:
                        parts.append(piece)
                        yield piece
                    if chunk.get("done"):
                        yield Completion("".join(parts), int(chunk.get("prompt_eval_count") or 0),
                                         int(chunk.get("eval_count") or 0), _elapsed_ms(start))
                        return
        except httpx.TimeoutException as exc:
            raise ProviderError("MODEL_TIMEOUT") from exc
        except (httpx.HTTPError, ValueError) as exc:
            raise ProviderError("MODEL_STREAM_INTERRUPTED") from exc
        raise ProviderError("MODEL_STREAM_INTERRUPTED")  # the stream ended without its final message


class OpenAICompatibleProvider:
    """Any OpenAI-compatible chat endpoint (e.g. a free tier of a hosted open-model API)."""

    def complete_json(self, *, model, system, messages, schema, max_tokens, temperature):
        settings = get_settings()
        if not settings.openai_compatible_url or not settings.openai_compatible_api_key:
            raise ProviderError("PROVIDER_NOT_CONFIGURED")
        start = time.monotonic()
        instruction = system + "\n\nRespond with a single JSON object matching this JSON schema:\n" + json.dumps(schema)
        try:
            response = httpx.post(
                f"{settings.openai_compatible_url.rstrip('/')}/chat/completions",
                headers={"Authorization": f"Bearer {settings.openai_compatible_api_key.get_secret_value()}"},
                json={
                    "model": model,
                    "max_tokens": max_tokens,
                    "temperature": temperature,
                    "response_format": {"type": "json_object"},
                    "messages": [{"role": "system", "content": instruction}]
                    + [{"role": m.role, "content": m.content} for m in messages],
                },
                timeout=settings.model_timeout_seconds,
            )
        except httpx.TimeoutException as exc:
            raise ProviderError("MODEL_TIMEOUT") from exc
        except httpx.HTTPError as exc:
            raise ProviderError("MODEL_UNAVAILABLE") from exc
        if response.status_code == 429:
            raise ProviderError("MODEL_RATE_LIMITED")
        if response.status_code != 200:
            raise ProviderError("MODEL_UNAVAILABLE")
        body = response.json()
        usage = body.get("usage") or {}
        choices = body.get("choices") or [{}]
        return Completion(
            text=(choices[0].get("message") or {}).get("content") or "",
            input_tokens=int(usage.get("prompt_tokens") or 0),
            output_tokens=int(usage.get("completion_tokens") or 0),
            latency_ms=_elapsed_ms(start),
        )


# Models that accept the server-side refusal fallback.
_FALLBACK_MODELS = frozenset({"claude-opus-5-5", "claude-opus-5", "claude-fable-5-1"})


class AnthropicProvider:
    """Claude for heavy work, only when the owner has enabled external AI."""

    def complete_json(self, *, model, system, messages, schema, max_tokens, temperature):
        settings = get_settings()
        if not settings.anthropic_api_key:
            raise ProviderError("PROVIDER_NOT_CONFIGURED")
        client = anthropic.Anthropic(
            api_key=settings.anthropic_api_key.get_secret_value(), timeout=settings.model_timeout_seconds, max_retries=1
        )
        params = dict(
            model=model,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": m.role, "content": m.content} for m in messages],
            output_config={"format": {"type": "json_schema", "schema": schema}},
        )
        start = time.monotonic()
        try:
            if model in _FALLBACK_MODELS:
                response = client.beta.messages.create(
                    **params, betas=["server-side-fallback-2026-07-01"], fallbacks="default"
                )
            else:
                response = client.messages.create(**params)
        except anthropic.RateLimitError as exc:
            raise ProviderError("MODEL_RATE_LIMITED") from exc
        except anthropic.APIStatusError as exc:
            raise ProviderError("MODEL_UNAVAILABLE") from exc
        except anthropic.APIConnectionError as exc:
            raise ProviderError("MODEL_UNAVAILABLE") from exc
        if response.stop_reason == "refusal":
            raise ProviderError("MODEL_REFUSED")
        text = "".join(block.text for block in response.content if block.type == "text")
        return Completion(text, response.usage.input_tokens, response.usage.output_tokens, _elapsed_ms(start))


class ScriptedProvider:
    """Deterministic provider for tests and offline demos: returns queued JSON replies."""

    def __init__(self) -> None:
        self.replies: deque[str] = deque()
        self.calls: list[dict] = []

    def complete_json(self, *, model, system, messages, schema, max_tokens, temperature):
        self.calls.append({"model": model, "system": system, "messages": messages, "schema": schema})
        if not self.replies:
            raise ProviderError("MODEL_UNAVAILABLE")
        return Completion(self.replies.popleft(), 10, 10, 1)

    def stream_json(self, *, model, system, messages, schema, max_tokens, temperature):
        """Streams the queued reply in small chunks, so incremental parsing is exercised in tests."""
        text = self.complete_json(model=model, system=system, messages=messages, schema=schema, max_tokens=max_tokens,
                                  temperature=temperature).text
        for i in range(0, len(text), 17):
            yield text[i:i + 17]
        yield Completion(text, 10, 10, 1)


PROVIDERS: dict[str, ModelProvider] = {
    "ollama": OllamaProvider(),
    "openai_compatible": OpenAICompatibleProvider(),
    "anthropic": AnthropicProvider(),
    "scripted": ScriptedProvider(),
}
