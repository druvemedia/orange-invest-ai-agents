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
  direct support, so it cannot raise confidence on its own;
- a hypothesis that names a retrieved change by its id (not negated) cites that change record as support. The
  record shows the change happened, not that it caused anything; the timeline checks still apply (run 4 of EVAL-01
  named CHG-E01-2 in its title but cited the record under another hypothesis);
- a failure or warning log that shares at least two mechanism terms with the hypothesis is not counted against it.
  It is set aside, not moved to support (run 4 cited "Timed out waiting for DB connection from pool" against a
  pool-size hypothesis);
- evidence cited both for and against the same hypothesis counts as neither;
- a cited metric sample that moved opposite to a claimed increase of that metric is moved to contradicting.
Whether a direct observation supports the *specific* causal claim, decreases, magnitudes and every other judgement
of meaning remain with the model (reasoning and critic); they are not decided here. ``assess_citations`` gives a
structured, per-citation assessment of what code can establish.
"""

import re
from typing import Literal

from app.agents.evidence import EvidenceClass, RefIndex
from app.schemas.common import StrictModel
from app.schemas.evidence import EvidenceSource

SYMPTOM_LOG_LEVELS = frozenset({"WARNING", "ERROR", "CRITICAL"})
MECHANISM_OVERLAP = 2  # shared content words for a failure log to count as describing the hypothesis's mechanism
# Words too generic to show a shared mechanism.
GENERIC_WORDS = frozenset({
    "the", "and", "for", "from", "with", "after", "before", "was", "were", "are", "has", "had", "not", "but", "into",
    "error", "errors", "failed", "failure", "failures", "failing", "issue", "issues", "service", "services",
    "request", "requests", "due", "caused", "cause", "causing", "incident", "this", "that", "than", "then", "its",
})
UNIT_WORDS = frozenset({"percent", "ms", "rps", "p95", "p99", "per", "min", "count", "total"})
CHANGE_ID = re.compile(r"\b[A-Za-z][A-Za-z0-9]*-[A-Za-z0-9][A-Za-z0-9-]*\b")

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
    action: Literal["moved_to_contradicting", "removed_from_support", "added_to_contradicting", "added_to_support",
                    "contradiction_set_aside", "removed_cited_both_ways"]
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
    kept, moved = _verify(hypothesis_id, claim_text, kept, moved, checks, refs, claims_increase)
    return kept, moved, checks


def _content_words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z][a-z0-9]{2,}", text.lower()) if w not in GENERIC_WORDS}


def _metric_phrase(metric_name: str) -> str:
    """'latency_p95_ms' -> 'latency'; 'error_rate_percent' -> 'error rate'."""
    return " ".join(w for w in metric_name.lower().split("_") if w not in UNIT_WORDS)


def named_changes(claim_text: str, refs: RefIndex) -> dict[str, str]:
    """Retrieved change records the claim names by id without negating them: {change_id: ref}."""
    by_id = {str(r.get("change_id")): ref for ref, src, r in refs.records() if src is EvidenceSource.CHANGES}
    named = {}
    for m in CHANGE_ID.finditer(claim_text):
        if m.group(0) in by_id and not _negated(words_before(claim_text, m.start(), NEGATION_REACH)):
            named[m.group(0)] = by_id[m.group(0)]
    return named


def _leaf_records(refs: RefIndex, ref: str) -> list[tuple[EvidenceSource | None, dict]]:
    return [found for leaf in (refs.direct_leaves([ref]) or [ref]) if (found := refs.record(leaf))]


def _verify(hypothesis_id, claim_text, kept, moved, checks, refs, claims_increase):
    """The run 3 and run 4 rules: named changes, mechanism-consistent logs, both-ways citations, metric direction."""
    def note(ref, action, detail):
        checks.append(CitationCheck(hypothesis_id=hypothesis_id, ref=ref, evidence_class=refs.classify(ref).evidence_class,
                                    action=action, detail=detail))

    for ref in [r for r in kept if r in moved]:
        kept = [r for r in kept if r != ref]
        moved = [r for r in moved if r != ref]
        note(ref, "removed_cited_both_ways", "cited both for and against the hypothesis, so it is counted as neither")

    cited = set(refs.direct_leaves(kept)) | set(kept) | set(moved)
    for change_id, ref in named_changes(claim_text, refs).items():
        if ref not in cited:
            kept = [*kept, ref]
            note(ref, "added_to_support", f"the hypothesis names {change_id}, so its change record is cited; it shows "
                                          "the change happened, not that it caused anything")

    mechanism = _content_words(claim_text)
    for ref in list(moved):
        records = _leaf_records(refs, ref)
        logs = [r for src, r in records if src is EvidenceSource.LOGS and r.get("level") in SYMPTOM_LOG_LEVELS]
        if records and len(logs) == len(records) and all(
                len(mechanism & _content_words(str(r.get("message", "")))) >= MECHANISM_OVERLAP for r in logs):
            moved = [r for r in moved if r != ref]
            note(ref, "contradiction_set_aside", "a failure log describing the hypothesis's own mechanism is not "
                                                 "counted against it; code does not count it as support either")

    if claims_increase:
        claim = claim_text.lower()
        for ref in list(kept):
            found = refs.record(ref)
            delta = refs.metric_delta(ref)
            if found and found[0] is EvidenceSource.METRICS and delta is not None and delta < 0 \
                    and refs.classify(ref).evidence_class is EvidenceClass.DIRECT \
                    and _metric_phrase(found[1]["metric_name"]) in claim:
                kept = [r for r in kept if r != ref]
                moved = [*moved, ref]
                note(ref, "moved_to_contradicting", f"the hypothesis claims an increase in "
                                                    f"{_metric_phrase(found[1]['metric_name'])}, but this sample fell "
                                                    f"({delta:+.0%} from its baseline)")
    return kept, moved


class EvidenceAssessment(StrictModel):
    """What code can establish about one citation. Judgements of meaning stay with the model."""

    hypothesis_id: str
    ref: str
    role: Literal["supporting", "contradicting"]
    exists: bool
    evidence_class: EvidenceClass | None
    sources: list[str]
    services: list[str]
    service_relevant: bool | None  # None when no cited record names a service
    flags: list[str]  # unknown_ref, missing_evidence, no_change_metric, contextual_only, off_service

    @property
    def unsupported(self) -> bool:
        """A supporting citation that cannot support anything code can verify."""
        return self.role == "supporting" and bool(
            {"unknown_ref", "missing_evidence", "no_change_metric", "off_service"} & set(self.flags))


def relevant_services(claim_text: str, refs: RefIndex, affected_services) -> set[str]:
    """Affected services and services the claim names, plus their direct (one-hop) dependency neighbours."""
    known, edges = set(), []
    for _, src, r in refs.records():
        for key in ("service", "depends_on"):
            if r.get(key):
                known.add(str(r[key]))
        if src is EvidenceSource.DEPENDENCIES and r.get("service") and r.get("depends_on"):
            edges.append((str(r["service"]), str(r["depends_on"])))
    claim = claim_text.lower()
    relevant = set(affected_services) | {s for s in known if s.lower() in claim}
    return relevant | {b for a, b in edges if a in relevant} | {a for a, b in edges if b in relevant}


def assess_citations(
    hypothesis_id: str, claim_text: str, supporting: list[str], contradicting: list[str], refs: RefIndex,
    affected_services=(),
) -> list[EvidenceAssessment]:
    """A structured assessment of every citation of one hypothesis (no citation is changed)."""
    relevant = relevant_services(claim_text, refs, affected_services)
    # A cited change on a relevant service makes evidence from its own service relevant too; a change is itself judged
    # only against the relevant services, so a change elsewhere cannot vouch for itself.
    relevant_for_records = relevant | {str(r["service"]) for ref in supporting for src, r in _leaf_records(refs, ref)
                                       if src is EvidenceSource.CHANGES and str(r.get("service")) in relevant}
    out = []
    for role, cited in (("supporting", supporting), ("contradicting", contradicting)):
        for ref in cited:
            exists = ref in refs
            c = refs.classify(ref) if exists else None
            records = _leaf_records(refs, ref) if exists else []
            services = sorted({str(r["service"]) for _, r in records if r.get("service")})
            flags = []
            if not exists:
                flags.append("unknown_ref")
            elif c.evidence_class is EvidenceClass.MISSING:
                flags.append("missing_evidence")
            elif c.evidence_class is EvidenceClass.NO_CHANGE:
                flags.append("no_change_metric")
            elif c.evidence_class is EvidenceClass.CONTEXTUAL:
                flags.append("contextual_only")
            is_change = any(src is EvidenceSource.CHANGES for src, _ in records)
            scope = relevant if is_change else relevant_for_records
            service_relevant = (any(s in scope for s in services) if services and scope else None)
            if service_relevant is False:
                flags.append("off_service")
            out.append(EvidenceAssessment(
                hypothesis_id=hypothesis_id, ref=ref, role=role, exists=exists,
                evidence_class=c.evidence_class if c else None,
                sources=sorted({s.value for s, _ in records if s}), services=services,
                service_relevant=service_relevant, flags=flags))
    return out


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
