"""Assistant and conversation endpoints.

Conversations are private to their owner (RLS in the conv schema). Users can
delete one or all of their chats at any time; the owner's retention setting
deletes old chats automatically. Stored evidence snapshots are stripped when a
conversation is re-opened: sources must be opened through endpoints that
re-check permission.
"""

import json
import logging
import threading
import time
from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID, uuid4

import anyio
from fastapi import APIRouter, Depends, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, StringConstraints
from sqlalchemy import text
from starlette.concurrency import run_in_threadpool

from abteilo import reports
from abteilo.ai import orchestrator
from abteilo.ai.providers import ChatMessage
from abteilo.authz import Principal
from abteilo.deps import current_principal, load_department, tx
from abteilo.errors import AppError, Forbidden, NotFound

router = APIRouter(prefix="/api", tags=["assistant"])
log = logging.getLogger("abteilo.assistant")

HISTORY_MESSAGES = 6


class AskBody(BaseModel):
    conversation_id: UUID | None = None
    department_id: UUID | None = None  # None = owner workspace
    message: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=4000)]
    mode: Literal["general", "heavy"] = "general"


class AssistantReply(orchestrator.AssistantResult):
    conversation_id: UUID
    message_id: UUID


class ConversationView(BaseModel):
    id: UUID
    department_id: UUID | None
    title: str
    created_at: datetime
    last_message_at: datetime


class MessageView(BaseModel):
    id: UUID
    role: Literal["user", "assistant"]
    content: str
    payload: dict
    created_at: datetime


# The owner workspace is stored with the nil UUID as its department id.
OWNER_WORKSPACE = UUID(int=0)


def _workspace(conn, p: Principal, department_id: UUID | None) -> orchestrator.Workspace:
    p.require("ai.chat.use")
    if department_id is None:
        if not p.has_company_wide("report.executive.read"):
            raise Forbidden()
        return orchestrator.Workspace(None)
    department = load_department(conn, department_id, "assistant")
    p.require("department.member", department_id)
    return orchestrator.Workspace(department)


def _strip_payload(payload: dict) -> dict:
    return {**payload, "evidence": [{**e, "content": None} for e in payload.get("evidence", [])]}


def _load(p: Principal, body: AskBody) -> tuple[orchestrator.Workspace, list[ChatMessage]]:
    """Authorisation and history, before any model work starts (errors surface as normal HTTP errors)."""
    with tx(p) as conn:
        ws = _workspace(conn, p, body.department_id)
        history: list[ChatMessage] = []
        if body.conversation_id is not None:
            conv = conn.execute(
                text("SELECT department_id FROM conv.conversations WHERE id = :id"), {"id": body.conversation_id}
            ).first()
            if conv is None or conv.department_id != (body.department_id or OWNER_WORKSPACE):
                raise NotFound()
            rows = conn.execute(
                text("SELECT role, content FROM conv.messages WHERE conversation_id = :id"
                     " ORDER BY created_at DESC LIMIT :n"),
                {"id": body.conversation_id, "n": HISTORY_MESSAGES},
            ).all()
            history = [ChatMessage(r.role, r.content[:2000]) for r in reversed(rows)]
    return ws, history


def _persist(p: Principal, body: AskBody, result: orchestrator.AssistantResult) -> AssistantReply:
    """Store the completed, verified reply. Evidence is stored as references only (INT-06): a stored chat never
    keeps a copy of source content that could outlive a revocation; sources re-open through permission checks."""
    with tx(p) as conn:
        conversation_id = body.conversation_id or uuid4()
        if body.conversation_id is None:
            conn.execute(
                text("INSERT INTO conv.conversations (id, company_id, user_id, department_id, title)"
                     " VALUES (:id, core.current_company_id(), :u, :d, :t)"),
                {"id": conversation_id, "u": p.user_id, "d": body.department_id or OWNER_WORKSPACE,
                 "t": body.message[:80]},
            )
        conn.execute(
            text("INSERT INTO conv.messages (id, company_id, conversation_id, user_id, role, content)"
                 " VALUES (:id, core.current_company_id(), :c, :u, 'user', :content)"),
            {"id": uuid4(), "c": conversation_id, "u": p.user_id, "content": body.message},
        )
        message_id = uuid4()
        # Only checked content is fed back to the model as conversation history.
        lead = result.summary if result.summary_status != "unsupported" else "(unverified summary omitted)"
        assistant_text = lead + "".join(f"\n- {c.text}" for c in result.claims if c.status == "verified")
        stored = result.model_copy(update={"evidence": [e.model_copy(update={"content": None}) for e in result.evidence]})
        conn.execute(
            text("INSERT INTO conv.messages (id, company_id, conversation_id, user_id, role, content, payload)"
                 " VALUES (:id, core.current_company_id(), :c, :u, 'assistant', :content, CAST(:payload AS jsonb))"),
            {"id": message_id, "c": conversation_id, "u": p.user_id, "content": assistant_text[:20000],
             "payload": stored.model_dump_json()},
        )
        conn.execute(text("UPDATE conv.conversations SET last_message_at = now() WHERE id = :id"), {"id": conversation_id})
        if result.report is not None:
            reports.store(conn, p, result, body.department_id or OWNER_WORKSPACE, conversation_id)
    return AssistantReply(**result.model_dump(), conversation_id=conversation_id, message_id=message_id)


@router.post("/assistant/messages", response_model=AssistantReply)
def api_ask(body: AskBody, p: Principal = Depends(current_principal)):
    ws, history = _load(p, body)
    fmt = reports.requested_format(body.message)
    if fmt:
        result = next(r for kind, r in reports.generate(p, ws, body.message, fmt, body.mode, body.conversation_id)
                      if kind == "result")
    else:
        result = orchestrator.run(p, ws, body.message, history, body.mode, body.conversation_id)
    return _persist(p, body, result)


class _ClosingStreamingResponse(StreamingResponse):
    """StreamingResponse that always closes its body iterator. With ASGI spec >= 2.4 Starlette learns about a client
    disconnect only as an OSError while sending the next chunk, and then leaves the async generator parked at its
    `yield`: its `finally` (cancel the model generation, record the run) would wait for garbage collection."""

    async def stream_response(self, send) -> None:
        try:
            await super().stream_response(send)
        finally:
            with anyio.CancelScope(shield=True):
                await self.body_iterator.aclose()


def _sse(event: str, data) -> str:
    return f"event: {event}\ndata: {json.dumps(jsonable_encoder(data), ensure_ascii=False)}\n\n"


@router.post("/assistant/messages/stream")
async def api_ask_stream(body: AskBody, request: Request, p: Principal = Depends(current_principal)):
    """The same request as /assistant/messages, answered progressively as Server-Sent Events:
    stage, evidence, summary, claim (each only after verification), then final (the stored reply) or error.
    Authorisation errors are normal HTTP errors, raised before the stream starts. If the client goes away,
    the generator is closed, which stops model generation; nothing is stored for an unfinished reply."""
    ws, history = await run_in_threadpool(_load, p, body)
    cancel = threading.Event()
    fmt = reports.requested_format(body.message)
    # A report request (deterministic detection) generates a report; anything else is answered as a question.
    events = (reports.generate(p, ws, body.message, fmt, body.mode, body.conversation_id, cancel) if fmt
              else orchestrator.run_stream(p, ws, body.message, history, body.mode, body.conversation_id, cancel))
    request_id = request.state.request_id

    def close_events() -> None:
        """Close the generator once the worker thread has handed it back. While a `next()` is still running in a
        worker thread, close() raises ValueError; that call returns within one model chunk (it checks `cancel`), so
        retrying briefly closes it deterministically instead of leaving cleanup to the garbage collector."""
        for _ in range(600):              # at most ~30 s
            try:
                events.close()
                return
            except ValueError:
                time.sleep(0.05)
        log.warning('{"event":"assistant_stream_close_timeout","request_id":"%s"}', request_id)

    async def stream():
        started, first_visible = time.monotonic(), None
        try:
            while True:
                if await request.is_disconnected():
                    return
                try:
                    kind, payload = await run_in_threadpool(next, events, (None, None))
                except AppError as exc:
                    yield _sse("error", {"code": exc.code, "message": exc.message, "request_id": request_id})
                    return
                except Exception:
                    log.exception('{"event":"assistant_stream_failed","request_id":"%s"}', request_id)
                    yield _sse("error", {"code": "INTERNAL_ERROR", "message": "The answer could not be completed.",
                                         "request_id": request_id})
                    return
                if kind is None:
                    return
                elapsed = int((time.monotonic() - started) * 1000)
                if kind == "result":
                    reply = await run_in_threadpool(_persist, p, body, payload)
                    timing = {"first_visible_ms": first_visible, "total_ms": elapsed}
                    log.info('{"event":"assistant_stream","request_id":"%s","first_visible_ms":%s,"total_ms":%d}',
                             request_id, first_visible if first_visible is not None else "null", elapsed)
                    yield _sse("final", {**reply.model_dump(mode="json"), "timing": timing})
                    return
                if kind in ("summary", "claim") and first_visible is None:
                    first_visible = elapsed
                yield _sse(kind, payload)
        finally:
            # On a client disconnect this task is being cancelled: signal the generator first (thread-safe), then
            # close it in a shielded scope so the cleanup is not itself cancelled.
            cancel.set()
            with anyio.CancelScope(shield=True):
                await run_in_threadpool(close_events)

    return _ClosingStreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})


@router.get("/conversations", response_model=list[ConversationView])
def api_conversations(department_id: UUID | None = None, p: Principal = Depends(current_principal)):
    with tx(p) as conn:
        rows = conn.execute(
            text("SELECT id, NULLIF(department_id, :owner) AS department_id, title, created_at, last_message_at"
                 " FROM conv.conversations WHERE (CAST(:d AS uuid) IS NULL OR department_id = :d)"
                 " ORDER BY last_message_at DESC LIMIT 100"),
            {"d": department_id, "owner": OWNER_WORKSPACE},
        ).mappings()
        return [ConversationView(**r) for r in rows]


@router.get("/conversations/{conversation_id}/messages", response_model=list[MessageView])
def api_messages(conversation_id: UUID, p: Principal = Depends(current_principal)):
    with tx(p) as conn:
        rows = conn.execute(
            text("SELECT id, role, content, payload, created_at FROM conv.messages WHERE conversation_id = :c"
                 " ORDER BY created_at LIMIT 500"),
            {"c": conversation_id},
        ).mappings().all()
        if not rows and not conn.execute(text("SELECT 1 FROM conv.conversations WHERE id = :c"), {"c": conversation_id}).first():
            raise NotFound()
        return [MessageView(**{**r, "payload": _strip_payload(r["payload"])}) for r in rows]


@router.delete("/conversations/{conversation_id}", status_code=204)
def api_delete_conversation(conversation_id: UUID, p: Principal = Depends(current_principal)):
    with tx(p) as conn:
        deleted = conn.execute(text("DELETE FROM conv.conversations WHERE id = :c RETURNING id"), {"c": conversation_id}).first()
        if deleted is None:
            raise NotFound()


@router.delete("/conversations", status_code=204)
def api_delete_all_conversations(p: Principal = Depends(current_principal)):
    with tx(p) as conn:
        conn.execute(text("DELETE FROM conv.conversations WHERE user_id = :u"), {"u": p.user_id})
