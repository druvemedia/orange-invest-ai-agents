"""Code-enforced rules applied to every final report (multi-agent and single-agent alike).

Each rule records what it changed, so the report shows both what the model wrote and what was accepted.

1. The conclusion is the adjudication's decision (app.agents.adjudication), not the model's preference.
2. Report confidences never exceed the reasoning's (code-capped) confidences.
3. Report citations get the same checks as reasoning citations (app.agents.grounding).
4. Facts must cite evidence and must not make causal or inferential claims. Such statements move
   to interpretations (deterministic phrase list) and uncited ones move to assumptions. "Indicate" is
   inferential unless it reports what an evidence item shows ("the logs indicate timeouts"). A fact
   that claims an increase (not negated, see app.agents.grounding) its cited metrics do not show also
   moves to interpretations.
5. "Evidence was missing" claims are validated against the evidence inventory (app.agents.inventory).
   Limitations are the inventory's genuine gaps plus the model's surviving limitations. A model
   limitation restating the critic round limit is dropped when code already states it.
6. If the model lists no assumptions while a causal hypothesis exists, the material assumption that the
   causal link is inferred rather than observed is added and marked as added by code.
7. A change that came after the first observed symptom, or whose rollback did not end the symptoms
   (app.agents.temporal), gets an interpretation saying it cannot explain the onset. It says the change
   may have worsened the incident only when symptom metrics rose further after it.
"""

import re

from app.agents.contracts import (
    CONFIDENCE_ORDER,
    Adjudication,
    EvidenceInventory,
    Hypothesis,
    ReportOutput,
    Statement,
    TemporalAssessment,
)
from app.agents.evidence import EvidenceClass, RefIndex
from app.agents.grounding import asserts_increase, check_citations, words_before
from app.agents.inventory import validate_availability_claims

CAUSAL = re.compile(
    r"\b(caus\w*|due to|because|led to|leads? to|leading to|result(?:s|ed|ing)? in|trigger\w*|"
    r"suggest\w*|likely|probabl\w*|impl(?:y|ies|ied)|therefore|attribut\w*|explain\w*|contribut\w*)\b",
    re.IGNORECASE,
)
INDICATES = re.compile(r"\bindicat\w*", re.IGNORECASE)
# Nouns for evidence items. "<item> indicates X" reports what the item shows, like "the logs show X".
EVIDENCE_ITEMS = frozenset({
    "log", "logs", "line", "lines", "entry", "entries", "message", "messages", "error", "errors", "record",
    "records", "alert", "alerts", "event", "events", "metric", "metrics", "sample", "samples", "reading",
    "readings", "data", "trace", "traces",
})
EVIDENCE_REACH = 5  # words before "indicate", within its clause
CODE_ADDED = "(added by code) "
# Start of the limitation code adds when the critic loop stops at its round limit (app.agents.reporting).
ROUND_LIMIT_NOTE = "Critic round limit reached"
ROUND_LIMIT_RESTATED = re.compile(r"\bcritic\b.*\bround limit\b|\bround limit\b.*\bcritic\b", re.IGNORECASE)


def makes_causal_claim(text: str) -> bool:
    """True if the text uses causal or inferential language that a fact must not."""
    if CAUSAL.search(text):
        return True
    return any(not EVIDENCE_ITEMS.intersection(words_before(text, m.start(), EVIDENCE_REACH))
               for m in INDICATES.finditer(text))


def finalize_report(
    report: ReportOutput,
    *,
    hypotheses: list[Hypothesis],
    refs: RefIndex,
    inventory: EvidenceInventory,
    adjudication: Adjudication,
    known_limitations: list[str],
    allow_accept: bool = True,
    timelines: list[TemporalAssessment] = (),
) -> tuple[ReportOutput, list[str]]:
    notes: list[str] = []
    by_id = {h.hypothesis_id: h for h in hypotheses}

    # 1. Conclusion
    decided = "probable_cause_identified" if adjudication.decision == "cause_accepted" and allow_accept else "inconclusive"
    if report.conclusion != decided:
        notes.append(f"Conclusion set to {decided} by adjudication ({adjudication.basis}); the report model proposed "
                      f"{report.conclusion}.")

    # 2-3. Confidence ceilings and citation checks on the report's own hypotheses
    ranked = []
    for h in sorted(report.ranked_hypotheses, key=lambda h: h.rank):
        ceiling = by_id[h.hypothesis_id].confidence if h.hypothesis_id in by_id else "low"
        if CONFIDENCE_ORDER[h.confidence] > CONFIDENCE_ORDER[ceiling]:
            notes.append(f"{h.hypothesis_id}: report confidence {h.confidence} lowered to {ceiling}.")
            h = h.model_copy(update={"confidence": ceiling})
        supporting, contradicting, checks = check_citations(
            h.hypothesis_id, f"{h.title} {h.assessment}", h.supporting_refs, h.contradicting_refs, refs)
        notes += [f"{c.hypothesis_id}: {c.ref} {c.action.replace('_', ' ')} ({c.detail})" for c in checks]
        ranked.append(h.model_copy(update={"supporting_refs": supporting, "contradicting_refs": contradicting}))
    leader = adjudication.leading_hypothesis
    if decided == "probable_cause_identified" and ranked and ranked[0].hypothesis_id != leader:
        ranked.sort(key=lambda h: h.hypothesis_id != leader)
        notes.append(f"{leader} ranked first: it is the hypothesis the adjudication accepted.")
    ranked = [h.model_copy(update={"rank": i}) for i, h in enumerate(ranked, start=1)]

    # 4. Facts vs interpretations vs assumptions
    facts, interpretations, assumptions = [], list(report.interpretations), list(report.assumptions)
    for f in report.facts:
        if not f.evidence_refs:
            assumptions.append(f.text)
            notes.append(f"Uncited 'fact' moved to assumptions: {f.text}")
        elif makes_causal_claim(f.text):
            interpretations.append(f)
            notes.append(f"'Fact' with causal or inferential language moved to interpretations: {f.text}")
        elif asserts_increase(f.text) and all(
                refs.classify(r).evidence_class is EvidenceClass.NO_CHANGE for r in f.evidence_refs):
            interpretations.append(f)
            notes.append(f"'Fact' claims a change its cited metrics do not show; moved to interpretations: {f.text}")
        else:
            facts.append(f)

    # 4b. Timeline: a change after the first symptom cannot be presented as the cause of onset
    for t in timelines:
        for c in t.changes:
            if c.relation == "follows_onset":
                text = (f"{CODE_ADDED}{c.change_id} ({c.change_type}) at {c.time} came after the first observed symptom at "
                        f"{t.onset}; it cannot explain how the incident began.")
                if t.may_have_worsened:
                    text += " Symptom metrics rose further after it, so it may have worsened the existing incident."
                cited = [c.change_ref, *t.onset_refs, *t.worsening_refs]
            elif c.relation == "persisted_after_rollback":
                text = (f"{CODE_ADDED}{c.change_id} was rolled back ({c.rollback_ref}) but symptoms continued afterwards; "
                        "it does not explain the incident.")
                cited = [c.change_ref, c.rollback_ref, *c.persisted_refs[:3]]
            else:
                continue
            if text not in {i.text for i in interpretations}:
                interpretations.append(Statement(text=text, evidence_refs=[r for r in dict.fromkeys(cited) if r]))
                notes.append(f"Timeline check on {t.hypothesis_id}: {c.detail}")

    # 5. Evidence-availability claims
    limitations, removed = validate_availability_claims(list(report.limitations), inventory)
    if any(k.startswith(ROUND_LIMIT_NOTE) for k in known_limitations):
        restated = [lim for lim in limitations if ROUND_LIMIT_RESTATED.search(lim)]
        limitations = [lim for lim in limitations if lim not in restated]
        notes += [f"Removed limitation \"{lim}\": the critic round limit is already stated by code, with how its "
                  "remaining issues were adjudicated." for lim in restated]
    assumptions, removed_a = validate_availability_claims(assumptions, inventory)
    notes += removed + removed_a
    limitations = list(dict.fromkeys([*inventory.unavailable, *known_limitations, *limitations]))
    if adjudication.unresolved_critic_issues and decided == "probable_cause_identified":
        limitations.append(f"The critic still had {len(adjudication.unresolved_critic_issues)} unresolved issue(s); none "
                           "cited direct evidence against the accepted cause (see the adjudication).")

    # 6. Material assumptions
    leader_h = by_id.get(leader) if leader else None
    if not assumptions and leader_h is not None:
        assumptions.append(f"{CODE_ADDED}{leader_h.title}: the causal link is inferred from timing and correlated "
                           "observations in the cited evidence; it was not directly observed.")
        if inventory.unavailable:
            assumptions.append(f"{CODE_ADDED}The unavailable evidence listed under limitations is assumed not to change "
                               "the ranking.")
    would_confirm = list(report.would_confirm)
    if not would_confirm and leader_h is not None and leader_h.missing_information:
        would_confirm = [f"{CODE_ADDED}{m}" for m in leader_h.missing_information]

    final = report.model_copy(update={
        "conclusion": decided, "ranked_hypotheses": ranked, "facts": facts, "interpretations": interpretations,
        "assumptions": assumptions, "would_confirm": would_confirm, "limitations": limitations,
    })
    return final, notes
