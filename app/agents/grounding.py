"""Deterministic checks that cited evidence can support the claim it is attached to.

These checks are structural, not semantic:
- a metric sample within its series' normal range (``no_change``) cannot support a claimed change.
  It is removed from support, or moved to contradicting when the hypothesis claims an increase (the
  trigger is a fixed keyword list, so this part is a heuristic);
- a dependency edge or past incident is contextual (see app.agents.evidence) and never counts as
  direct support, so it cannot raise confidence on its own.
Whether a direct observation supports the *specific* causal claim remains a semantic judgement
made by the model (reasoning and critic); it is not decided here.
"""

import re
from typing import Literal

from app.agents.evidence import EvidenceClass, RefIndex
from app.schemas.common import StrictModel

INCREASE_CLAIM = re.compile(
    r"\b(increas\w*|spik\w*|surg\w*|rise|rising|rose|grow\w*|higher|elevat\w*|load|traffic|volume|burst\w*|more requests)\b",
    re.IGNORECASE,
)


class CitationCheck(StrictModel):
    hypothesis_id: str
    ref: str
    evidence_class: EvidenceClass
    action: Literal["moved_to_contradicting", "removed_from_support"]
    detail: str


def check_citations(
    hypothesis_id: str, claim_text: str, supporting: list[str], contradicting: list[str], refs: RefIndex
) -> tuple[list[str], list[str], list[CitationCheck]]:
    """Return (supporting, contradicting, changes) after removing support that cannot support anything."""
    kept, moved, checks = [], list(contradicting), []
    claims_increase = bool(INCREASE_CLAIM.search(claim_text))
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
    return kept, moved, checks
