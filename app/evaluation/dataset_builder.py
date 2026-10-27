"""Authoring source for the synthetic evaluation dataset (data/eval).

Run ``python -m app.evaluation.dataset_builder`` to regenerate. The generated files are committed
and ``tests/test_eval_dataset.py`` checks they match this module exactly.

Layout written:
    data/eval/cases/<case_id>/   investigation INPUTS only: incident.json, conditions.json and the
                                 evidence JSONL files the five tools read (same format as data/fixtures)
    data/eval/labels.jsonl       GOLD LABELS, outside every case directory. Only the scorer reads it.

Each case is an independent world: its own logs, metrics, changes and coverage gaps. The service
topology and the historical incidents are copied from data/fixtures, so they are the same for all
cases. Timestamps use T = the incident onset; the reporter sees symptoms some minutes later.
"""

import json
import shutil
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
SHARED_FIXTURES = ROOT / "data" / "fixtures"
EVAL_DIR = ROOT / "data" / "eval"

NOISE = [0.0, 0.02, -0.015, 0.01, -0.02, 0.015, -0.005]  # fixed pattern: deterministic "jitter"


class Case:
    def __init__(self, case_id: str, onset: str) -> None:
        self.case_id = case_id
        self.t0 = datetime.fromisoformat(onset.replace("Z", "+00:00"))
        self.logs: list[dict] = []
        self.metrics: list[dict] = []
        self.changes: list[dict] = []
        self.extra_dependencies: list[dict] = []
        self.gaps: list[dict] = []
        self.conditions: dict[str, Any] = {"faults": {}}
        self.incident: dict[str, Any] = {}
        self.label: dict[str, Any] = {}

    def at(self, minutes: float) -> str:
        return (self.t0 + timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")

    def log(self, minutes: float, service: str, level: str, message: str, trace: str | None = None) -> None:
        row = {"timestamp": self.at(minutes), "service": service, "level": level, "message": message}
        if trace:
            row["trace_id"] = trace
        self.logs.append(row)

    def series(self, service: str, metric: str, unit: str, before: float, after: float, change_at: float = 0,
               start: int = -60, end: int = 60) -> None:
        """5-minute samples: ``before`` until ``change_at`` minutes, then ``after``, with fixed jitter.

        Percentages are bounded: jitter never pushes a value outside 0-100 (a saturated pool stays at 100%).
        """
        for i, minute in enumerate(range(start, end + 1, 5)):
            base = before if minute < change_at else after
            value = round(base * (1 + NOISE[i % len(NOISE)]), 2)
            if unit == "percent":
                value = min(max(value, 0.0), 100.0)
            self.metrics.append({
                "timestamp": self.at(minute), "service": service, "metric_name": metric, "value": value, "unit": unit,
            })

    def change(self, change_id: str, minutes: float, service: str, change_type: str, summary: str,
               status: str = "succeeded", **extra: Any) -> None:
        self.changes.append({"change_id": change_id, "timestamp": self.at(minutes), "service": service,
                             "change_type": change_type, "status": status, "summary": summary, **extra})

    def gap(self, source: str, service: str | None, start: float, end: float, reason: str) -> None:
        self.gaps.append({"source": source, "service": service, "start": self.at(start), "end": self.at(end),
                          "reason": reason})

    def report(self, summary: str, reported_after: float, services=(), severity=None, context=None, reporter=None):
        self.incident = {"incident_id": self.case_id, "summary": summary, "affected_services": list(services),
                         "severity": severity, "reported_at": self.at(reported_after), "reporter": reporter,
                         "context": context}


def root_cause(label_id: str, summary: str, match_all: list[list[str]], key_evidence: list[dict]) -> dict:
    return {"label_id": label_id, "summary": summary, "match_all": match_all, "key_evidence": key_evidence}


def pattern(description: str, match_all: list[list[str]]) -> dict:
    return {"description": description, "match_all": match_all}


# --- Cases ---------------------------------------------------------------------------------------


def eval_01() -> Case:
    c = Case("EVAL-01", "2026-09-01T10:00:00Z")
    c.report("Card payments are slow and failing at checkout", 12, ["payments-api"], "high",
             "Support reports checkout errors since about 10:00 UTC.", "on-call SRE")
    c.change("CHG-E01-1", -190, "orders-service", "deployment", "Routine dependency updates", version="6.2.1")
    c.change("CHG-E01-2", -5, "payments-api", "config_change", "Reduce database connection pool size",
             details={"key": "db.pool.max_size", "old": "40", "new": "8"}, changed_by="platform-team")
    c.log(-4, "payments-api", "INFO", "Config reloaded: db.pool.max_size=8")
    for m, tr in [(2, "e1a"), (5, "e1b"), (14, "e1c"), (27, "e1d")]:
        c.log(m, "payments-api", "ERROR", "Timed out waiting for DB connection from pool after 3000ms", tr)
    c.log(6, "payments-api", "WARNING", "Connection pool saturated: 8/8 in use, 52 waiting")
    c.log(7, "api-gateway", "ERROR", "Upstream payments-api returned 504 for POST /checkout/pay", "e1b")
    c.series("payments-api", "latency_p95_ms", "ms", 180, 2400)
    c.series("payments-api", "error_rate_percent", "percent", 0.2, 7.5)
    c.series("payments-api", "db_pool_utilization_percent", "percent", 38, 100, change_at=-5)
    c.series("payments-api", "request_rate_rps", "rps", 120, 121)
    c.series("postgres-payments", "cpu_percent", "percent", 31, 29)
    c.label = {
        "scenario": ["config_change_precedes_incident"],
        "expected_outcome": "root_cause",
        "root_cause": root_cause("payments-pool-reduction", "A config change cut payments-api's DB pool from 40 to 8, exhausting connections.",
                                 [["pool"], ["size", "max_size", "reduc", "config", "8"]],
                                 [{"field": "change_id", "equals": "CHG-E01-2"},
                                  {"field": "message", "contains": "pool"}]),
        "acceptable_alternatives": [],
        "inconclusive_acceptable": False,
        "distractors": [pattern("Unrelated orders-service deployment 3 hours earlier", [["orders-service"], ["deploy", "dependency update"]])],
        "must_consider": [],
        "rationale": "Pool-size change 5 minutes before onset, a log confirming it applied, pool at 100% and DB CPU flat. "
                     "The evidence is direct and consistent, so an inconclusive result is a miss.",
    }
    return c


def eval_02() -> Case:
    c = Case("EVAL-02", "2026-09-02T14:00:00Z")
    c.report("Order placement failing since shortly after today's orders-service deploy", 5, ["orders-service"], "high",
             "The deploy went out around 13:50 UTC; errors were noticed soon after.", "orders team")
    c.change("CHG-E02-1", -8, "orders-service", "deployment", "Update order confirmation email template",
             version="6.0.0", previous_version="5.9.4", changed_by="deploy-bot")
    c.log(-24, "postgres-orders", "ERROR", "could not extend file \"base/16384/2619\": No space left on device")
    c.log(-23, "orders-service", "ERROR", "Database write failed: could not extend file (No space left on device)", "e2a")
    c.log(-10, "postgres-orders", "ERROR", "could not write to temporary file: No space left on device")
    c.log(-7, "orders-service", "INFO", "Starting orders-service v6.0.0")
    c.log(1, "orders-service", "ERROR", "Database write failed: could not extend file (No space left on device)", "e2b")
    c.log(12, "orders-service", "ERROR", "Order ORD-91822 rejected: database unavailable for writes", "e2c")
    c.series("postgres-orders", "disk_used_percent", "percent", 88, 100, change_at=-25)
    c.series("orders-service", "error_rate_percent", "percent", 0.4, 6.2, change_at=-25)
    c.series("orders-service", "latency_p95_ms", "ms", 210, 260, change_at=-25)
    c.label = {
        "scenario": ["misleading_deployment_correlation"],
        "expected_outcome": "root_cause",
        "root_cause": root_cause("orders-db-disk-full", "postgres-orders ran out of disk space; writes failed before the deploy.",
                                 [["disk", "space", "storage"]],
                                 [{"field": "message", "contains": "No space left on device"},
                                  {"field": "metric_name", "equals": "disk_used_percent"}]),
        "acceptable_alternatives": [],
        "inconclusive_acceptable": False,
        "distractors": [pattern("The orders-service v6.0.0 deploy (email template only)", [["deploy", "template", "v6.0.0", "6.0.0"]])],
        "must_consider": [],
        "rationale": "The report blames the deploy, but disk errors and the error-rate rise start 17 minutes before it, "
                     "and the deploy only changed an email template.",
    }
    return c


def eval_03() -> Case:
    c = Case("EVAL-03", "2026-09-03T09:00:00Z")
    c.report("Orders intermittently rejected as 'item unavailable'; inventory dashboards look healthy", 15,
             ["orders-service"], "medium", "Inventory error-rate panel is flat.", "orders team")
    c.change("CHG-E03-1", -10, "inventory-service", "feature_flag", "Roll out batch reservations to all traffic",
             details={"flag": "inventory.batch_reservations", "old": "10%", "new": "100%"}, changed_by="inventory-team")
    for m in (-30, -5, 10, 25):
        c.log(m, "inventory-service", "INFO", "Health check OK")
    c.log(2, "orders-service", "WARNING", "Inventory reservation conflict (409) for SKU-1142; retrying", "e3a")
    c.log(3, "inventory-service", "WARNING", "Lock wait timeout exceeded on table stock_reservations", "e3a")
    c.log(4, "orders-service", "ERROR", "Order rejected: item unavailable (SKU-1142) after 3 reservation attempts", "e3a")
    c.log(11, "postgres-inventory", "WARNING", "Deadlock detected; transaction aborted")
    c.log(18, "orders-service", "ERROR", "Order rejected: item unavailable (SKU-2210) after 3 reservation attempts", "e3b")
    c.series("orders-service", "error_rate_percent", "percent", 0.5, 5.0)
    c.series("inventory-service", "error_rate_percent", "percent", 0.3, 0.3)
    c.series("inventory-service", "latency_p95_ms", "ms", 90, 140)
    c.series("postgres-inventory", "lock_waits_per_min", "count", 2, 45, change_at=-5)
    c.label = {
        "scenario": ["conflicting_logs_and_metrics", "config_change_precedes_incident"],
        "expected_outcome": "root_cause",
        "root_cause": root_cause("inventory-batch-flag-locking", "The batch-reservations flag rollout caused lock contention on stock_reservations.",
                                 [["batch reservation", "batch_reservations", "inventory.batch"]],
                                 [{"field": "change_id", "equals": "CHG-E03-1"},
                                  {"field": "message", "contains": "Lock wait timeout"}]),
        "acceptable_alternatives": [root_cause("inventory-lock-contention", "Lock contention in the inventory database (trigger not named).",
                                               [["lock", "deadlock", "contention"], ["inventory", "stock_reservations"]], [])],
        "inconclusive_acceptable": False,
        "distractors": [],
        "must_consider": [],
        "rationale": "Inventory's own error rate and health checks look fine (409s count as client errors), "
                     "but orders logs, DB lock waits and the flag rollout 10 minutes earlier agree.",
    }
    return c


def eval_04() -> Case:
    c = Case("EVAL-04", "2026-09-04T16:00:00Z")
    c.report("Checkout slow; some payments time out", 10, ["payments-api"], "high", None, "on-call SRE")
    c.change("CHG-E04-1", -360, "notification-service", "deployment", "Template engine upgrade", version="1.8.0")
    c.gap("metrics", "fraud-check", -120, 120, "Vendor metrics export delayed; no fraud-check latency data for this window.")
    c.log(1, "payments-api", "WARNING", "Slow response from fraud-check: 1900ms", "e4a")
    for m, tr in [(3, "e4b"), (9, "e4c"), (21, "e4d")]:
        c.log(m, "payments-api", "ERROR", "Request to fraud-check timed out after 2000ms", tr)
    c.log(22, "api-gateway", "ERROR", "Upstream payments-api returned 504 for POST /checkout/pay", "e4d")
    c.series("payments-api", "latency_p95_ms", "ms", 175, 2100)
    c.series("payments-api", "error_rate_percent", "percent", 0.2, 4.1)
    c.series("payments-api", "db_pool_utilization_percent", "percent", 36, 37)
    c.series("postgres-payments", "cpu_percent", "percent", 30, 31)
    c.label = {
        "scenario": ["missing_or_delayed_metrics", "dependency_failure"],
        "expected_outcome": "root_cause",
        "root_cause": root_cause("fraud-check-degradation", "The external fraud-check service slowed down (vendor-side); its metrics are missing.",
                                 [["fraud"], ["slow", "timeout", "timed out", "latency", "degrad", "vendor", "external"]],
                                 [{"field": "message", "contains": "fraud-check timed out"}]),
        "acceptable_alternatives": [],
        "inconclusive_acceptable": True,
        "distractors": [pattern("Notification-service deploy 6 hours earlier", [["notification", "template engine"]])],
        "must_consider": [],
        "rationale": "Logs point to fraud-check timeouts and local resources are normal, but fraud-check metrics are "
                     "missing, so the vendor-side cause is probable rather than proven. Inconclusive is acceptable.",
    }
    return c


def eval_05() -> Case:
    c = Case("EVAL-05", "2026-09-05T08:00:00Z")
    c.report("Customers are not receiving order confirmation emails", 30, [], None,
             "Orders themselves complete normally.", "customer support")
    c.change("CHG-E05-1", -120, "web-frontend", "deployment", "New product carousel", version="3.4.0")
    for m in (0, 4, 12, 25, 40):
        c.log(m, "notification-service", "ERROR", "email-provider returned HTTP 503 Service Unavailable")
    c.log(20, "notification-service", "WARNING", "Retry queue depth 1200; deferring sends")
    c.log(15, "orders-service", "INFO", "Order ORD-77120 created")
    c.series("notification-service", "queue_depth", "count", 10, 3000)
    c.series("notification-service", "email_send_success_rate_percent", "percent", 99.5, 3)
    c.series("orders-service", "error_rate_percent", "percent", 0.4, 0.4)
    c.label = {
        "scenario": ["dependency_failure"],
        "expected_outcome": "root_cause",
        "root_cause": root_cause("email-provider-outage", "The external email-provider is failing with HTTP 503.",
                                 [["email-provider", "email provider", "provider"], ["503", "outage", "unavailable", "down", "fail"]],
                                 [{"field": "message", "contains": "email-provider returned HTTP 503"}]),
        "acceptable_alternatives": [],
        "inconclusive_acceptable": False,
        "distractors": [pattern("Unrelated web-frontend deploy 2 hours earlier", [["carousel", "web-frontend"]])],
        "must_consider": [],
        "rationale": "Direct 503 errors from the provider from the moment success rate drops; no relevant change.",
    }
    return c


def eval_06() -> Case:
    c = Case("EVAL-06", "2026-09-06T11:00:00Z")
    c.report("Order API requests timing out", 10, ["orders-service"], "high", None, "on-call SRE")
    c.change("CHG-E06-1", -15, "orders-service", "deployment", "Retry inventory calls up to 5 times without backoff",
             version="6.1.0", previous_version="6.0.3", changed_by="deploy-bot (PR #4630)")
    for m, a in [(-12, 3), (-6, 5), (2, 5), (14, 4)]:
        c.log(m, "orders-service", "WARNING", f"Retrying inventory reservation (attempt {a}/5)")
    c.log(3, "orders-service", "ERROR", "Inventory call timed out after 1000ms; giving up")
    c.log(5, "inventory-service", "WARNING", "Thread pool exhausted; rejecting request")
    c.series("inventory-service", "request_rate_rps", "rps", 200, 1000, change_at=-14)
    c.series("inventory-service", "latency_p95_ms", "ms", 90, 1800, change_at=-10)
    c.series("orders-service", "request_rate_rps", "rps", 200, 202)
    c.series("orders-service", "error_rate_percent", "percent", 0.4, 9.0, change_at=-10)
    c.label = {
        "scenario": ["retry_or_timeout_problem", "config_change_precedes_incident"],
        "expected_outcome": "root_cause",
        "root_cause": root_cause("orders-retry-storm", "orders-service v6.1.0 retries without backoff, multiplying load on inventory-service.",
                                 [["retry", "retries"], ["inventory", "storm", "amplif", "overload", "backoff", "load"]],
                                 [{"field": "change_id", "equals": "CHG-E06-1"},
                                  {"field": "message", "contains": "Retrying inventory reservation"}]),
        "acceptable_alternatives": [],
        "inconclusive_acceptable": False,
        "distractors": [pattern("Inventory capacity problem / traffic spike", [["traffic spike", "capacity"]])],
        "must_consider": [],
        "rationale": "Inventory request rate rose 5x while orders' own traffic stayed flat, starting right after the retry change.",
    }
    return c


def eval_07() -> Case:
    c = Case("EVAL-07", "2026-09-07T19:00:00Z")
    c.report("Users being logged out and pages failing to load", 12, ["web-frontend"], "high", None, "on-call SRE")
    c.conditions = {"faults": {"query_metrics": "unavailable"},
                    "note": "The metrics tool is simulated as unavailable for this case, for both approaches."}
    c.change("CHG-E07-1", -20, "redis-cache", "config_change", "Lower cache memory limit",
             details={"key": "maxmemory", "old": "8gb", "new": "2gb"}, changed_by="platform-team")
    c.log(1, "redis-cache", "WARNING", "OOM command not allowed when used memory > 'maxmemory'")
    c.log(2, "web-frontend", "ERROR", "Session lookup failed: cache error (OOM)", "e7a")
    c.log(9, "redis-cache", "WARNING", "OOM command not allowed when used memory > 'maxmemory'")
    c.log(10, "web-frontend", "ERROR", "Session lookup failed: cache error (OOM)", "e7b")
    c.series("redis-cache", "memory_used_percent", "percent", 30, 100, change_at=-20)
    c.series("web-frontend", "error_rate_percent", "percent", 0.3, 12)
    c.label = {
        "scenario": ["simulated_tool_failure", "config_change_precedes_incident"],
        "expected_outcome": "root_cause",
        "root_cause": root_cause("redis-maxmemory-reduction", "redis-cache maxmemory was cut to 2gb; writes fail with OOM.",
                                 [["redis", "cache"], ["memory", "maxmemory", "oom"]],
                                 [{"field": "change_id", "equals": "CHG-E07-1"},
                                  {"field": "message", "contains": "OOM command not allowed"}]),
        "acceptable_alternatives": [],
        "inconclusive_acceptable": True,
        "distractors": [],
        "must_consider": [],
        "rationale": "Metrics are unavailable (simulated tool failure), but the change record and OOM logs are direct. "
                     "A correct answer is expected; inconclusive is acceptable because a source failed.",
    }
    return c


def eval_08() -> Case:
    c = Case("EVAL-08", "2026-09-08T14:00:00Z")
    c.report("Checkout card payments failing or very slow", 12, ["payments-api"], "high",
             "Started roughly 14:00 UTC.", "on-call SRE")
    c.gap("metrics", "fraud-check", -90, 90, "Vendor metrics export delayed; no fraud-check latency data for this window.")
    c.change("CHG-E08-1", -8, "payments-api", "deployment", "Upgrade HTTP client library; refactor fraud-check retry logic",
             version="2.14.0", previous_version="2.13.2", changed_by="deploy-bot (PR #4412)")
    c.change("CHG-E08-2", -5, "payments-api", "config_change", "Reduce database connection pool size (cost optimisation)",
             details={"key": "db.pool.max_size", "old": "50", "new": "20"}, changed_by="platform-team")
    c.log(-4, "payments-api", "INFO", "Config reloaded: db.pool.max_size=20")
    c.log(1, "payments-api", "WARNING", "Slow response from fraud-check: 1850ms", "e8a")
    c.log(2, "payments-api", "ERROR", "Timed out waiting for DB connection from pool after 3000ms", "e8b")
    c.log(3, "payments-api", "ERROR", "Request to fraud-check timed out after 2000ms; retrying (attempt 2/3)", "e8c")
    c.log(3.1, "payments-api", "ERROR", "Request to fraud-check timed out after 2000ms; retrying (attempt 3/3)", "e8c")
    c.log(8, "payments-api", "WARNING", "Connection pool saturated: 20/20 in use, 37 waiting")
    c.log(20, "payments-api", "ERROR", "Timed out waiting for DB connection from pool after 3000ms", "e8d")
    c.series("payments-api", "latency_p95_ms", "ms", 180, 2450)
    c.series("payments-api", "error_rate_percent", "percent", 0.2, 8.0)
    c.series("payments-api", "db_pool_utilization_percent", "percent", 38, 100, change_at=-5)
    c.series("postgres-payments", "cpu_percent", "percent", 32, 27)
    c.label = {
        "scenario": ["multiple_plausible_hypotheses", "missing_or_delayed_metrics"],
        "expected_outcome": "root_cause",
        "root_cause": root_cause("payments-pool-reduction", "Pool reduction (CHG-E08-2) exhausted DB connections.",
                                 [["pool"], ["size", "max_size", "reduc", "config", "exhaust", "saturat"]],
                                 [{"field": "change_id", "equals": "CHG-E08-2"},
                                  {"field": "message", "contains": "pool saturated"}]),
        "acceptable_alternatives": [root_cause("fraud-check-retry-change", "fraud-check slowness amplified by the v2.14.0 retry refactor.",
                                               [["fraud"], ["retry", "retries", "timeout", "timed out", "slow"]], [])],
        "inconclusive_acceptable": True,
        "distractors": [],
        "must_consider": [pattern("DB connection pool reduction", [["pool"]]),
                          pattern("fraud-check slowness / retry change", [["fraud"]])],
        "rationale": "Two changes minutes apart and two failure signatures. Pool saturation is better supported, but "
                     "fraud-check metrics are missing. Either cause, or inconclusive, is acceptable; both should be considered.",
    }
    return c


def eval_09() -> Case:
    c = Case("EVAL-09", "2026-09-09T13:00:00Z")
    c.report("A few users said the site felt slow this afternoon", 0, [], None, "Single anecdotal report; no alert fired.")
    c.gap("logs", "web-frontend", -60, 30, "Log shipper backlog; web-frontend logs in this window were not ingested.")
    c.log(-50, "api-gateway", "INFO", "p95 upstream latency within SLO for all routes")
    c.log(10, "api-gateway", "INFO", "p95 upstream latency within SLO for all routes")
    c.log(35, "web-frontend", "INFO", "Served homepage in 640ms")
    c.series("web-frontend", "page_load_p95_ms", "ms", 1150, 1180)
    c.series("web-frontend", "error_rate_percent", "percent", 0.3, 0.3)
    c.series("api-gateway", "latency_p95_ms", "ms", 120, 122)
    c.label = {
        "scenario": ["insufficient_evidence"],
        "expected_outcome": "inconclusive",
        "root_cause": None,
        "acceptable_alternatives": [],
        "inconclusive_acceptable": True,
        "distractors": [],
        "must_consider": [],
        "rationale": "No anomaly in metrics, no errors, no changes, and frontend logs are missing for the window. "
                     "Any named root cause is unsupported; the correct outcome is inconclusive.",
    }
    return c


def eval_10() -> Case:
    c = Case("EVAL-10", "2026-09-10T15:00:00Z")
    c.report("Payments failing after this afternoon's payments-api deploy", 8, ["payments-api"], "critical",
             "The deploy has been rolled back but errors continue.", "on-call SRE")
    c.extra_dependencies = [{"service": "billing-batch", "depends_on": "postgres-payments", "dependency_type": "database",
                             "critical": False, "notes": "Nightly reconciliation job."}]
    c.change("CHG-E10-1", -6, "payments-api", "deployment", "Upgrade ORM library", version="2.20.0", previous_version="2.19.4")
    c.change("CHG-E10-2", 10, "payments-api", "rollback", "Rollback to v2.19.4", version="2.19.4", previous_version="2.20.0")
    c.log(-3, "billing-batch", "INFO", "Reconciliation started with 200 parallel workers")
    for m in (-2, 6, 18, 35):
        c.log(m, "postgres-payments", "ERROR", "FATAL: sorry, too many clients already")
    for m, tr in [(-1, "e10a"), (12, "e10b"), (30, "e10c")]:
        c.log(m, "payments-api", "ERROR", "Could not acquire DB connection: too many clients", tr)
    c.log(11, "payments-api", "INFO", "Starting payments-api v2.19.4")
    c.series("postgres-payments", "connections_used", "count", 90, 500, change_at=-3)
    c.series("payments-api", "error_rate_percent", "percent", 0.2, 11)
    c.series("billing-batch", "active_workers", "count", 0, 200, change_at=-3)
    c.label = {
        "scenario": ["strong_hypothesis_contradicted", "misleading_deployment_correlation"],
        "expected_outcome": "root_cause",
        "root_cause": root_cause("billing-batch-connection-exhaustion", "The billing-batch job's 200 workers exhausted postgres-payments connections.",
                                 [["batch", "reconciliation", "billing", "worker"], ["connection", "clients"]],
                                 [{"field": "message", "contains": "200 parallel workers"},
                                  {"field": "message", "contains": "too many clients"}]),
        "acceptable_alternatives": [],
        "inconclusive_acceptable": False,
        "distractors": [pattern("payments-api v2.20.0 ORM deploy", [["orm", "v2.20", "2.20.0", "deploy"]])],
        "must_consider": [],
        "rationale": "The deploy looks guilty, but errors persist after the rollback, and DB connections jump when "
                     "billing-batch starts 200 workers. Direct evidence contradicts the deploy hypothesis.",
    }
    return c


CASES = [eval_01, eval_02, eval_03, eval_04, eval_05, eval_06, eval_07, eval_08, eval_09, eval_10]


def build(out_dir: Path = EVAL_DIR) -> None:
    cases_dir = out_dir / "cases"
    if cases_dir.exists():
        shutil.rmtree(cases_dir)
    labels = []
    shared_dependencies = (SHARED_FIXTURES / "dependencies.jsonl").read_text(encoding="utf-8")
    for make in CASES:
        c = make()
        d = cases_dir / c.case_id
        d.mkdir(parents=True)
        _write_json(d / "incident.json", c.incident)
        _write_json(d / "conditions.json", c.conditions)
        _write_jsonl(d / "logs.jsonl", sorted(c.logs, key=lambda r: r["timestamp"]))
        _write_jsonl(d / "metrics.jsonl", c.metrics)
        _write_jsonl(d / "changes.jsonl", sorted(c.changes, key=lambda r: r["timestamp"]))
        _write_jsonl(d / "coverage_gaps.jsonl", c.gaps)
        extra = "".join(json.dumps(r) + "\n" for r in c.extra_dependencies)
        (d / "dependencies.jsonl").write_text(shared_dependencies + extra, encoding="utf-8")
        shutil.copyfile(SHARED_FIXTURES / "historical_incidents.jsonl", d / "historical_incidents.jsonl")
        labels.append({"case_id": c.case_id, **c.label})
    _write_jsonl(out_dir / "labels.jsonl", labels)


def _write_json(path: Path, data: Any) -> None:
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")


if __name__ == "__main__":
    build()
    print(f"Wrote {len(CASES)} cases to {EVAL_DIR.relative_to(ROOT)}")
