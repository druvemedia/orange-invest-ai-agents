"""Investigations API end to end, with scripted or offline models (no network, no key)."""

import asyncio
import logging

import pytest
from fastapi.testclient import TestClient

from app.agents import AgentConfig, OrchestratorAgent
from app.core.config import Settings
from app.evaluation.heuristic_model import HeuristicLLMClient
from app.factory import create_app
from app.llm import ScriptedLLMClient
from app.orchestration import EngineLimits
from app.product import service as service_module
from app.product.service import InvestigationService
from tests import agent_scripts as s

PAYMENTS = {"summary": "Checkout card payments failing or very slow", "reported_at": "2026-10-20T14:12:00Z",
            "affected_services": ["payments-api"], "severity": "high", "evidence_source": "demo"}
FAKE_KEY = "sk-proj-APITESTKEYNOTREAL0123456789"


class Hangs:
    async def generate(self, request, output_type):
        await asyncio.Event().wait()


def make_client(factory, label="scripted-test", config=None, max_active=2, monkeypatch=None):
    service = InvestigationService(factory, model_label=label, agent_config=config or AgentConfig(), max_active=max_active)
    app = create_app(Settings(_env_file=None, app_env="test"), investigation_service=service)
    return TestClient(app), service


def finish(client, service, job_id):
    return client.portal.call(service.wait, job_id)


@pytest.fixture
def scripted(clean_env):
    client, service = make_client(lambda: ScriptedLLMClient(s.full_script()))
    with client:
        r = client.post("/investigations", json=PAYMENTS)
        job_id = r.json()["investigation_id"]
        finish(client, service, job_id)
        yield client, r, job_id


# --- Submission and validation ---------------------------------------------------------------------


def test_submission_returns_202_with_location_and_job(scripted):
    _, r, job_id = scripted

    assert r.status_code == 202
    assert r.headers["location"] == f"/investigations/{job_id}"
    body = r.json()
    assert body["status"] in ("queued", "running") and body["incident_id"].startswith("INC-")
    assert body["links"]["report_markdown"] == f"/investigations/{job_id}/report.md"


@pytest.mark.parametrize(
    ("change", "field"),
    [
        ({"reported_at": None}, "reported_at"),
        ({"reported_at": "2026-10-20T14:12:00"}, "reported_at"),  # no timezone
        ({"summary": "   "}, "summary"),
        ({"summary": "x" * 2001}, "summary"),
        ({"context": "x" * 5001}, "context"),
        ({"affected_services": ["Not A Service!"]}, "affected_services"),
        ({"affected_services": [f"svc-{i}" for i in range(11)]}, "affected_services"),
        ({"incident_id": "bad id with spaces"}, "incident_id"),
        ({"severity": "catastrophic"}, "severity"),
        ({"unexpected": 1}, "unexpected"),
        ({"evidence_source": "prod-db"}, "evidence_source"),
    ],
)
def test_invalid_submissions_are_rejected_with_422(clean_env, change, field):
    client, service = make_client(lambda: ScriptedLLMClient(s.full_script()))
    body = {k: v for k, v in {**PAYMENTS, **change}.items() if v is not None}
    with client:
        r = client.post("/investigations", json=body)

    assert r.status_code == 422
    assert any(field in [str(p) for p in e["loc"]] for e in r.json()["detail"])
    assert service.list() == []


def test_without_a_model_submissions_get_503_and_health_still_works(clean_env):
    app = create_app(Settings(_env_file=None, app_env="test"))
    with TestClient(app) as client:
        caps = client.get("/capabilities").json()
        r = client.post("/investigations", json=PAYMENTS)
        health = client.get("/health")

    assert caps["investigations_available"] is False and "LLM_PROVIDER" in caps["unavailable_reason"]
    assert r.status_code == 503 and "offline-heuristic" in r.json()["detail"]
    assert health.status_code == 200


def test_concurrency_limit_returns_429(clean_env):
    client, service = make_client(Hangs, max_active=1)
    with client:
        first = client.post("/investigations", json=PAYMENTS)
        second = client.post("/investigations", json=PAYMENTS)
        report = client.get(f"/investigations/{first.json()['investigation_id']}/report")

    assert first.status_code == 202
    assert second.status_code == 429 and second.headers["retry-after"] == "30"
    assert report.status_code == 409 and "still running" in report.json()["detail"]


@pytest.mark.parametrize("path", ["/investigations/inv-000000000000", "/investigations/x/report", "/investigations/x/report.md"])
def test_unknown_investigation_is_404(scripted, path):
    client, _, _ = scripted
    r = client.get(path)

    assert r.status_code == 404 and r.json() == {"detail": "Investigation not found."}


# --- Report content ------------------------------------------------------------------------------------


def test_completed_job_reports_outcome_and_usage(scripted):
    client, _, job_id = scripted
    job = client.get(f"/investigations/{job_id}").json()

    assert job["status"] == "completed"
    assert job["outcome"]["conclusion"] == "probable_cause_identified"
    assert job["outcome"]["top_hypothesis"].startswith("Connection pool reduction")
    assert job["usage"] == {"model_calls": 8, "input_tokens": None, "output_tokens": None}
    assert [j["investigation_id"] for j in client.get("/investigations").json()] == [job_id]


def test_report_resolves_every_citation_to_retrieved_evidence(scripted):
    client, _, job_id = scripted
    v = client.get(f"/investigations/{job_id}/report").json()

    assert v["unresolved_citations"] == []
    h1, h2 = v["hypotheses"]
    assert (h1["hypothesis_id"], h1["confidence"], h2["confidence"]) == ("H1", "medium", "low")
    kinds = {c["kind"] for h in v["hypotheses"] for c in h["supporting"] + h["contradicting"]}
    assert kinds <= {"record", "finding", "result"} and "finding" in kinds and "record" in kinds
    reload = next(c for c in h1["supporting"] if c["ref"].startswith("ev.c1.1#"))  # critic-requested evidence
    assert "Config reloaded: db.pool.max_size=20" in reload["text"]
    assert h1["contradicting"] and h1["contradicting"][0]["based_on"]  # finding resolved to its records
    assert all(c["text"] for h in v["hypotheses"] for c in h["supporting"])
    assert v["facts"] and v["immediate_actions"] == ["Restore db.pool.max_size to 50. (for H1)"]
    assert any(c["task_id"] == "ev.c1.1" for c in v["evidence_coverage"])
    assert v["process"]["critic_rounds"] == 2 and v["process"]["final_critic_verdict"] == "accept"
    assert v["observed_impact"] and all(st["evidence"] for st in v["observed_impact"])


def test_markdown_report_has_every_section(scripted):
    client, _, job_id = scripted
    r = client.get(f"/investigations/{job_id}/report.md")
    alt = client.get(f"/investigations/{job_id}/report", params={"format": "markdown"})

    assert r.status_code == 200 and r.headers["content-type"].startswith("text/markdown")
    assert alt.text == r.text
    for section in ["## Incident and impact", "## Ranked hypotheses", "Supporting evidence:", "Contradicting evidence:",
                    "## Facts", "## Assumptions", "**Immediate mitigation:**", "## Missing evidence and limitations",
                    "## Investigation process"]:
        assert section in r.text, section
    assert "Unresolved citations" not in r.text


def test_insufficient_evidence_gives_an_inconclusive_report(clean_env):
    client, service = make_client(HeuristicLLMClient, label="offline-heuristic")
    with client:
        r = client.post("/investigations", json={"summary": "A few users said the site felt slow",
                                                 "reported_at": "2026-09-09T13:00:00Z", "evidence_source": "EVAL-09"})
        finish(client, service, r.json()["investigation_id"])
        v = client.get(f"/investigations/{r.json()['investigation_id']}/report").json()
        md = client.get(f"/investigations/{r.json()['investigation_id']}/report.md").text

    assert v["conclusion"] == "inconclusive" and v["hypotheses"] == []
    assert "No hypothesis could be grounded" in md
    assert v["notices"] and "not by an AI model" in v["notices"][0]


def test_tool_failure_and_partial_evidence_are_reported(clean_env):
    client, service = make_client(HeuristicLLMClient, label="offline-heuristic")
    with client:
        r = client.post("/investigations", json={"summary": "Users being logged out", "reported_at": "2026-09-07T19:12:00Z",
                                                 "affected_services": ["web-frontend"], "evidence_source": "EVAL-07"})
        finish(client, service, r.json()["investigation_id"])
        v = client.get(f"/investigations/{r.json()['investigation_id']}/report").json()

    metrics = next(c for c in v["evidence_coverage"] if c["task_id"] == "ev.metrics")
    assert (metrics["outcome"], metrics["error_code"], metrics["attempts"]) == ("failed", "unavailable", 2)
    assert v["process"]["investigation_status"] == "partially_completed"
    assert any("ev.metrics" in lim for lim in v["limitations"])
    assert v["unresolved_citations"] == []


def test_invalid_model_output_exhausts_bounded_retries(clean_env):
    def invents(request):
        out = s.observability(request)
        out["observations"][0]["evidence_refs"] = ["ev.logs#999"]
        return out

    client, service = make_client(lambda: ScriptedLLMClient(s.full_script(observability=[invents])))
    with client:
        r = client.post("/investigations", json=PAYMENTS)
        finish(client, service, r.json()["investigation_id"])
        v = client.get(f"/investigations/{r.json()['investigation_id']}/report").json()

    assert any(t.startswith("observability: failed") for t in v["process"]["tasks_not_succeeded"])
    assert any("observability" in lim and "ev.logs#999" in lim for lim in v["limitations"])
    assert v["unresolved_citations"] == []


# --- Time bounds ------------------------------------------------------------------------------------------


def test_engine_deadline_still_produces_a_report(clean_env):
    config = AgentConfig(limits=EngineLimits(max_tasks=40, max_total_attempts=80, deadline_seconds=0.3))
    client, service = make_client(Hangs, config=config)
    with client:
        r = client.post("/investigations", json=PAYMENTS)
        job = finish(client, service, r.json()["investigation_id"])
        v = client.get(f"/investigations/{job.id}/report").json()

    assert job.status == "completed"
    assert v["process"]["termination_reason"] == "deadline_exceeded"
    assert v["process"]["report_generated_by"] == "fallback" and v["conclusion"] == "inconclusive"


def test_hard_time_limit_fails_a_stuck_job(clean_env, monkeypatch):
    async def stuck(self, incident, investigation_id=None):
        await asyncio.Event().wait()

    monkeypatch.setattr(OrchestratorAgent, "investigate", stuck)
    monkeypatch.setattr(service_module, "HARD_LIMIT_MARGIN_SECONDS", 0.0)
    config = AgentConfig(limits=EngineLimits(deadline_seconds=0.2))
    client, service = make_client(Hangs, config=config)
    with client:
        r = client.post("/investigations", json=PAYMENTS)
        job = finish(client, service, r.json()["investigation_id"])
        report = client.get(f"/investigations/{job.id}/report")

    assert job.status == "failed" and "hard time limit" in job.error
    assert report.status_code == 409 and "failed" in report.json()["detail"]


# --- Security -------------------------------------------------------------------------------------------------


def test_internal_errors_are_safe_and_secret_free(clean_env, monkeypatch, caplog):
    async def crash(self, incident, investigation_id=None):
        raise RuntimeError(f"Incorrect API key provided: {FAKE_KEY}")

    monkeypatch.setattr(OrchestratorAgent, "investigate", crash)
    client, service = make_client(lambda: ScriptedLLMClient(s.full_script()))
    with caplog.at_level(logging.DEBUG), client:
        r = client.post("/investigations", json=PAYMENTS)
        finish(client, service, r.json()["investigation_id"])
        job = client.get(f"/investigations/{r.json()['investigation_id']}")

    assert job.json()["status"] == "failed" and job.json()["error"] == "Internal error (RuntimeError)."
    assert FAKE_KEY not in job.text and FAKE_KEY not in caplog.text


def test_model_errors_carrying_secrets_never_reach_responses(clean_env):
    class Leaky:
        async def generate(self, request, output_type):
            raise RuntimeError(f"401 Incorrect API key provided: {FAKE_KEY}")

    client, service = make_client(Leaky)
    with client:
        r = client.post("/investigations", json=PAYMENTS)
        finish(client, service, r.json()["investigation_id"])
        texts = [client.get(f"/investigations/{r.json()['investigation_id']}{p}").text for p in ("", "/report", "/report.md")]

    assert all(FAKE_KEY not in t for t in texts)
    assert "[REDACTED]" in texts[1]


def test_evidence_sources_expose_inputs_but_never_labels(scripted):
    client, _, _ = scripted
    sources = client.get("/evidence-sources").json()

    assert [x["name"] for x in sources][:2] == ["demo", "EVAL-01"] and len(sources) == 11
    text = str(sources)
    for label_field in ("expected_outcome", "rationale", "acceptable_alternatives", "key_evidence"):
        assert label_field not in text
    assert next(x for x in sources if x["name"] == "EVAL-07")["simulated_faults"] == {"query_metrics": "unavailable"}


def test_web_ui_is_served_safely(scripted):
    client, _, _ = scripted
    r = client.get("/")

    assert r.status_code == 200 and "Incident Investigator" in r.text
    assert "default-src 'self'" in r.headers["content-security-policy"]
    assert "innerHTML" not in r.text and "<script src" not in r.text  # data is inserted as text only


def test_web_ui_layout_and_wording_regressions(scripted):
    """Found in real-browser testing (Phase 5.1): long text overflowed, narrow tables broke words, and the
    offline stand-in was labelled 'report by model'."""
    client, _, _ = scripted
    page = client.get("/").text

    assert "overflow-wrap: anywhere" in page.split("main {", 1)[1].split("}", 1)[0]
    assert "white-space: nowrap" in page and ".scroll { overflow-x: auto; }" in page
    assert 'el("div", null, "scroll")' in page
    assert "report written by ${v.process.report_author}" in page
    assert "report by ${v.process.report_generated_by}" not in page
