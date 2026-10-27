"""Deterministic timeline checks: can a hypothesis's proposed change explain when the incident began?

For each hypothesis that cites change records (deployments, config changes, feature flags, rollbacks)
as support:

  onset       the first symptom of an episode: an ERROR/CRITICAL log line or an anomalous metric sample
              (app.agents.evidence) on the services the hypothesis cites plus the incident's affected
              services, followed by another symptom within EPISODE_GAP. Isolated earlier symptoms (a stray
              error an hour before) are ignored for onset and named in the detail. A metric sample only
              bounds onset to within its sampling interval.
  relation    per cited change, compared as UTC instants (never as strings), with TOLERANCE either side:
                precedes_onset            change at or before (onset - precision - tolerance)
                follows_onset             change at or after (onset + tolerance): it cannot explain the onset
                ambiguous                 within the tolerance or precision: no ordering is inferred
                unknown                   timestamp missing, invalid or without a timezone
                persisted_after_rollback  the change preceded onset, but a later successful rollback that
                                          demonstrably reverted it (previous_version = its version, or naming
                                          its id/version) was followed by symptoms after ROLLBACK_SETTLE
  worsened    for a change after onset: a symptom metric rose a further WORSENING_THRESHOLD above its
              pre-change peak, so the change may have worsened an existing incident.

A hypothesis is blocked as the cause of onset only when every cited change follows onset or persisted
after rollback. Missing or ambiguous timestamps never discard a hypothesis; they are reported as such.
Not modelled: changes whose effect is delayed (e.g. a cache expiry), symptoms not captured as ERROR logs or
metric anomalies, and whether a symptom belongs to this incident or a concurrent one.
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from app.agents.contracts import ChangeTiming, Hypothesis, TemporalAssessment
from app.agents.evidence import EvidenceClass, RefIndex
from app.schemas.evidence import EvidenceSource

TOLERANCE = timedelta(seconds=60)
ROLLBACK_SETTLE = timedelta(minutes=5)
# Onset is the first symptom followed by another within this gap; isolated earlier symptoms are ignored.
EPISODE_GAP = timedelta(minutes=30)
WORSENING_THRESHOLD = 0.25
SYMPTOM_LEVELS = frozenset({"ERROR", "CRITICAL"})


def parse_instant(value: Any) -> datetime | None:
    """An aware datetime in UTC, or None if missing, unparseable or without a timezone."""
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str) and value.strip():
        try:
            dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if dt.tzinfo is None or dt.utcoffset() is None:
        return None
    return dt.astimezone(UTC)


def _iso(dt: datetime | None) -> str | None:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ") if dt else None


@dataclass(frozen=True)
class _Symptom:
    ref: str
    service: str
    time: datetime  # when observed
    earliest: datetime  # earliest the symptom could have begun (sampling interval for metrics)
    series: tuple[str, str] | None
    value: float | None


def _symptoms(refs: RefIndex, services: set[str]) -> list[_Symptom]:
    out: list[_Symptom] = []
    series_times: dict[tuple[str, str], list[datetime]] = {}
    for _, source, r in refs.records():
        t = parse_instant(r.get("timestamp"))
        if source is EvidenceSource.METRICS and t:
            series_times.setdefault((r["service"], r["metric_name"]), []).append(t)
    intervals = {k: min((b - a for a, b in zip(sorted(v), sorted(v)[1:]) if b > a), default=timedelta(0))
                 for k, v in series_times.items()}
    for ref, source, r in refs.records():
        if r.get("service") not in services:
            continue
        t = parse_instant(r.get("timestamp"))
        if t is None:
            continue
        if source is EvidenceSource.LOGS and r.get("level") in SYMPTOM_LEVELS:
            out.append(_Symptom(ref, r["service"], t, t, None, None))
        elif source is EvidenceSource.METRICS and refs.classify(ref).evidence_class is EvidenceClass.DIRECT:
            key = (r["service"], r["metric_name"])
            out.append(_Symptom(ref, r["service"], t, t - intervals.get(key, timedelta(0)), key, float(r["value"])))
    return sorted(out, key=lambda s: (s.time, s.ref))


def assess(hypothesis: Hypothesis, refs: RefIndex, affected_services: list[str]) -> TemporalAssessment:
    leaves = refs.direct_leaves(hypothesis.supporting_refs)
    cited = [(ref, found) for ref in leaves if (found := refs.record(ref)) is not None]
    changes = [(ref, rec) for ref, (src, rec) in cited if src is EvidenceSource.CHANGES]
    base = dict(hypothesis_id=hypothesis.hypothesis_id, onset=None, onset_refs=[], onset_precision_seconds=None,
                changes=[], blocks_cause=False, may_have_worsened=False, worsening_refs=[])
    if not changes:
        return TemporalAssessment(**base, status="not_applicable", detail="the hypothesis cites no change as its cause")

    services = {rec.get("service") for _, (src, rec) in cited if src is not EvidenceSource.CHANGES} | set(affected_services)
    symptoms = _symptoms(refs, {s for s in services if s})
    first, isolated = _episode_onset(symptoms)
    timings = [_timing(ref, rec, first, symptoms, refs) for ref, rec in changes]
    ignored = f" (earlier isolated symptom(s) not counted as onset: {', '.join(isolated[:3])})" if isolated else ""
    if not symptoms:
        return TemporalAssessment(**{**base, "changes": timings}, status="unknown",
                                  detail="no timestamped symptom (ERROR log or metric anomaly) to compare with")
    if first is None:
        return TemporalAssessment(**{**base, "changes": timings}, status="unknown",
                                  detail=f"every symptom is isolated (none followed by another within "
                                         f"{EPISODE_GAP.seconds // 60} min), so no onset is established")

    onset = dict(onset=_iso(first.time), onset_refs=[s.ref for s in symptoms if s.time == first.time],
                 onset_precision_seconds=(first.time - first.earliest).total_seconds())
    relations = [t.relation for t in timings]
    if "precedes_onset" in relations:
        status, blocks = "consistent", False
    elif "ambiguous" in relations:
        status, blocks = "ordering_ambiguous", False
    elif all(r in ("follows_onset", "persisted_after_rollback") for r in relations):
        status, blocks = ("change_follows_onset" if "follows_onset" in relations else "persisted_after_rollback"), True
    else:
        status, blocks = "unknown", False

    worsening = _worsening([t for t in timings if t.relation == "follows_onset"], symptoms)
    detail = "; ".join(t.detail for t in timings) + ignored
    return TemporalAssessment(hypothesis_id=hypothesis.hypothesis_id, status=status, changes=timings,
                              blocks_cause=blocks, may_have_worsened=bool(worsening), worsening_refs=worsening,
                              detail=detail, **onset)


def _episode_onset(symptoms: list[_Symptom]) -> tuple[_Symptom | None, list[str]]:
    """The first symptom of an episode: one followed by another symptom within EPISODE_GAP.

    Earlier isolated symptoms (e.g. a stray error an hour before) are returned separately and not used as onset,
    so background noise cannot exclude a true cause. With a single symptom, that symptom is the onset; if every
    symptom is isolated, no onset is established (None), which never excludes anything.
    """
    if len(symptoms) == 1:
        return symptoms[0], []
    for i, s in enumerate(symptoms):
        if any(timedelta(0) <= o.time - s.time <= EPISODE_GAP for o in symptoms[i + 1:]):
            return s, [x.ref for x in symptoms[:i]]
    return None, [x.ref for x in symptoms]


def _timing(ref: str, change: dict, first: _Symptom | None, symptoms: list[_Symptom], refs: RefIndex) -> ChangeTiming:
    tc = parse_instant(change.get("timestamp"))
    common = dict(change_ref=ref, change_id=str(change.get("change_id", "?")), change_type=str(change.get("change_type", "?")),
                  change_status=str(change.get("status", "?")), service=str(change.get("service", "?")), time=_iso(tc))
    note = " (the change did not complete; its effect is uncertain)" if change.get("status") == "failed" else ""
    if tc is None:
        return ChangeTiming(**common, relation="unknown",
                            detail=f"{common['change_id']}: timestamp missing, invalid or without a timezone; order unknown")
    if first is None:
        return ChangeTiming(**common, relation="unknown",
                            detail=f"{common['change_id']} at {_iso(tc)}: no symptom episode to compare with")
    if tc <= first.earliest - TOLERANCE:
        relation, detail = "precedes_onset", f"{common['change_id']} at {_iso(tc)} precedes the first symptom at {_iso(first.time)} ({first.ref})"
    elif tc >= first.time + TOLERANCE:
        relation, detail = "follows_onset", (f"{common['change_id']} at {_iso(tc)} follows the first symptom at {_iso(first.time)} "
                                             f"({first.ref}); it cannot explain the onset")
    else:
        span = (first.time - first.earliest).total_seconds() + TOLERANCE.total_seconds()
        relation, detail = "ambiguous", (f"{common['change_id']} at {_iso(tc)} is within {span:.0f}s of the first symptom at "
                                         f"{_iso(first.time)} ({first.ref}); their order cannot be determined")
    rollback_ref, persisted = None, []
    if relation == "precedes_onset":
        rollbacks = sorted(
            ((rt, rref, r.get("change_id", "?")) for rref, src, r in refs.records()
             if src is EvidenceSource.CHANGES and r.get("change_type") == "rollback" and r.get("status") == "succeeded"
             and r.get("service") == change.get("service") and rref != ref and _reverts(r, change)
             and (rt := parse_instant(r.get("timestamp"))) is not None and rt > tc),
        )
        if rollbacks:
            tr, rollback_ref, rollback_id = rollbacks[0]
            persisted = [s.ref for s in symptoms if s.time > tr + ROLLBACK_SETTLE]
            if persisted:
                relation = "persisted_after_rollback"
                detail = (f"{common['change_id']} at {_iso(tc)} was rolled back by {rollback_id} at {_iso(tr)} ({rollback_ref}), but symptoms "
                          f"continued more than {ROLLBACK_SETTLE.seconds // 60} min later ({', '.join(persisted[:3])}); "
                          "it does not explain the incident")
    return ChangeTiming(**common, relation=relation, detail=detail + note, rollback_ref=rollback_ref, persisted_refs=persisted)


def _reverts(rollback: dict, change: dict) -> bool:
    """True only on positive evidence that ``rollback`` reverted ``change``.

    Either the rollback's previous_version is the change's version, or its summary or details name the
    change's id or version. A rollback of some other change on the same service never excludes this one.
    """
    version = change.get("version")
    if version and rollback.get("previous_version") == version:
        return True
    text = f"{rollback.get('summary', '')} {rollback.get('details') or ''}"
    return any(token and len(str(token)) >= 3 and str(token) in text for token in (change.get("change_id"), version))


def _worsening(later_changes: list[ChangeTiming], symptoms: list[_Symptom]) -> list[str]:
    refs: list[str] = []
    for change in later_changes:
        tc = parse_instant(change.time)
        by_series: dict[tuple[str, str], list[_Symptom]] = {}
        for s in symptoms:
            if s.series:
                by_series.setdefault(s.series, []).append(s)
        for points in by_series.values():
            before = [p.value for p in points if p.time < tc]
            after = [p for p in points if p.time > tc + TOLERANCE]
            if before and after:
                peak_after = max(after, key=lambda p: p.value)
                if max(before) > 0 and peak_after.value >= max(before) * (1 + WORSENING_THRESHOLD):
                    refs.append(peak_after.ref)
    return list(dict.fromkeys(refs))
