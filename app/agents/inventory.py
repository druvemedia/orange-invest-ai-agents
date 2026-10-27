"""The authoritative evidence inventory, and validation of "evidence was missing" claims against it.

The inventory is built only from tool outcomes and retrieved records, never from model text. It
distinguishes a query that ran and matched nothing (``no_matches``) from one that failed, timed out
or never ran. A failed query is never treated as proof that the signal is absent.

Claim validation is deterministic: a statement is contradicted when it says something was unavailable
(fixed phrase list) AND names a signal the inventory shows was retrieved (metric name, a metric
name in words such as "request rate", or "metrics"/"logs"/"change history" together with the
service). Paraphrases outside these patterns are not caught; that limitation is documented.
"""

import re

from app.agents.contracts import EvidenceInventory, InventoryEntry
from app.agents.evidence import EvidenceSet
from app.orchestration.models import EvidenceOutcome
from app.schemas.evidence import EvidenceSource

ALL_TOOLS = ("search_logs", "query_metrics", "get_change_history", "get_service_dependencies", "search_historical_incidents")

UNAVAILABLE = re.compile(
    r"\b(not available|unavailable|missing|lack(?:ed|ing|s)?|absent|no data|not (?:been )?(?:retrieved|collected|"
    r"provided|obtained|captured)|could not be (?:retrieved|obtained|collected)|were not|was not|insufficient data)\b",
    re.IGNORECASE,
)
_STATUS = {
    EvidenceOutcome.COLLECTED: "retrieved",
    EvidenceOutcome.PARTIAL: "partial",
    EvidenceOutcome.NO_MATCHES: "no_matches",
    EvidenceOutcome.FAILED: "failed",
    EvidenceOutcome.NOT_COLLECTED: "not_run",
}
_KIND_WORDS = {"metrics": ("metric",), "logs": ("log",), "change history": ("change", "deploy", "config")}


def build_inventory(sets: list[EvidenceSet]) -> EvidenceInventory:
    entries, unavailable = [], []
    for s in sets:
        status = _STATUS[s.outcome]
        if status == "failed" and s.error_code in ("timeout", "model_timeout"):
            status = "timed_out"
        signals = sorted({_signal(s.source, i.record) for i in s.items})
        gap_notes = [w for w in s.warnings if "coverage gap" in w.lower() or "malformed" in w.lower() or "first" in w.lower()]
        detail = s.error if status in ("failed", "timed_out", "not_run") else ("; ".join(gap_notes) or None)
        entries.append(InventoryEntry(
            task_id=s.ref, tool=s.tool, status=status, requested_by=s.requested_by, query=s.query,
            record_count=len(s.items), signals=signals, detail=detail,
        ))
        if status in ("failed", "timed_out", "not_run"):
            unavailable.append(f"{s.ref} ({s.tool}) {status.replace('_', ' ')}: no evidence was obtained"
                               f"{f' ({s.error})' if s.error else ''}. This is a gap, not proof the signal is absent.")
        elif status == "partial":
            unavailable += [f"{s.ref} ({s.tool}) is partial: {w}" for w in gap_notes]
    queried = {e.tool for e in entries}
    never = [t for t in ALL_TOOLS if t not in queried]
    unavailable += [f"{t} was never queried in this investigation." for t in never]
    retrieved = sorted({sig for e in entries for sig in e.signals})
    return EvidenceInventory(entries=entries, retrieved_signals=retrieved, unavailable=unavailable, never_queried_tools=never)


def _signal(source: EvidenceSource | None, r: dict) -> str:
    if source is EvidenceSource.METRICS:
        return f"metrics: {r['service']}/{r['metric_name']}"
    if source is EvidenceSource.LOGS:
        return f"logs: {r['service']}"
    if source is EvidenceSource.CHANGES:
        return f"change history: {r['service']}"
    if source is EvidenceSource.DEPENDENCIES:
        return f"dependencies: {r['service']} -> {r['depends_on']}"
    if source is EvidenceSource.HISTORICAL_INCIDENTS:
        return "historical incidents"
    return "unknown"


def _mentions(text: str, signal: str) -> bool:
    """True if ``text`` names this retrieved signal (see the module docstring for the exact rule)."""
    kind, _, rest = signal.partition(": ")
    if kind not in _KIND_WORDS:
        return False
    t = text.lower()
    service, _, metric = rest.partition("/")
    names = {service, service.split("-")[0]} if len(service.split("-")[0]) >= 5 else {service}
    if metric:
        words = metric.lower().replace("_", " ")
        stem = re.sub(r" (rps|ms|percent|per (sec|min)|count)$", "", words)  # "request rate rps" -> "request rate"
        if metric.lower() in t or (len(stem) >= 6 and stem in t):
            return True
    return any(w in t for w in _KIND_WORDS[kind]) and any(n and n in t for n in names)


def validate_availability_claims(statements: list[str], inventory: EvidenceInventory) -> tuple[list[str], list[str]]:
    """Return (kept statements, notes about removed ones). Removes claims that retrieved evidence was missing."""
    kept, removed = [], []
    for text in statements:
        if UNAVAILABLE.search(text):
            contradicted = [sig for sig in inventory.retrieved_signals if _mentions(text, sig)]
            if contradicted:
                tasks = sorted({e.task_id for e in inventory.entries if set(e.signals) & set(contradicted)})
                removed.append(f"Removed claim \"{text}\": the evidence inventory shows {', '.join(contradicted[:4])} "
                               f"{'were' if len(contradicted) > 1 else 'was'} retrieved ({', '.join(tasks)}).")
                continue
        kept.append(text)
    return kept, removed
