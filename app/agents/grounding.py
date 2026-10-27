"""Deterministic checks that cited evidence can support the claim it is attached to.

These checks are structural, not semantic:
- a metric sample within its series' normal range (``no_change``) cannot support a claimed change.
  It is removed from support, or moved to contradicting when the hypothesis claims an increase (the
  trigger is a fixed keyword list, ignoring increases negated within their clause, such as "did not
  rise"; so this part is a heuristic);
- a claimed increase in load or traffic cannot rest on symptom metrics (latency, errors) alone when the
  request rate is shown flat: those citations are removed from support and the flat request-rate sample is
  added as contradicting evidence (run 3 of EVAL-01 supported "increased load" with latency only);
- a dependency edge or past incident is contextual (see app.agents.evidence) and never counts as
  direct support, so it cannot raise confidence on its own.
Whether a direct observation supports the *specific* causal claim remains a semantic judgement
made by the model (reasoning and critic); it is not decided here.
"""

import re
from typing import Literal

from app.agents.evidence import EvidenceClass, RefIndex
from app.schemas.common import StrictModel
from app.schemas.evidence import EvidenceSource

INCREASE_CLAIM = re.compile(
    r"\b(increas\w*|spik\w*|surg\w*|rise|rising|rose|grow\w*|higher|elevat\w*|load|traffic|volume|burst\w*|more requests)\b",
    re.IGNORECASE,
)
# A clause ends at punctuation or a contrasting conjunction; negation never reaches across one.
CLAUSE_BREAK = re.compile(r"[,;:.!?()]|\b(?:but|yet|although|though|however|while|whereas)\b", re.IGNORECASE)
NEGATORS = frozenset({"not", "no", "never", "without", "neither", "nor", "none", "nothing"})
NOT_NEGATIONS = frozenset({("not", "only"), ("no", "doubt"), ("without", "doubt"), ("no", "question")})
NEGATION_REACH = 4  # words before the increase word, within its clause
LOAD_CLAIM = re.compile(r"\b(load|traffic|demand|throughput|request rate|requests|volume)\b", re.IGNORECASE)
REQUEST_RATE_METRIC = re.compile(r"request|rps|throughput|traffic", re.IGNORECASE)


def words_before(text: str, position: int, n: int) -> list[str]:
    """The last ``n`` words before ``position`` in the same clause, lower-cased."""
    clause = CLAUSE_BREAK.split(text[:position])[-1]
    return re.findall(r"[a-z']+", clause.lower())[-n:]


def _negated(words: list[str]) -> bool:
    for i, w in enumerate(words):
        following = words[i + 1] if i + 1 < len(words) else None
        if (w in NEGATORS or w.endswith("n't")) and (w, following) not in NOT_NEGATIONS:
            return True
    return False


def asserts_increase(text: str) -> bool:
    """True if the text claims an increase that it does not negate ("did not rise" is not a claim)."""
    return any(not _negated(words_before(text, m.start(), NEGATION_REACH)) for m in INCREASE_CLAIM.finditer(text))


class CitationCheck(StrictModel):
    hypothesis_id: str
    ref: str
    evidence_class: EvidenceClass
    action: Literal["moved_to_contradicting", "removed_from_support", "added_to_contradicting"]
    detail: str


def check_citations(
    hypothesis_id: str, claim_text: str, supporting: list[str], contradicting: list[str], refs: RefIndex
) -> tuple[list[str], list[str], list[CitationCheck]]:
    """Return (supporting, contradicting, changes) after removing support that cannot support anything."""
    kept, moved, checks = [], list(contradicting), []
    claims_increase = asserts_increase(claim_text)
    for ref in supporting:
        c = refs.classify(ref)
        if c.evidence_class is not EvidenceClass.NO_CHANGE:
            kept.append(ref)
            continue
        if claims_increase:
            if ref not in moved:
                moved.append(ref)
            checks.append(CitationCheck(
                hypothesis_id=hypothesis_id, ref=ref, evidence_class=c.evidence_class, action="moved_to_contradicting",
                detail=f"cited as support for a claimed increase, but {c.reason}"))
        else:
            checks.append(CitationCheck(
                hypothesis_id=hypothesis_id, ref=ref, evidence_class=c.evidence_class, action="removed_from_support",
                detail=f"shows no change, so it cannot support the hypothesis: {c.reason}"))
    if claims_increase and LOAD_CLAIM.search(claim_text):
        kept, moved = _check_load_claim(hypothesis_id, kept, moved, checks, refs)
    return kept, moved, checks


def flat_request_rate(refs: RefIndex) -> str | None:
    """The highest request-rate sample if every request-rate sample shows no change, else None (rising or unknown)."""
    samples = [(r["value"], ref) for ref, src, r in refs.records()
               if src is EvidenceSource.METRICS and REQUEST_RATE_METRIC.search(str(r.get("metric_name", "")))]
    classes = {ref: refs.classify(ref).evidence_class for _, ref in samples}
    if not samples or any(c is not EvidenceClass.NO_CHANGE for c in classes.values()):
        return None
    return max(samples)[1]


def _check_load_claim(hypothesis_id, kept, moved, checks, refs):
    """Symptom metrics (latency, errors) do not show load; with a flat request rate they cannot support it."""
    flat = flat_request_rate(refs)
    if flat is None:
        return kept, moved
    why = refs.classify(flat).reason
    remaining = []
    for ref in kept:
        leaves = refs.direct_leaves([ref])
        metrics = [refs.record(leaf)[1] for leaf in leaves if refs.record(leaf)[0] is EvidenceSource.METRICS]
        if leaves and len(metrics) == len(leaves) and not any(REQUEST_RATE_METRIC.search(m["metric_name"]) for m in metrics):
            checks.append(CitationCheck(
                hypothesis_id=hypothesis_id, ref=ref, evidence_class=refs.classify(ref).evidence_class,
                action="removed_from_support",
                detail=f"shows a symptom, not load; the request rate was flat ({why})"))
        else:
            remaining.append(ref)
    if flat not in moved:
        moved = [*moved, flat]
        checks.append(CitationCheck(
            hypothesis_id=hypothesis_id, ref=flat, evidence_class=EvidenceClass.NO_CHANGE, action="added_to_contradicting",
            detail=f"the claim needs more requests, but the request rate was flat: {why}"))
    return remaining, moved
