"""App-level smoke tests: the service imports, boots, and serves under pydantic v2."""

from fastapi.testclient import TestClient


def test_main_imports_and_serves_ui():
    from backend.main import app

    with TestClient(app) as client:
        response = client.get("/")
        assert response.status_code == 200
        assert "text/html" in response.headers["content-type"]


def test_welcome_endpoint_routes():
    from backend.main import app

    with TestClient(app) as client:
        response = client.get("/api/welcome")
        # Unauthenticated: the route must exist and answer cleanly (401), not 5xx.
        assert response.status_code in (200, 401)
