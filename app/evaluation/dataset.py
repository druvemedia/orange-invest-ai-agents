"""Evaluation cases (investigation inputs) and gold labels (scoring only), loaded separately.

``load_cases`` reads only data/eval/cases/*. ``load_labels`` reads only data/eval/labels.jsonl.
Investigation code receives an ``EvalCase``, which has no field that could carry a label.
"""

import json
from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator

from app.schemas.common import NonEmptyStr, StrictModel
from app.schemas.incident import IncidentReport
from app.tools import FaultInjector, FixtureStore, SimulatedFault, build_evidence_tools
from app.tools.base import EvidenceTool

EVAL_DIR = Path(__file__).resolve().parents[2] / "data" / "eval"


class CaseConditions(StrictModel):
    """Infrastructure conditions applied identically to every approach (never shown to a model)."""

    faults: dict[str, SimulatedFault] = Field(default_factory=dict)
    note: str | None = None


class EvalCase(StrictModel):
    case_id: NonEmptyStr
    data_dir: Path
    incident: IncidentReport
    conditions: CaseConditions

    def tools(self) -> dict[str, EvidenceTool]:
        """The five registered tools over this case's data, with this case's simulated faults."""
        return build_evidence_tools(FixtureStore(self.data_dir), FaultInjector(dict(self.conditions.faults)))


class EvidenceMatcher(StrictModel):
    field: NonEmptyStr
    equals: str | None = None
    contains: str | None = None


class RootCauseLabel(StrictModel):
    label_id: NonEmptyStr
    summary: NonEmptyStr
    # Every inner group must match: at least one term of each group appears (case-insensitive).
    match_all: list[list[NonEmptyStr]] = Field(min_length=1)
    # Records that directly show the cause; used for the key-evidence citation metric.
    key_evidence: list[EvidenceMatcher] = Field(default_factory=list)


class Pattern(StrictModel):
    description: NonEmptyStr
    match_all: list[list[NonEmptyStr]] = Field(min_length=1)


class GoldLabel(StrictModel):
    case_id: NonEmptyStr
    scenario: list[NonEmptyStr] = Field(min_length=1)
    expected_outcome: Literal["root_cause", "inconclusive"]
    root_cause: RootCauseLabel | None
    acceptable_alternatives: list[RootCauseLabel]
    inconclusive_acceptable: bool
    distractors: list[Pattern]
    must_consider: list[Pattern]
    rationale: NonEmptyStr

    @model_validator(mode="after")
    def _consistent(self) -> "GoldLabel":
        if (self.expected_outcome == "root_cause") != (self.root_cause is not None):
            raise ValueError(f"{self.case_id}: root_cause is required exactly when expected_outcome is root_cause")
        if self.expected_outcome == "inconclusive" and not self.inconclusive_acceptable:
            raise ValueError(f"{self.case_id}: an expected-inconclusive case must accept inconclusive")
        return self


def load_cases(eval_dir: Path = EVAL_DIR) -> list[EvalCase]:
    cases = []
    for d in sorted((eval_dir / "cases").iterdir()):
        if not d.is_dir():
            continue
        cases.append(
            EvalCase(
                case_id=d.name,
                data_dir=d,
                incident=IncidentReport.model_validate_json((d / "incident.json").read_text(encoding="utf-8")),
                conditions=CaseConditions.model_validate_json((d / "conditions.json").read_text(encoding="utf-8")),
            )
        )
    return cases


def load_labels(eval_dir: Path = EVAL_DIR) -> dict[str, GoldLabel]:
    labels = {}
    for line in (eval_dir / "labels.jsonl").read_text(encoding="utf-8").splitlines():
        if line.strip():
            label = GoldLabel.model_validate(json.loads(line))
            labels[label.case_id] = label
    return labels
