"""Block D: graph + live records + documents in one evidence pipeline, sufficiency, outcomes and verified streaming.

The model is the deterministic scripted provider; PostgreSQL, permissions, the graph and the verifier are real.
"""

import json
from uuid import uuid4

import pytest
from sqlalchemy import text

from abteilo.ai import gateway, orchestrator
from abteilo.ai.providers import PROVIDERS
from abteilo.graph_projection import project_company
from abteilo.intelligence import graph_source
from tests.conftest import department_id, owner_conn, user_id

scripted = PROVIDERS["scripted"]


def _script(*replies):
    scripted.replies.clear()
    scripted.calls.clear()
    scripted.replies.extend(json.dumps(r) for r in replies)


def _ask(client, dept, message="Why is A17 blocked?", stream=False):
    path = "/api/assistant/messages/stream" if stream else "/api/assistant/messages"
    return client.post(path, json={"department_id": dept, "message": message})


def _events(response) -> list[tuple[str, dict]]:
    out = []
    for block in response.text.strip().split("\n\n"):
        lines = dict(line.split(": ", 1) for line in block.splitlines())
        out.append((lines["event"], json.loads(lines["data"])))
    return out


@pytest.fixture()
def graph_demo(demo):
    """Demo company, projected, plus one reviewed blocking edge: the retest is blocked by the cooling module order."""
    project_company(demo)
    with owner_conn() as conn:
        ids = dict(conn.execute(text("SELECT label, id FROM core.graph_entities WHERE kind = 'task'")).all())
        doc, version = conn.execute(text(
            "SELECT d.id, v.id FROM core.documents d JOIN core.document_versions v ON v.document_id = d.id"
            " AND v.version = d.current_version WHERE d.title = 'A17 Thermal Validation Report'")).one()
        rel = uuid4()
        conn.execute(text("INSERT INTO core.graph_relationships (id, company_id, src_id, dst_id, kind, authority, document_id)"
                          " VALUES (:id, :c, :s, :d, 'blocked_by', 'reviewed', :doc)"),
                     {"id": rel, "c": demo, "s": ids["Thermal validation retest"], "d": ids["Order replacement cooling module"],
                      "doc": doc})
        conn.execute(text("INSERT INTO core.graph_evidence (id, company_id, relationship_id, source_type, source_id,"
                          " document_version_id, quote) VALUES (:id, :c, :r, 'document_version', :v, :v, :q)"),
                     {"id": uuid4(), "c": demo, "r": rel, "v": version,
                      "q": "A replacement cooling module and a new test rig are required before the retest."})
    return demo


PLAN_RETEST = {"needs": [{"type": "causal", "subject": "Thermal validation retest"}],
               "tool_calls": [{"tool": "graph.related", "arguments": {"subject": "Thermal validation retest"}}]}


def test_causal_claims_need_a_recorded_causal_link_and_structure_is_not_a_cause(login, graph_demo):
    jonas = login("jonas.becker@bina.example")
    eng = department_id(jonas, "Engineering")
    answer = {"summary": "The retest waits for the cooling module order.", "summary_evidence_ids": ["E1"], "claims": [
        {"text": "Thermal validation retest is blocked because it depends on Order replacement cooling module.",
         "evidence_ids": ["E1"], "type": "causal"},
        {"text": "A17 is blocked because of the Thermal test rig purchase request.", "evidence_ids": ["E1"], "type": "causal"},
    ]}
    _script(PLAN_RETEST, answer)
    body = _ask(jonas, eng).json()
    assert body["tools_used"] == ["graph.related"]
    rel = body["evidence"][0]
    assert rel["kind"] == "relationship" and rel["freshness"] == "projection"
    assert "-blocked_by->" in rel["content"] and "status=" in rel["content"]       # live re-read statuses
    true_cause, structural = body["claims"]
    assert (true_cause["status"], true_cause["reason"]) == ("verified", "causal_link_recorded")
    assert (structural["status"], structural["reason"]) == ("unsupported", "causal_link_not_in_evidence")
    assert body["outcome"] == "answered" and body["missing"] == []
    with owner_conn() as conn:                      # the assistant's graph read is in the read log (INT-09)
        assert conn.execute(text("SELECT operation, outcome FROM core.graph_read_log")).all() == [("assistant", "ok")]


def test_relationship_claims_and_a_stale_projection(login, graph_demo):
    jonas = login("jonas.becker@bina.example")
    eng = department_id(jonas, "Engineering")
    plan = {"needs": [{"type": "relationship", "subject": "A17"}],
            "tool_calls": [{"tool": "graph.related", "arguments": {"subject": "A17"}}]}
    answer = {"summary": "Thermal test rig is requested for A17.", "summary_evidence_ids": ["E1"],
              "claims": [{"text": "The Thermal test rig purchase request is for A17.", "evidence_ids": ["E1"], "type": "relationship"}]}
    _script(plan, answer)
    body = _ask(jonas, eng, "What is connected to A17?").json()
    assert body["claims"][0]["status"] == "verified" and body["outcome"] == "answered"
    with owner_conn() as conn:
        conn.execute(text("UPDATE core.graph_projection_state SET last_finished_at = now() - interval '1 hour'"))
    _script(plan, answer)
    body = _ask(jonas, eng, "What is connected to A17?").json()
    assert body["evidence"][0]["freshness"] == "projection_stale"
    assert (body["claims"][0]["status"], body["claims"][0]["reason"]) == ("cited", "relationship_projection_stale")


def test_sufficiency_gate_and_unrelated_passages(login, graph_demo):
    jonas = login("jonas.becker@bina.example")
    eng = department_id(jonas, "Engineering")
    # Nothing retrievable: the model is not asked to answer at all.
    _script({"needs": [{"type": "current_state", "subject": "Z99"}],
             "tool_calls": [{"tool": "engineering.get_project", "arguments": {"project_code": "Z99"}}]},
            {"summary": "Z99 is on track.", "claims": [{"text": "Z99 is active.", "evidence_ids": ["E1"]}]})
    body = _ask(jonas, eng, "What is the status of Z99?").json()
    assert body["outcome"] == "insufficient" and body["claims"] == [] and body["evidence"] == []
    assert body["missing"] == ["the current state of Z99"]
    assert [c["system"][:22] for c in scripted.calls] == ["You plan data lookups "]     # no answer call
    # A true sentence from an unrelated policy does not answer the canteen question.
    _script({"needs": [{"type": "policy", "subject": "canteen opening hours"}],
             "tool_calls": [{"tool": "documents.search", "arguments": {"query": "leave policy"}}]},
            {"summary": "Employees receive 30 days of annual leave.", "summary_evidence_ids": ["E1"],
             "claims": [{"text": "Full-time employees receive 30 days of annual leave per calendar year.",
                         "evidence_ids": ["E1"], "type": "policy"}]})
    body = _ask(jonas, eng, "When is the canteen open?").json()
    assert body["claims"][0]["status"] in ("verified", "cited")   # true, and still not an answer
    assert body["outcome"] == "insufficient" and body["missing"] == ["what the documents say about canteen opening hours"]


def test_graph_timeout_degrades_without_failing_the_answer(login, graph_demo, monkeypatch):
    jonas = login("jonas.becker@bina.example")
    eng = department_id(jonas, "Engineering")

    def timeout(*a, **k):
        raise graph_source.GraphDegraded("GRAPH_TIMEOUT")

    monkeypatch.setattr(graph_source, "related", timeout)
    _script({"needs": [{"type": "current_state", "subject": "A17"}],
             "tool_calls": [{"tool": "graph.related", "arguments": {"subject": "A17"}},
                            {"tool": "engineering.get_project", "arguments": {"project_code": "A17"}}]},
            {"summary": "A17 is blocked.", "summary_evidence_ids": ["E1"],
             "claims": [{"text": "Project A17 is blocked.", "evidence_ids": ["E1"], "type": "current_state"}]})
    body = _ask(jonas, eng, "Status of A17?").json()
    assert body["degradations"] == [{"source": "graph.related", "code": "GRAPH_TIMEOUT"}]
    assert body["tools_used"] == ["engineering.get_project"] and body["claims"][0]["status"] == "verified"
    assert body["outcome"] == "answered"


def test_stored_chats_keep_evidence_references_not_content(login, graph_demo):
    jonas = login("jonas.becker@bina.example")
    eng = department_id(jonas, "Engineering")
    _script(PLAN_RETEST, {"summary": "ok", "claims": []})
    conversation = _ask(jonas, eng).json()["conversation_id"]
    with owner_conn(user_id("jonas.becker@bina.example")) as conn:   # read the raw stored row (owner-private RLS)
        payload = conn.execute(text("SELECT payload FROM conv.messages WHERE conversation_id = :c AND role = 'assistant'"),
                               {"c": conversation}).scalar_one()
    assert payload["evidence"] and all(e["content"] is None for e in payload["evidence"])
    assert payload["evidence"][0]["kind"] == "relationship"         # the reference itself is kept


def test_streaming_sends_verified_claims_progressively_and_matches_the_final_reply(login, graph_demo):
    jonas = login("jonas.becker@bina.example")
    eng = department_id(jonas, "Engineering")
    answer = {"summary": "The retest waits for the cooling module order.", "summary_evidence_ids": ["E1"], "claims": [
        {"text": "Thermal validation retest is blocked because it depends on Order replacement cooling module.",
         "evidence_ids": ["E1"], "type": "causal"},
        {"text": "A17 is blocked because of the Thermal test rig purchase request.", "evidence_ids": ["E1"], "type": "causal"},
        {"text": "Quote with \"braces {like this}\" stays one claim.", "evidence_ids": ["E1"]},
    ]}
    _script(PLAN_RETEST, answer)
    r = _ask(jonas, eng, stream=True)
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
    events = _events(r)
    kinds = [k for k, _ in events]
    assert kinds[:4] == ["stage", "evidence", "stage", "summary"] and kinds[-1] == "final"
    streamed = [d for k, d in events if k == "claim"]
    final = events[-1][1]
    assert [(c["text"], c["status"], c["reason"]) for c in streamed] == \
        [(c["text"], c["status"], c["reason"]) for c in final["claims"]]          # identical to the stored reply
    assert streamed[1]["status"] == "unsupported"                                  # sent with its status, never as fact
    assert kinds.index("claim") < kinds.index("final") and final["timing"]["first_visible_ms"] is not None
    assert final["outcome"] == "answered" and final["conversation_id"]
    stored = jonas.get(f"/api/conversations/{final['conversation_id']}/messages").json()
    assert [c["status"] for c in stored[-1]["payload"]["claims"]] == [c["status"] for c in final["claims"]]
    # The same request through the one-shot endpoint gives the same verified result.
    _script(PLAN_RETEST, answer)
    plain = _ask(jonas, eng).json()
    assert [(c["status"], c["reason"]) for c in plain["claims"]] == [(c["status"], c["reason"]) for c in final["claims"]]


def test_streaming_errors_auth_and_bounded_generation(login, graph_demo, monkeypatch):
    jonas = login("jonas.becker@bina.example")
    eng = department_id(jonas, "Engineering")
    jonas.headers.pop("X-Abteilo-CSRF")
    assert _ask(jonas, eng, stream=True).status_code == 403                          # CSRF as for every write
    jonas.headers["X-Abteilo-CSRF"] = "1"
    assert jonas.post("/api/assistant/messages/stream", json={"department_id": None, "message": "x"}).status_code == 403
    # The model fails during the answer: an error event, nothing stored.
    _script(PLAN_RETEST)
    events = _events(_ask(jonas, eng, stream=True))
    assert events[-1] == ("error", {"code": "MODEL_UNAVAILABLE", "message": "The AI model is not available right now.",
                                    "request_id": events[-1][1]["request_id"]})
    assert jonas.get("/api/conversations").json() == []
    # Cancellation: closing the stream early stops generation and is recorded as such.
    _script(PLAN_RETEST, {"summary": "x" * 400, "claims": []})
    from abteilo.authz import Principal, load_grants
    from abteilo.db import transaction
    with transaction(graph_demo) as conn:
        principal = Principal(user_id("jonas.becker@bina.example"), graph_demo, "", "",
                              load_grants(conn, graph_demo, user_id("jonas.becker@bina.example")))
    profile = gateway.Profile("scripted", "Scripted", "scripted", "scripted", 1000, 0.0)
    scripted.replies.popleft()
    chunks = gateway.stream(principal, profile, output_model=orchestrator.Draft, system="s", messages=[], stage="answer",
                            task_class="general", department_id=None, conversation_id=None)
    next(chunks)
    chunks.close()
    with owner_conn() as conn:
        assert conn.execute(text("SELECT status, error_code FROM core.ai_runs ORDER BY created_at DESC LIMIT 1")).one() \
            == ("error", "CLIENT_CANCELLED")
    # Bounded inference: with every generation slot taken, a request gets MODEL_BUSY instead of queueing forever.
    import threading
    held = threading.BoundedSemaphore(1)
    held.acquire()
    monkeypatch.setattr(gateway, "_slots", lambda: held)
    monkeypatch.setattr(gateway.get_settings(), "generation_queue_seconds", 0.05)
    _script(PLAN_RETEST, {"summary": "ok", "claims": []})
    r = _ask(jonas, eng)
    assert r.status_code == 503 and r.json()["error"]["code"] == "MODEL_BUSY"


def test_draft_stream_parser_handles_partial_json():
    parser = orchestrator.DraftStream()
    text_ = json.dumps({"summary": "S {x}", "summary_evidence_ids": ["E1"],
                        "claims": [{"text": "a \"q\" {b}", "evidence_ids": ["E1"]}, {"text": "c", "evidence_ids": []}]})
    events = []
    for i in range(0, len(text_), 5):
        events += parser.feed(text_[i:i + 5])
    assert [k for k, _ in events] == ["summary", "claim", "claim"]
    assert events[0][1] == ("S {x}", ["E1"]) and events[1][1].text == 'a "q" {b}'


def test_block_d_l1_probes(database):
    """Deterministic evaluation level L1 (eval/intelligence.py) on its own seed: true and false causality,
    record-versus-document conflict, cross-tenant request, stale document."""
    from eval.intelligence import PASSWORD, run_l1
    from eval.intelligence_seed import build
    from pathlib import Path

    from tests.conftest import OWNER_URL
    from sqlalchemy import create_engine

    owner = create_engine(OWNER_URL)
    with owner.begin() as conn:
        conn.execute(text("TRUNCATE core.companies, core.users, conv.conversations, core.login_failures CASCADE"))
    owner.dispose()
    company_id = build(PASSWORD)
    from abteilo.db import transaction
    with transaction(company_id) as conn:
        conn.execute(text("INSERT INTO core.model_profiles (company_id, code, display_name, provider, model_name)"
                          " VALUES (:c, 'scripted', 'Scripted', 'scripted', 'scripted')"), {"c": company_id})
        conn.execute(text("UPDATE core.model_routes SET profile_code = 'scripted'"))
    cases = json.loads((Path(__file__).parents[1] / "eval" / "intelligence_cases.json").read_text(encoding="utf-8"))["cases"]
    report = run_l1(cases)
    failed = [r for r in report["results"] if not (r["probe_claims_ok"] and r["probe_outcome_ok"])
              or r["leaked"] or r["stale_asserted"] or r["false_causal_verified"]]
    assert report["probes"] == 7 and failed == [], failed


def test_l2_regressions_from_the_first_real_model_run(login, graph_demo):
    """Defects found by the first real-Qwen evaluation run (2026-10-20), each pinned here."""
    from abteilo.ai.evidence import EvidenceItem, verify_claims
    from abteilo.intelligence.sufficiency import InformationNeed, postcheck, view
    from abteilo.ai.evidence import VerifiedClaim

    jonas, owner = login("jonas.becker@bina.example"), login("owner@bina.example")
    eng = department_id(jonas, "Engineering")
    # 1. A suspended member is never named in assistant evidence (records still show the task as assigned).
    tim = user_id("tim.schulz@bina.example")
    with owner_conn() as conn:
        conn.execute(text("UPDATE core.tasks SET assignee_id = :u WHERE title = 'Downtime alert thresholds'"), {"u": tim})
    assert owner.patch(f"/api/admin/members/{tim}", json={"status": "disabled"}).status_code == 200
    _script({"needs": [{"type": "current_state", "subject": "B24"}],
             "tool_calls": [{"tool": "engineering.get_project", "arguments": {"project_code": "B24"}}]},
            {"summary": "B24 is active.", "summary_evidence_ids": ["E1"], "claims": []})
    body = _ask(jonas, eng, "Who works on B24?").json()
    assert "Tim Schulz" not in json.dumps(body) and "an inactive member" in body["evidence"][0]["content"]
    # 2. A task's status is checked on the task's own line, not on its project's line.
    items = {"E1": EvidenceItem("Project A17 | Cooling | status=blocked\ntasks of A17:\n"
                                "- Update tolerance specification | status=done | assignee=Jonas", "record")}
    done, wrong = verify_claims([("The update tolerance specification task for Project A17 is completed.", ["E1"]),
                                 ("Project A17 is completed.", ["E1"])], items)
    assert done.status == "verified" and wrong.status == "unsupported"
    # 3. Model-style subjects (underscores, plurals, German) still match; a documented figure is a policy need,
    #    while a changing quantity needs structured evidence.
    v = {"E1": view("record", "Pending leave requests", "Jonas Becker | 2026-11-02 to 2026-11-06 | 5.0 days", set()),
         "E2": view("document", "Leave Policy (v2, part 1)", "Full-time employees receive 30 days of annual leave.", set())}
    def claim(claim_text, evidence_id):
        return VerifiedClaim(text=claim_text, evidence_ids=[evidence_id], status="verified")
    cov = postcheck([InformationNeed(type="quantity", subject="pending_leave_requests"),
                     InformationNeed(type="policy", subject="annual leave days")],
                    v, [claim("Jonas Becker has 5.0 days pending.", "E1"), claim("Employees receive 30 days of annual leave.", "E2")])
    assert [c.covered for c in cov] == [True, True]
    only_document = postcheck([InformationNeed(type="quantity", subject="annual leave days")], v,
                              [claim("Employees receive 30 days of annual leave.", "E2")])
    assert only_document[0].covered is False          # a document never covers a changing quantity
    # 6. A recorded dependency or impact is not proof of the actual cause; only blocked_by / delays verify a cause.
    from abteilo.ai.evidence import EdgeFact
    for kind, status in (("depends_on", "cited"), ("affects", "cited"), ("blocked_by", "verified")):
        edge = EdgeFact("Thermal validation retest", "task", "blocked", kind, "Order replacement cooling module", "task",
                        "in_progress", "reviewed")
        if kind == "affects":   # src affects dst: the cooling order is the alleged cause of the retest's state
            edge = EdgeFact("Order replacement cooling module", "task", "in_progress", kind, "Thermal validation retest",
                            "task", "blocked", "reviewed")
        rel = {"E1": EvidenceItem("Thermal validation retest [task, status=blocked] -%s-> ..." % kind, "relationship",
                                  edges=(edge,))}
        got = verify_claims([("Thermal validation retest is blocked because of Order replacement cooling module.", ["E1"])], rel)[0]
        assert got.status == status, (kind, got)
    # 4. A project code resolves to the project, not to a document whose title starts with it.
    with owner_conn() as conn:
        conn.execute(text("INSERT INTO core.graph_entities (id, company_id, kind, ref_id, label, vis_scope, vis_department_id,"
                          " active) SELECT gen_random_uuid(), company_id, 'document', gen_random_uuid(), 'B24 Notes', 'company',"
                          " NULL, true FROM core.graph_entities WHERE kind = 'project' LIMIT 1"))
    _script({"needs": [{"type": "relationship", "subject": "B24"}],
             "tool_calls": [{"tool": "graph.related", "arguments": {"subject": "B24"}}]},
            {"summary": "x", "claims": []})
    assert _ask(jonas, eng, "What is connected to B24?").json()["evidence"][0]["title"] == "Relationships of B24 Production Dashboard"
    # 5. The generation schema makes the model cite: every claim needs at least one evidence id.
    claim_schema = orchestrator.DRAFT_SCHEMA["$defs"]["_StrictClaim"]["properties"]["evidence_ids"]
    assert claim_schema["minItems"] == 1 and claim_schema["items"]["pattern"] == "^E[0-9]{1,2}$"


def test_blocking_wording_needs_a_cause_asserting_edge():
    """Second real-Qwen run (2026-10-20): 'X ... is blocking the project's progress' was verified from a structural
    has_task edge. Blocking/holding-up wording is causal; only a recorded blocked_by/delays edge verifies it."""
    from abteilo.ai.evidence import EdgeFact, EvidenceItem, verify_claims

    has_task = EdgeFact("A17 Cooling System Redesign", "project", "blocked", "has_task", "Order replacement cooling module",
                        "task", "in_progress", "record")
    blocked = EdgeFact("Thermal validation retest", "task", "blocked", "blocked_by", "Order replacement cooling module",
                       "task", "in_progress", "reviewed")
    ev = {"E1": EvidenceItem("A17 Cooling System Redesign [project, status=blocked] -has_task-> Order replacement cooling "
                             "module [task, status=in_progress]\nThermal validation retest [task, status=blocked] "
                             "-blocked_by-> Order replacement cooling module [task, status=in_progress]",
                             "relationship", edges=(has_task, blocked), title="Relationships of A17")}
    structural, effect_first, cause_first, reversed_ = verify_claims([
        ("The Order replacement cooling module task is part of A17 and is blocking the project's progress.", ["E1"]),
        ("The thermal validation retest task is blocked by the order replacement cooling module task.", ["E1"]),
        ("The replacement cooling module order is blocking the thermal validation retest.", ["E1"]),
        ("The thermal validation retest is blocking the order replacement cooling module.", ["E1"]),
    ], ev)
    assert structural.status == "unsupported"
    assert (effect_first.status, effect_first.reason) == ("verified", "causal_link_recorded")   # exact record names
    # Paraphrased names may find the candidate edge, but identity is not proven: cited, never verified.
    assert (cause_first.status, cause_first.reason) == ("cited", "subject_identity_not_established")
    assert reversed_.status != "verified"                                                    # direction matters
    # Narrative wording may come from the evidence title; a paraphrased task name selects the task's own line.
    record = {"E1": EvidenceItem("Jonas Becker | 2026-11-02 to 2026-11-06 | 5.0 days | status=submitted", "record",
                                 title="Pending leave requests"),
              "E2": EvidenceItem("Project A17 | Cooling | status=blocked\n- Thermal validation retest | status=blocked\n"
                                 "- Order replacement cooling module | status=in_progress", "record")}
    pending, person, paraphrase = verify_claims([
        ("There is one pending leave request.", ["E1"]),
        ("The pending leave request is for Jonas Becker.", ["E1"]),
        ("The replacement cooling module for the thermal validation retest is being ordered and is in progress.", ["E2"]),
    ], record)
    assert pending.status == "verified" and person.status in ("verified", "cited")
    # The claim names the retest exactly (blocked) and paraphrases the order task (in progress). An exactly named
    # record is never swapped for a paraphrased one (reviewer rule, 2026-10-23), so the claim is not supported.
    assert (paraphrase.status, paraphrase.reason) == ("unsupported", "status_contradicts_or_missing")


def test_structural_edges_never_verify_a_cause_in_either_direction_or_language():
    """Reviewer requirement (2026-10-20): has_task (or any structural edge) never verifies 'is blocking', 'caused by',
    'leading to', 'held up by' or equivalents, effect-first or cause-first, English or German. Identity for
    'verified' comes from codes or exact unambiguous record names; paraphrase is candidate discovery only."""
    from abteilo.ai.evidence import EdgeFact, EvidenceItem, verify_claims

    task, project = "Thermal validation retest", "A17 Cooling System Redesign"
    structural = EdgeFact(project, "project", "blocked", "has_task", task, "task", "blocked", "record", src_id="p", dst_id="t")
    ev = {"E1": EvidenceItem(f"{project} [project, status=blocked] -has_task-> {task} [task, status=blocked]",
                             "relationship", edges=(structural,))}
    phrasings = [
        f"A17 is blocking the {task}.", f"The {task} is blocking A17.", f"A17 is blocked by the {task}.",
        f"A17 is blocked because of the {task}.", f"A17 is held up by the {task}.", f"The delay of A17 is caused by the {task}.",
        f"The {task} is leading to the block of A17.", f"The {task} results in A17 being blocked.",
        f"The {task} delays A17.", f"The {task} prevents A17 from finishing.",
        f"A17 ist blockiert, weil der {task} blockiert ist.", f"A17 wird durch den {task} blockiert.",
        f"Der {task} blockiert das Projekt A17.", f"Der {task} führt zu der Verzögerung von A17.",
        f"A17 ist wegen dem {task} verzögert.", f"Der {task} verzögert die Fertigstellung von A17.",
    ]
    verified = [(p, r.reason) for p, r in zip(phrasings, verify_claims([(p, ["E1"]) for p in phrasings], ev))
                if r.status == "verified"]
    assert verified == []
    # The same wording with a recorded blocking edge between exactly named records does verify (both languages).
    blocking = EdgeFact(task, "task", "blocked", "blocked_by", "Order replacement cooling module", "task", "in_progress",
                        "reviewed", src_id="t", dst_id="o")
    ev2 = {"E1": EvidenceItem(f"{task} [task, status=blocked] -blocked_by-> Order replacement cooling module "
                              "[task, status=in_progress]", "relationship", edges=(blocking,))}
    good = verify_claims([
        (f"The {task} is blocked by the Order replacement cooling module.", ["E1"]),
        (f"The Order replacement cooling module is blocking the {task}.", ["E1"]),
        (f"Der {task} wird durch die Order replacement cooling module blockiert.", ["E1"]),
        (f"Die Order replacement cooling module blockiert den {task}.", ["E1"]),
    ], ev2)
    assert [(r.status, r.reason) for r in good] == [("verified", "causal_link_recorded")] * 4
    # Similar names: 'Cooling Module Assembly' is never taken for 'Cooling Module Validation' (or a shorter name).
    from abteilo.ai.evidence import EdgeFact as E
    a = E("Cooling Module Validation", "task", "blocked", "blocked_by", "Cooling Module Assembly", "task", "in_progress",
          "reviewed", src_id="v", dst_id="a")
    ev3 = {"E1": EvidenceItem("Cooling Module Validation [task, status=blocked] -blocked_by-> Cooling Module Assembly "
                              "[task, status=in_progress]\n- Cooling Module | status=done", "relationship", edges=(a,))}
    wrong, short = verify_claims([("Cooling Module Assembly is blocked by Cooling Module Validation.", ["E1"]),
                                  ("Cooling Module Validation is blocked by Cooling Module.", ["E1"])], ev3)
    assert wrong.status != "verified" and short.status != "verified"
    # Two different entities with the same name are never identified by that name.
    d1 = E("Line audit", "task", "blocked", "blocked_by", "PLC access", "task", "todo", "reviewed", src_id="x1", dst_id="y")
    d2 = E("Line audit", "task", "done", "has_task", "B24 Production Dashboard", "project", "active", "record", src_id="x2", dst_id="b")
    ev4 = {"E1": EvidenceItem("...", "relationship", edges=(d1, d2))}
    dup = verify_claims([("Line audit is blocked by PLC access.", ["E1"])], ev4)[0]
    assert dup.status != "verified"


def test_inactive_member_never_named_in_assistant_evidence_in_any_department(login, graph_demo, monkeypatch):
    """E8 at the data boundary: with name masking switched off, the typed *_active fields alone keep a suspended
    member out of Engineering, Finance, HR and IT evidence. The Projects view keeps the historical assignee."""
    owner = login("owner@bina.example")
    lena, sara, mila, tim = (login(f"{u}@bina.example") for u in ("lena.weber", "sara.koch", "mila.hoffmann", "tim.schulz"))
    jonas_id = user_id("jonas.becker@bina.example")     # assignee, purchase requester, leave employee, access target
    assert owner.patch(f"/api/admin/members/{jonas_id}", json={"status": "disabled"}).status_code == 200
    monkeypatch.setattr(orchestrator, "_mask_inactive", lambda e, names: e)   # prove the typed path alone suffices
    cases = [
        (lena, "Engineering", {"tool": "engineering.get_project", "arguments": {"project_code": "A17"}}),
        (sara, "Finance", {"tool": "finance.list_purchase_requests", "arguments": {}}),
        (mila, "Human Resources", {"tool": "hr.pending_leave", "arguments": {}}),
        (tim, "IT & Security", {"tool": "it.list_access_requests", "arguments": {}}),
    ]
    for client, dept_name, call in cases:
        _script({"needs": [{"type": "open", "subject": "requests"}], "tool_calls": [call]},
                {"summary": "x", "claims": []})
        body = _ask(client, department_id(client, dept_name), "Show the current records.").json()
        content = "\n".join(e["content"] or "" for e in body["evidence"])
        assert body["tools_used"] == [call["tool"]], (dept_name, body["tools_used"])
        assert "Jonas Becker" not in content and "an inactive member" in content, dept_name
    # The system of record keeps the historical assignment, marked inactive, for those allowed to read the project.
    eng = department_id(lena, "Engineering")
    a17 = next(p for p in lena.get(f"/api/engineering/{eng}/projects").json() if p["code"] == "A17")
    tasks = lena.get(f"/api/engineering/projects/{a17['id']}/tasks").json()
    retest = next(t for t in tasks if t["title"] == "Thermal validation retest")
    assert (retest["assignee_name"], retest["assignee_active"]) == ("Jonas Becker", False)


def test_cancel_flag_stops_generation_mid_answer(login, graph_demo):
    """Live HTTP acceptance (2026-10-20) found that a client disconnect did not stop generation: the cancelled
    task could not close a generator that was still running in a worker thread. The stream now checks a
    thread-safe cancel flag between model chunks and closes the provider stream itself."""
    import threading

    from abteilo.authz import Principal, load_grants
    from abteilo.db import transaction
    from abteilo.deps import load_department

    jonas_id = user_id("jonas.becker@bina.example")
    long_answer = {"summary": "s", "summary_evidence_ids": ["E1"],
                   "claims": [{"text": f"Project A17 is blocked ({i}).", "evidence_ids": ["E1"]} for i in range(6)]}
    _script({"needs": [{"type": "current_state", "subject": "A17"}],
             "tool_calls": [{"tool": "engineering.get_project", "arguments": {"project_code": "A17"}}]}, long_answer)
    with transaction(graph_demo, jonas_id) as conn:
        p = Principal(jonas_id, graph_demo, "", "", load_grants(conn, graph_demo, jonas_id))
        eng = conn.execute(text("SELECT id FROM core.departments WHERE template = 'engineering'")).scalar_one()
        ws = orchestrator.Workspace(load_department(conn, eng, "assistant"))
    cancel = threading.Event()
    events = orchestrator.run_stream(p, ws, "Status of A17?", [], "general", None, cancel)
    seen = []
    for kind, _ in events:
        seen.append(kind)
        if kind == "claim":
            cancel.set()          # the client went away after the first verified claim
    assert seen.count("claim") == 1 and "result" not in seen
    with owner_conn() as conn:
        assert conn.execute(text("SELECT stage, status, error_code FROM core.ai_runs ORDER BY created_at DESC LIMIT 1")).one() \
            == ("answer", "error", "CLIENT_CANCELLED")
    # Second path: the HTTP layer closes a generator that is parked at a yield (client gone between events).
    _script({"needs": [{"type": "current_state", "subject": "A17"}],
             "tool_calls": [{"tool": "engineering.get_project", "arguments": {"project_code": "A17"}}]}, long_answer)
    events = orchestrator.run_stream(p, ws, "Status of A17?", [], "general", None, threading.Event())
    for kind, _ in events:
        if kind == "claim":
            break
    events.close()
    with owner_conn() as conn:
        assert conn.execute(text("SELECT stage, status, error_code FROM core.ai_runs ORDER BY created_at DESC LIMIT 1")).one() \
            == ("answer", "error", "CLIENT_CANCELLED")
