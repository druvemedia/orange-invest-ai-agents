"""Evaluation command.

    python -m app.evaluation offline                      # free, deterministic, rule-based model
    python -m app.evaluation live --confirm-billed        # REAL model calls; billed

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
# Measured once (Phase 3.1, INC-2041, gpt-4o-mini): one multi-agent run. The baseline has not been measured.
MEASURED_MULTI_AGENT_RUN = {"input_tokens": 55_052, "output_tokens": 5_743, "seconds": 104}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("mode", choices=["offline", "live"])
    parser.add_argument("--cases", help="Comma-separated case IDs (default: all)")
    parser.add_argument("--approach", choices=["both", *APPROACHES], default="both")
    parser.add_argument("--out", type=Path, help="Output directory")
    parser.add_argument("--confirm-billed", action="store_true", help="Required for live mode: you accept the cost")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.ERROR)
    case_ids = args.cases.split(",") if args.cases else None
    approaches = APPROACHES if args.approach == "both" else (args.approach,)

    if args.mode == "offline":
        result = run_evaluation("offline-heuristic", HeuristicLLMClient, approaches=approaches, case_ids=case_ids)
        out = args.out or ROOT / "evaluation_results" / "offline-heuristic"
    else:
        from app.llm import LLMNotConfiguredError, build_llm_client, check_llm_settings

        settings = get_settings()
        n_cases = len(case_ids) if case_ids else 10
        runs = n_cases * len(approaches)
        print(
            f"Live evaluation: {runs} investigations ({n_cases} cases x {len(approaches)} approaches) with model "
            f"'{settings.llm_model or '?'}'.\nThe only measured run used about {MEASURED_MULTI_AGENT_RUN['input_tokens']:,} "
            f"input / {MEASURED_MULTI_AGENT_RUN['output_tokens']:,} output tokens and {MEASURED_MULTI_AGENT_RUN['seconds']} s "
            "for ONE multi-agent investigation; the baseline's usage has not been measured.",
            file=sys.stderr,
        )
        if not args.confirm_billed:
            print("Refusing to make billed calls without --confirm-billed.", file=sys.stderr)
            return 2
        try:
            check_llm_settings(settings, billing_confirmed=True)  # --confirm-billed is the explicit opt-in here
        except LLMNotConfiguredError as exc:
            print(f"LLM is not configured: {exc}", file=sys.stderr)
            return 2
        try:
            agent_config, baseline_config = AgentConfig.from_settings(settings), BaselineConfig.from_settings(settings)
        except ValueError as exc:
            print(f"Invalid investigation limits: {exc}", file=sys.stderr)
            return 2
        result = run_evaluation("live", lambda: build_llm_client(settings, billing_confirmed=True),
                                approaches=approaches, case_ids=case_ids,
                                agent_config=agent_config, baseline_config=baseline_config, model_name=settings.llm_model)
        out = args.out or ROOT / "evaluation_results" / "live"

    write_outputs(result, out)
    print(f"Wrote {out}/report.md, results.json, per_case.csv")
    for approach, agg in result["aggregates"].items():
        print(f"  {approach:13} strict {agg['strict_accuracy']}  lenient {agg['lenient_accuracy']}  "
              f"wrong {agg['wrong_conclusion_rate']}  failed {agg['failure_rate']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
