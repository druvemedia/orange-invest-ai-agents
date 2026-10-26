import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings, get_settings
from app.factory import create_app
from app.tools import EvidenceTool, FixtureStore, build_evidence_tools

TEST_SECRET = "test-secret-should-never-leak"


@pytest.fixture(scope="session")
def fixture_store() -> FixtureStore:
    """The shared synthetic fixtures in data/fixtures (read-only)."""
    return FixtureStore()


@pytest.fixture
def tools(fixture_store: FixtureStore) -> dict[str, EvidenceTool]:
    return build_evidence_tools(fixture_store)


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove this app's settings from os.environ so a developer's shell exports can't leak into tests."""
    for field in Settings.model_fields:
        monkeypatch.delenv(field.upper(), raising=False)
        monkeypatch.delenv(field, raising=False)


@pytest.fixture
def settings(clean_env: None) -> Settings:
    # _env_file=None keeps tests independent of any developer's local .env.
    return Settings(_env_file=None, app_env="test", llm_api_key=TEST_SECRET)


@pytest.fixture
def client(settings: Settings):
    app = create_app(settings)
    app.dependency_overrides[get_settings] = lambda: settings
    with TestClient(app) as test_client:
        yield test_client
