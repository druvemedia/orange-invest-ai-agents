"""Billed model calls need an explicit opt-in. Without it, no OpenAI client is built and no connection is opened.

Every test here runs with the OpenAI SDK client constructor and socket connections replaced by functions that
fail the test if they are ever called, so a regression shows up as a failure, never as a real request.
"""

import socket

import pytest
from fastapi.testclient import TestClient

from app import cli
from app.core.config import Settings
from app.evaluation import __main__ as evaluation_cli
from app.factory import build_investigation_service, create_app
from app.llm import BilledCallsNotAllowedError, OpenAIClient, build_llm_client, check_llm_settings

FAKE_KEY = "sk-proj-BILLINGGUARDNOTREAL0123456789"
OPENAI = {"llm_provider": "openai", "llm_model": "gpt-4o-mini", "llm_api_key": FAKE_KEY}
INCIDENT = {"summary": "Checkout card payments failing or very slow", "reported_at": "2026-10-20T14:12:00Z",
            "affected_services": ["payments-api"], "severity": "high", "evidence_source": "demo"}


@pytest.fixture
def forbidden(monkeypatch):
    """Fail the test if an OpenAI SDK client is created or any network connection is attempted."""
    def no_client(*args, **kwargs):
        raise AssertionError("an OpenAI SDK client was created")

    def no_network(*args, **kwargs):
        raise AssertionError("a network connection was attempted")

    monkeypatch.setattr("openai.AsyncOpenAI", no_client)
    monkeypatch.setattr(socket.socket, "connect", no_network)
    monkeypatch.setattr(socket, "create_connection", no_network)


@pytest.fixture
def recorded_clients(monkeypatch):
    """Replace the SDK client with a recorder, for tests where building a client is allowed."""
    built = []
    monkeypatch.setattr("openai.AsyncOpenAI", lambda **kwargs: built.append(kwargs) or object())
    return built


def test_billed_calls_are_off_by_default_and_enabled_only_by_an_explicit_true(clean_env, monkeypatch):
    assert Settings(_env_file=None, **OPENAI).llm_allow_billed_calls is False

    monkeypatch.setenv("LLM_ALLOW_BILLED_CALLS", "true")
    assert Settings(_env_file=None, **OPENAI).llm_allow_billed_calls is True


def test_without_opt_in_no_client_is_built(clean_env, forbidden):
    settings = Settings(_env_file=None, **OPENAI)

    for attempt in (lambda: check_llm_settings(settings), lambda: build_llm_client(settings)):
        with pytest.raises(BilledCallsNotAllowedError, match="LLM_ALLOW_BILLED_CALLS") as info:
            attempt()
        assert FAKE_KEY not in str(info.value)


def test_missing_settings_are_still_reported_before_the_billing_check(clean_env, forbidden):
    with pytest.raises(Exception, match="LLM_API_KEY") as info:
        build_llm_client(Settings(_env_file=None, llm_provider="openai", llm_model="m"))
    assert not isinstance(info.value, BilledCallsNotAllowedError)


def test_opt_in_setting_or_confirmed_command_allows_a_client(clean_env, recorded_clients):
    opted_in = build_llm_client(Settings(_env_file=None, llm_allow_billed_calls=True, **OPENAI))
    confirmed = build_llm_client(Settings(_env_file=None, **OPENAI), billing_confirmed=True)

    assert isinstance(opted_in, OpenAIClient) and isinstance(confirmed, OpenAIClient)
    assert len(recorded_clients) == 2 and all(c["max_retries"] == 0 for c in recorded_clients)


def test_server_rejects_submissions_without_opt_in_before_any_client(clean_env, forbidden):
    app = create_app(Settings(_env_file=None, app_env="test", **OPENAI))
    with TestClient(app) as client:
        caps = client.get("/capabilities")
        r = client.post("/investigations", json=INCIDENT)
        health = client.get("/health")

    assert r.status_code == 503
    assert "LLM_ALLOW_BILLED_CALLS" in r.json()["detail"] and "offline-heuristic" in r.json()["detail"]
    assert caps.json()["investigations_available"] is False
    assert health.status_code == 200
    assert FAKE_KEY not in r.text + caps.text


def test_server_startup_with_opt_in_builds_no_client_until_an_investigation_runs(clean_env, recorded_clients):
    service = build_investigation_service(Settings(_env_file=None, llm_allow_billed_calls=True, **OPENAI))

    assert service.available and service.model_label == "gpt-4o-mini"
    assert recorded_clients == []


def test_cli_exits_without_opt_in_before_any_client(clean_env, monkeypatch, capsys, forbidden):
    monkeypatch.setattr(cli, "get_settings", lambda: Settings(_env_file=None, **OPENAI))

    assert cli.main(["EVAL-01"]) == 2
    out, err = capsys.readouterr()
    assert "LLM_ALLOW_BILLED_CALLS" in err
    assert FAKE_KEY not in out + err


def test_offline_mode_needs_no_key_and_no_opt_in(clean_env, monkeypatch, capsys, forbidden):
    monkeypatch.setattr(cli, "get_settings", lambda: Settings(_env_file=None, llm_provider="offline-heuristic"))

    assert cli.main(["EVAL-01"]) == 0
    assert "model offline-heuristic" in capsys.readouterr().out


def test_live_evaluation_needs_both_opt_ins_and_a_budget(clean_env, monkeypatch, recorded_clients, tmp_path, capsys):
    """LLM_ALLOW_BILLED_CALLS=true (the same guard as the API and CLI), --confirm-billed and --max-cost-usd."""
    factories, written = [], []

    def fake_run_evaluation(mode, factory, **kwargs):
        factories.append((factory, kwargs))
        return {"aggregates": {}}

    monkeypatch.setattr(evaluation_cli, "run_evaluation", fake_run_evaluation)
    monkeypatch.setattr(evaluation_cli, "write_outputs", lambda result, out: written.append(out))
    monkeypatch.setattr("app.evaluation.live.write_provenance", lambda out, result, plan: None)
    out = tmp_path / "live-run"
    args = ["live", "--cases", "EVAL-01", "--max-cost-usd", "1", "--out", str(out)]

    monkeypatch.setattr(evaluation_cli, "get_settings", lambda: Settings(_env_file=None, **OPENAI))
    assert evaluation_cli.main([*args, "--confirm-billed"]) == 2  # --confirm-billed alone is no longer enough
    assert "LLM_ALLOW_BILLED_CALLS" in capsys.readouterr().err

    opted_in = Settings(_env_file=None, llm_allow_billed_calls=True, **OPENAI)
    monkeypatch.setattr(evaluation_cli, "get_settings", lambda: opted_in)
    assert evaluation_cli.main(["live", "--cases", "EVAL-01", "--confirm-billed"]) == 2  # no budget
    assert evaluation_cli.main(args) == 2  # no --confirm-billed
    assert factories == [] and recorded_clients == [] and written == []

    assert evaluation_cli.main([*args, "--confirm-billed"]) == 0
    factory, kwargs = factories[0]
    assert kwargs["budget"].max_usd == 1 and written == [out] and recorded_clients == []
    assert isinstance(factory(), OpenAIClient) and len(recorded_clients) == 1  # built through the normal guard


def test_live_evaluation_never_overwrites_and_preflight_makes_no_calls(clean_env, monkeypatch, recorded_clients, tmp_path,
                                                                        capsys):
    monkeypatch.setattr(evaluation_cli, "get_settings",
                        lambda: Settings(_env_file=None, llm_allow_billed_calls=True, **OPENAI))
    monkeypatch.setattr(evaluation_cli, "run_evaluation", lambda *a, **k: (_ for _ in ()).throw(AssertionError("ran")))

    existing = tmp_path / "old"
    existing.mkdir()
    assert evaluation_cli.main(["live", "--max-cost-usd", "5", "--confirm-billed", "--out", str(existing)]) == 2
    assert "never overwritten" in capsys.readouterr().err

    assert evaluation_cli.main(["live", "--max-cost-usd", "5", "--preflight-only", "--out", str(tmp_path / "x")]) == 0
    plan = capsys.readouterr().err
    assert "20 investigations" in plan and "worst case" in plan and recorded_clients == []
    assert not (tmp_path / "x").exists()

    assert evaluation_cli.main(["live", "--max-cost-usd", "0.05", "--preflight-only", "--out", str(tmp_path / "y")]) == 2
    assert "below the worst case of a single investigation" in capsys.readouterr().err

    # The preview needs no billing opt-in (it never builds a client); a real run without it is still refused.
    monkeypatch.setattr(evaluation_cli, "get_settings", lambda: Settings(_env_file=None, **OPENAI))
    assert evaluation_cli.main(["live", "--max-cost-usd", "5", "--preflight-only", "--out", str(tmp_path / "z")]) == 0
    assert evaluation_cli.main(["live", "--max-cost-usd", "5", "--confirm-billed", "--out", str(tmp_path / "z")]) == 2
    assert recorded_clients == [] and not (tmp_path / "z").exists()
