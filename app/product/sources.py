"""Evidence sources a user can investigate against.

All evidence in this product is synthetic. ``demo`` is the shared fixture world (data/fixtures);
each ``EVAL-NN`` is one evaluation world (data/eval/cases/EVAL-NN, inputs only, never labels).
An evaluation world's simulated tool faults (e.g. the metrics tool being down) apply to every
investigation against it, so failure handling can be seen end to end.
"""

from dataclasses import dataclass
from pathlib import Path

from app.evaluation.dataset import load_cases
from app.schemas.incident import IncidentReport
from app.tools import DEFAULT_FIXTURES_DIR, FaultInjector, FixtureStore, SimulatedFault, build_evidence_tools
from app.tools.base import EvidenceTool

DEMO = "demo"


@dataclass(frozen=True)
class EvidenceSource:
    name: str
    description: str
    data_dir: Path
    faults: dict[str, SimulatedFault]
    examples: tuple[IncidentReport, ...]

    def tools(self) -> dict[str, EvidenceTool]:
        return build_evidence_tools(FixtureStore(self.data_dir), FaultInjector(dict(self.faults)))


def load_sources() -> dict[str, EvidenceSource]:
    demo_incidents = FixtureStore(DEFAULT_FIXTURES_DIR).load("incidents.jsonl", IncidentReport).records
    sources = {
        DEMO: EvidenceSource(
            name=DEMO,
            description="Shared synthetic world: payments (2026-10-20), orders/inventory (2026-10-22), frontend (2026-10-24).",
            data_dir=DEFAULT_FIXTURES_DIR,
            faults={},
            examples=tuple(demo_incidents),
        )
    }
    for case in load_cases():
        faults = dict(case.conditions.faults)
        note = f" Simulated faults: {', '.join(f'{t} {f}' for t, f in faults.items())}." if faults else ""
        sources[case.case_id] = EvidenceSource(
            name=case.case_id,
            description=f"Synthetic evaluation world {case.case_id}.{note}",
            data_dir=case.data_dir,
            faults=faults,
            examples=(case.incident,),
        )
    return sources
