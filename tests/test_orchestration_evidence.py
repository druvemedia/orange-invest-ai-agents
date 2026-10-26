"""The engine running the real evidence tools over the shared fixtures."""

import asyncio
import json
from pathlib import Path

from app.orchestration import (
    EVIDENCE_TASK_KIND,
    EvidenceOutcome,
    EvidenceTaskHandler,
    InvestigationEngine,
    InvestigationPlan,
    InvestigationStatus,
    TaskErrorCode,
    TaskStatus,
    TerminationReason,
    evidence_task,
)
from app.schemas.evidence import EvidenceStatus
from app.schemas.incident import IncidentReport
from app.tools import FaultInjector, FixtureStore, SimulatedFault, build_evidence_tools

PAYMENTS = {"start": "2026-10-20T13:30:00Z", "end": "2026-10-20T15:00:00Z"}
QUIET = {"start": "2026-10-10T00:00:00Z", "end": "2026-10-10T06:00:00Z"}
INCIDENT = IncidentReport(incident_id="INC-2041", summary="Checkout card payments failing or very slow")


def payments_plan(*extra):
    return InvestigationPlan(
        tasks=[
            evidence_task("deps", "get_service_dependencies", {"service": "payments-api"}),
            evidence_task("logs", "search_logs", {**PAYMENTS, "services": ["payments-api"], "min_level": "WARNING"}),
            evidence_task("metrics", "query_metrics", {**PAYMENTS, "services": ["payments-api"]}),
            evidence_task("changes", "get_change_history", {**PAYMENTS, "services": ["payments-api"]}),
            evidence_task("history", "search_historical_incidents", {"services": ["payments-api"]}, depends_on=["changes"]),
            *extra,
        ]
    )


def run(plan, store=None, faults=None):
    tools = build_evidence_tools(store or FixtureStore(), faults)
    engine = InvestigationEngine({EVIDENCE_TASK_KIND: EvidenceTaskHandler(tools)})
    return asyncio.run(engine.run(INCIDENT, plan, investigation_id="inv-evidence"))


def outcomes(inv) -> dict[str, EvidenceOutcome]:
    return {ref.task_id: ref.outcome for ref in inv.evidence}


def test_full_plan_collects_evidence_with_provenance():
    inv = run(payments_plan())

    assert inv.status is InvestigationStatus.COMPLETED
    assert set(outcomes(inv).values()) == {EvidenceOutcome.COLLECTED}
    changes = inv.tasks["changes"].output.evidence
    assert [r.change_id for r in changes.records] == ["CHG-3101", "CHG-3102"]
    assert changes.query["services"] == ["payments-api"]  # the exact query that produced the records
    assert inv.tasks["changes"].attempts[0].evidence is changes


def test_partial_and_empty_evidence_are_successes_but_stay_distinguishable():
    inv = run(
        payments_plan(
            evidence_task("fraud_metrics", "query_metrics", {**PAYMENTS, "services": ["fraud-check"]}),
            evidence_task("quiet_logs", "search_logs", {**QUIET, "services": ["payments-api"]}),
        )
    )

    assert inv.status is InvestigationStatus.COMPLETED  # every task ran; evidence quality is reported separately
    assert outcomes(inv)["fraud_metrics"] is EvidenceOutcome.PARTIAL
    assert outcomes(inv)["quiet_logs"] is EvidenceOutcome.NO_MATCHES
    fraud = inv.tasks["fraud_metrics"].output.evidence
    assert fraud.status is EvidenceStatus.PARTIAL and fraud.records == []
    assert any("coverage gap" in w for w in fraud.warnings)


def test_unavailable_tool_gives_partial_investigation_and_no_invented_evidence():
    inv = run(payments_plan(), faults=FaultInjector({"get_change_history": SimulatedFault.UNAVAILABLE}))

    changes = inv.tasks["changes"]
    assert changes.status is TaskStatus.FAILED
    assert changes.output is None
    assert len(changes.attempts) == 3  # default max_attempts; the fault is retryable
    assert changes.error.code is TaskErrorCode.UNAVAILABLE
    assert changes.error.simulated is True
    assert all(a.evidence.status is EvidenceStatus.FAILED for a in changes.attempts)

    assert inv.tasks["history"].status is TaskStatus.BLOCKED
    assert outcomes(inv) == {
        "deps": EvidenceOutcome.COLLECTED,
        "logs": EvidenceOutcome.COLLECTED,
        "metrics": EvidenceOutcome.COLLECTED,
        "changes": EvidenceOutcome.FAILED,
        "history": EvidenceOutcome.NOT_COLLECTED,
    }
    assert inv.tasks["logs"].output.evidence.record_count > 0  # unaffected by the failure
    assert inv.status is InvestigationStatus.PARTIALLY_COMPLETED
    assert inv.termination_reason is TerminationReason.SOME_TASKS_DID_NOT_SUCCEED


def test_simulated_timeout_is_retried_then_reported_as_simulated():
    inv = run(
        InvestigationPlan(tasks=[evidence_task("logs", "search_logs", PAYMENTS, retry={"max_attempts": 2})]),
        faults=FaultInjector({"search_logs": SimulatedFault.TIMEOUT}),
    )

    record = inv.tasks["logs"]
    assert len(record.attempts) == 2
    assert record.error.code is TaskErrorCode.TIMEOUT
    assert record.error.simulated is True
    assert inv.status is InvestigationStatus.FAILED


def test_invalid_query_is_not_retried():
    inv = run(InvestigationPlan(tasks=[evidence_task("logs", "search_logs", {"start": PAYMENTS["end"], "end": PAYMENTS["start"]})]))

    record = inv.tasks["logs"]
    assert len(record.attempts) == 1
    assert record.error.code is TaskErrorCode.INVALID_QUERY
    assert outcomes(inv)["logs"] is EvidenceOutcome.FAILED


def test_unknown_tool_is_an_input_error():
    inv = run(InvestigationPlan(tasks=[evidence_task("x", "search_tea_leaves", {})]))

    assert inv.tasks["x"].error.code is TaskErrorCode.INVALID_INPUT
    assert len(inv.tasks["x"].attempts) == 1


def test_missing_dataset_is_a_real_permanent_failure(tmp_path: Path):
    inv = run(InvestigationPlan(tasks=[evidence_task("logs", "search_logs", PAYMENTS)]), store=FixtureStore(tmp_path))

    record = inv.tasks["logs"]
    assert record.error.code is TaskErrorCode.DATA_UNAVAILABLE
    assert record.error.simulated is False
    assert len(record.attempts) == 1


def test_investigation_serialises_to_json():
    inv = run(payments_plan(), faults=FaultInjector({"query_metrics": SimulatedFault.TIMEOUT}))

    payload = json.loads(inv.model_dump_json())

    assert payload["status"] == "partially_completed"
    assert payload["tasks"]["changes"]["output"]["evidence"]["records"][0]["change_id"] == "CHG-3101"
    assert payload["tasks"]["metrics"]["attempts"][0]["evidence"]["errors"][0]["simulated"] is True
    assert {e["task_id"]: e["outcome"] for e in payload["evidence"]}["metrics"] == "failed"
