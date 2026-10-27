"""Scripted model responses for agent tests (INC-2041, the payments incident).

These play the part of the language model. Each response is a function of the request the agent
actually sent, so references are taken from the input it received, like a model reading its
input, rather than hard-coded. They exist only for tests; no production code reads them.
"""

import json
from collections.abc import Callable
from typing import Any

from app.llm import LLMRequest

PAYMENTS_WINDOW = {"start": "2026-10-20T13:30:00Z", "end": "2026-10-20T15:00:00Z"}


def payload(request: LLMRequest) -> dict[str, Any]:
    return json.loads(request.input_json)


def items(data: dict[str, Any]) -> list[dict[str, Any]]:
    return [item for s in data["evidence"] for item in s["items"]]


def refs_where(data: dict[str, Any], predicate: Callable[[dict[str, Any]], bool]) -> list[str]:
    return [i["ref"] for i in items(data) if predicate(i["record"])]


def message_has(text: str) -> Callable[[dict[str, Any]], bool]:
    return lambda r: text.lower() in str(r.get("message", "")).lower()


def summary_ref(data: dict[str, Any], service: str, metric: str, which: str = "peak_ref") -> str:
    for m in data["metric_summaries"]:
        if m["service"] == service and m["metric_name"] == metric:
            return m[which]
    raise AssertionError(f"no summary for {service}/{metric}")


# --- Orchestrator -----------------------------------------------------------------------------

def scope(window=PAYMENTS_WINDOW, services=("payments-api",), keywords=("checkout", "timeout")):
    return {
        "window_start": window["start"],
        "window_end": window["end"],
        "focus_services": list(services),
        "keywords": list(keywords),
        "rationale": "Checkout failures reported around 14:00 UTC on payments-api.",
    }


# --- Observability -----------------------------------------------------------------------------

def observability(request: LLMRequest) -> dict[str, Any]:
    d = payload(request)
    pool_logs = refs_where(d, message_has("DB connection from pool"))
    fraud_logs = refs_where(d, message_has("fraud-check timed out"))
    return {
        "observations": [
            {"observation_id": "obs-1", "kind": "fact", "statement": "Repeated DB pool acquisition timeouts from 14:02.",
             "services": ["payments-api"], "evidence_refs": pool_logs},
            {"observation_id": "obs-2", "kind": "fact", "statement": "fraud-check requests timed out and were retried.",
             "services": ["payments-api", "fraud-check"], "evidence_refs": fraud_logs},
            {"observation_id": "obs-3", "kind": "fact", "statement": "payments-api p95 latency peaked far above baseline.",
             "services": ["payments-api"], "evidence_refs": [summary_ref(d, "payments-api", "latency_p95_ms")]},
            {"observation_id": "obs-4", "kind": "fact", "statement": "DB pool utilisation reached 100%.",
             "services": ["payments-api"], "evidence_refs": [summary_ref(d, "payments-api", "db_pool_utilization_percent")]},
            {"observation_id": "obs-5", "kind": "fact", "statement": "postgres-payments CPU stayed flat.",
             "services": ["postgres-payments"],
             "evidence_refs": [summary_ref(d, "postgres-payments", "cpu_percent", "first_ref"),
                               summary_ref(d, "postgres-payments", "cpu_percent", "peak_ref")]},
            {"observation_id": "obs-6", "kind": "interpretation",
             "statement": "Pool saturation coincides with the latency rise.",
             "services": ["payments-api"], "evidence_refs": pool_logs[:1] + [summary_ref(d, "payments-api", "latency_p95_ms")]},
        ],
        "onset_estimate": "2026-10-20T14:00:00Z",
        "evidence_gaps": ["fraud-check metrics are missing for the window."],
        "summary": "Latency and errors rose from about 14:00 with both pool and fraud-check timeouts.",
    }


# --- Dependency ----------------------------------------------------------------------------------

def dependency(request: LLMRequest) -> dict[str, Any]:
    d = payload(request)
    by_id = {c["change_id"]: c["change_ref"] for c in d["change_relations"]}
    fraud_edge = refs_where(d, lambda r: r.get("depends_on") == "fraud-check")
    return {
        "findings": [
            {"finding_id": "dep-1", "kind": "fact", "statement": "Pool size reduced 50 -> 20 at 13:55.",
             "services": ["payments-api"], "evidence_refs": [by_id["CHG-3102"]]},
            {"finding_id": "dep-2", "kind": "fact", "statement": "v2.14.0 deployed at 13:52, refactoring fraud-check retries.",
             "services": ["payments-api"], "evidence_refs": [by_id["CHG-3101"]]},
            {"finding_id": "dep-3", "kind": "fact", "statement": "payments-api depends on fraud-check (external, critical).",
             "services": ["payments-api", "fraud-check"], "evidence_refs": fraud_edge},
            {"finding_id": "dep-4", "kind": "interpretation",
             "statement": "Both changes landed minutes before onset; timing alone does not show which, if either, caused it.",
             "services": ["payments-api"], "evidence_refs": [by_id["CHG-3101"], by_id["CHG-3102"]]},
        ],
        "evidence_gaps": ["fraud-check change history is not visible."],
        "summary": "Two payments-api changes precede onset; fraud-check is an opaque critical dependency.",
    }


# --- Reasoning -------------------------------------------------------------------------------------

def reasoning(request: LLMRequest) -> dict[str, Any]:
    d = payload(request)
    if d["round"] == 1:
        return {
            "hypotheses": [
                {"hypothesis_id": "H1", "title": "Connection pool reduction exhausted DB connections",
                 "description": "CHG-3102 shrank the pool; pool saturation and acquisition timeouts followed.",
                 "supporting_refs": ["dependency/dep-1", "observability/obs-1", "observability/obs-4"],
                 "contradicting_refs": [], "missing_information": ["Confirmation the new pool size was applied"],
                 "confidence": "high", "confidence_rationale": "Change, logs and metrics agree."},
                {"hypothesis_id": "H2", "title": "fraud-check slowness amplified by new retry logic",
                 "description": "fraud-check timeouts with retries (v2.14.0) held connections and threads.",
                 "supporting_refs": ["observability/obs-2", "dependency/dep-2"],
                 "contradicting_refs": [], "missing_information": ["fraud-check latency metrics"],
                 "confidence": "medium", "confidence_rationale": "Timeouts are visible but vendor metrics are missing."},
            ],
            "insufficient_evidence": False,
            "critique_responses": [],
            "summary": "Two competing explanations.",
        }
    new_refs = [i["ref"] for i in items(d) if i["ref"].split("#")[0] in d["new_evidence_refs"]]
    reload_refs = [r for r in new_refs if r in refs_where(d, message_has("Config reloaded: db.pool.max_size=20"))]
    return {
        "hypotheses": [
            {"hypothesis_id": "H1", "title": "Connection pool reduction exhausted DB connections",
             "description": "The pool size change was applied at 13:55:20, before saturation began.",
             "supporting_refs": ["dependency/dep-1", "observability/obs-1", "observability/obs-4", *reload_refs],
             "contradicting_refs": ["observability/obs-2"],
             "missing_information": ["fraud-check latency metrics for the window"],
             "confidence": "medium", "confidence_rationale": "Well supported, but fraud-check timeouts are unexplained."},
            {"hypothesis_id": "H2", "title": "fraud-check slowness amplified by new retry logic",
             "description": "Possible contributor; cannot be assessed without vendor metrics.",
             "supporting_refs": ["observability/obs-2"], "contradicting_refs": ["observability/obs-4"],
             "missing_information": ["fraud-check latency metrics"],
             "confidence": "low", "confidence_rationale": "Vendor data is missing."},
        ],
        "insufficient_evidence": False,
        "critique_responses": ["C1: lowered H1 to medium; the new log confirms the pool change took effect at 13:55:20."],
        "summary": "Pool reduction is the better-supported explanation; fraud-check remains open.",
    }


# --- Critic ----------------------------------------------------------------------------------------

POOL_RELOAD_QUERY = {**PAYMENTS_WINDOW, "services": ["payments-api"], "keywords": ["config reloaded"], "min_level": "INFO"}


def critic_requests_pool_evidence(request: LLMRequest) -> dict[str, Any]:
    d = payload(request)
    if d["round"] == 1:
        return {
            "verdict": "revise",
            "issues": [
                {"issue_id": "C1", "hypothesis_id": "H1", "issue_type": "overconfidence",
                 "detail": "High confidence while fraud-check metrics are missing and H2 is unrefuted.",
                 "evidence_refs": ["ev.metrics"]},
            ],
            "evidence_requests": [
                {"tool": "search_logs", "query_json": json.dumps(POOL_RELOAD_QUERY),
                 "reason": "Check when the pool change took effect relative to the first timeouts.", "hypothesis_id": "H1"},
            ],
            "summary": "H1 is plausible but overconfident; verify the config was applied before onset.",
        }
    return {"verdict": "accept", "issues": [], "evidence_requests": [], "summary": "Revised confidence is justified."}


def critic_always_revise(request: LLMRequest) -> dict[str, Any]:
    """A critic that is never satisfied and asks for a different query every round."""
    d = payload(request)
    query = {**PAYMENTS_WINDOW, "keywords": [f"round-{d['round']}"], "min_level": "DEBUG"}
    return {
        "verdict": "revise",
        "issues": [{"issue_id": "C1", "hypothesis_id": None, "issue_type": "missing_evidence",
                    "detail": "Still not convinced.", "evidence_refs": []}],
        "evidence_requests": [{"tool": "search_logs", "query_json": json.dumps(query), "reason": "More logs.",
                               "hypothesis_id": None}],
        "summary": "Revise again.",
    }


# --- Reporting ---------------------------------------------------------------------------------------

def report(conclusion: str = "probable_cause_identified"):
    def respond(request: LLMRequest) -> dict[str, Any]:
        d = payload(request)
        hyps = d["final_reasoning"]["output"]["hypotheses"] if d["final_reasoning"] else []
        return {
            "conclusion": conclusion,
            "headline": "Checkout failures on payments-api",
            "summary": "See ranked hypotheses.",
            "ranked_hypotheses": [
                {"rank": i, "hypothesis_id": h["hypothesis_id"], "title": h["title"], "confidence": "high",
                 "supporting_refs": h["supporting_refs"], "contradicting_refs": h["contradicting_refs"],
                 "assessment": "As reasoned."}
                for i, h in enumerate(hyps, start=1)
            ],
            "facts": [{"text": "Pool utilisation reached 100%.", "evidence_refs": ["observability/obs-4"]}] if hyps else [],
            "interpretations": [],
            "assumptions": [],
            "would_confirm": [],
            "would_disconfirm": [],
            "limitations": ["Report-specific limitation."],
            "corrective_actions": [{"action": "Restore db.pool.max_size to 50.", "timing": "immediate", "hypothesis_id": "H1"}]
            if hyps else [],
            "open_questions": [],
        }

    return respond


def full_script(**overrides):
    script = {
        "orchestrator": [scope()],
        "observability": [observability],
        "dependency": [dependency],
        "reasoning": [reasoning],
        "critic": [critic_requests_pool_evidence],
        "reporting": [report()],
    }
    script.update(overrides)
    return script
