import logging
from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.schemas.evidence import (
    ChangeType,
    Completeness,
    EvidenceSource,
    EvidenceStatus,
    LogLevel,
    ToolErrorCode,
)
from app.schemas.queries import LogSearchQuery
from app.tools import (
    TOOL_CLASSES,
    FaultInjector,
    FixtureStore,
    LogSearchTool,
    SimulatedFault,
    build_evidence_tools,
)
from app.tools.base import ABSENCE_NOTE

# Scenario windows in the shared fixtures (see data/README.md).
PAYMENTS = {"start": "2026-10-20T13:30:00Z", "end": "2026-10-20T15:00:00Z"}
ORDERS = {"start": "2026-10-22T08:30:00Z", "end": "2026-10-22T10:00:00Z"}
FRONTEND = {"start": "2026-10-24T17:30:00Z", "end": "2026-10-24T19:00:00Z"}
QUIET = {"start": "2026-10-10T00:00:00Z", "end": "2026-10-10T06:00:00Z"}

# One valid query per tool, used for behaviour that every tool shares.
VALID_QUERIES = {
    "search_logs": PAYMENTS,
    "query_metrics": PAYMENTS,
    "get_change_history": PAYMENTS,
    "get_service_dependencies": {"service": "payments-api"},
    "search_historical_incidents": {"keywords": ["payments"]},
}


def test_every_tool_has_a_documented_contract():
    assert {cls.name for cls in TOOL_CLASSES} == set(VALID_QUERIES)
    for cls in TOOL_CLASSES:
        assert cls.description
        assert cls.query_model.model_json_schema()["properties"]
        assert cls.record_model.model_json_schema()["properties"]


# --- search_logs -----------------------------------------------------------------


def test_logs_filter_by_service_level_and_window(tools):
    result = tools["search_logs"].run({**PAYMENTS, "services": ["payments-api"], "min_level": "ERROR"})

    assert result.status is EvidenceStatus.OK
    assert result.source is EvidenceSource.LOGS
    assert result.record_count == 7
    assert {r.service for r in result.records} == {"payments-api"}
    assert all(r.level.rank >= LogLevel.ERROR.rank for r in result.records)
    timestamps = [r.timestamp for r in result.records]
    assert timestamps == sorted(timestamps)


def test_logs_keyword_match_is_case_insensitive_any(tools):
    result = tools["search_logs"].run({**PAYMENTS, "keywords": ["FRAUD-CHECK", "pool saturated"]})

    messages = [r.message for r in result.records]
    assert len(messages) == 5
    assert all("fraud-check" in m.lower() or "pool saturated" in m.lower() for m in messages)


def test_logs_filter_by_trace_id(tools):
    result = tools["search_logs"].run({**PAYMENTS, "trace_id": "tr-a103"})

    assert [r.service for r in result.records] == ["payments-api"] * 3 + ["api-gateway"]


def test_logs_accept_a_query_model_instance(tools):
    query = LogSearchQuery.model_validate({**PAYMENTS, "services": ["postgres-payments"]})

    result = tools["search_logs"].run(query)

    assert result.record_count == 2
    assert result.query["services"] == ["postgres-payments"]


def test_logs_known_gap_marks_result_partial(tools):
    result = tools["search_logs"].run({**FRONTEND, "services": ["web-frontend"]})

    assert result.status is EvidenceStatus.PARTIAL
    assert result.record_count > 0
    assert any("Log shipper backlog" in w for w in result.warnings)


# --- query_metrics ---------------------------------------------------------------


def test_metrics_filter_by_service_and_metric(tools):
    result = tools["query_metrics"].run(
        {**PAYMENTS, "services": ["payments-api"], "metric_names": ["latency_p95_ms"]}
    )

    assert result.status is EvidenceStatus.OK
    assert result.record_count == 18
    assert {(r.service, r.metric_name, r.unit) for r in result.records} == {("payments-api", "latency_p95_ms", "ms")}
    assert result.records[0].value < 200 < 2000 < result.records[-1].value


def test_metrics_missing_source_is_partial_not_proof_of_absence(tools):
    result = tools["query_metrics"].run({**PAYMENTS, "services": ["fraud-check"]})

    assert result.records == []
    assert result.status is EvidenceStatus.PARTIAL
    assert any("Vendor metrics export delayed" in w for w in result.warnings)
    assert ABSENCE_NOTE in result.warnings


def test_metrics_truncation_is_reported(tools):
    result = tools["query_metrics"].run({**PAYMENTS, "limit": 10})

    assert result.record_count == 10
    assert result.truncated
    assert result.completeness is Completeness.PARTIAL
    assert any("first 10 of" in w for w in result.warnings)


# --- get_change_history ----------------------------------------------------------


def test_changes_in_window_include_deploy_and_config_change(tools):
    result = tools["get_change_history"].run({**PAYMENTS, "services": ["payments-api"]})

    assert result.status is EvidenceStatus.OK
    assert [r.change_id for r in result.records] == ["CHG-3101", "CHG-3102"]
    assert result.records[1].details == {"key": "db.pool.max_size", "old": "50", "new": "20"}


def test_changes_filter_by_type(tools):
    result = tools["get_change_history"].run({**ORDERS, "change_types": [ChangeType.FEATURE_FLAG]})

    assert [r.change_id for r in result.records] == ["CHG-3120"]


def test_unfiltered_query_inherits_gaps_of_any_service(tools):
    # No service filter means "all services", which includes fraud-check's invisible history.
    result = tools["get_change_history"].run(PAYMENTS)

    assert result.status is EvidenceStatus.PARTIAL
    assert result.record_count == 2


def test_changes_for_third_party_are_flagged_as_unknown(tools):
    result = tools["get_change_history"].run({**PAYMENTS, "services": ["fraud-check"]})

    assert result.records == []
    assert result.status is EvidenceStatus.PARTIAL
    assert any("not visible to us" in w for w in result.warnings)


# --- get_service_dependencies ----------------------------------------------------


def test_dependencies_direct_downstream(tools):
    result = tools["get_service_dependencies"].run({"service": "payments-api", "direction": "downstream"})

    assert [(r.service, r.depends_on) for r in result.records] == [
        ("payments-api", "fraud-check"),
        ("payments-api", "postgres-payments"),
        ("payments-api", "redis-cache"),
    ]


def test_dependencies_upstream_transitive(tools):
    result = tools["get_service_dependencies"].run({"service": "payments-api", "direction": "upstream", "depth": 2})

    assert [(r.service, r.depends_on) for r in result.records] == [
        ("api-gateway", "payments-api"),
        ("web-frontend", "api-gateway"),
    ]


def test_dependencies_both_directions_do_not_include_siblings(tools):
    result = tools["get_service_dependencies"].run({"service": "redis-cache", "direction": "both", "depth": 2})

    edges = {(r.service, r.depends_on) for r in result.records}
    assert ("web-frontend", "redis-cache") in edges
    assert ("api-gateway", "payments-api") in edges  # caller of a caller
    assert ("payments-api", "postgres-payments") not in edges  # sibling dependency, not related


def test_dependencies_unknown_service_is_not_reported_as_having_none(tools):
    result = tools["get_service_dependencies"].run({"service": "billing-service"})

    assert result.status is EvidenceStatus.NO_MATCHES
    assert any("not in the dependency catalog" in w for w in result.warnings)


# --- search_historical_incidents -------------------------------------------------


def test_history_ranks_by_keyword_matches_then_recency(tools):
    result = tools["search_historical_incidents"].run(
        {"keywords": ["connection pool", "payments"], "services": ["payments-api"]}
    )

    assert [r.incident_id for r in result.records] == ["HIST-1873", "HIST-1655"]


def test_history_filters_by_severity_and_date(tools):
    result = tools["search_historical_incidents"].run(
        {"severities": ["medium"], "occurred_after": "2026-05-01T00:00:00Z"}
    )

    assert [r.incident_id for r in result.records] == ["HIST-1790"]


def test_history_keeps_unknown_root_cause(tools):
    result = tools["search_historical_incidents"].run({"keywords": ["email"]})

    assert [r.incident_id for r in result.records] == ["HIST-1801"]
    assert result.records[0].root_cause is None


# --- Empty results ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("tool_name", "query"),
    [
        ("search_logs", QUIET),
        ("query_metrics", QUIET),
        ("get_change_history", {**QUIET, "services": ["orders-service"]}),
        ("search_historical_incidents", {"keywords": ["kubernetes upgrade"]}),
    ],
)
def test_no_matches_is_explicit_and_complete(tools, tool_name, query):
    result = tools[tool_name].run(query)

    assert result.status is EvidenceStatus.NO_MATCHES
    assert result.completeness is Completeness.COMPLETE
    assert result.records == []
    assert result.errors == []
    assert ABSENCE_NOTE in result.warnings


# --- Failures ---------------------------------------------------------------------


@pytest.mark.parametrize("tool_name", sorted(VALID_QUERIES))
@pytest.mark.parametrize(
    ("fault", "code"), [(SimulatedFault.TIMEOUT, ToolErrorCode.TIMEOUT), (SimulatedFault.UNAVAILABLE, ToolErrorCode.UNAVAILABLE)]
)
def test_injected_faults_return_failed_results(fixture_store, tool_name, fault, code):
    tools = build_evidence_tools(fixture_store, FaultInjector({tool_name: fault}))

    result = tools[tool_name].run(VALID_QUERIES[tool_name])

    assert result.status is EvidenceStatus.FAILED
    assert result.completeness is Completeness.NONE
    assert result.records == []
    [error] = result.errors
    assert error.code is code
    assert error.simulated is True
    assert error.retryable is True
    assert "Simulated" in error.message


def test_fault_only_affects_the_targeted_tool(fixture_store):
    tools = build_evidence_tools(fixture_store, FaultInjector({"search_logs": SimulatedFault.TIMEOUT}))

    assert tools["search_logs"].run(PAYMENTS).status is EvidenceStatus.FAILED
    assert tools["query_metrics"].run({**PAYMENTS, "services": ["payments-api"]}).status is EvidenceStatus.OK


@pytest.mark.parametrize(
    ("tool_name", "query", "expected_fragment"),
    [
        ("search_logs", {"start": "2026-10-20T15:00:00Z", "end": "2026-10-20T14:00:00Z"}, "end must be after start"),
        ("search_logs", {"start": "2026-10-20T14:00:00", "end": "2026-10-20T15:00:00"}, "timezone"),
        ("query_metrics", {**PAYMENTS, "limit": 0}, "limit"),
        ("get_service_dependencies", {}, "service"),
        ("search_historical_incidents", {"keyword": ["x"]}, "keyword"),
    ],
)
def test_invalid_queries_return_structured_errors(tools, tool_name, query, expected_fragment):
    result = tools[tool_name].run(query)

    assert result.status is EvidenceStatus.FAILED
    [error] = result.errors
    assert error.code is ToolErrorCode.INVALID_QUERY
    assert error.retryable is False
    assert error.simulated is False
    assert expected_fragment in error.message
    assert result.query == query


def test_missing_dataset_is_a_real_unsimulated_failure(tmp_path: Path):
    tool = LogSearchTool(FixtureStore(tmp_path))

    result = tool.run(PAYMENTS)

    [error] = result.errors
    assert error.code is ToolErrorCode.DATA_UNAVAILABLE
    assert error.simulated is False
    assert str(tmp_path) not in error.message


# --- Malformed data ---------------------------------------------------------------


def test_malformed_lines_are_skipped_and_reported(tmp_path: Path):
    (tmp_path / "logs.jsonl").write_text(
        "\n".join(
            [
                '{"timestamp": "2026-10-20T14:00:00Z", "service": "a", "level": "ERROR", "message": "boom"}',
                '{"timestamp": "2026-10-20T14:01:00Z", "service": "a", "level": "ERR',  # truncated JSON
                '{"timestamp": "2026-10-20T14:02:00Z", "service": "a", "level": "LOUD", "message": "x"}',
                "",
                '{"timestamp": "2026-10-20T14:03:00Z", "service": "a", "level": "INFO", "message": "ok"}',
            ]
        ),
        encoding="utf-8",
    )

    result = LogSearchTool(FixtureStore(tmp_path)).run(PAYMENTS)

    assert result.status is EvidenceStatus.PARTIAL
    assert [r.message for r in result.records] == ["boom", "ok"]
    assert any("2 malformed record(s)" in w and "line 2" in w for w in result.warnings)


def test_unreadable_coverage_metadata_is_flagged(tmp_path: Path):
    (tmp_path / "logs.jsonl").write_text(
        '{"timestamp": "2026-10-20T14:00:00Z", "service": "a", "level": "INFO", "message": "m"}\n', encoding="utf-8"
    )
    (tmp_path / "coverage_gaps.jsonl").write_text("not json\n", encoding="utf-8")

    result = LogSearchTool(FixtureStore(tmp_path)).run(PAYMENTS)

    assert result.status is EvidenceStatus.OK
    assert any("coverage-gap entries could not be read" in w for w in result.warnings)


# --- Output contract ----------------------------------------------------------------


def test_results_serialise_to_json_with_status(tools):
    payload = tools["get_change_history"].run({**PAYMENTS, "services": ["payments-api"]}).model_dump(mode="json")

    assert payload["status"] == "ok"
    assert payload["record_count"] == len(payload["records"])
    assert payload["query"]["start"] == "2026-10-20T13:30:00Z"


def test_tool_logging_does_not_include_record_content(tools, caplog):
    with caplog.at_level(logging.INFO, logger="app.tools.base"):
        tools["search_logs"].run(PAYMENTS)

    assert "tool=search_logs status=ok" in caplog.text
    assert "DB connection" not in caplog.text
