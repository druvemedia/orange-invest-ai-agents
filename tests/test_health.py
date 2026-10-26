from app import __version__
from app.core.config import Settings
from tests.conftest import TEST_SECRET


def test_health_returns_200_and_json(client):
    response = client.get("/health")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    assert response.json() == {
        "status": "ok",
        "app_name": "Orange Invest AI Agent",
        "version": __version__,
        "environment": "test",
    }


def test_health_does_not_leak_secrets(client):
    response = client.get("/health")

    assert TEST_SECRET not in response.text


def test_secret_is_masked_in_settings_repr(settings: Settings):
    assert TEST_SECRET not in repr(settings)
    assert TEST_SECRET not in str(settings.llm_api_key)
    assert settings.llm_api_key.get_secret_value() == TEST_SECRET


def test_unknown_route_returns_404(client):
    assert client.get("/does-not-exist").status_code == 404
