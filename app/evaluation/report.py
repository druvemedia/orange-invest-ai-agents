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
            "error": r["error"] or "",
        })
    return rows


def _write_csv(result: dict, path: Path) -> None:
    rows = _rows(result)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0]) if rows else ["case_id"], lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


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
        ("Strict accuracy", lambda a: _fmt(a["strict_accuracy"], True)),
        ("Lenient accuracy (acceptable inconclusive counts)", lambda a: _fmt(a["lenient_accuracy"], True)),
        ("Wrong-conclusion rate (confidently wrong)", lambda a: _fmt(a["wrong_conclusion_rate"], True)),
        ("Failure rate", lambda a: _fmt(a["failure_rate"], True)),
        ("Top-1 hypothesis hit (gold cases)", lambda a: _fmt(a["top1_hit_rate"], True)),
        ("Top-3 hypothesis hit (gold cases)", lambda a: _fmt(a["top3_hit_rate"], True)),
        ("Distractor ranked first (gold cases)", lambda a: _fmt(a["distractor_top1_rate"], True)),
        ("Must-consider coverage (mean)", lambda a: _fmt(a["mean_must_consider_coverage"], True)),
        ("Key-evidence recall (mean)", lambda a: _fmt(a["mean_key_evidence_recall"], True)),
        ("Refs cited (total)", lambda a: _fmt(a["grounding"]["cited_refs"])),
        ("Ref existence rate", lambda a: _fmt(a["grounding"]["existence_rate"], True)),
        ("Ref availability rate", lambda a: _fmt(a["grounding"]["availability_rate"], True)),
        ("Lexical support rate (heuristic)", lambda a: _fmt(a["grounding"]["lexical_support_rate"], True)),
        ("Tool calls per case (mean)", lambda a: _fmt(a["tool_calls"]["mean"])),
        ("Model calls per case (mean)", lambda a: _fmt(a["model_calls"]["mean"])),
        ("Invalid model outputs (total)", lambda a: _fmt(a["invalid_outputs_total"])),
        ("Latency s (mean / median / max)", lambda a: " / ".join(_fmt(a["latency_seconds"][k]) for k in ("mean", "median", "max"))),
        ("Tokens in / out (total)", lambda a: f"{a['tokens']['input_total']} / {a['tokens']['output_total']}" if a["tokens"] else "n/a (not reported)"),
    ]
    for name, fn in rows:
        lines.append(f"| {name} | " + " | ".join(fn(aggs[a]) for a in approaches) + " |")

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
