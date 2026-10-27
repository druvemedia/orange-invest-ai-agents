"""Write evaluation results as JSON, CSV and a readable Markdown report."""

import csv
import json
from pathlib import Path

OFFLINE_CAVEAT = (
    "**These are OFFLINE results produced with a rule-based stand-in for the language model** "
    "(`app/evaluation/heuristic_model.py`: 'the most recent change is the cause, else the most frequent error'). "
    "Both approaches use the same rules, so these numbers test the harness and show how the metrics respond. "
    "They say **nothing** about the quality of real LLM reasoning or about which architecture is better."
)
LIVE_CAVEAT = (
    "These results come from real model calls. They cover a small synthetic dataset (one run per case per "
    "approach, no repetitions), so treat differences of one or two cases as noise."
)


def write_outputs(result: dict, out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "results.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    _write_csv(result, out_dir / "per_case.csv")
    (out_dir / "report.md").write_text(render_markdown(result), encoding="utf-8")


def _rows(result: dict) -> list[dict]:
    runs = {(r["case_id"], r["approach"]): r for r in result["runs"]}
    rows = []
    for s in result["scores"]:
        r = runs[(s["case_id"], s["approach"])]
        g = s["grounding"]
        top = r["hypotheses"][0] if r["hypotheses"] else None
        rows.append({
            "case_id": s["case_id"],
            "approach": s["approach"],
            "expected": result["labels"][s["case_id"]]["expected_outcome"],
            "outcome": s["outcome"],
            "conclusion": r["conclusion"],
            "status": r["status"],
            "top_hypothesis": (top["title"] if top else ""),
            "top_confidence": (top["confidence"] if top else ""),
            "top1_match": s["top1_match"],
            "top3_match": s["top3_match"],
            "distractor_top1": s["distractor_top1"],
            "key_evidence_recall": s["key_evidence_recall"],
            "refs_cited": g["cited"],
            "refs_existing": g["existing"],
            "refs_available": g["available"],
            "tool_calls": r["tool_calls"],
            "model_calls": r["model_calls"],
            "invalid_outputs": r["invalid_outputs"],
            "latency_seconds": r["latency_seconds"],
            "input_tokens": r["input_tokens"],
            "output_tokens": r["output_tokens"],
            "supporting_refs_assessed": r["assessed_supporting_refs"],
            "supporting_refs_unsupported": r["unsupported_supporting_refs"],
            "evidence_gaps": r["evidence_gaps"],
            "timeouts": r["timeouts"],
            "termination_reason": r["termination_reason"] or "",
            "estimated_cost_usd": r["estimated_cost_usd"],
            "error": r["error"] or "",
        })
    return rows


def _write_csv(result: dict, path: Path) -> None:
    rows = _rows(result)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0]) if rows else ["case_id"], lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def _frac(count: dict) -> str:
    """'40% (4/10)': a rate with its numerator and denominator; 'n/a (0/0)' when nothing was measured."""
    return f"{count['n'] / count['of'] * 100:.0f}% ({count['n']}/{count['of']})" if count["of"] else "n/a (0/0)"


def _fmt(v, pct=False) -> str:
    if v is None:
        return "n/a"
    if pct:
        return f"{v * 100:.0f}%"
    return f"{v:g}" if isinstance(v, float) else str(v)


def render_markdown(result: dict) -> str:
    meta, aggs = result["meta"], result["aggregates"]
    approaches = meta["approaches"]
    caveat = OFFLINE_CAVEAT if meta["mode"].startswith("offline") else LIVE_CAVEAT
    lines = [
        f"# Evaluation report: {meta['mode']}",
        "",
        f"Generated {meta['generated_at']} - model: {meta['model'] or 'none (offline heuristic)'} - "
        f"{len(meta['cases'])} cases x {len(approaches)} approaches. Metric definitions: `docs/EVALUATION.md`.",
        "",
        caveat,
        "",
        "## Aggregate comparison",
        "",
        "| Metric | " + " | ".join(approaches) + " |",
        "|---|" + "---|" * len(approaches),
    ]
    rows = [
        ("Runs completed / planned (not run)", lambda a: f"{a['runs_completed']} / {a['cases']} ({a['runs_not_run']})"),
        ("Strict accuracy", lambda a: _frac(a["counts"]["strict"])),
        ("Lenient accuracy (acceptable inconclusive counts)", lambda a: _frac(a["counts"]["lenient"])),
        ("Wrong-conclusion rate (confidently wrong)", lambda a: _frac(a["counts"]["wrong_conclusion"])),
        ("Inconclusive outcomes", lambda a: _frac(a["counts"]["inconclusive"])),
        ("Failure rate", lambda a: _frac(a["counts"]["failed"])),
        ("Top-1 hypothesis hit (gold cases)", lambda a: _frac(a["counts"]["top1"])),
        ("Top-3 hypothesis hit (gold cases)", lambda a: _frac(a["counts"]["top3"])),
        ("Distractor ranked first (gold cases)", lambda a: _fmt(a["distractor_top1_rate"], True)),
        ("Must-consider coverage (mean)", lambda a: _fmt(a["mean_must_consider_coverage"], True)),
        ("Key-evidence recall (mean)", lambda a: _fmt(a["mean_key_evidence_recall"], True)),
        ("Refs cited (total)", lambda a: _fmt(a["grounding"]["cited_refs"])),
        ("Ref existence rate", lambda a: _fmt(a["grounding"]["existence_rate"], True)),
        ("Ref availability rate", lambda a: _fmt(a["grounding"]["availability_rate"], True)),
        ("Lexical support rate (heuristic)", lambda a: _fmt(a["grounding"]["lexical_support_rate"], True)),
        ("Unsupported supporting citations (unverifiable / assessed)",
         lambda a: f"{a['unsupported_support']['unsupported']} / {a['unsupported_support']['assessed']}"),
        ("Evidence gaps (failed, missing or partial results)", lambda a: _fmt(a["evidence_gaps_total"])),
        ("Timeouts (tool, model or deadline)", lambda a: _fmt(a["timeouts_total"])),
        ("Tool calls per case (mean)", lambda a: _fmt(a["tool_calls"]["mean"])),
        ("Model calls per case (mean)", lambda a: _fmt(a["model_calls"]["mean"])),
        ("Invalid model outputs (total)", lambda a: _fmt(a["invalid_outputs_total"])),
        ("Latency s (mean / median / max)", lambda a: " / ".join(_fmt(a["latency_seconds"][k]) for k in ("mean", "median", "max"))),
        ("Tokens in / out (total; runs measured)",
         lambda a: (f"{a['tokens']['input_total']} / {a['tokens']['output_total']} ({a['tokens']['runs_with_usage']}/{a['cases']})"
                    if a["tokens"] else f"n/a (not reported; 0/{a['cases']})")),
        ("Estimated cost USD (total; runs priced)",
         lambda a: (f"{a['cost_usd']['total']:.4f} ({a['cost_usd']['runs_priced']}/{a['cases']})"
                    if a["cost_usd"] else f"n/a (no price or usage; 0/{a['cases']})")),
    ]
    for name, fn in rows:
        lines.append(f"| {name} | " + " | ".join(fn(aggs[a]) for a in approaches) + " |")

    lines += ["", "## Configuration", ""]
    if meta.get("model_config"):
        lines.append("- Model: " + ", ".join(f"{k} {v}" for k, v in meta["model_config"].items()))
    if meta.get("pricing"):
        pr = meta["pricing"]
        lines.append(f"- Pricing: ${pr['input_per_million']} / ${pr['output_per_million']} per million input / output "
                     f"tokens ({pr['source']}, checked {pr['checked']})")
    if meta.get("budget"):
        b = meta["budget"]
        lines.append(f"- Budget: ${b['max_usd']:.2f}; estimated spend ${b['estimated_spend_usd']:.4f}; worst case per run "
                     + ", ".join(f"{k} ${v:.4f}" for k, v in b["worst_case_usd"].items())
                     + (f"; **{b['stopped']}**" if b["stopped"] else ""))
    lines += [f"- Limits: multi-agent {meta['agent_config']['max_critic_rounds']} critic rounds, "
              f"{meta['agent_config']['agent_retry']['max_attempts']} attempts per task, deadline "
              f"{meta['agent_config']['limits']['deadline_seconds']:g} s; single agent {meta['baseline_config']['max_steps']} "
              f"steps, {meta['baseline_config']['model_attempts_per_step']} attempts per step, "
              f"{meta['baseline_config']['max_tool_calls']} tool calls, deadline {meta['baseline_config']['deadline_seconds']:g} s"]

    lines += ["", "Outcome counts:", ""]
    for a in approaches:
        counts = ", ".join(f"{k} {v}" for k, v in aggs[a]["outcomes"].items() if v)
        lines.append(f"- **{a}**: {counts}")

    lines += ["", "## Per-case results", "",
              "| Case | Scenario | Expected | Approach | Outcome | Conclusion | Top hypothesis (confidence) | Refs ok/cited | Tools | Model calls | Latency s |",
              "|---|---|---|---|---|---|---|---|---|---|---|"]
    runs = {(r["case_id"], r["approach"]): r for r in result["runs"]}
    for row in _rows(result):
        label = result["labels"][row["case_id"]]
        top = row["top_hypothesis"][:60] + ("..." if len(row["top_hypothesis"]) > 60 else "")
        top_cell = f"{top} ({row['top_confidence']})" if top else "-"
        lines.append(
            f"| {row['case_id']} | {', '.join(label['scenario'])} | {row['expected']} | {row['approach']} | "
            f"**{row['outcome']}** | {row['conclusion'] or '-'} | {top_cell} | "
            f"{row['refs_available']}/{row['refs_cited']} | {row['tool_calls']} | {row['model_calls']} | {row['latency_seconds']} |"
        )
        if runs[(row["case_id"], row["approach"])]["error"]:
            lines.append(f"| | | | | error: {row['error']} | | | | | | |")

    lines += ["", "## Gold labels (for review)", ""]
    for cid, label in result["labels"].items():
        rc = label["root_cause"]["summary"] if label["root_cause"] else "none (inconclusive expected)"
        lines.append(f"- **{cid}**: {rc} Inconclusive acceptable: {label['inconclusive_acceptable']}. {label['rationale']}")
    lines.append("")
    return "\n".join(lines)
