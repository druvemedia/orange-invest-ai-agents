"""Investigate an incident from the command line (same service, limits and report as the API).

    python -m app.cli INC-2041                              # an example incident (any source; see --list)
    python -m app.cli EVAL-07 --format markdown             # full readable report
    python -m app.cli --incident-file my_incident.json --source demo
    python -m app.cli --list                                # evidence sources and example incidents

The model is chosen by settings: LLM_PROVIDER=openai (+ LLM_MODEL, LLM_API_KEY) makes real, billed calls, and
only when LLM_ALLOW_BILLED_CALLS=true; without it the command exits with code 2 before building a client.
LLM_PROVIDER=offline-heuristic runs a keyless rule-based stand-in for demos (not an AI model).
"""

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

from pydantic import ValidationError

from app.core.config import get_settings
from app.core.logging import configure_logging
from app.factory import build_investigation_service
from app.product.report_view import render_markdown
from app.product.sources import DEMO, load_sources
from app.schemas.incident import IncidentReport
from app.schemas.investigation_api import InvestigationRequest
from app.tools.datastore import summarize_validation_error


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("incident_id", nargs="?", help="ID of an example incident, e.g. INC-2041 or EVAL-07")
    parser.add_argument("--incident-file", type=Path, help="JSON file with your incident (same fields as POST /investigations)")
    parser.add_argument("--source", help=f"Evidence source (default: the example's own, or '{DEMO}')")
    parser.add_argument("--format", choices=["summary", "markdown", "json"], default="summary")
    parser.add_argument("--json", action="store_true", help="Same as --format json")
    parser.add_argument("--list", action="store_true", help="List evidence sources and example incidents")
    args = parser.parse_args(argv)
    fmt = "json" if args.json else args.format

    sources = load_sources()
    if args.list:
        for s in sources.values():
            print(f"{s.name:8} {s.description}")
            for e in s.examples:
                print(f"         example {e.incident_id}: {e.summary}")
        return 0

    try:
        incident, source_name = _incident(args, sources)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    settings = get_settings()
    configure_logging(settings.log_level)
    service = build_investigation_service(settings)
    if not service.available:
        print(f"Investigations are unavailable: {service.unavailable_reason}", file=sys.stderr)
        return 2

    async def run():
        job = service.submit(incident, sources[source_name])
        return await service.wait(job.id)

    started = time.perf_counter()
    job = asyncio.run(run())
    elapsed = time.perf_counter() - started
    if job.status == "failed":
        print(f"{job.id} failed: {job.error}", file=sys.stderr)
        return 1

    view = job.view
    if fmt == "json":
        print(view.model_dump_json(indent=2))
    elif fmt == "markdown":
        print(render_markdown(view))
    else:
        p = view.process
        for notice in view.notices:
            print(f"NOTICE: {notice}")
        print(f"{view.investigation_id}: {p.investigation_status} ({p.termination_reason}); source {view.evidence_source}; model {view.model}")
        print(f"Conclusion: {view.conclusion} - {view.headline}")
        for h in view.hypotheses:
            print(f"  {h.rank}. [{h.confidence}] {h.title}  ({len(h.supporting)} supporting, {len(h.contradicting)} contradicting)")
        for c in view.evidence_coverage:
            if c.outcome != "collected":
                print(f"  evidence {c.task_id}: {c.outcome}{f' ({c.error_code})' if c.error_code else ''}")
        print("Full report: add --format markdown")
    usage = job.usage or {}
    print(f"\nElapsed {elapsed:.1f}s; model calls {usage.get('model_calls')}; tokens in={usage.get('input_tokens')} "
          f"out={usage.get('output_tokens')}", file=sys.stderr)
    return 0


def _incident(args, sources) -> tuple[IncidentReport, str]:
    if args.incident_file:
        try:
            body = InvestigationRequest.model_validate(json.loads(args.incident_file.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"Cannot read {args.incident_file}: {type(exc).__name__}") from None
        except ValidationError as exc:
            raise ValueError(f"Invalid incident: {summarize_validation_error(exc, max_errors=5)}") from None
        source = args.source or body.evidence_source
        incident = IncidentReport(
            incident_id=body.incident_id or "INC-LOCAL", summary=body.summary, affected_services=body.affected_services,
            severity=body.severity, reported_at=body.reported_at, reporter=body.reporter, context=body.context,
        )
    elif args.incident_id:
        found = [(e, s.name) for s in sources.values() for e in s.examples if e.incident_id == args.incident_id]
        if not found:
            raise ValueError(f"Unknown example incident {args.incident_id!r}; see --list, or use --incident-file.")
        incident, own_source = found[0]
        source = args.source or own_source
    else:
        raise ValueError("Give an example incident ID or --incident-file (see --help).")
    if source not in sources:
        raise ValueError(f"Unknown evidence source {source!r}. Use one of: {', '.join(sources)}.")
    return incident, source


if __name__ == "__main__":
    sys.exit(main())
