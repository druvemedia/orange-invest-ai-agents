"""Final adjudication: may the report name a probable cause? Deterministic and fully recorded.

The critic's verdict is an input, not the decision. A cause is accepted only when every check passes:

  review_completed        multi-agent only: at least one critic review finished
  hypotheses_available    reasoning produced hypotheses and did not mark the evidence insufficient
  leader_confident        the leading hypothesis (highest confidence after code caps) is at least medium
  leader_clearly_ahead    no other hypothesis has the same or higher confidence
  direct_support          the leader's direct, incident-specific support is independently corroborated:
                          observations from >= 2 different evidence sources after merging records of the
                          same event (app.agents.independence); topology, past incidents, interpretations and
                          unchanged metrics do not count
  no_material_contradiction  direct evidence cited against the leader does not span as many independent
                          sources as its direct support, and no critic contradiction or alternative-explanation
                          issue about the leader cites direct evidence. Weaker contradictions are kept and shown:
                          listing counter-evidence must not be penalised.
  no_credible_alternative no other hypothesis has direct support from >= 2 independent sources
  follow_ups_obtained     every follow-up query the critic requested ran (no failure, timeout or skip)
  temporal_consistency    the leader's cited change(s) can explain the onset (app.agents.temporal): not all of
                          them follow the first symptom or persisted after a rollback. Unknown or ambiguous
                          timing passes, and is reported as such

Critic issues that cite no direct evidence (e.g. "missing evidence", "overconfidence") are listed as
unresolved in the report but do not block on their own: a critic still saying "revise" at its round
limit cannot by itself force an inconclusive result, and cannot by itself force acceptance either.
"""

from app.agents.contracts import (
    CONFIDENCE_ORDER,
    Adjudication,
    AdjudicationCheck,
    CriticResult,
    EvidenceInventory,
    Hypothesis,
    TemporalAssessment,
)
from app.agents.evidence import RefIndex
from app.agents.independence import assess_independence

BLOCKING_ISSUE_TYPES = frozenset({"contradiction", "alternative_explanation"})


def adjudicate(
    hypotheses: list[Hypothesis],
    insufficient: bool,
    refs: RefIndex,
    inventory: EvidenceInventory,
    *,
    final_critique: CriticResult | None,
    critiques: list[CriticResult] | None,
    critic_expected: bool,
    timelines: list[TemporalAssessment] | None = None,
) -> Adjudication:
    """``final_critique`` must be the review of the final reasoning round; ``critiques`` are all rounds."""
    checks: list[AdjudicationCheck] = []

    def check(name: str, passed: bool, detail: str) -> None:
        checks.append(AdjudicationCheck(name=name, passed=passed, detail=detail))

    critiques = critiques or []
    if critic_expected:
        check("review_completed", final_critique is not None,
              f"critic round {final_critique.round} finished with verdict '{final_critique.final_verdict}'"
              if final_critique else "no critic review of the final reasoning finished")

    ranked = sorted(hypotheses, key=lambda h: -CONFIDENCE_ORDER[h.confidence])
    leader = ranked[0] if ranked else None
    check("hypotheses_available", bool(ranked) and not insufficient,
          "reasoning marked the evidence insufficient" if insufficient else
          (f"{len(ranked)} hypothesis(es)" if ranked else "no hypothesis was produced"))

    unresolved: list[str] = []
    if leader is not None:
        others = ranked[1:]
        check("leader_confident", CONFIDENCE_ORDER[leader.confidence] >= CONFIDENCE_ORDER["medium"],
              f"{leader.hypothesis_id} confidence {leader.confidence} (after code caps)")
        ties = [h.hypothesis_id for h in others if CONFIDENCE_ORDER[h.confidence] >= CONFIDENCE_ORDER[leader.confidence]]
        check("leader_clearly_ahead", not ties,
              f"{', '.join(ties)} as confident as {leader.hypothesis_id}" if ties else "every alternative is less confident")

        support = assess_independence(leader.supporting_refs, refs)
        check("direct_support", support.status == "independent", f"independence {support.status}: {support.detail}")
        direct_sources = {g.primary_source for g in support.groups}

        direct_against = [r for r in leader.contradicting_refs if refs.is_direct_evidence(r)]
        against_sources = {g.primary_source for g in assess_independence(direct_against, refs).groups}
        material = bool(direct_against) and len(against_sources) >= len(direct_sources)
        critic_against = [
            f"{i.issue_id} ({i.issue_type}) citing {', '.join(r for r in i.evidence_refs if refs.is_direct_evidence(r))}"
            for i in (final_critique.output.issues if final_critique else [])
            if i.hypothesis_id == leader.hypothesis_id and i.issue_type in BLOCKING_ISSUE_TYPES
            and any(refs.is_direct_evidence(r) for r in i.evidence_refs)
        ]
        if material or critic_against:
            detail = "; ".join(filter(None, [
                f"direct evidence against {leader.hypothesis_id} from {len(against_sources)} source(s) "
                f"({', '.join(direct_against)}) is as broad as its direct support" if material else "",
                f"critic: {'; '.join(critic_against)}" if critic_against else "",
            ]))
        elif direct_against:
            detail = (f"weaker contradicting evidence retained ({', '.join(direct_against)}: {len(against_sources)} source(s) "
                      f"vs {len(direct_sources)} supporting)")
        else:
            detail = "no direct evidence contradicts the leader"
        check("no_material_contradiction", not material and not critic_against, detail)

        credible = [h.hypothesis_id for h in others if assess_independence(h.supporting_refs, refs).status == "independent"]
        check("no_credible_alternative", not credible,
              f"{', '.join(credible)} also has independently corroborated direct support" if credible
              else "no alternative has comparable direct support")

        timeline = next((t for t in timelines or [] if t.hypothesis_id == leader.hypothesis_id), None)
        if timeline is None:
            check("temporal_consistency", True, "not assessed")
        else:
            check("temporal_consistency", not timeline.blocks_cause, f"{timeline.status}: {timeline.detail}")

        unresolved = [f"{i.issue_id} ({i.issue_type}{', ' + i.hypothesis_id if i.hypothesis_id else ''}): {i.detail}"
                      for i in (final_critique.output.issues if final_critique and final_critique.final_verdict != "accept" else [])]

    requested = {t for c in critiques for t in c.accepted_requests}
    not_obtained = [f"{e.task_id} {e.status}" for e in inventory.entries
                    if e.task_id in requested and e.status in ("failed", "timed_out", "not_run")]
    check("follow_ups_obtained", not not_obtained,
          f"requested follow-up evidence not obtained: {', '.join(not_obtained)}" if not_obtained
          else f"{len(requested)} requested follow-up query(ies) ran" if requested else "no follow-up queries were requested")

    accepted = all(c.passed for c in checks)
    verdict = final_critique.final_verdict if final_critique else None
    if not critic_expected:
        basis = "evidence sufficiency (single agent, no critic)"
    elif verdict == "accept":
        basis = "critic accepted; evidence sufficiency confirmed"
    else:
        basis = f"critic verdict '{verdict}' adjudicated on evidence sufficiency" if verdict else "no critic review"
    return Adjudication(
        decision="cause_accepted" if accepted else "inconclusive",
        leading_hypothesis=leader.hypothesis_id if leader else None,
        basis=basis,
        critic_verdict=verdict,
        checks=checks,
        blocking_issues=[f"{c.name}: {c.detail}" for c in checks if not c.passed],
        unresolved_critic_issues=unresolved,
    )
