"""Report wording checks: what counts as a causal claim, what counts as a claimed increase, and duplicate limitations.

Regressions from live run 2 of EVAL-01 (artifacts/live-runs/EVAL-01-run2): two descriptive facts were moved to
interpretations because they contained "indicate", one because "was not rapidly increasing" was read as a claimed
increase, and the critic's round limit was stated twice with contradicting glosses.
"""

import pytest

from app.agents.contracts import Statement
from app.agents.grounding import check_citations
from app.agents.inventory import build_inventory
from app.agents.report_rules import finalize_report
from tests.test_phase6_quality import decide, hyp, report, world  # noqa: F401  (world is a fixture)

RUN2_ROUND_LIMIT_CODE = ("Critic round limit reached with verdict 'revise'; its remaining issues were adjudicated on the "
                         "evidence (see the adjudication).")
RUN2_ROUND_LIMIT_MODEL = "Critic round limit reached with verdict 'revise'; remaining issues not resolved due to insufficient evidence."


def finalize(world, facts=(), limitations=(), known=()):
    sets, refs = world
    leader = hyp("H1", "medium", ["c#0", "l#0"], title="Pool size reduction")
    return finalize_report(report(facts=facts, limitations=limitations), hypotheses=[leader], refs=refs,
                           inventory=build_inventory(sets), adjudication=decide(world, [leader], critic_expected=False),
                           known_limitations=list(known))


# --- Causal and inferential wording in facts ----------------------------------------------------------------------

DESCRIPTIVE = [
    "Multiple error messages from 'payments-api' indicate timeouts waiting for a DB connection starting at 10:02 UTC.",
    "At 10:07 UTC, 'api-gateway' logged a 504 error indicating 'payments-api' was not responsive.",
    "The logs indicate timeouts at 10:02.",
    "Log entries from payments-api indicated pool waits.",
]
INFERENTIAL = [
    "Latency spikes indicate pool exhaustion.",  # no evidence item is reporting its own content
    "This indicates a capacity problem.",
    "The logs indicate the change caused the outage.",  # a causal verb is still caught
    "Pool saturation caused the timeouts.",
    "Errors rose due to the deploy.",
    "This suggests the config change is responsible.",
    "Timeouts likely followed the smaller pool.",
    "Errors appeared; this indicates exhaustion.",  # the evidence noun is in another clause
]


@pytest.mark.parametrize("text", DESCRIPTIVE)
def test_indicate_reporting_what_evidence_shows_stays_a_fact(world, text):
    out, notes = finalize(world, facts=[Statement(text=text, evidence_refs=["l#0"])])

    assert [f.text for f in out.facts] == [text], notes


@pytest.mark.parametrize("text", INFERENTIAL)
def test_causal_or_inferential_facts_still_move_to_interpretations(world, text):
    out, notes = finalize(world, facts=[Statement(text=text, evidence_refs=["l#0"])])

    assert out.facts == [] and [i.text for i in out.interpretations] == [text]
    assert any("causal or inferential language" in n for n in notes)


@pytest.mark.parametrize("text", DESCRIPTIVE)
def test_makes_causal_claim_table_descriptive(text):
    from app.agents.report_rules import makes_causal_claim

    assert not makes_causal_claim(text)


@pytest.mark.parametrize("text", INFERENTIAL)
def test_makes_causal_claim_table_inferential(text):
    from app.agents.report_rules import makes_causal_claim

    assert makes_causal_claim(text)


# --- Claimed increases and negation --------------------------------------------------------------------------------

NEGATED = [
    "Request rate did not rise.",
    "The request rate was relatively stable, peaking at 123.42 rps at 10:15 UTC, yet was not rapidly increasing "
    "prior to the incident.",
    "Request rate didn't increase.",
    "There was no increase in request rate.",
    "Request rate showed no significant spike.",
    "Request rate stayed flat without any rise.",
    "Neither request rate nor CPU increased.",
    "Request rate was never higher than 125 rps.",
]
CLAIMED = [
    "Request rate increased sharply.",
    "Traffic surged before the incident.",
    "Request rate rose to 123 rps.",
    "Not only did traffic rise, request rate also climbed.",  # "not only" is not a negation
    "No doubt the request rate increased.",
    "Latency did not rise, but request rate increased.",  # one claim negated, the other not
    "Request rate did not fall; it rose.",
    "Request rate was higher than usual.",
]


@pytest.mark.parametrize("text", NEGATED)
def test_negated_increase_on_a_flat_metric_stays_a_fact(world, text):
    out, notes = finalize(world, facts=[Statement(text=text, evidence_refs=["m.rate#8"])])

    assert [f.text for f in out.facts] == [text], notes


@pytest.mark.parametrize("text", CLAIMED)
def test_claimed_increase_on_a_flat_metric_still_moves(world, text):
    out, notes = finalize(world, facts=[Statement(text=text, evidence_refs=["m.rate#8"])])

    assert out.facts == [] and any("claims a change its cited metrics do not show" in n for n in notes), notes


@pytest.mark.parametrize(("text", "expected"), [*((t, False) for t in NEGATED), *((t, True) for t in CLAIMED)])
def test_asserts_increase_table(text, expected):
    from app.agents.grounding import asserts_increase

    assert asserts_increase(text) is expected


def test_flat_metric_is_not_turned_against_a_hypothesis_that_denies_an_increase(world):
    _, refs = world

    _, against, checks = check_citations("H3", "Request rate did not increase before the incident", ["m.rate#8"], [], refs)
    assert against == [] and checks[0].action == "removed_from_support"

    # Known limit: "load", "traffic" and "volume" are claims by themselves, so this still counts as one.
    _, against, _ = check_citations("H3", "Load did not increase before the incident", ["m.rate#8"], [], refs)
    assert "m.rate#8" in against  # (the load rule may also add the flat request-rate peak)

    _, against, checks = check_citations("H3", "Increased load on payments-api", ["m.rate#8"], [], refs)
    assert against[0] == "m.rate#8" and checks[0].action == "moved_to_contradicting"
    assert any(c.action == "added_to_contradicting" for c in checks)  # the flat request-rate peak (load rule)


# --- Duplicate limitations -----------------------------------------------------------------------------------------


def test_model_restatement_of_the_critic_round_limit_is_dropped_when_code_states_it(world):
    out, notes = finalize(world, limitations=[RUN2_ROUND_LIMIT_MODEL, "Only synthetic data was available."],
                          known=[RUN2_ROUND_LIMIT_CODE])

    assert RUN2_ROUND_LIMIT_MODEL not in out.limitations
    assert out.limitations.count(RUN2_ROUND_LIMIT_CODE) == 1 and "Only synthetic data was available." in out.limitations
    assert any("round limit" in n and "already stated" in n for n in notes)


def test_model_round_limit_statement_is_kept_when_code_states_nothing_about_it(world):
    out, _ = finalize(world, limitations=[RUN2_ROUND_LIMIT_MODEL])

    assert RUN2_ROUND_LIMIT_MODEL in out.limitations


def test_other_critic_limitations_are_not_mistaken_for_the_round_limit(world):
    other = "The critic did not review the dependency graph."
    out, _ = finalize(world, limitations=[other], known=[RUN2_ROUND_LIMIT_CODE])

    assert RUN2_ROUND_LIMIT_CODE in out.limitations and other in out.limitations
