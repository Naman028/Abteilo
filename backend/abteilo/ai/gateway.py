"""Model gateway: route -> profile -> policy check -> provider -> validated JSON.

Every call is recorded in core.ai_runs with metadata only (no prompt or answer
content). External providers are refused unless the owner has enabled external
AI for the company.
"""

import json
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from functools import lru_cache
from typing import TypeVar
from uuid import UUID, uuid4

from pydantic import BaseModel, ValidationError
from sqlalchemy import Connection, text

from abteilo.ai.providers import EXTERNAL_PROVIDERS, PROVIDERS, ChatMessage, Completion, ProviderError
from abteilo.authz import Principal
from abteilo.config import get_settings
from abteilo.db import transaction
from abteilo.errors import AppError

T = TypeVar("T", bound=BaseModel)


class ModelUnavailable(AppError):
    status_code, code, message = 503, "MODEL_UNAVAILABLE", "The AI model is not available right now."


class ModelBusy(AppError):
    status_code, code, message = 503, "MODEL_BUSY", "The AI model is busy with other requests. Try again in a moment."


@lru_cache(maxsize=1)
def _slots() -> threading.BoundedSemaphore:
    return threading.BoundedSemaphore(max(1, get_settings().max_concurrent_generations))


@contextmanager
def _generation_slot():
    """Bounded inference concurrency: one local GPU serves a few generations at a time; others queue briefly."""
    if not _slots().acquire(timeout=get_settings().generation_queue_seconds):
        raise ModelBusy()
    try:
        yield
    finally:
        _slots().release()


class ExternalAiDisabled(AppError):
    status_code, code, message = 409, "EXTERNAL_AI_DISABLED", "This model uses an external provider, which is disabled for your company."


@dataclass(frozen=True)
class Profile:
    code: str
    display_name: str
    provider: str
    model_name: str
    max_output_tokens: int
    temperature: float


def resolve_profile(conn: Connection, department_id: UUID | None, task_class: str) -> Profile:
    """Department route first, then the company default route."""
    row = conn.execute(
        text(
            "SELECT mp.code, mp.display_name, mp.provider, mp.model_name, mp.max_output_tokens, mp.temperature, mp.enabled"
            " FROM core.model_routes r JOIN core.model_profiles mp ON mp.company_id = r.company_id AND mp.code = r.profile_code"
            " WHERE r.task_class = :t AND (r.department_id = :d OR r.department_id IS NULL)"
            " ORDER BY r.department_id IS NULL LIMIT 1"
        ),
        {"t": task_class, "d": department_id},
    ).first()
    if row is None or not row.enabled:
        raise ModelUnavailable("No enabled model is routed for this work. Ask the owner to configure one.")
    if row.provider in EXTERNAL_PROVIDERS:
        allowed = conn.execute(text("SELECT external_ai_enabled FROM core.company_settings")).scalar()
        if not allowed:
            raise ExternalAiDisabled()
    if row.provider == "scripted" and get_settings().environment not in ("development", "test"):
        raise ModelUnavailable()
    return Profile(row.code, row.display_name, row.provider, row.model_name, row.max_output_tokens, float(row.temperature))


def complete(
    p: Principal,
    profile: Profile,
    *,
    output_model: type[T],
    system: str,
    messages: list[ChatMessage],
    stage: str,
    task_class: str,
    department_id: UUID | None,
    conversation_id: UUID | None,
    max_tokens: int | None = None,
    schema: dict | None = None,
) -> T:
    """`schema` constrains generation (may be stricter); `output_model` validates the result."""
    provider = PROVIDERS[profile.provider]
    status, error_code, completion = "ok", None, None
    try:
        with _generation_slot():
            completion = provider.complete_json(
                model=profile.model_name,
                system=system,
                messages=messages,
                schema=schema or output_model.model_json_schema(),
                max_tokens=min(max_tokens or profile.max_output_tokens, profile.max_output_tokens),
                temperature=profile.temperature,
            )
        try:
            return output_model.model_validate(json.loads(completion.text))
        except (ValueError, ValidationError) as exc:
            status, error_code = "invalid_output", "MODEL_INVALID_OUTPUT"
            raise ModelUnavailable("The model returned an invalid response.") from exc
    except ProviderError as exc:
        status, error_code = "error", exc.code
        raise ModelUnavailable() from exc
    finally:
        _record_run(p, profile, stage, task_class, department_id, conversation_id, status, error_code, completion)


def stream(
    p: Principal,
    profile: Profile,
    *,
    output_model: type[T],
    system: str,
    messages: list[ChatMessage],
    stage: str,
    task_class: str,
    department_id: UUID | None,
    conversation_id: UUID | None,
    max_tokens: int | None = None,
    schema: dict | None = None,
) -> Iterator[str | T]:
    """Like `complete`, but yields text chunks while the model generates, then exactly one validated
    `output_model` instance. Providers without native streaming deliver the whole text as one chunk.
    Closing the iterator early cancels generation and is recorded as CLIENT_CANCELLED."""
    provider = PROVIDERS[profile.provider]
    status, error_code, completion = "ok", None, None
    args = dict(model=profile.model_name, system=system, messages=messages, schema=schema or output_model.model_json_schema(),
                max_tokens=min(max_tokens or profile.max_output_tokens, profile.max_output_tokens),
                temperature=profile.temperature)
    try:
        with _generation_slot():
            chunks = provider.stream_json(**args) if hasattr(provider, "stream_json") else _whole(provider, args)
            try:
                for item in chunks:
                    if isinstance(item, Completion):
                        completion = item
                    else:
                        yield item
            finally:
                close = getattr(chunks, "close", None)
                if close:
                    close()   # stops a provider stream that is still running (client went away)
        if completion is None:
            raise ProviderError("MODEL_STREAM_INTERRUPTED")
        try:
            parsed = output_model.model_validate(json.loads(completion.text))
        except (ValueError, ValidationError) as exc:
            status, error_code = "invalid_output", "MODEL_INVALID_OUTPUT"
            raise ModelUnavailable("The model returned an invalid response.") from exc
        yield parsed
    except ProviderError as exc:
        status, error_code = "error", exc.code
        raise ModelUnavailable() from exc
    except GeneratorExit:
        status, error_code = "error", "CLIENT_CANCELLED"
        raise
    finally:
        _record_run(p, profile, stage, task_class, department_id, conversation_id, status, error_code, completion)


def _whole(provider, args: dict) -> Iterator[str | Completion]:
    completion = provider.complete_json(**args)
    yield completion.text
    yield completion


def _record_run(p, profile, stage, task_class, department_id, conversation_id, status, error_code, completion) -> None:
    with transaction(p.company_id, p.user_id) as conn:
        conn.execute(
            text(
                "INSERT INTO core.ai_runs (id, company_id, user_id, department_id, conversation_id, profile_code, provider,"
                " model_name, task_class, stage, status, error_code, input_tokens, output_tokens, latency_ms)"
                " VALUES (:id, :c, :u, :d, :conv, :pc, :prov, :m, :tc, :st, :s, :e, :it, :ot, :lat)"
            ),
            {
                "id": uuid4(), "c": p.company_id, "u": p.user_id, "d": department_id, "conv": conversation_id,
                "pc": profile.code, "prov": profile.provider, "m": profile.model_name, "tc": task_class, "st": stage,
                "s": status, "e": error_code,
                "it": completion.input_tokens if completion else 0,
                "ot": completion.output_tokens if completion else 0,
                "lat": completion.latency_ms if completion else 0,
            },
        )
