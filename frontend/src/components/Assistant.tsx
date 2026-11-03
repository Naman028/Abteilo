import { FormEvent, KeyboardEvent, useEffect, useLayoutEffect, useRef, useState } from "react";
import { ApiError, api, streamEvents } from "../api";
import { useLoad } from "../hooks";
import type { AssistantPayload, AssistantReply, ChatMessage, Claim, Conversation, DocumentContent, Evidence, ReportRef } from "../types";

const CLAIM_MARK: Record<string, [string, string]> = {
  verified: ["✓", "Checked against company data"],
  cited: ["≈", "Taken from the cited source; wording or cause not machine-checked"],
  outdated: ["⟲", "Only an older document says this; the live record says otherwise"],
  conflicting: ["≠", "Live records disagree; check the sources"],
  unsupported: ["?", "Not supported by the evidence"],
};

const AUTHORITY: Record<string, string> = {
  record: "live record", aggregate: "live summary", relationship: "relationships", document: "document",
};

const FRESHNESS: Record<string, string> = {
  projection_stale: "relationship data may be out of date",
  projection: "statuses re-read live",
};

const REASONS: Record<string, string> = {
  number_not_in_evidence: "a number does not appear in the source",
  identifier_not_in_evidence: "a project or name does not appear in the source",
  status_contradicts_or_missing: "the source states a different status",
  negated_status_not_verifiable: "negated statements cannot be checked",
  negated_fact_not_verifiable: "negated statements cannot be checked",
  negated_statement_not_verifiable: "negated statements cannot be checked",
  wording_not_found_in_evidence: "the source does not say this",
  unknown_evidence_cited: "it cites a source that was not retrieved",
  no_evidence_cited: "no source cited",
  narrative_not_machine_checked: "wording not machine-checked",
  causal_link_not_in_evidence: "no recorded relationship or source states this cause",
  causal_link_stated_in_source: "the cause is stated by a source, not proven",
  causal_link_recorded: "the cause is a recorded blocking relationship",
  dependency_recorded_not_cause: "a dependency is recorded; that it is the cause is not established",
  relationship_projection_stale: "the relationship data may be out of date",
  relationship_from_document_mention: "a document mentions it; no recorded relationship",
  superseded_by_live_record: "a newer live record says otherwise",
  records_disagree: "live records disagree",
  document_disagrees_record_preferred: "a document disagrees; the live record is used",
  document_not_authoritative_for_current_state: "only a document says this; no live record confirms it",
  insufficient_evidence: "nothing in the data you may open answers this",
  computed_from_records: "computed directly from the records",
};

const STAGE: Record<string, string> = {
  planning: "Finding the company data you may open…",
  collecting: "Collecting every record the report covers…",
  writing: "Writing the report from those records…",
  checking: "Checking every statement before it goes into the report…",
  answering: "Writing; each statement is checked before it appears…",
  finalizing: "Final checks…",
};

const DEGRADED: Record<string, string> = {
  GRAPH_TIMEOUT: "Relationship data did not answer in time",
  EMBEDDING_UNAVAILABLE: "Semantic search was unavailable; keyword search was used",
};

/** Small inline icons (decorative: the buttons carry the accessible name). */
const Icon = {
  send: <svg viewBox="0 0 16 16" aria-hidden="true"><path d="M8 13V3M3.5 7.5 8 3l4.5 4.5" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" /></svg>,
  stop: <svg viewBox="0 0 16 16" aria-hidden="true"><rect x="4" y="4" width="8" height="8" rx="1.5" fill="currentColor" /></svg>,
  plus: <svg viewBox="0 0 16 16" aria-hidden="true"><path d="M8 3v10M3 8h10" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" /></svg>,
};

type Live = Partial<AssistantPayload> & { stage?: string; conversation_id?: string; message_id?: string };
type Turn = { role: "user"; text: string } | { role: "assistant"; payload: Live; messageId?: string };

/** Space below the chat inside the page, so the chat column ends at the bottom of the window. */
const BOTTOM_GAP = 32;   // matches .main bottom padding, so the page itself never scrolls

/** departmentId null = owner workspace. */
export default function Assistant({ departmentId, examples }: { departmentId: string | null; examples: string[] }) {
  const listPath = departmentId ? `/api/conversations?department_id=${departmentId}` : "/api/conversations";
  const conversations = useLoad<Conversation[]>(listPath);
  const [active, setActive] = useState<string | null>(null);
  const [turns, setTurns] = useState<Turn[]>([]);
  const [text, setText] = useState("");
  const [mode, setMode] = useState<"general" | "heavy">("general");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [height, setHeight] = useState<number | undefined>(undefined);
  const abort = useRef<AbortController | null>(null);
  const shell = useRef<HTMLDivElement>(null);
  const scroller = useRef<HTMLDivElement>(null);
  const pinned = useRef(true);             // follow new text only while the reader is at the bottom
  const input = useRef<HTMLTextAreaElement>(null);

  const ownerList = (conversations.data ?? []).filter((c) => (departmentId ? true : c.department_id === null));

  // The chat fills the window below the page header; only the message list scrolls, never the page.
  useLayoutEffect(() => {
    const fit = () => {
      // On a phone the menu stacks above the page, so the chat takes one full screen of its own instead.
      if (window.innerWidth <= 860) return setHeight(Math.round(window.innerHeight * 0.9));
      const top = shell.current?.getBoundingClientRect().top ?? 0;
      setHeight(Math.max(420, window.innerHeight - top - BOTTOM_GAP));
    };
    fit();
    window.addEventListener("resize", fit);
    return () => window.removeEventListener("resize", fit);
  }, []);

  useEffect(() => {
    const el = scroller.current;
    if (el && pinned.current) el.scrollTop = el.scrollHeight;
  }, [turns, busy, error]);
  useEffect(() => () => abort.current?.abort(), []);   // leaving the page cancels a running answer

  // The composer grows with its text, up to a limit, like common chat apps.
  useLayoutEffect(() => {
    const el = input.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = `${Math.min(el.scrollHeight, 200)}px`;
  }, [text]);

  const onScroll = () => {
    const el = scroller.current;
    if (el) pinned.current = el.scrollHeight - el.scrollTop - el.clientHeight < 80;
  };

  const open = async (id: string) => {
    abort.current?.abort();
    setActive(id);
    setError(null);
    pinned.current = true;
    const messages = await api.get<ChatMessage[]>(`/api/conversations/${id}/messages`);
    setTurns(messages.map((m) => (m.role === "user" ? { role: "user", text: m.content }
      : { role: "assistant", payload: m.payload, messageId: m.id })));
  };

  const startNew = () => {
    abort.current?.abort();
    setActive(null);
    setTurns([]);
    setError(null);
    input.current?.focus();
  };

  /** Updates the assistant turn that is being streamed (always the last one). */
  const patchLive = (update: (live: Live) => Live) =>
    setTurns((t) => {
      const last = t[t.length - 1];
      return last?.role === "assistant" ? [...t.slice(0, -1), { role: "assistant", payload: update(last.payload) }] : t;
    });

  const send = async (e?: FormEvent, preset?: string) => {
    e?.preventDefault();
    const message = (preset ?? text).trim();
    if (!message || busy) return;
    setText("");
    setError(null);
    setBusy(true);
    pinned.current = true;
    setTurns((t) => [...t, { role: "user", text: message }, { role: "assistant", payload: { stage: "planning", claims: [] } }]);
    const controller = new AbortController();
    abort.current = controller;
    try {
      await streamEvents("/api/assistant/messages/stream",
        { conversation_id: active, department_id: departmentId, message, mode },
        (event, data) => {
          if (event === "stage") patchLive((l) => ({ ...l, stage: data as string }));
          else if (event === "evidence") patchLive((l) => ({ ...l, evidence: data as Evidence[] }));
          else if (event === "summary") {
            const s = data as { text: string; status: Claim["status"]; reason: string | null };
            patchLive((l) => ({ ...l, summary: s.text, summary_status: s.status, summary_reason: s.reason }));
          } else if (event === "claim") patchLive((l) => ({ ...l, claims: [...(l.claims ?? []), data as Claim] }));
          else if (event === "final") {
            const reply = data as AssistantReply;
            patchLive(() => reply);
            if (!active) {
              setActive(reply.conversation_id);
              conversations.reload();
            }
          } else if (event === "error") {
            const err = data as { message: string };
            setTurns((t) => t.slice(0, -1));
            setError(err.message);
          }
        }, controller.signal);
    } catch (err) {
      setTurns((t) => (t[t.length - 1]?.role === "assistant" ? t.slice(0, -1) : t));
      if (!(err instanceof DOMException && err.name === "AbortError")) {
        setError(err instanceof ApiError ? err.message : "The answer could not be completed.");
      }
    } finally {
      setBusy(false);
      abort.current = null;
    }
  };

  const onKeyDown = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) send(e);
  };

  return (
    <div className="assistant" ref={shell} style={{ height }}>
      <nav className="chat-history" aria-label="Chats">
        <button className="new-chat" onClick={startNew}>{Icon.plus} New chat</button>
        <div className="history-label">Recent</div>
        <div className="conv-list">
          {ownerList.map((c) => (
            <button key={c.id} className={c.id === active ? "active" : ""} onClick={() => open(c.id)} title={c.title}>
              {c.title}
            </button>
          ))}
          {ownerList.length === 0 && <div className="empty">No chats yet.</div>}
        </div>
      </nav>
      <section className="chat" aria-label="Assistant">
        <div className="messages" ref={scroller} onScroll={onScroll}>
          <div className="thread">
            {turns.length === 0 && (
              <div className="welcome">
                <h2>How can I help?</h2>
                <p className="sub">Answers use only company data you may open, and every statement is checked before it
                  appears. Ask for a PDF or Word report and it is written for you.</p>
                <div className="suggestions">
                  {[...examples, "Make me a PDF report of the current progress"].map((ex) => (
                    <button key={ex} onClick={() => send(undefined, ex)}>{ex}</button>
                  ))}
                </div>
              </div>
            )}
            {turns.map((t, i) => (t.role === "user"
              ? <div key={i} className="bubble-user">{t.text}</div>
              : <Reply key={i} payload={t.payload} conversationId={active} messageId={t.messageId ?? t.payload.message_id} />))}
            {error && <div className="error" role="alert">{error}</div>}
          </div>
        </div>
        <div className="sr-only" role="status" aria-atomic="true">
          {!busy && turns[turns.length - 1]?.role === "assistant"
            ? ((turns[turns.length - 1] as { payload: Live }).payload.report ? "Report ready to download." : "Answer complete.")
            : ""}
        </div>
        <form className="composer" onSubmit={send}>
          <div className="composer-box">
            <textarea ref={input} value={text} maxLength={4000} rows={1} aria-label="Message"
                      placeholder="Ask anything about your work, or ask for a report…"
                      onChange={(e) => setText(e.target.value)} onKeyDown={onKeyDown} />
            <div className="composer-row">
              <select value={mode} onChange={(e) => setMode(e.target.value as "general" | "heavy")} aria-label="Model">
                <option value="general">General</option>
                <option value="heavy">Heavy</option>
              </select>
              <button type="button" className="chip" disabled={busy}
                      title="Write a report of this area from its current records (PDF or Word)"
                      onClick={() => send(undefined, "Create a progress report of this area as PDF")}>Create report</button>
              <span className="spacer" />
              {busy
                ? <button type="button" className="send stop" aria-label="Stop generating" title="Stop" onClick={() => abort.current?.abort()}>{Icon.stop}</button>
                : <button className="send" aria-label="Send message" title="Send" disabled={!text.trim()}>{Icon.send}</button>}
            </div>
          </div>
          <div className="composer-hint">✓ checked against company data · ≈ cited, not proven · ? not supported</div>
        </form>
      </section>
    </div>
  );
}

function OutcomeBanner({ payload }: { payload: Live }) {
  const missing = payload.missing ?? [];
  if (payload.outcome === "insufficient") {
    return (
      <div className="notice outcome">
        <strong>Cannot determine this from the company data available to you.</strong>
        {missing.length > 0 && <ul>{missing.map((m) => <li key={m}>Not found: {m}</li>)}</ul>}
      </div>
    );
  }
  if (payload.outcome === "partial" && missing.length > 0) {
    return (
      <div className="notice outcome">
        Partly answered. Not determined from the available evidence:
        <ul>{missing.map((m) => <li key={m}>{m}</li>)}</ul>
      </div>
    );
  }
  if (payload.outcome === "conflicting") {
    return <div className="notice outcome warn">Live records disagree. Both are shown below; check the sources before acting.</div>;
  }
  return null;
}

/** Downloads a generated file. Permission is re-checked by the server on every download. */
async function download(url: string): Promise<string | null> {
  const res = await fetch(url, { credentials: "same-origin" });
  if (!res.ok) {
    const body = await res.json().catch(() => null);
    return body?.error?.message ?? "The file could not be downloaded.";
  }
  const name = /filename="([^"]+)"/.exec(res.headers.get("content-disposition") ?? "")?.[1] ?? "abteilo-report";
  const href = URL.createObjectURL(await res.blob());
  const a = document.createElement("a");
  a.href = href;
  a.download = name;
  a.click();
  setTimeout(() => URL.revokeObjectURL(href), 1000);
  return null;
}

function ReportCard({ report }: { report: ReportRef }) {
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const get = async (format: "pdf" | "docx") => {
    setBusy(true);
    setError(await download(`/api/reports/${report.id}?format=${format}`));
    setBusy(false);
  };
  const other = report.format === "pdf" ? "docx" : "pdf";
  return (
    <div className="doc-card" role="group" aria-label={`Generated report: ${report.title}`}>
      <div className="doc-icon" aria-hidden="true">{report.format === "pdf" ? "PDF" : "DOC"}</div>
      <div className="spacer">
        <strong>{report.title}</strong>{" "}
        <span className={`badge ${report.complete ? "verified" : "outdated"}`}>{report.complete ? "Complete" : "Partial"}</span>
        <div className="sub">
          {report.complete ? "Covers every record you may see in this area." : "Some records could not be included; the document lists what is missing."}
          {" "}Available until {new Date(report.expires_at).toLocaleDateString("de-DE")}.
        </div>
        {error && <div className="error" role="alert">{error}</div>}
      </div>
      <button type="button" className="primary" disabled={busy} onClick={() => get(report.format)}>
        Download {report.format === "pdf" ? "PDF" : "Word"}
      </button>
      <button type="button" disabled={busy} onClick={() => get(other)}>{other === "pdf" ? "PDF" : "Word"}</button>
    </div>
  );
}

function Reply({ payload, conversationId, messageId }: { payload: Live; conversationId: string | null; messageId?: string }) {
  const [openId, setOpenId] = useState<string | null>(null);
  const [doc, setDoc] = useState<DocumentContent | null>(null);
  const [docError, setDocError] = useState<string | null>(null);
  const evidence = payload.evidence ?? [];
  const shown = evidence.find((e) => e.id === openId);
  const streaming = payload.stage !== undefined && payload.model_profile === undefined;
  const conv = payload.conversation_id ?? conversationId;
  const exportBase = conv && messageId ? `/api/conversations/${conv}/messages/${messageId}/export` : null;
  const [exportError, setExportError] = useState<string | null>(null);

  const openSource = async (sourceId: string) => {
    setDocError(null);
    try {
      setDoc(await api.get<DocumentContent>(`/api/documents/${sourceId}`));
    } catch {
      setDocError("You no longer have access to this source.");
    }
  };

  return (
    <div className="reply" aria-busy={streaming}>
      {streaming && <div className="stage sub" role="status">{STAGE[payload.stage!] ?? "Working…"}</div>}
      {payload.summary !== undefined && (
        <p className={`summary${payload.summary_status === "unsupported" ? " unverified" : ""}`}
           title={payload.summary_reason ? `Summary not verified: ${REASONS[payload.summary_reason] ?? payload.summary_reason}` : undefined}>
          {payload.summary}
          {payload.summary_status === "unsupported" && (payload.claims?.length ?? 0) > 0 && (
            <span className="badge unsupported">Summary not verified</span>
          )}
        </p>
      )}
      {!streaming && <OutcomeBanner payload={payload} />}
      {payload.claims && payload.claims.length > 0 && (
        <ul className="claims">
          {payload.claims.map((c, i) => (
            <li key={i} className={c.status}>
              <span className={`mark ${c.status}`} title={CLAIM_MARK[c.status]?.[1]}>{CLAIM_MARK[c.status]?.[0] ?? "?"}</span>
              <span className="text spacer">
                {c.text}
                {c.status !== "verified" && c.reason && <span className="sub"> — {REASONS[c.reason] ?? c.reason}</span>}
                {c.status === "verified" && c.reason === "causal_link_recorded" && <span className="sub"> — {REASONS[c.reason]}</span>}
                {c.status === "verified" && c.conflicts.length > 0 && (
                  <span className="sub"> — {c.conflicts.join(", ")} disagrees; the live record is used</span>
                )}
              </span>
              {c.evidence_ids.map((id) => (
                <button type="button" key={id} className="ev-chip" aria-expanded={openId === id}
                        onClick={() => setOpenId(openId === id ? null : id)}>{id}</button>
              ))}
            </li>
          ))}
        </ul>
      )}
      {payload.follow_up && <p>{payload.follow_up}</p>}
      {(payload.degradations ?? []).map((d) => (
        <p key={d.source + d.code} className="sub degraded">{DEGRADED[d.code] ?? `${d.source} was unavailable`}; answered without it.</p>
      ))}
      {!streaming && payload.report && <ReportCard report={payload.report} />}
      {evidence.length > 0 && (
        <div className="evidence">
          <div className="row">
            <span className="evidence-title">Sources</span>
            {evidence.map((e) => (
              <button type="button" key={e.id} className={`ev-chip${e.freshness === "projection_stale" ? " stale" : ""}`}
                      aria-expanded={openId === e.id} onClick={() => setOpenId(openId === e.id ? null : e.id)}
                      title={`${AUTHORITY[e.kind] ?? e.kind}${e.as_of ? ` · as of ${new Date(e.as_of).toLocaleString("de-DE")}` : ""}`
                             + `${e.freshness && FRESHNESS[e.freshness] ? ` · ${FRESHNESS[e.freshness]}` : ""}`}>
                {e.id} · {e.title} <span className="sub">({AUTHORITY[e.kind] ?? e.kind}{e.as_of ? `, ${new Date(e.as_of).toLocaleDateString("de-DE")}` : ""}
                {e.freshness === "projection_stale" ? ", may be out of date" : ""})</span>
              </button>
            ))}
          </div>
          {shown && (
            <div>
              {shown.content ? <pre>{shown.content}</pre> : <p className="sub">Snapshot not stored. Open the source to view it.</p>}
              {shown.source_type === "document" && shown.source_id && (
                <button className="small" onClick={() => openSource(shown.source_id!)}>Open source document</button>
              )}
            </div>
          )}
          {doc && <pre>{doc.title} (v{doc.current_version}){"\n\n"}{doc.content}</pre>}
          {docError && <div className="error" role="alert">{docError}</div>}
        </div>
      )}
      {payload.model_profile && (
        <div className="meta">
          <span>
            {payload.model_profile}
            {payload.tools_used && payload.tools_used.length > 0 && ` · ${payload.tools_used.join(", ")}`}
            {payload.timing && ` · ${(payload.timing.total_ms / 1000).toFixed(1)} s`}
          </span>
          {exportBase && !payload.report && (
            <span className="downloads" title="A copy of this answer as it was given; not re-checked against current records">
              Export answer:{" "}
              <button type="button" className="link" onClick={async () => setExportError(await download(`${exportBase}?format=pdf`))}>PDF</button>
              {" · "}
              <button type="button" className="link" onClick={async () => setExportError(await download(`${exportBase}?format=docx`))}>Word</button>
            </span>
          )}
        </div>
      )}
      {exportError && <div className="error" role="alert">{exportError}</div>}
    </div>
  );
}
