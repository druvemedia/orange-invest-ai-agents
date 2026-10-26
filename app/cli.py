"""Run one investigation against the configured model provider.

    python -m app.cli INC-2041            # summary
    python -m app.cli INC-2041 --json     # full investigation state and report

Needs LLM_PROVIDER, LLM_MODEL and LLM_API_KEY (see .env.example). This makes real, billed API calls.
"""

import argparse
import asyncio
import sys
import time

from app.agents import build_orchestrator
from app.core.config import get_settings
from app.core.logging import configure_logging
from app.llm import LLMNotConfiguredError, build_llm_client
from app.schemas.incident import IncidentReport
from app.tools import FixtureStore, build_evidence_tools


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("incident_id", help="An incident from data/fixtures/incidents.jsonl, e.g. INC-2041")
    parser.add_argument("--json", action="store_true", help="Print the full result as JSON")
    args = parser.parse_args(argv)

    settings = get_settings()
    configure_logging(settings.log_level)
    try:
        llm = build_llm_client(settings)
    except LLMNotConfiguredError as exc:
        print(f"LLM is not configured: {exc}", file=sys.stderr)
        return 2

    store = FixtureStore()
    incidents = {r.incident_id: r for r in store.load("incidents.jsonl", IncidentReport).records}
    if args.incident_id not in incidents:
        print(f"Unknown incident {args.incident_id!r}. Known: {sorted(incidents)}", file=sys.stderr)
        return 2

    orchestrator = build_orchestrator(llm, settings, build_evidence_tools(store))
    started = time.perf_counter()
    result = asyncio.run(orchestrator.investigate(incidents[args.incident_id]))
    elapsed = time.perf_counter() - started

    if args.json:
        print(result.model_dump_json(indent=2))
        print_usage(llm, elapsed, file=sys.stderr)
        return 0
    inv, report = result.investigation, result.report
    print(f"{inv.investigation_id}: {inv.status} ({inv.termination_reason}); {inv.attempts_used} attempts")
    for tid, t in inv.tasks.items():
        print(f"  {tid:28} {t.status:10} attempts={len(t.attempts)} {t.error.code if t.error else ''}")
    print(f"\nReport ({report.generated_by}): {report.report.conclusion} - {report.report.headline}")
    for h in report.report.ranked_hypotheses:
        print(f"  {h.rank}. [{h.confidence}] {h.hypothesis_id} {h.title}")
    for limitation in report.report.limitations:
        print(f"  limitation: {limitation}")
    print_usage(llm, elapsed)
    return 0


def print_usage(llm, elapsed: float, file=None) -> None:
    """Model calls, outcomes, latency and token usage. Never prints content or credentials."""
    calls = getattr(llm, "calls", [])
    print(f"\nElapsed: {elapsed:.1f}s; model calls: {len(calls)}", file=file)
    for c in calls:
        print(f"  {c.agent:14} {c.outcome:15} {c.latency_ms:8.0f}ms  tokens in={c.input_tokens} out={c.output_tokens}", file=file)
    tokens_in = sum(c.input_tokens or 0 for c in calls)
    tokens_out = sum(c.output_tokens or 0 for c in calls)
    print(f"  total tokens: in={tokens_in} out={tokens_out}", file=file)


if __name__ == "__main__":
    sys.exit(main())
