from fastapi.testclient import TestClient

from app.main import app


def test_healthz() -> None:
    with TestClient(app) as client:
        response = client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_dashboard() -> None:
    with TestClient(app) as client:
        response = client.get("/")

    assert response.status_code == 200
    assert "ダッシュボード" in response.text
    assert "DRY RUN" in response.text


def test_static_assets() -> None:
    with TestClient(app) as client:
        assert client.get("/static/js/htmx.min.js").status_code == 200
        assert client.get("/static/js/chart.umd.min.js").status_code == 200
