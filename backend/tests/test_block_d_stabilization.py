"""Block D stabilisation (2026-10-23): entity resolution (E2/F3), deterministic conflicts (E7), claim-specific
support (I4) and role attribution (I1). Each test protects one fix against regression."""

from datetime import UTC, datetime

from abteilo.ai.evidence import EvidenceItem, VerifiedClaim, verify_claims
from abteilo.ai.orchestrator import Prepared, conflict_claims
from abteilo.ai.tools import Evidence
from abteilo.db import transaction
from abteilo.graph_projection import project_company
from abteilo.intelligence import graph_source, sufficiency
from abteilo.intelligence.sufficiency import InformationNeed
from abteilo.security import login, resolve_session
from tests.conftest import PASSWORD


def _principal(email):
    return resolve_session(login(email, PASSWORD).token)


def test_resolver_finds_wrapped_names_only_among_visible_records(demo):
    project_company(demo)
    jonas, sara = _principal("jonas.becker@bina.example"), _principal("sara.koch@bina.example")
    cases = [(jonas, "project A17", "A17 Cooling System Redesign"),            # E2: kind word + code
             (jonas, "Projekt B24", "B24 Production Dashboard"),
             (sara, "the thermal test rig purchase", "Thermal test rig"),     # F3: label inside the subject
             (sara, "project A17", None)]                                     # Finance may not see the project
    for p, subject, expected in cases:
        with transaction(p.company_id, p.user_id) as conn:
            found = graph_source.related(conn, p, subject)
        assert (found.root_label if found else None) == expected, subject
    with transaction(sara.company_id, sara.user_id) as conn:
        rig = graph_source.related(conn, sara, "thermal test rig purchase")
    assert "Cooling System Redesign" not in rig.content                      # hidden neighbour never named


def test_conflicts_are_found_by_comparing_sources_not_by_the_model():
    now = datetime.now(UTC)
    prepared = Prepared(profile=None, needs=[InformationNeed(type="current_state", subject="B24")], precheck=[],
                        tools_used=[], question="Is project B24 blocked?", evidence={
        "E1": Evidence("record", "Project B24", "project", None,
                       "Project B24 | Production Dashboard | status=active | updated=2026-10-21", now),
        "E2": Evidence("document", "B24 Weekly Status (v1, part 1)", "document", None,
                       "Project B24 is blocked: the team is waiting for PLC access on line 2.", now)})
    # The model only offered an unrecorded cause, which the verifier rejected.
    rejected = [VerifiedClaim(text="Project B24 is blocked because the team is waiting for PLC access.",
                              evidence_ids=["E2"], status="unsupported", reason="causal_link_not_in_evidence")]
    added, conflicts = conflict_claims(prepared, rejected)
    assert [(c.subject, c.record_status, c.document_status) for c in conflicts] == [("B24", "active", "blocked")]
    assert len(added) == 1 and added[0].status == "verified" and added[0].conflicts == ["E2"]
    assert "B24 is active according to the live record" in added[0].text
    # If the model already stated the live status, nothing is duplicated (the conflict is still reported).
    stated = [VerifiedClaim(text="Project B24 is active.", evidence_ids=["E1"], status="verified")]
    assert conflict_claims(prepared, stated)[0] == [] and len(conflict_claims(prepared, stated)[1]) == 1
    # The planner may fetch only relationships: the document's claim then arrives as a quoted source sentence.
    relationships = Prepared(profile=None, needs=[], precheck=[], tools_used=[], question="Is project B24 blocked?",
                             evidence={"E1": Evidence("relationship", "Relationships of B24", "graph_relationships", None,
        "B24 Production Dashboard [project, status=active] -has_task-> Downtime alert thresholds [task, status=todo]"
        " (authority=record)\nB24 Weekly Status [document, status=active] -mentions-> B24 Production Dashboard"
        ' [project, status=active] (authority=document_mention)\n  source sentence: "Project B24 is blocked: the team'
        ' is waiting for PLC access on line 2."', now)})
    added, conflicts = conflict_claims(relationships, rejected)
    assert [(c.document_title, c.document_status) for c in conflicts] == [("B24 Weekly Status", "blocked")]
    assert added and added[0].evidence_ids == ["E1"]


def test_a_related_policy_passage_does_not_answer_a_different_question():
    policy = EvidenceItem("Purchase requests are submitted in Abteilo with a justification and the budget period.",
                          "document", title="Purchasing Policy")
    inserted, plain = verify_claims([("Purchase requests for business cards must be submitted in Abteilo.", ["E1"]),
                                     ("Purchase requests are submitted in Abteilo.", ["E1"])], {"E1": policy})
    assert (inserted.status, inserted.reason) == ("unsupported", "wording_not_found_in_evidence")
    assert plain.status == "cited"
    view = {"E1": sufficiency.view("document", policy.title, policy.content, set())}
    need = [InformationNeed(type="policy", subject="purchase request process")]   # the planner's generic rewording
    asked = lambda q: sufficiency.postcheck(need, view, [plain], q)[0].covered     # noqa: E731
    assert not asked("How do I order business cards?")                           # I4: not an answer
    assert asked("How are purchase requests submitted?")
    assert asked("Wie werden Bestellanforderungen eingereicht?")                  # German: judged by the need


def test_people_are_checked_by_their_role_on_the_record():
    requests = EvidenceItem(
        "Jonas Becker | role=department_editor | dept=Engineering | status=proposed | proposed_by=Tim Schulz",
        "record", title="Access requests")
    leave = EvidenceItem("Jonas Becker | 2026-11-02 to 2026-11-06 | 5.0 days | status=submitted", "record",
                         title="Pending leave requests")
    cases = [("Tim Schulz proposed the access request.", ["E1"], "verified"),                    # I1
             ("The access request for Jonas Becker was proposed by Tim Schulz.", ["E1"], "verified"),
             ("Jonas Becker proposed the access request.", ["E1"], "unsupported"),              # target, not proposer
             ("The access request for Jonas Becker is proposed.", ["E1"], "verified"),          # a status, not a role
             ("Jonas Becker requested leave from 2026-11-02 to 2026-11-06.", ["E2"], "verified")]
    results = verify_claims([(t, ids) for t, ids, _ in cases], {"E1": requests, "E2": leave})
    assert [r.status for r in results] == [s for _, _, s in cases]


def test_a_cited_document_status_contradicted_by_the_live_record_is_outdated():
    from abteilo.ai.evidence import StatusConflict
    from abteilo.ai.orchestrator import AnswerClaim, _superseded

    conflict = StatusConflict(subject="B24", record_status="active", record_evidence_id="E1", record_title="Project B24",
                              record_as_of=None, document_status="blocked", document_evidence_id="E2",
                              document_title="B24 Weekly Status", document_as_of=None)
    cited = AnswerClaim(text="Project B24 is blocked", evidence_ids=["E2"], status="cited",
                        reason="relationship_from_document_mention")
    other = AnswerClaim(text="Project A17 is blocked", evidence_ids=["E2"], status="cited", reason="x")
    assert (_superseded(cited, [conflict]).status, _superseded(cited, [conflict]).reason) == \
        ("outdated", "superseded_by_live_record")
    assert _superseded(other, [conflict]).status == "cited"                         # another subject: untouched
