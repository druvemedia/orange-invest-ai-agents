"""Configuration robustness: a bad local .env must not break unrelated code, but must still fail loudly."""

import subprocess
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.core.config import Settings

PROJECT_ROOT = Path(__file__).resolve().parents[1]
FAKE_KEY = "fake-key-must-not-appear-in-errors"


def _import_in_dir_with_env(tmp_path: Path, env_file: str, module: str) -> subprocess.CompletedProcess:
    """Import ``module`` in a fresh interpreter whose working directory holds ``env_file`` as .env."""
    (tmp_path / ".env").write_text(env_file, encoding="utf-8")
    return subprocess.run(
        [sys.executable, "-c", f"import {module}"],
        cwd=tmp_path,
        env={"PYTHONPATH": str(PROJECT_ROOT), "PATH": "/usr/bin:/bin"},
        capture_output=True,
        text=True,
        timeout=30,
    )


BAD_ENV = f"APP_ENV=staging\nLLM_API_KEY={FAKE_KEY}\n"


def test_factory_import_ignores_malformed_env(tmp_path):
    result = _import_in_dir_with_env(tmp_path, BAD_ENV, "app.factory")

    assert result.returncode == 0, result.stderr


def test_entry_point_still_fails_fast_on_malformed_env(tmp_path):
    result = _import_in_dir_with_env(tmp_path, BAD_ENV, "app.main")

    assert result.returncode != 0
    assert "app_env" in result.stderr
    assert FAKE_KEY not in result.stderr + result.stdout


def test_entry_point_reports_invalid_log_level_as_config_error(tmp_path):
    result = _import_in_dir_with_env(tmp_path, "LOG_LEVEL=LOUD\n", "app.main")

    assert result.returncode != 0
    assert "ValidationError" in result.stderr
    assert "log_level" in result.stderr


def test_log_level_is_case_insensitive(clean_env):
    assert Settings(_env_file=None, log_level=" warning ").log_level == "WARNING"


@pytest.mark.parametrize("level", ["LOUD", "", "10"])
def test_invalid_log_level_is_rejected(clean_env, level):
    with pytest.raises(ValidationError) as exc_info:
        Settings(_env_file=None, log_level=level)

    assert [err["loc"] for err in exc_info.value.errors()] == [("log_level",)]
