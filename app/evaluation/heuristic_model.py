"""A deterministic, rule-based stand-in for the language model, used by the OFFLINE evaluation.

It implements LLMClient, so both approaches run unchanged and every code path (tools, validation,
grounding checks, budgets, scoring) is exercised without network access or cost.

The rules are deliberately naive and identical for both approaches:
  H1 = the most recent change before the report; H2 = the most frequent error log message.
They do not reason. Offline scores therefore test the harness and show how the metrics react,
for example to a misleading deployment. They are NOT evidence of either approach's real quality.
"""

import json
from collections import Counter
from datetime import datetime, timedelta
from typing import Any

from app.llm.client import LLMRequest, OutputT, validate_output

LOOKBACK = timedelta(hours=24)


class HeuristicLLMClient:
    def __init__(self) -> None:
        self.calls: list = []  # no token usage: nothing is sent anywhere

    async def generate(self, request: LLMRequest, output_type: type[OutputT]) -> OutputT:
        data = json.loads(request.input_json)
        respond = _RESPONDERS[request.agent]
        return validate_output(respond(data), output_type)


# --- Shared naive analysis -----------------------------------------------------------------------


def _items(evidence: list[dict], source: str) -> list[dict]:
    return [i for s in evidence if s.get("source") == source for i in s["items"]]


def _when(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def analyse(evidence: list[dict], reported_at: str | None) -> dict[str, Any]:
    report_time = _when(reported_at) if reported_at else None
    changes = [
        i for i in _items(evidence, "changes")
        if report_time is None or report_time - LOOKBACK <= _when(i["record"]["timestamp"]) <= report_time
    ]
    changes.sort(key=lambda i: i["record"]["timestamp"], reverse=True)
    errors = [i for i in _items(evidence, "logs") if i["record"]["level"] in ("ERROR", "CRITICAL")]
    groups: dict[str, list[str]] = {}
    for i in errors:
        groups.setdefault(i["record"]["message"][:70], []).append(i["ref"])
    top_error = Counter({k: len(v) for k, v in groups.items()}).most_common(1)
    anomalies = []
    series: dict[tuple, list[dict]] = {}
    for i in _items(evidence, "metrics"):
        r = i["record"]
        series.setdefault((r["service"], r["metric_name"]), []).append(i)
    for (service, metric), points in series.items():
        points.sort(key=lambda p: p["record"]["timestamp"])
        values = [p["record"]["value"] for p in points]
        base = sum(values[: max(1, len(values) // 3)]) / max(1, len(values) // 3)
        peak = max(range(len(values)), key=values.__getitem__)
        if base > 0 and values[peak] >= 2 * base:
            anomalies.append({"service": service, "metric": metric, "ref": points[peak]["ref"]})
    return {
        "change": changes[0] if changes else None,
        "error": (top_error[0][0], groups[top_error[0][0]]) if top_error else None,
        "error_refs_by_service": {
            svc: [i["ref"] for i in errors if i["record"]["service"] == svc][:2]
            for svc in {i["record"]["service"] for i in errors}
        },
        "anomaly_refs_by_service": {a["service"]: a["ref"] for a in anomalies},
        "anomalies": anomalies,
        "errors": errors,
    }


def hypotheses(evidence: list[dict], reported_at: str | None) -> list[dict]:
    a = analyse(evidence, reported_at)
    out = []
    if a["change"]:
        r = a["change"]["record"]
        support = [a["change"]["ref"], *a["error_refs_by_service"].get(r["service"], [])]
        if r["service"] in a["anomaly_refs_by_service"]:
            support.append(a["anomaly_refs_by_service"][r["service"]])
        out.append({
            "title": f"{r['summary']} ({r['service']} {r['change_type']})",
            "description": f"Most recent change before the report: {r['change_id']} {r['summary']} {r.get('details', {})}",
            "supporting_refs": support,
        })
    if a["error"]:
        message, refs = a["error"]
        out.append({"title": f"Errors: {message}", "description": f"Most frequent error message: {message}",
                    "supporting_refs": refs[:3]})
    for i, h in enumerate(out):
        h["hypothesis_id"] = f"H{i + 1}"
        h["confidence"] = "medium" if i == 0 else "low"
    return out


# --- Per-agent responders (multi-agent) --------------------------------------------------------------


def _orchestrator(d):
    reported = _when(d["incident"]["reported_at"])
    return {"window_start": (reported - timedelta(minutes=90)).isoformat(),
            "window_end": (reported + timedelta(minutes=45)).isoformat(),
            "focus_services": d["incident"]["affected_services"], "keywords": [],
            "rationale": "Heuristic default window around the reported time."}


def _observability(d):
    a = analyse(d["evidence"], d["incident"]["reported_at"])
    obs = []
    if a["error"]:
        obs.append(("fact", f"Repeated errors: {a['error'][0]}", a["error"][1][:3]))
    for an in a["anomalies"][:4]:
        obs.append(("fact", f"{an['service']} {an['metric']} peaked at over twice its baseline", [an["ref"]]))
    first = next((i["ref"] for s in d["evidence"] for i in s["items"]), None)
    if not obs and first:
        obs.append(("fact", "Evidence reviewed; no clear anomaly found.", [first]))
    return {"observations": [{"observation_id": f"obs-{n}", "kind": k, "statement": s, "services": [], "evidence_refs": r}
                             for n, (k, s, r) in enumerate(obs, start=1)],
            "onset_estimate": None, "evidence_gaps": [], "summary": "Heuristic telemetry summary."}


def _dependency(d):
    findings = [(f"{c['change_id']} on {c['service']} ({c['change_type']}) {c['minutes_before_report']} min before the report",
                 [c["change_ref"]])
                for c in d["change_relations"]
                if c["minutes_before_report"] is not None and 0 <= c["minutes_before_report"] <= 24 * 60]
    first = next((i["ref"] for s in d["evidence"] for i in s["items"]), None)
    if not findings and first:
        findings = [("No change in the 24 hours before the report.", [first])]
    return {"findings": [{"finding_id": f"dep-{n}", "kind": "fact", "statement": s, "services": [], "evidence_refs": r}
                         for n, (s, r) in enumerate(findings, start=1)],
            "evidence_gaps": [], "summary": "Heuristic change summary."}


def _reasoning(d):
    hyps = hypotheses(d["evidence"], d["incident"]["reported_at"])
    return {"hypotheses": [{**h, "contradicting_refs": [], "missing_information": [],
                            "confidence_rationale": "Heuristic: recency of change / error frequency."} for h in hyps],
            "insufficient_evidence": not hyps,
            "critique_responses": ["Acknowledged."] if d["critique"] else [],
            "summary": "Heuristic hypotheses."}


def _critic(d):
    return {"verdict": "accept", "issues": [], "evidence_requests": [], "summary": "Heuristic critic: no review performed."}


def _report(hyps: list[dict], conclusion: str) -> dict:
    ranked = sorted(hyps, key=lambda h: {"high": 0, "medium": 1, "low": 2}[h["confidence"]])
    return {
        "conclusion": conclusion,
        "headline": ranked[0]["title"] if ranked else "Inconclusive",
        "summary": "Heuristic report.",
        "ranked_hypotheses": [{"rank": i, "hypothesis_id": h["hypothesis_id"], "title": h["title"],
                               "confidence": h["confidence"], "supporting_refs": h["supporting_refs"],
                               "contradicting_refs": h.get("contradicting_refs", []), "assessment": h["description"]}
                              for i, h in enumerate(ranked, start=1)],
        "facts": [], "interpretations": [], "assumptions": [], "limitations": [],
        "would_confirm": [], "would_disconfirm": [],
        "corrective_actions": [{"action": f"Investigate: {ranked[0]['title']}", "timing": "follow_up",
                                "hypothesis_id": ranked[0]["hypothesis_id"]}] if ranked else [],
        "open_questions": [],
    }


def _reporting(d):
    hyps = d["final_reasoning"]["output"]["hypotheses"] if d["final_reasoning"] else []
    return _report(hyps, "probable_cause_identified" if d["conclusion_allowed"] else "inconclusive")


# --- Baseline responder --------------------------------------------------------------------------------


def _baseline(d):
    incident = d["incident"]
    if not d["evidence"] and not d["must_report_now"]:
        reported = _when(incident["reported_at"])
        window = {"start": (reported - timedelta(minutes=90)).isoformat(), "end": (reported + timedelta(minutes=45)).isoformat()}
        calls = [
            ("search_logs", {**window, "min_level": "WARNING", "limit": 200}),
            ("query_metrics", {**window, "limit": 1000}),
            ("get_change_history", {"start": (reported - timedelta(minutes=90) - LOOKBACK).isoformat(), "end": window["end"]}),
            *[("get_service_dependencies", {"service": s, "direction": "both", "depth": 2}) for s in incident["affected_services"]],
            ("search_historical_incidents", {"services": incident["affected_services"], "limit": 5}),
        ][: d["max_tool_calls_per_step"]]
        return {"action": "call_tools", "report": None,
                "tool_calls": [{"tool": t, "query_json": json.dumps(q), "reason": "Initial broad sweep."} for t, q in calls]}
    hyps = hypotheses(d["evidence"], incident["reported_at"])
    levels = [h["confidence"] for h in hyps]
    conclusive = bool(hyps) and levels[0] == "medium" and "medium" not in levels[1:]
    return {"action": "final_report", "tool_calls": [],
            "report": _report(hyps, "probable_cause_identified" if conclusive else "inconclusive")}


_RESPONDERS = {
    "orchestrator": _orchestrator,
    "observability": _observability,
    "dependency": _dependency,
    "reasoning": _reasoning,
    "critic": _critic,
    "reporting": _reporting,
    "baseline": _baseline,
}
