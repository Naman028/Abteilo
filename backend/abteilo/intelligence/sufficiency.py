"""Information needs, evidence coverage and the answer outcome (Block D, D5; INT-03).

The planner states what the answer must establish (at most four needs). Coverage is decided from evidence
and verified claims only; retrieval scores never contribute. Two passes:

- before the answer: can the gathered evidence cover the need at all? If no need can be covered, the model
  is not asked to answer and the outcome is `insufficient`;
- after verification: a need is covered only by a claim that passed verification (verified or cited) and
  cites evidence of the kind that need requires.

Outcomes: answered (every need covered), partial (some), insufficient (none), conflicting (live records
disagree on a needed fact; both are shown, nothing is chosen).
"""

from dataclasses import dataclass
from typing import Literal

import re

from pydantic import BaseModel, ConfigDict, Field

from abteilo.ai.evidence import _STOPWORDS, CAUSAL_EDGE_KINDS, VerifiedClaim

MIN_SUBJECT_MATCH = 0.5          # share of a policy/open subject's word stems that must be found
NeedType = Literal["current_state", "quantity", "relationship", "causal", "policy", "open"]
Outcome = Literal["answered", "partial", "insufficient", "conflicting"]
MAX_NEEDS = 4

# Which evidence authorities can cover each need type. Changing figures (budgets, balances, pending counts) need
# structured record or aggregate evidence; a static documented figure (a leave entitlement) is a policy need.
_COVERING = {
    "current_state": {"record", "aggregate", "relationship"},   # relationship lines carry live re-read statuses
    "quantity": {"record", "aggregate"},
    "relationship": {"relationship"},
    "causal": {"relationship", "document"},
    "policy": {"document"},
    "open": {"record", "aggregate", "relationship", "document"},
}
_PHRASE = {
    "current_state": "the current state of", "quantity": "the figures for", "relationship": "how this is connected:",
    "causal": "the cause of", "policy": "what the documents say about", "open": "an answer to",
}
# Wording that explains why something is so. Used only to decide whether a backed claim addresses a causal
# need; whether a cause is *verified* is decided by the verifier (INT-04), not by this list.
_EXPLAINS = re.compile(r"\b(because|due to|caused by|as a result|owing to|blocked by|depends? on|depending on|"
                       r"waiting for|waits for|required before|requires?|needs?|held up by|weil|wegen|aufgrund|"
                       r"wartet auf|abhängig von)\b", re.I)
_TOKEN = re.compile(r"[a-zäöüß0-9]+")


class InformationNeed(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: NeedType
    subject: str = Field(min_length=1, max_length=120)


@dataclass(frozen=True)
class EvidenceView:
    """What coverage needs to know about one evidence item."""
    authority: str
    title: str
    content: str
    has_causal_edge: bool

    @property
    def text(self) -> str:
        return f"{self.title}\n{self.content}".lower()


@dataclass(frozen=True)
class Coverage:
    need: InformationNeed
    covered: bool
    evidence_ids: tuple[str, ...]


def describe(need: InformationNeed) -> str:
    return f"{_PHRASE[need.type]} {need.subject.replace('_', ' ')}".strip()


def _stems(subject: str) -> list[str]:
    """Subject words, split on anything that is not a letter or digit (pending_leave_requests -> pending, leave,
    requests), without stopwords, cut to a 5-character stem so plural and German inflection still match."""
    words = [w for w in _TOKEN.findall(subject.lower()) if len(w) >= 3 and w not in _STOPWORDS]
    return list(dict.fromkeys(w[:5] for w in words))


def _match(subject: str, text: str) -> float:
    stems = _stems(subject)
    return 1.0 if not stems else sum(s in text for s in stems) / len(stems)


def _kind_fits(need: InformationNeed, item: EvidenceView) -> bool:
    if item.authority not in _COVERING[need.type]:
        return False
    return not (need.type == "causal" and item.authority == "relationship" and not item.has_causal_edge)  # INT-04


def precheck(needs: list[InformationNeed], evidence: dict[str, EvidenceView]) -> list[Coverage]:
    """Could anything gathered cover the need? Only the kind of source is checked here; whether the evidence
    actually answers is decided after verification. If nothing could, the model is not asked to answer."""
    return [Coverage(n, bool(ids), ids) for n in needs
            for ids in [tuple(eid for eid, item in evidence.items() if _kind_fits(n, item))]]


_GERMAN = re.compile(r"[äöüß]|\b(wie|was|wer|wann|welche[rsn]?|gibt|ist|sind|kann|darf|der|die|das|ein|eine|für|"
                     r"ich|wir|bekommen|zuschuss)\b", re.I)


_ASKING = re.compile(r"\b(how|who|what|when|where|which|why|can|could|would|should|get|gets|does|do|did|is|are|"
                     r"there|any|tell|me|please)\b", re.I)


def _asks_in_english(question: str) -> bool:
    return not _GERMAN.search(question)


def _question_match(question: str, text: str) -> float:
    """Share of the question's own key terms (asking words removed) found in the cited sources."""
    return _match(_ASKING.sub(" ", question), text)


def postcheck(needs: list[InformationNeed], evidence: dict[str, EvidenceView], claims: list[VerifiedClaim],
              question: str = "") -> list[Coverage]:
    """A need is covered by a backed (verified or cited) statement that cites a fitting source and is about the
    need's subject. For policy and open needs, half of the subject's word stems must appear in the statement or
    its source, so a true sentence from an unrelated passage is not an answer.

    The planner may reword a need's subject into something generic ("purchase request process" for "How do I
    order business cards?"), so for policy and open needs the cited sources must also contain half of the
    question's own key terms (I4, 2026-10-23). Only for questions asked in the language of the sources: a German
    question answered from an English policy is judged by the planner's subject alone."""
    check_question = bool(question) and _asks_in_english(question)
    backed = [c for c in claims if c.status in ("verified", "cited")]
    result = []
    for n in needs:
        ids = []
        for c in backed:
            if n.type == "causal" and not _EXPLAINS.search(c.text):
                continue
            cited = [e for e in c.evidence_ids if e in evidence and _kind_fits(n, evidence[e])]
            if not cited:
                continue
            text = c.text.lower() + "\n" + "\n".join(evidence[e].text for e in cited)
            needed = MIN_SUBJECT_MATCH if n.type in ("policy", "open") else 1e-9
            if _match(n.subject, text) < needed:
                continue
            if n.type in ("policy", "open") and check_question and \
                    _question_match(question, "\n".join(evidence[e].text for e in cited)) < MIN_SUBJECT_MATCH:
                continue
            ids += cited
        result.append(Coverage(n, bool(ids), tuple(dict.fromkeys(ids))))
    return result


def outcome(coverage: list[Coverage], claims: list[VerifiedClaim]) -> Outcome:
    if any(c.status == "conflicting" for c in claims):
        return "conflicting"
    covered = sum(c.covered for c in coverage)
    if covered and covered == len(coverage):
        return "answered"
    return "partial" if covered else "insufficient"


def default_needs(question: str) -> list[InformationNeed]:
    return [InformationNeed(type="open", subject=question.strip()[:120] or "question")]


def view(authority: str, title: str, content: str, edge_kinds: set[str]) -> EvidenceView:
    return EvidenceView(authority, title, content, bool(edge_kinds & CAUSAL_EDGE_KINDS))
