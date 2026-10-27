"""Evidence independence: count independent observations, not records.

Direct evidence references are grouped into observations before counting. Records are merged into one
observation when the data model shows they describe the same event:
  - duplicate records: identical content retrieved by different queries;
  - same request: log lines sharing a trace_id;
  - echo of a change: a log line naming a change's change_id, version or config key
    (e.g. "Config reloaded: db.pool.max_size=8" echoes the change that set it).

Corroboration then requires observations from different evidence sources (log pipeline, metrics,
change history). Different sources come from different collection systems, so their provenance
supports independence. Several observations from one source cannot be shown to be independent
(two log lines may be the same failure seen twice), so that case is reported as "unknown", never as
independent. Correlation between different metrics of the same service is not modelled.
"""

import re
from typing import Literal

from app.agents.contracts import ObservationGroup
from app.agents.evidence import RefIndex
from app.schemas.common import StrictModel
from app.schemas.evidence import EvidenceSource

Independence = Literal["independent", "unknown", "none"]


class IndependenceAssessment(StrictModel):
    status: Independence  # independent: >= 2 sources; unknown: >= 2 observations, one source; none: <= 1 observation
    independent_sources: list[str]
    groups: list[ObservationGroup]
    detail: str


def group_observations(refs_cited: list[str], refs: RefIndex) -> list[ObservationGroup]:
    leaves = refs.direct_leaves(refs_cited)
    groups: list[dict] = []

    def find(pred) -> dict | None:
        return next((g for g in groups if pred(g)), None)

    for ref in leaves:
        found = refs.record(ref)
        if found is None:
            continue
        source, record = found
        key = _content_key(record)
        trace = record.get("trace_id")
        g = find(lambda g: key in g["keys"])
        reason = "duplicate record"
        if g is None and trace:
            g = find(lambda g: trace in g["traces"])
            reason = f"same request (trace {trace})"
        if g is None:
            g = find(lambda g: _echoes(record, g["records"]) or any(_echoes(r, [record]) for r in g["records"]))
            reason = "log line echoes the change"
        if g is None:
            groups.append({"refs": [ref], "sources": [source], "keys": {key}, "traces": {trace} - {None},
                           "records": [record], "reasons": []})
            continue
        g["refs"].append(ref)
        g["sources"].append(source)
        g["keys"].add(key)
        g["traces"] |= {trace} - {None}
        g["records"].append(record)
        g["reasons"].append(f"{ref}: {reason}")
    return [
        ObservationGroup(
            refs=g["refs"],
            primary_source=_primary(g["sources"]),
            sources=sorted({s.value for s in g["sources"] if s}),
            merged_because=g["reasons"],
        )
        for g in groups
    ]


def assess_independence(refs_cited: list[str], refs: RefIndex) -> IndependenceAssessment:
    groups = group_observations(refs_cited, refs)
    sources = sorted({g.primary_source for g in groups})
    if len(sources) >= 2:
        status, detail = "independent", f"{len(groups)} observation(s) from {len(sources)} independent sources: {', '.join(sources)}"
    elif len(groups) >= 2:
        status, detail = "unknown", (f"{len(groups)} observation(s), all from {sources[0]}: independence cannot be "
                                     "established from one source")
    else:
        status, detail = "none", f"{len(groups)} independent observation(s)"
    merged = [m for g in groups for m in g.merged_because]
    if merged:
        detail += f" (merged as the same event: {'; '.join(merged)})"
    return IndependenceAssessment(status=status, independent_sources=sources if status == "independent" else [],
                                  groups=groups, detail=detail)


def _content_key(record: dict) -> str:
    return repr(sorted(record.items()))


def _echoes(log: dict, others: list[dict]) -> bool:
    """True if ``log`` is a log line naming one of the change records in ``others``."""
    message = str(log.get("message", ""))
    if not message or "level" not in log:
        return False
    for change in others:
        if "change_id" not in change:
            continue
        tokens = [change["change_id"], change.get("version") or "", (change.get("details") or {}).get("key", "")]
        for token in filter(None, tokens):
            # Exact token with boundaries; versions may carry a "v" prefix ("v6.0.0"), and 6.0.1 never matches 6.0.10.
            if len(token) >= 3 and re.search(rf"(?<![\w.])v?{re.escape(token)}(?![\w])", message):
                return True
    return False


def _primary(sources: list[EvidenceSource | None]) -> str:
    """A merged group is attributed to its originating source: a change echoed in logs is change history."""
    known = [s for s in sources if s]
    if EvidenceSource.CHANGES in known:
        return EvidenceSource.CHANGES.value
    return known[0].value if known else "unknown"
