"""Deterministic orchestration engine: dependency-aware, bounded execution of investigation tasks.

See docs/ARCHITECTURE.md for the execution lifecycle.
"""

from app.orchestration.engine import InvestigationEngine, PlanRejectedError
from app.orchestration.handlers import (
    EvidenceTaskHandler,
    TaskContext,
    TaskFailure,
    TaskHandler,
    evidence_task,
)
from app.orchestration.models import (
    EVIDENCE_TASK_KIND,
    DependencyPolicy,
    EngineLimits,
    EvidenceOutcome,
    InvalidTransitionError,
    Investigation,
    InvestigationStatus,
    RetryPolicy,
    TaskError,
    TaskErrorCode,
    TaskOutput,
    TaskRecord,
    TaskSpec,
    TaskStatus,
    TerminationReason,
)
from app.orchestration.plan import InvestigationPlan, validate_task_graph

__all__ = [
    "EVIDENCE_TASK_KIND",
    "DependencyPolicy",
    "EngineLimits",
    "EvidenceOutcome",
    "EvidenceTaskHandler",
    "InvalidTransitionError",
    "Investigation",
    "InvestigationEngine",
    "InvestigationPlan",
    "InvestigationStatus",
    "PlanRejectedError",
    "RetryPolicy",
    "TaskContext",
    "TaskError",
    "TaskErrorCode",
    "TaskFailure",
    "TaskHandler",
    "TaskOutput",
    "TaskRecord",
    "TaskSpec",
    "TaskStatus",
    "TerminationReason",
    "evidence_task",
    "validate_task_graph",
]
