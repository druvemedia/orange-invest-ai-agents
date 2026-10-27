"""Evaluation command.

    python -m app.evaluation offline                      # free, deterministic, rule-based model
    python -m app.evaluation live --max-cost-usd 1 --preflight-only          # plan and cost estimate; no calls
    LLM_ALLOW_BILLED_CALLS=true python -m app.evaluation live --max-cost-usd 1 --confirm-billed   # REAL calls; billed

Common options: --cases EVAL-01,EVAL-02  --approach both|multi_agent|single_agent  --out DIR
"""

import argparse
import logging
import sys
from pathlib import Path

from app.agents import AgentConfig
from app.baseline import BaselineConfig
from app.core.config import get_settings
from app.evaluation.heuristic_model import HeuristicLLMClient
from app.evaluation.report import write_outputs
from app.evaluation.runner import APPROACHES, run_evaluation

ROOT = Path(__file__).resolve().parents[2]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("mode", choices=["offline", "live"])
    parser.add_argument("--cases", help="Comma-separated case IDs (default: all)")
    parser.add_argument("--approach", choices=["both", *APPROACHES], default="both")
    parser.add_argument("--out", type=Path, help="Output directory")
    parser.add_argument("--confirm-billed", action="store_true", help="Required for live mode: you accept the cost")
    parser.add_argument("--max-cost-usd", type=float, help="Required for live mode: the spending limit")
    parser.add_argument("--preflight-only", action="store_true", help="Live mode: print the plan and estimate, no calls")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.ERROR)
    case_ids = args.cases.split(",") if args.cases else None
    approaches = APPROACHES if args.approach == "both" else (args.approach,)

    if args.mode == "offline":
        result = run_evaluation("offline-heuristic", HeuristicLLMClient, approaches=approaches, case_ids=case_ids)
        out = args.out or ROOT / "evaluation_results" / "offline-heuristic"
    else:
        from app.evaluation.live import describe, preflight, unique_output_dir, write_provenance
        from app.llm import LLMNotConfiguredError, build_llm_client, check_llm_settings

        settings = get_settings()
        n_cases = len(case_ids) if case_ids else 10
        # Two separate opt-ins: LLM_ALLOW_BILLED_CALLS=true (the same guard as the API and CLI) and --confirm-billed.
        # A preflight-only preview builds no client and sends nothing, so it needs a valid model setting but not the
        # billing opt-in.
        try:
            check_llm_settings(settings, billing_confirmed=args.preflight_only)
        except LLMNotConfiguredError as exc:
            print(f"Refusing the live evaluation: {exc}", file=sys.stderr)
            return 2
        if args.max_cost_usd is None:
            print("Refusing the live evaluation without --max-cost-usd.", file=sys.stderr)
            return 2
        try:
            plan = preflight(settings, approaches, n_cases, args.max_cost_usd)
            agent_config, baseline_config = AgentConfig.from_settings(settings), BaselineConfig.from_settings(settings)
            out = unique_output_dir(ROOT, args.out)
        except (ValueError, FileExistsError) as exc:
            print(f"Refusing the live evaluation: {exc}", file=sys.stderr)
            return 2
        print(describe(plan), file=sys.stderr)
        if args.preflight_only:
            return 0
        if not args.confirm_billed:
            print("Refusing to make billed calls without --confirm-billed.", file=sys.stderr)
            return 2
        result = run_evaluation("live", lambda: build_llm_client(settings), approaches=approaches, case_ids=case_ids,
                                agent_config=agent_config, baseline_config=baseline_config, model_name=plan["model"],
                                model_config=plan["limits"], budget=plan["budget"])
        write_outputs(result, out)
        write_provenance(out, result, plan)
        print(f"Wrote {out}/report.md, results.json, per_case.csv, README.md, SHA256SUMS")
        for approach, agg in result["aggregates"].items():
            print(f"  {approach:13} strict {agg['strict_accuracy']}  lenient {agg['lenient_accuracy']}  "
                  f"wrong {agg['wrong_conclusion_rate']}  failed {agg['failure_rate']}  not run {agg['runs_not_run']}")
        return 0

    write_outputs(result, out)
    print(f"Wrote {out}/report.md, results.json, per_case.csv")
    for approach, agg in result["aggregates"].items():
        print(f"  {approach:13} strict {agg['strict_accuracy']}  lenient {agg['lenient_accuracy']}  "
              f"wrong {agg['wrong_conclusion_rate']}  failed {agg['failure_rate']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
