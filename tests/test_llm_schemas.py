"""Offline regression tests for every schema sent to the OpenAI SDK as structured output.

These check the schemas locally, with the SDK's own strict-schema converter. They cannot prove the API
accepts them; that needs a real API response, which the test suite never makes.
"""

import asyncio
import json

import pytest
from openai.lib._pydantic import to_strict_json_schema

from app.agents import (
    AgentConfig,
    CriticAgent,
    DependencyAgent,
    ObservabilityAgent,
    OrchestratorAgent,
    ReasoningAgent,
    ReportingAgent,
)
from app.agents.contracts import ReportOutput
from app.baseline.single_agent import BaselineStep
from app.llm import LLMError, LLMErrorKind, ScriptedLLMClient, validate_output
from app.product.report_view import build_view, report_author
from tests import agent_scripts as s
from tests.test_agents import INC_2041, investigate

OUTPUT_MODELS = [a.output_model for a in (OrchestratorAgent, ObservabilityAgent, DependencyAgent, ReasoningAgent,
                                          CriticAgent, ReportingAgent)] + [BaselineStep]
# Keywords this project relies on in strict mode. Anything else (defaults, min/max, patterns) is rejected here.
ALLOWED = {"type", "properties", "required", "additionalProperties", "items", "enum", "anyOf", "$ref", "$defs",
           "title", "description", "format"}


def _problems(node, path=""):
    out = []
    if isinstance(node, dict):
        for key, value in node.items():
            if key in ("properties", "$defs"):  # children are names, not keywords
                for name, child in value.items():
                    out += _problems(child, f"{path}.{key}.{name}")
                continue
            if key not in ALLOWED:
                out.append(f"{path}: unsupported keyword '{key}'")
            out += _problems(value, f"{path}.{key}")
        if node.get("type") == "object":
            if node.get("additionalProperties") is not False:
                out.append(f"{path}: object is not closed")
            if set(node.get("properties", {})) != set(node.get("required", [])):
                out.append(f"{path}: not every property is required")
        if "format" in node and node["format"] != "date-time":
            out.append(f"{path}: format '{node['format']}'")
    elif isinstance(node, list):
        for i, item in enumerate(node):
            out += _problems(item, f"{path}[{i}]")
    return out


@pytest.mark.parametrize("model", OUTPUT_MODELS, ids=lambda m: m.__name__)
def test_output_schema_is_strict_compatible(model):
    schema = to_strict_json_schema(model)

    assert _problems(schema) == []
    assert '"default"' not in json.dumps(schema)


def test_report_schema_contains_the_phase_6_fields_as_required():
    schema = to_strict_json_schema(ReportOutput)

    assert {"interpretations", "would_confirm", "would_disconfirm"} <= set(schema["required"])
    assert schema["properties"]["conclusion"]["enum"] == ["probable_cause_identified", "inconclusive"]


def test_tool_names_are_an_enum_in_the_schema_but_checked_per_request_in_code():
    schema = to_strict_json_schema(BaselineStep)
    tool = schema["$defs"]["ToolCall"]["properties"]["tool"]

    assert tool["type"] == "string" and len(tool["enum"]) == 5
    step = validate_output({"action": "call_tools", "report": None,
                            "tool_calls": [{"tool": "rm_rf", "query_json": "{}", "reason": "r"}]}, BaselineStep)
    assert step.tool_calls[0].tool == "rm_rf"  # accepted by the model class, rejected later per call (test_baseline)


VALID_REPORT = {
    "conclusion": "inconclusive", "headline": "h", "summary": "s",
    "ranked_hypotheses": [{"rank": 1, "hypothesis_id": "H1", "title": "t", "confidence": "low", "supporting_refs": [],
                           "contradicting_refs": [], "assessment": "a"}],
    "facts": [], "interpretations": [{"text": "x", "evidence_refs": ["ev.logs#0"]}], "assumptions": [],
    "would_confirm": [], "would_disconfirm": [], "limitations": [],
    "corrective_actions": [{"action": "a", "timing": "immediate", "hypothesis_id": None}], "open_questions": [],
}


def test_a_valid_extended_report_validates():
    assert validate_output(VALID_REPORT, ReportOutput).interpretations[0].evidence_refs == ["ev.logs#0"]


@pytest.mark.parametrize(
    "mutate",
    [
        lambda r: r.pop("interpretations"),  # the pre-Phase-6 shape
        lambda r: r.pop("would_confirm"),
        lambda r: r.update(extra_field=1),
        lambda r: r.update(conclusion="root_cause_found"),
        lambda r: r.update(facts=None),
        lambda r: r["ranked_hypotheses"][0].update(confidence="certain"),
        lambda r: r["ranked_hypotheses"][0].update(rank="first"),
        lambda r: r["corrective_actions"][0].update(timing="later"),
        lambda r: r["interpretations"][0].pop("evidence_refs"),
    ],
)
def test_invalid_model_output_is_an_explicit_invalid_output_error(mutate):
    report = json.loads(json.dumps(VALID_REPORT))
    mutate(report)

    with pytest.raises(LLMError) as info:
        validate_output(report, ReportOutput)
    assert info.value.kind is LLMErrorKind.INVALID_OUTPUT and info.value.retryable


def test_old_report_shape_ends_in_a_labelled_deterministic_fallback_not_a_model_conclusion():
    def pre_phase_6(request):  # a model that never fills the new fields
        out = s.report()(request)
        for key in ("interpretations", "would_confirm", "would_disconfirm"):
            out.pop(key)
        return out

    result, llm = investigate(s.full_script(reporting=[pre_phase_6]))
    task = result.investigation.tasks["report"]

    assert len(llm.calls_for("reporting")) == 2  # retried once, then gave up
    assert all(a.error.code.value == "model_invalid_output" for a in task.attempts if a.error)
    assert result.report.generated_by == "fallback"
    assert result.report.report.conclusion == "inconclusive"  # never a cause without a model write-up
    assert result.report.report.headline.startswith("Inconclusive: the reporting model was unavailable")
    view = build_view(result, evidence_source="demo", model="gpt-4o-mini")
    assert view.process.report_author == "deterministic fallback (no model output was usable)"
    assert report_author("fallback", "gpt-4o-mini") != "model gpt-4o-mini"


def test_baseline_with_old_report_shape_also_falls_back_explicitly():
    from app.baseline import SingleAgentInvestigator
    from app.evaluation.dataset import load_cases

    def old_report(request):
        d = s.payload(request)
        if not d["evidence"]:
            return {"action": "call_tools", "report": None, "tool_calls": [
                {"tool": "search_logs", "query_json": json.dumps({"start": "2026-09-01T08:40:00Z",
                                                                 "end": "2026-09-01T10:57:00Z"}), "reason": "r"}]}
        report = {k: v for k, v in VALID_REPORT.items() if k not in ("interpretations", "would_confirm", "would_disconfirm")}
        return {"action": "final_report", "tool_calls": [], "report": report}

    case = {c.case_id: c for c in load_cases()}["EVAL-01"]
    result = asyncio.run(SingleAgentInvestigator(ScriptedLLMClient({"baseline": [old_report]}), case.tools())
                         .investigate(case.incident))

    assert result.status == "fallback" and result.report.generated_by == "fallback"
    assert result.report.report.conclusion == "inconclusive" and result.invalid_outputs == 2


def test_reporting_agent_instructions_mention_every_new_field():
    for field in ("interpretations", "would_confirm", "would_disconfirm", "timeline_checks", "evidence_inventory"):
        assert field in ReportingAgent.instructions
    assert AgentConfig().agent_retry.max_attempts == 2 and INC_2041.incident_id == "INC-2041"
