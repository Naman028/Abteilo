"""Deterministic claim verification (AI-04, EVD-01..03).

Every claim (and the summary) must cite evidence returned in this request.
Each evidence item has an authority:

- record:    the live system of record (projects, budgets, requests); authoritative
             for current state;
- aggregate: computed from records (owner overview); authoritative like a record;
- document:  descriptive text with a version date; never authoritative for
             current state.

Checks, in order:
1. citations exist in this request;
2. identifiers (project codes, quoted names) appear in the cited evidence;
3. numbers appear in the cited evidence (digits inside identifiers excluded);
4. statuses appear on the same evidence line as the subject they describe;
   negated facts are never verified;
5. status conflicts across the whole evidence bundle: a live record wins over a
   document; two records that disagree are reported, not resolved;
6. causal explanations ("because", "due to", ...) are never verified by word
   matching. They are at most "cited", and only when a cited source itself
   states the causal link; otherwise they are unsupported, even when the
   effect part is a verified fact.

Statuses:
- verified:    checkable facts all match authoritative evidence;
- cited:       wording or causal link comes from a cited source; meaning not proven;
- outdated:    supported only by a document that a live record contradicts;
- conflicting: authoritative records disagree;
- unsupported: anything else.
"""

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from pydantic import BaseModel

ClaimStatus = Literal["verified", "cited", "outdated", "conflicting", "unsupported"]
ReplyStatus = Literal["verified", "partially_verified", "insufficient_evidence"]
Authority = Literal["record", "aggregate", "relationship", "document"]
CAUSAL_EDGE_KINDS = frozenset({"blocked_by", "depends_on", "delays", "affects"})   # relevant to "why" questions
# Only these state that one thing actually holds up another. A dependency (depends_on) or an impact (affects) is
# not proof of the actual cause of an incident: claims resting on them are at most "cited" (INT-04).
CAUSE_ASSERTING_KINDS = frozenset({"blocked_by", "delays"})
TRUSTED_EDGE_AUTHORITIES = frozenset({"record", "reviewed"})

MIN_NARRATIVE_OVERLAP = 0.6
# Changed whenever a verification rule changes. Stored reports carry the version that checked them, so their
# labels are never presented as current after the rules have moved on.
VERIFIER_VERSION = "2026-10-23.7"

_NUMBER = re.compile(r"\d[\d.,]*\d|\d")
_EVIDENCE_REF = re.compile(r"\[?\bE\d+\b\]?")
_CODE = re.compile(r"\b[A-Z][A-Z0-9]*\d[A-Z0-9]*(?:-[A-Z0-9]+)*\b")
_QUOTED = re.compile(r"[\"'“‘]([^\"'”’]{3,80})[\"'”’]")
# Absence and negation (English and German). Bare "no" counts ("there is no document", "no blocked tasks"):
# proving absence needs completeness guarantees the sources do not state, so such claims are never verified.
_NEGATION = re.compile(r"\b(not|no|none|nothing|nobody|without|no longer|isn't|aren't|wasn't|weren't|hasn't|haven't|"
                       r"never|neither|nor|nicht|nichts|kein|keine|keinen|keiner|keinem|ohne|nie|niemals)\b", re.I)
_WORD = re.compile(r"[a-zäöüß]{4,}")
# Causal connectors, by direction (English and German). Effect first: "<effect> because / blocked by <cause>".
_CAUSAL = re.compile(r"\b(because(?: of)?|due to|caused by|as a result of|owing to|is blocked by|blocked by|"
                     r"(?:is )?held up by|(?:is )?delayed by|weil|wegen|aufgrund|da(?:durch)? dass|"
                     r"blockiert (?:durch|von)|verzögert (?:durch|von)|verursacht (?:durch|von)|"
                     r"(?:wird|ist|wurde) durch)\b", re.I)
# Cause first: "<cause> blocks / is blocking / leads to <effect>".
_CAUSE_FIRST = re.compile(r"\b(which is why|therefore|leading to|led to|results? in|resulted in|so that|"
                          r"is blocking|blocking|blocks|is holding up|holds up|is delaying|delays|prevents|preventing|"
                          r"führt zu|führte zu|deshalb|daher|blockiert (?:den|die|das)|verzögert (?:den|die|das)|"
                          r"verhindert)\b", re.I)
_CAUSAL_IN_SOURCE = re.compile(r"\b(because|due to|caused by|root cause|as a result|owing to|blocked by|"
                               r"required (?:before|for)|requires|weil|wegen|aufgrund|ursache)\b", re.I)

# Claim wording -> canonical statuses that may appear in evidence.
_STATUS_TERMS: dict[str, set[str]] = {
    "blocked": {"blocked"},
    "complete": {"done"}, "completed": {"done"}, "finished": {"done"}, "done": {"done"},
    "in progress": {"in_progress"}, "in_progress": {"in_progress"}, "ongoing": {"in_progress", "active"},
    "planned": {"planned"}, "active": {"active"},
    "to do": {"todo"}, "todo": {"todo"}, "not started": {"todo", "planned"},
    "pending": {"submitted", "proposed"}, "awaiting": {"submitted", "proposed"},
    "submitted": {"submitted"}, "proposed": {"proposed"},
    "approved": {"approved"}, "rejected": {"rejected"},
    "cancelled": {"cancelled"}, "canceled": {"cancelled"}, "archived": {"archived"},
    # German
    "blockiert": {"blocked"}, "abgeschlossen": {"done"}, "erledigt": {"done"}, "in bearbeitung": {"in_progress"},
    "aktiv": {"active"}, "geplant": {"planned"}, "offen": {"submitted", "proposed", "todo"},
    "eingereicht": {"submitted"}, "beantragt": {"submitted"}, "genehmigt": {"approved"}, "abgelehnt": {"rejected"},
}
_STATUS_RE = re.compile(r"\b(" + "|".join(sorted(map(re.escape, _STATUS_TERMS), key=len, reverse=True)) + r")\b", re.I)

_STOPWORDS = frozenset(
    "that this with from have been will which their there they them were what when where about into than then also "
    "only some such these those over after before under because while would could should shall does each other "
    "more most very much many your yours ours project document according evidence".split()
)


@dataclass(frozen=True)
class EdgeFact:
    """One current graph edge as relationship evidence; endpoint statuses are re-read from live records."""
    src_label: str
    src_kind: str
    src_status: str | None
    kind: str
    dst_label: str
    dst_kind: str
    dst_status: str | None
    authority: str            # record | reviewed | document_mention
    stale: bool = False       # projection older than the bound, failed, or an endpoint changed since
    src_id: str | None = None  # stable entity ids: two entities with the same name are never treated as one
    dst_id: str | None = None


@dataclass(frozen=True)
class EvidenceItem:
    content: str
    authority: Authority
    as_of: datetime | None = None
    edges: tuple[EdgeFact, ...] = ()
    title: str = ""          # part of the evidence: narrative wording may come from it


class VerifiedClaim(BaseModel):
    text: str
    evidence_ids: list[str]
    status: ClaimStatus
    reason: str | None = None
    conflicts: list[str] = []  # evidence ids that disagree with this claim or with each other


# --- helpers ------------------------------------------------------------------------------
def _number_values(token: str) -> set[float]:
    """Interpret 12,500.50 and 12.500,50 both ways; unparseable tokens give nothing."""
    values = set()
    for candidate in (token.replace(",", ""), token.replace(".", "").replace(",", ".")):
        try:
            values.add(round(float(candidate), 4))
        except ValueError:
            pass
    return values


def numbers_in(text: str) -> list[set[float]]:
    return [_number_values(t) for t in _NUMBER.findall(_EVIDENCE_REF.sub(" ", text))]


def _canonical_statuses(text: str) -> set[str]:
    found: set[str] = set()
    for term in _STATUS_RE.findall(text.replace("_", " ")):
        found |= _STATUS_TERMS[term.lower()]
    return found


def _identifiers(text: str) -> list[str]:
    text = _EVIDENCE_REF.sub(" ", text)  # citation markers like [E1] are not subjects
    return [c for c in _CODE.findall(text) if not c.isdigit()] + _QUOTED.findall(text)


def _lines_about(content: str, ident: str) -> list[str]:
    return [line for line in content.splitlines() if ident.lower() in line.lower()]


def _content_words(text: str) -> set[str]:
    return {w for w in _WORD.findall(_EVIDENCE_REF.sub(" ", text).lower()) if w not in _STOPWORDS}


def _overlap(text: str, evidence_text: str) -> float:
    words = _content_words(text)
    return len(words & set(_WORD.findall(evidence_text.lower()))) / len(words) if words else 0.0


def _negated(text: str) -> bool:
    return bool(_NEGATION.search(_STATUS_RE.sub(" ", text)))  # "not started" is a status, not a negation


_SENTENCE = re.compile(r"(?<=[.!?])\s+|\n+")
NEGATION_SOURCE_OVERLAP = 0.8


def _negation_stated_in_document(claim: str, cited: list[str], evidence: dict) -> bool:
    """A negative a cited *document* itself states ("Nobody may approve their own request.") is a documented rule,
    not an absence inferred from missing data, so it may be cited. Records never qualify: a list that does not show
    something does not prove it does not exist."""
    for c in cited:
        if evidence[c].authority != "document":
            continue
        for sentence in _SENTENCE.split(evidence[c].content):
            if _negated(sentence) and _overlap(claim, sentence) >= NEGATION_SOURCE_OVERLAP:
                return True
    return False


_PERSON_FIELD = re.compile(r"\b(?:assignee|requested_by|proposed_by)=([^|\n]+)")
# The role a sentence gives a person, tied to that person ("proposed by Tim Schulz", "Tim Schulz proposed",
# "assigned to Jonas Becker"); "the request is proposed" is a status, not a role. Maps to the record field.
_ROLE_PATTERNS = {
    # "X is assigned to Y": Y is the assignee, X the work. So the person comes after "assigned to", or is the
    # subject of "is responsible", "works on" or "is assigned to the task".
    "assignee": (r"(?:assigned to|assignee:?|responsible:?|zugewiesen an|verantwortlich:?)\s+{p}",
                 r"{p}\s+(?:is|was)\s+responsible|{p}\s+works? on|{p}\s+(?:is|was|has been)\s+assigned\s+to\s+"
                 r"(?:the\s+)?tasks?\b|{p}\s+ist\s+(?:für\s+\S+\s+)?verantwortlich"),
    "requested_by": (r"(?:requested by|requester:?|submitted by|beantragt von)\s+{p}",
                     r"{p}\s+(?:has\s+|had\s+)?(?:requested|submitted)|{p}\s+hat\s+\S+(?:\s+\S+)?\s+beantragt"),
    "proposed_by": (r"(?:proposed by|proposer:?|vorgeschlagen von)\s+{p}",
                    r"{p}\s+(?:has\s+|had\s+)?proposed|{p}\s+hat\s+\S+(?:\s+\S+)?\s+vorgeschlagen"),
}


def _role_of(claim: str, person: str) -> str | None:
    p = re.escape(person)
    for field, patterns in _ROLE_PATTERNS.items():
        if any(re.search(pattern.format(p=p), claim, re.I) for pattern in patterns):
            return field
    return None


def _holds(line: str, person: str, role: str | None) -> bool:
    """Does this record line name the person in the field of the stated role? Without a stated role, any person
    field counts, and so does the line's own subject (a leave request's employee, an access request's target)."""
    fields = {k: v.strip().lower() for k, v in re.findall(r"\b(assignee|requested_by|proposed_by)=([^|\n]+)", line)}
    is_subject = (_line_subject(line) or "").lower() == person
    if role == "requested_by" and not {"requested_by", "proposed_by"} & fields.keys():
        return is_subject     # a leave request names no requester: its employee is the one who asked
    if role:
        return fields.get(role) == person
    return person in fields.values() or is_subject
_NO_PERSON = {"unassigned", "none", "an inactive member"}
# Words that only frame a fact ("the task ... of project A17 is currently ..."); everything else a verified
# claim says must be found in the authoritative evidence, or the claim is at most cited.
_FRAMING = frozenset("""project projects task tasks request requests status currently current assigned responsible
open item items record records department departments total team their this that with from have which
""".split())
MAX_UNBACKED_WORDS = 1


def _unbacked_words(claim: str, evidence_text: str) -> set[str]:
    """Content words of a claim that the evidence does not contain (framing words excepted)."""
    return _content_words(claim) - set(_WORD.findall(evidence_text.lower())) - _FRAMING


def _line_subject(line: str) -> str | None:
    """The subject of a structured record line: the text before its first ' | ' ('- Update tolerance
    specification | status=done' -> 'Update tolerance specification'). Lines without that shape have none."""
    if " | " not in line:
        return None
    subject = line.split(" | ", 1)[0].strip().lstrip("-").strip()
    return subject if len(subject) >= 4 else None


# --- core checks -----------------------------------------------------------------------------
def _check_facts(claim: str, cited: list[str], evidence: dict[str, EvidenceItem]) -> tuple[bool, str | None, bool]:
    """Identifiers, numbers and statuses against the cited evidence. Returns (ok, reason, had_checkable_fact).
    Subjects are identified strictly; if the facts hold only when the subject is identified by paraphrase, the
    result is ok with reason `subject_identity_not_established` (the claim can then be cited, never verified)."""
    ok, reason, checkable = _check_facts_with(claim, cited, evidence, strict=True)
    # Facts that hold only on the line of a paraphrased subject ("connecting line 2 PLC data" for the task
    # "Connect line 2 PLC data") are possibly true, never proven (INT-04a): cited, not verified, not rejected.
    lines = [line for c in cited for line in evidence[c].content.splitlines() if line.strip()]
    # Records the claim names by their full name (a project named only by its code is context, not the subject).
    codes = [i for i in _identifiers(claim) if _CODE.fullmatch(i)]
    exact = [line for line in _subject_lines(claim, lines, True, _identifiers(claim))[0]
             if not any(re.search(rf"\b{re.escape(c)}\b", _line_subject(line)) for c in codes)]
    # The fallback is for paraphrased subjects only: a record the claim names exactly is never swapped for a
    # similar one ("Connect line 3 PLC data is due 2026-11-07" must not borrow line 2's date).
    if not ok and not exact and reason in ("status_contradicts_or_missing", "number_not_in_evidence",
                                           "person_not_on_subject_line", "person_without_subject"):
        fuzzy_ok, _, _ = _check_facts_with(claim, cited, evidence, strict=False)
        if fuzzy_ok:
            return True, "subject_identity_not_established", checkable
    return ok, reason, checkable


def _subject_lines(claim: str, lines: list[str], strict: bool, identifiers) -> tuple[list[str], set[str]]:
    """Record lines whose subject the claim names, and the named subjects other than the claim's identifiers
    (a named project is context for its task, not a rival)."""
    subjects = [s.lower() for line in lines if (s := _line_subject(line))]
    labels = frozenset(subjects)
    ambiguous = frozenset(s for s in labels if subjects.count(s) > 1)   # two records with the same name
    named = [line for line in lines
             if (s := _line_subject(line)) and _names(claim, s, strict, labels, ambiguous)]
    candidates = {_line_subject(line).lower() for line in named
                  if not any(i.lower() in line.lower() for i in identifiers)}
    return named, candidates


def _check_facts_with(claim: str, cited: list[str], evidence: dict[str, EvidenceItem],
                      strict: bool) -> tuple[bool, str | None, bool]:
    text_all = "\n".join(evidence[c].content for c in cited)
    lowered = text_all.lower()
    checkable = False

    # Identifiers anchor a claim to its subject; on their own they are not a checked fact.
    identifiers = _identifiers(claim)
    for ident in identifiers:
        if ident.lower() not in lowered:
            return False, "identifier_not_in_evidence", True

    lines = [line for c in cited for line in evidence[c].content.splitlines() if line.strip()]
    # Every fact must sit on the line of the subject the claim names: a task's own line when the claim names the
    # task ("the retest for A17 is blocked"), otherwise the lines of its identifiers (project codes). Without a
    # named subject, the whole cited evidence is the scope.
    named, candidates = _subject_lines(claim, lines, strict, identifiers)
    if not strict and len(candidates) > 1:
        # A paraphrase that fits several records identifies none of them: no record lends it a fact.
        return False, "subject_identity_ambiguous", True
    scope = named or ([line for line in lines if any(i.lower() in line.lower() for i in identifiers)]
                      if identifiers else lines)
    # Responsible people, by their structured role on a record line (assignee, requester, proposer); the role
    # the claim states ("proposed by", "assigned to") must be the field the person holds (I1, 2026-10-23).
    people = {p.strip().lower() for p in _PERSON_FIELD.findall(text_all) if p.strip().lower() not in _NO_PERSON}
    # A line's own subject may be a person too (an access request's target): if the sentence gives that name a
    # role ("Jonas Becker proposed the request"), the role must be his on a record line.
    named_people = people | {s.lower() for line in lines if (s := _line_subject(line))}
    mentioned = [p for p in named_people if re.search(rf"\b{re.escape(p)}\b", claim.lower())]
    for person in mentioned:
        role = _role_of(claim, person)
        if person not in people and role is None:
            continue          # a record named as the subject, not a person given a role
        checkable = True
        holders = [line for line in lines if _holds(line, person, role)]
        if not named and not identifiers:
            # No named subject: the person and role identify the record only if exactly one line fits.
            # "Tim Schulz proposed the access request" (one request he proposed) does; "Lena Weber is assigned
            # to connecting the PLC data" (two tasks of hers) does not.
            if len(holders) != 1:
                return False, "person_without_subject", True
            scope = holders
        elif not any(_holds(line, person, role) for line in scope):
            return False, "person_not_on_subject_line", True
    scope_text = "\n".join(scope)

    # Numbers and dates: on the subject's lines ("A17 is due 2026-11-14" is false when only a task of A17 is).
    available = set().union(*numbers_in(scope_text)) if numbers_in(scope_text) else set()
    for values in numbers_in(_CODE.sub(" ", claim)):
        checkable = True
        if not values & available:
            return False, "number_not_in_evidence", True

    if _canonical_statuses(claim):
        checkable = True
        if _negated(claim):
            return False, "negated_status_not_verifiable", True
        for term in _STATUS_RE.findall(claim.replace("_", " ")):
            if not any(_STATUS_TERMS[term.lower()] & _canonical_statuses(line) for line in scope):
                return False, "status_contradicts_or_missing", True
    elif checkable and _negated(claim):
        return False, "negated_fact_not_verifiable", True
    return True, None, checkable


def _status_conflicts(claim: str, cited: list[str], evidence: dict[str, EvidenceItem]) -> tuple[ClaimStatus | None, str | None, list[str]]:
    """Compare the claim's status for each identifier with every evidence item in the bundle."""
    claimed = _canonical_statuses(claim)
    identifiers = _identifiers(claim)
    if not claimed or not identifiers:
        return None, None, []
    for ident in identifiers:
        by_source: dict[str, set[str]] = {}
        for eid, item in evidence.items():
            statuses: set[str] = set()
            for line in _lines_about(item.content, ident):
                statuses |= _canonical_statuses(line)
            if statuses:
                by_source[eid] = statuses
        records = {eid: s for eid, s in by_source.items() if evidence[eid].authority != "document"}
        documents = {eid: s for eid, s in by_source.items() if evidence[eid].authority == "document"}
        record_sets = list(records.values())
        if len(records) > 1 and any(a.isdisjoint(b) for a in record_sets for b in record_sets):
            return "conflicting", "records_disagree", sorted(records)
        if not records:
            continue
        if not any(claimed & s for s in record_sets):
            if any(c in documents and claimed & documents[c] for c in cited):
                return "outdated", "superseded_by_live_record", sorted(records)
        else:
            disagreeing = sorted(eid for eid, s in documents.items() if not claimed & s)
            if disagreeing:
                return None, "document_disagrees_record_preferred", disagreeing
    return None, None, []


# A further cause asserted inside the cause part of a causal sentence.
_FURTHER_CAUSE = re.compile(r"\b(which|that|der|die|das)\s+(in turn\s+|wiederum\s+)?(requires?|needs?|depends? on|"
                            r"waits? for|is blocked by|benötigt|wartet auf)\b|\bin turn\b|\bwiederum\b", re.I)


class StatusConflict(BaseModel):
    """A live record and a document disagree about one record's status (deterministic, no model involved)."""
    subject: str
    record_status: str
    record_evidence_id: str
    record_title: str
    record_as_of: datetime | None
    document_status: str
    document_evidence_id: str
    document_title: str
    document_as_of: datetime | None


_STATUS_FIELD = re.compile(r"\bstatus=([a-z_]+)")
_SENTENCES = re.compile(r"(?<=[.!?:;])\s+|\n+")


def _record_status(code: str, item: EvidenceItem) -> str | None:
    """The status a live record (or a live re-read in relationship evidence) gives the record with this code."""
    if item.authority == "relationship":
        m = re.search(rf"\b{re.escape(code)}\b[^\[\n]*\[[a-z_]+, status=([a-z_]+)\]", item.content)
        return m.group(1) if m else None
    for line in item.content.splitlines():
        subject = _line_subject(line)
        if subject and re.search(rf"\b{re.escape(code)}\b", subject) and (m := _STATUS_FIELD.search(line)):
            return m.group(1)
    return None


def detect_status_conflicts(codes: list[str], evidence: dict[str, EvidenceItem]) -> list[StatusConflict]:
    """For each record code: its live status against every status a document states about it in a sentence that
    names the code. Live records are authoritative (EVD-02); the disagreement is reported, never decided by the
    model. Negated document sentences are not read as statements of status."""
    conflicts = []
    for code in dict.fromkeys(codes):
        live = next(((eid, s) for eid, item in evidence.items() if item.authority != "document"
                     and (s := _record_status(code, item))), None)
        if live is None:
            continue
        live_terms = _canonical_statuses(live[1]) or {live[1]}
        for eid, title, as_of, text in _document_statements(evidence):
            stated = set()
            for sentence in _SENTENCES.split(text):
                if re.search(rf"\b{re.escape(code)}\b", sentence) and not _negated(sentence):
                    stated |= _canonical_statuses(sentence)
            if stated and not stated & live_terms:
                record = evidence[live[0]]
                conflicts.append(StatusConflict(
                    subject=code, record_status=live[1].replace("_", " "), record_evidence_id=live[0],
                    record_title=record.title or live[0], record_as_of=record.as_of,
                    document_status=", ".join(sorted(stated)).replace("_", " "), document_evidence_id=eid,
                    document_title=title, document_as_of=as_of))
                break
    return conflicts


_MENTION = re.compile(r'^(?P<doc>[^\[\n]+?) \[document[^\]]*\] -mentions-> .*\n\s+source sentence: "(?P<quote>[^"]+)"',
                      re.M)


def _document_statements(evidence: dict[str, EvidenceItem]):
    """What documents say, with their source: whole document items, and the quoted sentence a document-mention
    edge carries inside relationship evidence ("B24 Weekly Status -mentions-> B24", with its sentence)."""
    for eid, item in evidence.items():
        if item.authority == "document":
            yield eid, item.title or eid, item.as_of, item.content
        elif item.authority == "relationship":
            for m in _MENTION.finditer(item.content):
                yield eid, m.group("doc").strip(), item.as_of, m.group("quote")


def _causal_split(claim: str) -> tuple[str, str] | None:
    """(effect, cause) of a causal claim, whichever order the connector uses; None for non-causal claims."""
    m = _CAUSAL.search(claim)
    if m:
        return claim[:m.start()].strip(" ,;"), claim[m.end():].strip(" ,;.")
    m = _CAUSE_FIRST.search(claim)
    if m:
        return claim[m.end():].strip(" ,;."), claim[:m.start()].strip(" ,;")
    return None


def _causal_link_in_source(effect: str, cause: str, cited: list[str], evidence: dict[str, EvidenceItem]) -> bool:
    """A source must itself state the link: one paragraph with a causal marker that mentions the cause."""
    subject = _identifiers(effect)
    for c in cited:
        content = evidence[c].content
        if subject and not any(s.lower() in content.lower() for s in subject):
            continue
        # Relationship evidence is a list of edges, not prose: only its quoted source sentences may state a cause.
        paragraphs = (re.findall(r'source sentence: "([^"]+)"', content) if evidence[c].edges
                      else re.split(r"\n\s*\n", content))
        for paragraph in paragraphs:
            if _CAUSAL_IN_SOURCE.search(paragraph) and _overlap(cause, paragraph) >= MIN_NARRATIVE_OVERLAP:
                return True
    return False


# --- relationship and causal evidence (Block D, INT-04) ------------------------------------------------
def _code_of(label: str) -> str | None:
    code = label.split(" ", 1)[0]
    return code if _CODE.fullmatch(code) else None


def _names(text: str, label: str, strict: bool, labels: frozenset[str] = frozenset(), ambiguous: frozenset[str] = frozenset()) -> bool:
    """Does the claim name this entity?

    strict (may support "verified"): the entity's code (A17), or its exact full record name - unless a longer
    record name that contains it is the one actually written ('Cooling Module Validation' does not name 'Cooling
    Module'), or the name belongs to more than one entity in the evidence (`ambiguous`).
    candidate (never proof on its own): every significant word of a multi-word name, by 5-letter stem."""
    lowered = text.lower()
    if label.lower() in ambiguous:
        return False if strict else label.lower() in lowered
    code = _code_of(label)
    if code:
        return re.search(rf"\b{re.escape(code)}\b", text) is not None
    if label.lower() in lowered:
        shadowed = any(o != label.lower() and label.lower() in o and o in lowered for o in labels)
        return not shadowed
    if strict:
        return False
    stems = {w[:5] for w in _WORD.findall(label.lower()) if w not in _STOPWORDS}
    return len(stems) >= 2 and all(s in lowered for s in stems)


def _mentions(text: str, label: str) -> bool:
    """Candidate naming (fuzzy allowed) - for discovery only, never proof."""
    return _names(text, label, strict=False)


def _cited_edges(cited: list[str], evidence: dict[str, EvidenceItem]) -> list[EdgeFact]:
    return [e for c in cited for e in evidence[c].edges]


def _edge_names(edges: list[EdgeFact]) -> tuple[frozenset[str], frozenset[str]]:
    """All endpoint names, and the names that belong to more than one distinct entity (by id) in this evidence."""
    ids: dict[str, set] = {}
    for e in edges:
        ids.setdefault(e.src_label.lower(), set()).add(e.src_id or e.src_label)
        ids.setdefault(e.dst_label.lower(), set()).add(e.dst_id or e.dst_label)
    return frozenset(ids), frozenset(n for n, s in ids.items() if len(s) > 1)


def _relationship_check(claim: str, cited: list[str], evidence: dict[str, EvidenceItem]) -> tuple[ClaimStatus, str | None] | None:
    """A stated relationship between two named entities, backed by a current edge between exactly those. The two
    entities must be named strictly (code or exact unambiguous record name) for "verified"; a relationship found
    only through paraphrase is at most "cited"."""
    edges = _cited_edges(cited, evidence)
    labels, ambiguous = _edge_names(edges)
    strict = [e for e in edges if _names(claim, e.src_label, True, labels, ambiguous)
              and _names(claim, e.dst_label, True, labels, ambiguous)]
    if not strict:
        if any(_mentions(claim, e.src_label) and _mentions(claim, e.dst_label) for e in edges):
            return "cited", "subject_identity_not_established"
        return None
    trusted = [e for e in strict if e.authority in TRUSTED_EDGE_AUTHORITIES]
    if any(not e.stale for e in trusted):
        return "verified", None
    if trusted:
        return "cited", "relationship_projection_stale"
    return "cited", "relationship_from_document_mention"   # a document names the project; that is all it proves


def _effect_cause(e: EdgeFact) -> tuple[str, str]:
    # src blocked_by/depends_on dst: src is held up by dst. src delays/affects dst: src is the cause.
    return (e.src_label, e.dst_label) if e.kind in ("blocked_by", "depends_on") else (e.dst_label, e.src_label)


def _causal_chain(effect: str, cause: str, cited: list[str], evidence: dict[str, EvidenceItem],
                  strict: bool = True) -> list[EdgeFact]:
    """At most two causal edges with record/reviewed authority linking the claimed effect to the claimed cause.
    Structural edges (owns, has_task, requested_for, assigned_to, mentions, ...) never qualify."""
    all_edges = _cited_edges(cited, evidence)
    labels, ambiguous = _edge_names(all_edges)
    edges = [e for e in all_edges if e.kind in CAUSAL_EDGE_KINDS and e.authority in TRUSTED_EDGE_AUTHORITIES]

    def named(text: str, label: str) -> bool:
        return _names(text, label, strict, labels, ambiguous)

    for e in edges:
        eff, cau = _effect_cause(e)
        if named(effect, eff) and named(cause, cau):
            return [e]
    for first in edges:
        eff, mid = _effect_cause(first)
        if not named(effect, eff):
            continue
        for second in edges:
            eff2, cau2 = _effect_cause(second)
            if second is not first and eff2 == mid and named(cause, cau2):
                return [first, second]
    return []


def verify_claims(claims: list[tuple[str, list[str]]], evidence: dict[str, EvidenceItem]) -> list[VerifiedClaim]:
    results = []
    for claim_text, cited in claims:
        cited = list(dict.fromkeys(cited))[:8]

        def result(status: ClaimStatus, reason: str | None = None, conflicts: list[str] | None = None) -> VerifiedClaim:
            return VerifiedClaim(text=claim_text, evidence_ids=[c for c in cited if c in evidence], status=status,
                                 reason=reason, conflicts=conflicts or [])

        if not cited:
            results.append(result("unsupported", "no_evidence_cited"))
            continue
        if any(c not in evidence for c in cited):
            results.append(result("unsupported", "unknown_evidence_cited"))
            continue

        causal = _causal_split(claim_text)
        main = causal[0] if causal else claim_text
        conflict_status, conflict_reason, conflicts = _status_conflicts(main, cited, evidence)
        ok, reason, checkable = _check_facts(main, cited, evidence)

        if conflict_status in ("conflicting", "outdated"):
            results.append(result(conflict_status, conflict_reason, conflicts))
        elif not ok:
            results.append(result("unsupported", reason))
        elif causal:
            # The effect may be a fact, but a cause is never proven by matching words: only a recorded or
            # reviewed causal edge between the two verifies it (INT-04); a source sentence makes it cited.
            chain = _causal_chain(main, causal[1], cited, evidence)
            if not chain and _causal_chain(main, causal[1], cited, evidence, strict=False):
                # A causal edge exists, but the claim names its records only by paraphrase: not proof.
                results.append(result("cited", "subject_identity_not_established", conflicts))
            elif chain and reason == "subject_identity_not_established":
                results.append(result("cited", reason, conflicts))
            elif chain and any(e.stale for e in chain):
                results.append(result("cited", "relationship_projection_stale", conflicts))
            elif chain and all(e.kind in CAUSE_ASSERTING_KINDS for e in chain) and _FURTHER_CAUSE.search(causal[1]):
                # "…blocked because it depends on X, which in turn requires Y": the first link is recorded, the
                # further one is not, so the sentence as a whole is not proven (E5 stream, 2026-10-23).
                results.append(result("cited", "causal_link_partly_recorded", conflicts))
            elif chain and all(e.kind in CAUSE_ASSERTING_KINDS for e in chain):
                results.append(result("verified", "causal_link_recorded", conflicts))
            elif chain:
                results.append(result("cited", "dependency_recorded_not_cause", conflicts))
            elif _causal_link_in_source(main, causal[1], cited, evidence):
                results.append(result("cited", "causal_link_stated_in_source", conflicts))
            else:
                results.append(result("unsupported", "causal_link_not_in_evidence"))
        elif (relationship := _relationship_check(claim_text, cited, evidence)) is not None:
            results.append(result(relationship[0], relationship[1], conflicts))
        elif checkable and reason == "subject_identity_not_established":
            # The fact matches only when its subject is identified by paraphrase: possibly true, not proven.
            results.append(result("cited", reason, conflicts))
        elif checkable:
            # "Verified" means a structured fact matched a live record or aggregate. Facts that only a
            # document states are at most cited, because documents describe, they do not record (EVD-02).
            authoritative = [c for c in cited if evidence[c].authority != "document"]
            if authoritative and _check_facts(main, authoritative, evidence) == (True, None, True):
                record_text = "\n".join(f"{evidence[c].title or ''}\n{evidence[c].content}" for c in authoritative)
                if len(_unbacked_words(main, record_text)) > MAX_UNBACKED_WORDS:
                    # The checked fact holds, but the claim says more than the record ("B24 is active but stalled
                    # waiting for PLC access"): the extra wording is not a recorded fact.
                    results.append(result("cited", "narrative_beyond_record", conflicts))
                else:
                    results.append(result("verified", conflict_reason, conflicts))
            elif len(_unbacked_words(main, "\n".join(f"{evidence[c].title or ''}\n{evidence[c].content}"
                                                      for c in cited))) > MAX_UNBACKED_WORDS:
                # A number or status matched, but the sentence says more than its source ("Purchase requests for
                # business cards must be submitted": the policy never mentions business cards). Claim-specific
                # support is required; a related passage is not an answer (I4, 2026-10-23).
                results.append(result("unsupported", "wording_not_found_in_evidence"))
            else:
                results.append(result("cited", "document_not_authoritative_for_current_state"))
        elif _negated(claim_text) and not _negation_stated_in_document(claim_text, cited, evidence):
            results.append(result("unsupported", "negated_statement_not_verifiable"))
        elif _overlap(claim_text, "\n".join(f"{evidence[c].title}\n{evidence[c].content}" for c in cited)) >= MIN_NARRATIVE_OVERLAP:
            results.append(result("cited", "narrative_not_machine_checked"))
        else:
            results.append(result("unsupported", "wording_not_found_in_evidence"))
    return results


def reply_status(claims: list[VerifiedClaim], evidence_count: int) -> ReplyStatus:
    backed = sum(c.status in ("verified", "cited") for c in claims)
    if evidence_count == 0 or backed == 0:
        return "insufficient_evidence"
    return "verified" if all(c.status == "verified" for c in claims) else "partially_verified"
