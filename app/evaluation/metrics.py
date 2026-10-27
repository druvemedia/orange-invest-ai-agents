"""Scoring. Every metric is defined in docs/EVALUATION.md; the docstrings below are the exact rules."""

import json
import re
import statistics
from typing import Literal

from app.evaluation.dataset import EvidenceMatcher, GoldLabel, Pattern, RootCauseLabel
from app.evaluation.normalize import UNUSABLE, NormalizedHypothesis, RunRecord
from app.schemas.common import StrictModel

Outcome = Literal["correct", "correct_inconclusive", "acceptable_inconclusive", "missed_inconclusive", "wrong_conclusion",
                  "failed", "not_run"]

STOPWORDS = frozenset(
    "the a an and or of to in on for with from by at is was were be been are this that these those it its as "
    "after before than then into over under not no only most more less due caused cause causing because "
    "error errors failure failed incident service services change changes".split()
)


def matches(text: str, match_all: list[list[str]]) -> bool:
    """True if, for every group, at least one of its terms occurs in ``text`` (case-insensitive substring)."""
    lowered = text.lower()
    return all(any(term.lower() in lowered for term in group) for group in match_all)


def hypothesis_text(h: NormalizedHypothesis) -> str:
    return f"{h.title} {h.assessment}"


def matched_label(h: NormalizedHypothesis, label: GoldLabel) -> RootCauseLabel | None:
    for candidate in ([label.root_cause] if label.root_cause else []) + label.acceptable_alternatives:
        if matches(hypothesis_text(h), candidate.match_all):
            return candidate
    return None


class Grounding(StrictModel):
    cited: int  # references cited by ranked hypotheses (support + contradiction) and facts
    existing: int  # ...that resolve to evidence collected in this run
    available: int  # ...that resolve to usable evidence (not a failed / never-collected result)
    supporting_records: int  # supporting refs that resolve to at least one record
    lexically_supporting: int  # ...whose record text shares a content word with the hypothesis


class CaseScore(StrictModel):
    case_id: str
    approach: str
    outcome: Outcome
    strict_correct: bool
    lenient_correct: bool
    matched_label: str | None  # gold or alternative label id matched by the top hypothesis
    top1_match: bool | None  # None when the case has no gold root cause
    top3_match: bool | None
    distractor_top1: bool
    must_consider_coverage: float | None
    key_evidence_recall: float | None
    grounding: Grounding


def score(run: RunRecord, label: GoldLabel) -> CaseScore:
    hyps = run.hypotheses
    top = hyps[0] if hyps else None
    top_label = matched_label(top, label) if top else None
    has_gold = label.root_cause is not None

    if run.status == "not_run":
        outcome: Outcome = "not_run"
    elif run.status == "failed":
        outcome = "failed"
    elif label.expected_outcome == "inconclusive":
        outcome = "correct_inconclusive" if run.conclusion == "inconclusive" else "wrong_conclusion"
    elif run.conclusion == "probable_cause_identified":
        outcome = "correct" if top_label else "wrong_conclusion"
    else:
        outcome = "acceptable_inconclusive" if label.inconclusive_acceptable else "missed_inconclusive"

    return CaseScore(
        case_id=run.case_id,
        approach=run.approach,
        outcome=outcome,
        strict_correct=outcome in ("correct", "correct_inconclusive"),
        lenient_correct=outcome in ("correct", "correct_inconclusive", "acceptable_inconclusive"),
        matched_label=top_label.label_id if top_label else None,
        top1_match=(top_label is not None) if has_gold else None,
        top3_match=any(matched_label(h, label) for h in hyps[:3]) if has_gold else None,
        distractor_top1=bool(top) and top_label is None and any(matches(hypothesis_text(top), d.match_all) for d in label.distractors),
        must_consider_coverage=_coverage(hyps, label.must_consider),
        key_evidence_recall=_key_evidence_recall(top, top_label or label.root_cause, run) if has_gold else None,
        grounding=grounding(run),
    )


def _coverage(hyps: list[NormalizedHypothesis], patterns: list[Pattern]) -> float | None:
    if not patterns:
        return None
    found = sum(1 for p in patterns if any(matches(hypothesis_text(h), p.match_all) for h in hyps))
    return found / len(patterns)


def resolve_records(ref: str, run: RunRecord, depth: int = 0) -> list[str]:
    """Record texts a reference ultimately points to (findings are followed to their evidence)."""
    info = run.evidence.get(ref)
    if info is None or depth > 3:
        return []
    if info.kind == "record":
        return [info.text]
    if info.kind == "finding":
        return [t for r in info.resolves_to for t in resolve_records(r, run, depth + 1)]
    return []


def _is_available(ref: str, run: RunRecord, depth: int = 0) -> bool:
    info = run.evidence.get(ref)
    if info is None or depth > 3:
        return False
    if info.kind == "finding":
        return bool(info.resolves_to) and all(_is_available(r, run, depth + 1) for r in info.resolves_to)
    return info.outcome not in UNUSABLE


def _content_words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z][a-z0-9_.-]{3,}", text.lower()) if w not in STOPWORDS}


def grounding(run: RunRecord) -> Grounding:
    cited = [r for h in run.hypotheses for r in h.supporting_refs + h.contradicting_refs] + run.fact_refs
    supporting_records = lexical = 0
    for h in run.hypotheses:
        words = _content_words(hypothesis_text(h))
        for ref in h.supporting_refs:
            texts = resolve_records(ref, run)
            if texts:
                supporting_records += 1
                if any(words & _content_words(t) for t in texts):
                    lexical += 1
    return Grounding(
        cited=len(cited),
        existing=sum(1 for r in cited if r in run.evidence),
        available=sum(1 for r in cited if _is_available(r, run)),
        supporting_records=supporting_records,
        lexically_supporting=lexical,
    )


def _key_evidence_recall(top: NormalizedHypothesis | None, label: RootCauseLabel | None, run: RunRecord) -> float | None:
    if label is None or not label.key_evidence:
        return None
    if top is None:
        return 0.0
    records = [json.loads(t) for r in top.supporting_refs for t in resolve_records(r, run)]
    hit = sum(1 for m in label.key_evidence if any(_record_matches(rec, m) for rec in records))
    return hit / len(label.key_evidence)


def _record_matches(record: dict, m: EvidenceMatcher) -> bool:
    value = str(record.get(m.field, ""))
    if m.equals is not None:
        return value == m.equals
    return m.contains is not None and m.contains.lower() in value.lower()


# --- Aggregates ---------------------------------------------------------------------------------------


def _rate(values: list[bool]) -> float | None:
    return round(sum(values) / len(values), 3) if values else None


def _count(values: list[bool]) -> dict:
    """Numerator and denominator, so a rate is never shown without what it was computed over."""
    return {"n": sum(values), "of": len(values)}


def _mean(values: list[float]) -> float | None:
    return round(statistics.fmean(values), 3) if values else None


def aggregate(runs: list[RunRecord], scores: list[CaseScore]) -> dict:
    by_case = {s.case_id: s for s in scores}
    ok_runs = [r for r in runs if r.status != "failed"]
    g = [s.grounding for s in scores]
    cited = sum(x.cited for x in g)
    latencies = [r.latency_seconds for r in runs]
    tokens_known = [r for r in runs if r.input_tokens is not None]
    priced = [r for r in runs if r.estimated_cost_usd is not None]
    gold = [s for s in scores if s.top1_match is not None]
    assessed = sum(r.assessed_supporting_refs for r in runs)
    unsupported = sum(r.unsupported_supporting_refs for r in runs)
    return {
        "cases": len(runs),
        "runs_completed": sum(1 for r in runs if r.status in ("completed", "fallback")),
        "runs_not_run": sum(1 for r in runs if r.status == "not_run"),
        "counts": {
            "strict": _count([s.strict_correct for s in scores]),
            "lenient": _count([s.lenient_correct for s in scores]),
            "wrong_conclusion": _count([s.outcome == "wrong_conclusion" for s in scores]),
            "failed": _count([s.outcome == "failed" for s in scores]),
            "inconclusive": _count([s.outcome.endswith("inconclusive") for s in scores]),
            "top1": _count([bool(s.top1_match) for s in gold]),
            "top3": _count([bool(s.top3_match) for s in gold]),
        },
        "unsupported_support": {"unsupported": unsupported, "assessed": assessed,
                                "rate": round(unsupported / assessed, 3) if assessed else None},
        "evidence_gaps_total": sum(r.evidence_gaps for r in runs),
        "timeouts_total": sum(r.timeouts for r in runs),
        "cost_usd": ({"runs_priced": len(priced), "total": round(sum(r.estimated_cost_usd for r in priced), 4),
                      "mean": round(statistics.fmean(r.estimated_cost_usd for r in priced), 4)} if priced else None),
        "strict_accuracy": _rate([s.strict_correct for s in scores]),
        "lenient_accuracy": _rate([s.lenient_correct for s in scores]),
        "wrong_conclusion_rate": _rate([s.outcome == "wrong_conclusion" for s in scores]),
        "failure_rate": _rate([s.outcome == "failed" for s in scores]),
        "outcomes": {o: sum(1 for s in scores if s.outcome == o) for o in Outcome.__args__},
        "top1_hit_rate": _rate([bool(s.top1_match) for s in gold]),
        "top3_hit_rate": _rate([bool(s.top3_match) for s in gold]),
        "distractor_top1_rate": _rate([s.distractor_top1 for s in gold]),
        "mean_must_consider_coverage": _mean([s.must_consider_coverage for s in scores if s.must_consider_coverage is not None]),
        "mean_key_evidence_recall": _mean([s.key_evidence_recall for s in scores if s.key_evidence_recall is not None]),
        "grounding": {
            "cited_refs": cited,
            "existence_rate": round(sum(x.existing for x in g) / cited, 3) if cited else None,
            "availability_rate": round(sum(x.available for x in g) / cited, 3) if cited else None,
            "lexical_support_rate": (round(sum(x.lexically_supporting for x in g) / sum(x.supporting_records for x in g), 3)
                                     if sum(x.supporting_records for x in g) else None),
        },
        "tool_calls": {"mean": _mean([r.tool_calls for r in ok_runs]), "total": sum(r.tool_calls for r in runs)},
        "model_calls": {"mean": _mean([r.model_calls for r in runs]), "total": sum(r.model_calls for r in runs)},
        "invalid_outputs_total": sum(r.invalid_outputs for r in runs),
        "latency_seconds": {"mean": _mean(latencies), "median": round(statistics.median(latencies), 3) if latencies else None,
                            "max": max(latencies) if latencies else None},
        "tokens": ({"runs_with_usage": len(tokens_known),
                    "input_total": sum(r.input_tokens for r in tokens_known),
                    "output_total": sum(r.output_tokens or 0 for r in tokens_known),
                    "input_mean": _mean([r.input_tokens for r in tokens_known]),
                    "output_mean": _mean([r.output_tokens or 0 for r in tokens_known])}
                   if tokens_known else None),
        "per_case_outcome": {cid: s.outcome for cid, s in sorted(by_case.items())},
    }
