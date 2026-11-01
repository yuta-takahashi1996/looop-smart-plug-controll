import pytest
from fastapi.testclient import TestClient

from app.api.auth import hash_password, verify_password
from app.main import app
from tests.conftest import TEST_PASSWORD, csrf_token

PAGES = [
    ("/", "ダッシュボード"),
    ("/forecast", "電力予報"),
    ("/forecast?day=tomorrow", "電力予報"),
    ("/schedule", "運転スケジュール"),
    ("/history", "制御履歴"),
    ("/history?date=2026-01-15", "制御履歴"),
    ("/settings", "設定"),
]

VALID_SETTINGS = {
    "operation_start": "08:30",
    "operation_end": "17:30",
    "min_saving_spread": "25",
    "round_trip_efficiency": "80",
    "max_continuous_off_minutes": "90",
    "max_daily_off_minutes": "150",
    "min_switch_interval_minutes": "30",
    "estimated_average_power_w": "200",
}


def test_healthz(client: TestClient) -> None:
    response = client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_static_assets(client: TestClient) -> None:
    assert client.get("/static/js/htmx.min.js").status_code == 200
    assert client.get("/static/js/chart.umd.min.js").status_code == 200
    assert client.get("/static/js/forecast.js").status_code == 200


# --- 接続元制限 ---


def test_rejects_client_outside_allowed_networks() -> None:
    with TestClient(app, client=("203.0.113.10", 50000)) as client:
        assert client.get("/login").status_code == 403
        assert client.get("/healthz").status_code == 403


def test_rejects_non_ip_client() -> None:
    with TestClient(app) as client:  # 既定の接続元は "testclient"
        assert client.get("/login").status_code == 403


# --- 認証 ---


@pytest.mark.parametrize("path", ["/", "/forecast", "/schedule", "/history", "/settings"])
def test_pages_require_login(client: TestClient, path: str) -> None:
    response = client.get(path, follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"].startswith("/login?next=")


def test_htmx_request_without_login_gets_hx_redirect(client: TestClient) -> None:
    response = client.get(
        "/partials/dashboard/status",
        headers={"HX-Request": "true", "HX-Current-URL": "http://testserver/"},
        follow_redirects=False,
    )

    assert response.status_code == 401
    assert response.headers["HX-Redirect"] == "/login?next=%2F"


def test_login_success_redirects_to_next(client: TestClient) -> None:
    response = client.post(
        "/login",
        data={
            "username": "admin",
            "password": TEST_PASSWORD,
            "next": "/forecast",
            "csrf_token": csrf_token(client),
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/forecast"
    assert client.get("/forecast").status_code == 200


@pytest.mark.parametrize(
    ("username", "password"),
    [("admin", "wrong-password"), ("someone", TEST_PASSWORD)],
)
def test_login_failure(client: TestClient, username: str, password: str) -> None:
    response = client.post(
        "/login",
        data={"username": username, "password": password, "csrf_token": csrf_token(client)},
        follow_redirects=False,
    )

    assert response.status_code == 401
    assert "正しくありません" in response.text
    assert client.get("/", follow_redirects=False).status_code == 303


def test_login_rejects_external_next(client: TestClient) -> None:
    response = client.post(
        "/login",
        data={
            "username": "admin",
            "password": TEST_PASSWORD,
            "next": "//evil.example.com/",
            "csrf_token": csrf_token(client),
        },
        follow_redirects=False,
    )

    assert response.headers["location"] == "/"


def test_login_requires_csrf_token(client: TestClient) -> None:
    client.get("/login")
    response = client.post(
        "/login",
        data={"username": "admin", "password": TEST_PASSWORD},
        follow_redirects=False,
    )

    assert response.status_code == 403


def test_login_regenerates_csrf_token(client: TestClient) -> None:
    before = csrf_token(client)
    client.post(
        "/login",
        data={"username": "admin", "password": TEST_PASSWORD, "csrf_token": before},
    )

    assert csrf_token(client) != before


def test_logout(logged_in_client: TestClient) -> None:
    response = logged_in_client.post(
        "/logout", data={"csrf_token": csrf_token(logged_in_client)}, follow_redirects=False
    )

    assert response.status_code == 303
    assert logged_in_client.get("/", follow_redirects=False).status_code == 303


def test_password_hash_roundtrip() -> None:
    encoded = hash_password("secret")

    assert verify_password("secret", encoded)
    assert not verify_password("Secret", encoded)
    assert not verify_password("secret", "invalid")


# --- 画面 ---


@pytest.mark.parametrize(("path", "heading"), PAGES)
def test_pages_render(logged_in_client: TestClient, path: str, heading: str) -> None:
    response = logged_in_client.get(path)

    assert response.status_code == 200
    assert heading in response.text
    assert "DRY RUN" in response.text
    assert 'aria-current="page"' in response.text


def test_dashboard_status_partial(logged_in_client: TestClient) -> None:
    response = logged_in_client.get("/partials/dashboard/status", headers={"HX-Request": "true"})

    assert response.status_code == 200
    assert "現在の電源料金単価" in response.text
    assert "<html" not in response.text


def test_forecast_rejects_unknown_day(logged_in_client: TestClient) -> None:
    assert logged_in_client.get("/forecast?day=yesterday").status_code == 422


# --- 設定 ---


def test_settings_defaults_come_from_environment(logged_in_client: TestClient) -> None:
    response = logged_in_client.get("/settings")

    assert 'name="operation_start" value="09:00"' in response.text
    assert 'name="round_trip_efficiency" value="85"' in response.text


def test_settings_save_and_reload(logged_in_client: TestClient) -> None:
    response = logged_in_client.post(
        "/settings",
        data={**VALID_SETTINGS, "csrf_token": csrf_token(logged_in_client)},
        headers={"HX-Request": "true"},
    )

    assert response.status_code == 200
    assert "設定を保存しました" in response.text
    assert "<html" not in response.text

    reloaded = logged_in_client.get("/settings").text
    assert 'name="operation_start" value="08:30"' in reloaded
    assert 'name="round_trip_efficiency" value="80"' in reloaded
    assert 'name="estimated_average_power_w" value="200"' in reloaded


def test_settings_accepts_csrf_header(logged_in_client: TestClient) -> None:
    response = logged_in_client.post(
        "/settings",
        data=VALID_SETTINGS,
        headers={"HX-Request": "true", "X-CSRF-Token": csrf_token(logged_in_client)},
    )

    assert response.status_code == 200


@pytest.mark.parametrize(
    ("override", "message"),
    [
        ({"operation_start": "18:00", "operation_end": "09:00"}, "運用開始時刻は運用終了時刻より前"),
        ({"max_continuous_off_minutes": "200", "max_daily_off_minutes": "100"}, "最大連続OFF時間は"),
        ({"round_trip_efficiency": "120"}, "充放電往復効率"),
        ({"min_saving_spread": "abc"}, "最低節約単価差"),
    ],
)
def test_settings_rejects_invalid_values(
    logged_in_client: TestClient, override: dict[str, str], message: str
) -> None:
    response = logged_in_client.post(
        "/settings",
        data={**VALID_SETTINGS, **override, "csrf_token": csrf_token(logged_in_client)},
        headers={"HX-Request": "true"},
    )

    assert response.status_code == 422
    assert message in response.text
    # 保存されていないこと
    assert 'name="operation_start" value="09:00"' in logged_in_client.get("/settings").text


def test_settings_requires_csrf_token(logged_in_client: TestClient) -> None:
    response = logged_in_client.post("/settings", data=VALID_SETTINGS)

    assert response.status_code == 403


def test_settings_post_requires_login(client: TestClient) -> None:
    response = client.post(
        "/settings",
        data={**VALID_SETTINGS, "csrf_token": csrf_token(client)},
        follow_redirects=False,
    )

    assert response.status_code == 303
