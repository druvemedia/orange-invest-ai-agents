"""Critic agent: challenges the reasoning and decides, within limits, whether to investigate further.

The model reviews the hypotheses and may request evidence. Code decides what actually happens:
- deterministic grounding checks add issues the model cannot overrule;
- each evidence request must name a registered tool and pass that tool's query schema, must not
  repeat an earlier query, and must fit the per-round limit;
- a new round (evidence tasks, then reasoning.r<n+1>, then critic.r<n+1>) is scheduled only while
  rounds remain. The engine still applies its task, attempt and deadline budgets on top.
"""

import json
from collections.abc import Mapping

from pydantic import ValidationError

from app.agents.base import LLMAgent, Prepared, read_result, unknown_ref_problems
from app.agents.contracts import (
    CriticInput,
    CriticOutput,
    CriticResult,
    CritiqueIssue,
    EvidenceRequest,
    ReasoningResult,
    RejectedRequest,
)
from app.agents.evidence import RefIndex, duplicate_ids
from app.agents.reasoning import analysis_context
from app.orchestration import (
    EvidenceOutcome,
    TaskContext,
    TaskError,
    TaskErrorCode,
    TaskFailure,
    TaskSpec,
    evidence_task,
)
from app.tools.base import EvidenceTool
from app.tools.datastore import summarize_validation_error


class CriticAgent(LLMAgent[CriticInput, CriticOutput, CriticResult]):
    name = "critic"
    kind = "agent.critic"
    output_model = CriticOutput
    instructions = """
You are the Critic in an incident investigation team. Challenge the Reasoning agent's hypotheses:
- unsupported claims: statements not backed by the cited evidence;
- contradictions: evidence that cuts against a hypothesis but was ignored;
- alternative explanations that fit the evidence equally well;
- overconfidence, and causation inferred from timing or topology alone;
- missing evidence that would discriminate between hypotheses.
Each issue ("C1", "C2", ...) names its hypothesis (or null) and cites references from the input.
The rule_issues were found by automatic checks; take them into account.

Verdict: "accept" if the hypotheses and confidence levels are justified by the evidence;
"revise" if the reasoning should be redone; "insufficient_evidence" if no amount of revision helps
without new data. You may request at most max_evidence_requests targeted queries, and only if
rounds_remaining > 0. Each names a tool from available_tools and gives query_json, a JSON object
matching that tool's schema, with a reason that says which hypothesis it would test. Do not repeat
queries already shown in the evidence.
""".strip()

    def __init__(self, llm, tools: Mapping[str, EvidenceTool]) -> None:
        super().__init__(llm)
        self._tools = dict(tools)

    def prepare(self, ctx: TaskContext) -> Prepared[CriticInput]:
        reviewed = ctx.task.inputs["reviews"]
        reasoning = read_result(ctx.dependencies.get(reviewed), ReasoningResult)
        if reasoning is None:
            raise TaskFailure(
                TaskError(code=TaskErrorCode.DEPENDENCY_FAILED, message=f"Nothing to review: '{reviewed}' did not succeed.")
            )
        _, obs, dep, sets, refs = analysis_context(ctx.dependencies)
        round_no, max_rounds = int(ctx.task.inputs["round"]), int(ctx.task.inputs["max_rounds"])
        findings = (obs.output.observations if obs else []) + (dep.output.findings if dep else [])
        return Prepared(
            input=CriticInput(
                incident=ctx.incident,
                round=round_no,
                max_rounds=max_rounds,
                rounds_remaining=max(0, max_rounds - round_no),
                max_evidence_requests=int(ctx.task.inputs["max_evidence_requests"]),
                hypotheses=reasoning.output.hypotheses,
                reasoning_insufficient_evidence=reasoning.output.insufficient_evidence,
                evidence=sets,
                findings=findings,
                rule_issues=rule_issues(reasoning, refs),
                available_tools={name: tool.query_model.model_json_schema() for name, tool in self._tools.items()},
            ),
            refs=refs,
            consumed=sorted(ctx.dependencies),
            extra={"reviewed": reviewed, "reasoning_spec": ctx.dependencies[reviewed].spec},
        )

    def check(self, output: CriticOutput, prepared: Prepared[CriticInput]) -> list[str]:
        problems = [f"duplicate issue id '{d}'" for d in duplicate_ids(i.issue_id for i in output.issues)]
        known = {h.hypothesis_id for h in prepared.input.hypotheses}
        for issue in output.issues:
            if issue.hypothesis_id is not None and issue.hypothesis_id not in known:
                problems.append(f"{issue.issue_id} refers to unknown hypothesis '{issue.hypothesis_id}'")
            problems += unknown_ref_problems(issue.issue_id, issue.evidence_refs, prepared.refs)
        return problems

    def finish(self, ctx: TaskContext, output: CriticOutput, prepared: Prepared[CriticInput]):
        inp = prepared.input
        adjustments = []
        issues = [*inp.rule_issues, *output.issues]
        verdict = output.verdict
        if verdict == "accept" and inp.rule_issues:
            verdict = "revise"
            adjustments.append("Verdict changed from accept to revise: automatic grounding checks found issues.")

        follow_ups: list[TaskSpec] = []
        accepted: list[str] = []
        rejected: list[RejectedRequest] = []
        wants_more = verdict != "accept"
        if not wants_more:
            decision = "accepted"
        elif inp.rounds_remaining <= 0:
            decision = "max_rounds_reached"
            rejected = [RejectedRequest(request=r, reason="no critic rounds remaining") for r in output.evidence_requests]
        else:
            evidence, rejected = self._evidence_tasks(output.evidence_requests, inp, ctx)
            if verdict == "insufficient_evidence" and not evidence:
                decision = "nothing_new_to_investigate"
            else:
                decision = "revision_requested"
                accepted = [t.task_id for t in evidence]
                follow_ups = evidence + next_round(ctx, prepared.extra["reasoning_spec"], accepted)

        result = CriticResult(
            round=inp.round,
            reviewed_task=prepared.extra["reviewed"],
            output=output.model_copy(update={"issues": issues}),
            final_verdict=verdict,
            loop_decision=decision,
            accepted_requests=accepted,
            rejected_requests=rejected,
        )
        return result, adjustments, follow_ups

    def _evidence_tasks(
        self, requests: list[EvidenceRequest], inp: CriticInput, ctx: TaskContext
    ) -> tuple[list[TaskSpec], list[RejectedRequest]]:
        """Validate each request on its own: one bad request never discards the valid ones."""
        seen = {_query_key(s.tool, s.query) for s in inp.evidence if s.query}
        hypotheses = {h.hypothesis_id for h in inp.hypotheses}
        tasks, rejected = [], []
        for request in requests:
            query, reason = self._validate_request(request, hypotheses)
            if reason is None and _query_key(request.tool, query) in seen:
                reason = "the same query was already run"
            if reason is None and len(tasks) >= inp.max_evidence_requests:
                reason = f"exceeds the limit of {inp.max_evidence_requests} requests per round"
            if reason:
                rejected.append(RejectedRequest(request=request, reason=reason))
                continue
            seen.add(_query_key(request.tool, query))
            tasks.append(
                evidence_task(
                    f"ev.c{inp.round}.{len(tasks) + 1}",
                    request.tool,
                    query,
                    retry=ctx.task.inputs["evidence_retry"],
                    timeout_seconds=ctx.task.inputs["evidence_timeout_seconds"],
                    description=f"Requested by critic round {inp.round}: {request.reason}"[:300],
                )
            )
        return tasks, rejected

    def _validate_request(self, request: EvidenceRequest, hypotheses: set[str]) -> tuple[dict | None, str | None]:
        """Return (validated query, None), or (None, reason). Only registered tools ever pass."""
        tool = self._tools.get(request.tool)
        if tool is None:
            return None, f"unknown tool '{request.tool[:64]}'"
        if request.hypothesis_id is not None and request.hypothesis_id not in hypotheses:
            return None, f"unknown hypothesis '{request.hypothesis_id[:16]}'"
        try:
            raw = json.loads(request.query_json)
            if not isinstance(raw, dict):
                return None, "invalid query: query_json must be a JSON object"
            return tool.query_model.model_validate(raw).model_dump(mode="json"), None
        except ValidationError as exc:
            return None, f"invalid query: {summarize_validation_error(exc)}"
        except ValueError as exc:  # json.JSONDecodeError
            return None, f"invalid query: {type(exc).__name__}"


def next_round(ctx: TaskContext, reasoning_spec: TaskSpec, new_evidence: list[str]) -> list[TaskSpec]:
    round_no = int(ctx.task.inputs["round"])
    reasoning_deps = [*reasoning_spec.depends_on, reasoning_spec.task_id, ctx.task.task_id, *new_evidence]
    reasoning = TaskSpec.model_validate(
        reasoning_spec.model_dump()
        | {
            "task_id": f"reasoning.r{round_no + 1}",
            "depends_on": list(dict.fromkeys(reasoning_deps)),
            "inputs": {"round": round_no + 1},
        }
    )
    critic = TaskSpec.model_validate(
        ctx.task.model_dump()
        | {
            "task_id": f"critic.r{round_no + 1}",
            "depends_on": [*reasoning.depends_on, reasoning.task_id],
            "inputs": ctx.task.inputs | {"round": round_no + 1, "reviews": reasoning.task_id},
        }
    )
    return [reasoning, critic]


def rule_issues(reasoning: ReasoningResult, refs: RefIndex) -> list[CritiqueIssue]:
    """Grounding problems found by code, independent of the model's opinion."""
    issues: list[CritiqueIssue] = []

    def add(h_id, issue_type, detail, evidence_refs=()):
        issues.append(
            CritiqueIssue(
                issue_id=f"R{len(issues) + 1}",
                hypothesis_id=h_id,
                issue_type=issue_type,
                detail=detail,
                evidence_refs=list(evidence_refs),
            )
        )

    for h in reasoning.output.hypotheses:
        if not h.supporting_refs:
            add(h.hypothesis_id, "unsupported_claim", "No supporting evidence is cited.")
            continue
        if not any(refs.is_direct_evidence(r) for r in h.supporting_refs):
            reasons = "; ".join(sorted({refs.classify(r).reason for r in h.supporting_refs}))[:300]
            add(
                h.hypothesis_id,
                "unsupported_claim",
                f"No direct incident evidence: the cited support is context only ({reasons}). A dependency edge or past "
                "incident shows a relationship, not that anything failed during this incident.",
                h.supporting_refs,
            )
        absence = [r for r in h.supporting_refs if refs.outcome(r) is EvidenceOutcome.NO_MATCHES]
        if absence:
            add(h.hypothesis_id, "unsupported_claim", "Uses an empty search result (absence) as support.", absence)
    return issues


def _query_key(tool: str, query: dict) -> str:
    return tool + json.dumps(query, sort_keys=True, default=str)
