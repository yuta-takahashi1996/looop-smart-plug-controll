from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from httpx import Response
from sqlalchemy import select

from app.api.auth import hash_password, verify_password
from app.clients.switchbot import PlugStatus
from app.main import app
from app.models.forecast import PriceAdjustment
from app.models.schedule import ControlLog, ControlSchedule
from app.models.settings import SystemEvent
from app.providers.base import ForecastFetchError, Notice
from app.services.controller import PlugSnapshot
from app.services.forecast import check_price_notices
from tests.conftest import JST, TEST_PASSWORD, FakeProvider, csrf_token

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
    "round_trip_efficiency": "80",
    "max_continuous_off_minutes": "90",
    "max_daily_off_minutes": "150",
    "min_switch_interval_minutes": "30",
    "estimated_average_power_w": "200",
    "battery_min_percent": "5",
    "battery_capacity_wh": "1070",
    "battery_discharge_efficiency": "85",
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
    assert "電力情報" in response.text
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
        ({"estimated_average_power_w": "abc"}, "想定平均負荷"),
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


# --- 実データ（料金予報・加算額・プラグ状態・お知らせ） ---


def _fetch(client: TestClient) -> None:
    response = client.post(
        "/forecast/refresh", data={"csrf_token": csrf_token(client), "next": "/"}, follow_redirects=False
    )
    assert response.headers["location"] == "/?refresh=ok"


def _add_adjustment(client: TestClient, **fields: str) -> Response:
    data = {"category": "SERVICE", "amount": "7.0", "effective_from": "2020-01-01", "effective_to": "", "note": ""}
    return client.post(
        "/settings/adjustments",
        data={**data, **fields, "csrf_token": csrf_token(client)},
        follow_redirects=False,
    )


def _set_view(client: TestClient, view: str, next_path: str = "/") -> Response:
    return client.post(
        "/price-view", data={"view": view, "next": next_path, "csrf_token": csrf_token(client)}, follow_redirects=False
    )


def test_dashboard_without_forecast(logged_in_client: TestClient) -> None:
    text = logged_in_client.get("/").text

    assert "本日の料金予報が未取得です" in text
    assert "加算額が未登録" not in text  # 電源単価表示（既定）では加算額の警告を出さない

    _set_view(logged_in_client, "billing")
    assert "加算額が未登録" in logged_in_client.get("/").text


def test_dashboard_shows_energy_price_by_default(logged_in_client: TestClient, provider: FakeProvider) -> None:
    _fetch(logged_in_client)
    _add_adjustment(logged_in_client, amount="7.5")

    text = logged_in_client.get("/").text

    assert provider.calls == 1
    assert "現在の電源単価" in text
    assert "請求単価の目安" in text
    assert "制御の判断は電源単価で行います" in text
    assert "本日の料金予報が未取得です" not in text


def test_price_view_toggle_switches_to_billing(logged_in_client: TestClient) -> None:
    _fetch(logged_in_client)
    _add_adjustment(logged_in_client, amount="7.5")

    response = _set_view(logged_in_client, "billing", "/forecast?day=tomorrow")

    assert response.status_code == 303
    assert response.headers["location"] == "/forecast?day=tomorrow"
    text = logged_in_client.get("/").text
    assert "現在の請求単価" in text
    assert "加算額 7.50" in text
    assert "請求単価（30分単位）" in logged_in_client.get("/forecast").text
    assert "この日の加算額の内訳" in logged_in_client.get("/forecast").text

    _set_view(logged_in_client, "energy")
    assert "現在の電源単価" in logged_in_client.get("/").text


def test_price_view_toggle_validation(logged_in_client: TestClient) -> None:
    assert _set_view(logged_in_client, "billing", "https://evil.example.com/").headers["location"] == "/"
    assert _set_view(logged_in_client, "unknown").status_code == 422
    assert logged_in_client.post("/price-view", data={"view": "billing"}).status_code == 403


def test_forecast_page_shows_levels(logged_in_client: TestClient) -> None:
    _fetch(logged_in_client)

    text = logged_in_client.get("/forecast").text

    assert "注意報・警報のコマ" in text
    assert "17:00" in text and "でんき注意報" in text
    assert "19:00" in text and "でんき警報" in text
    assert '"levels": ["NORMAL"' in text


def test_refresh_forecast_failure_is_reported(logged_in_client: TestClient, provider: FakeProvider) -> None:
    provider.errors = [ForecastFetchError("料金予報の取得に失敗しました: timeout")]

    response = logged_in_client.post(
        "/forecast/refresh",
        data={"csrf_token": csrf_token(logged_in_client), "next": "/forecast?day=today"},
        follow_redirects=False,
    )

    assert response.headers["location"] == "/forecast?day=today&refresh=error"
    text = logged_in_client.get(response.headers["location"]).text
    assert "料金予報を取得できませんでした" in text
    assert "timeout" in text
    assert "異常" in logged_in_client.get("/").text


def test_refresh_forecast_rejects_external_next(logged_in_client: TestClient) -> None:
    response = logged_in_client.post(
        "/forecast/refresh",
        data={"csrf_token": csrf_token(logged_in_client), "next": "https://evil.example.com/"},
        follow_redirects=False,
    )

    assert response.headers["location"] == "/forecast?refresh=ok"


def test_refresh_forecast_requires_csrf(logged_in_client: TestClient, provider: FakeProvider) -> None:
    assert logged_in_client.post("/forecast/refresh", data={"next": "/"}).status_code == 403
    assert provider.calls == 0


def test_forecast_page_with_data(logged_in_client: TestClient) -> None:
    _fetch(logged_in_client)

    for day in ("today", "tomorrow"):
        text = logged_in_client.get(f"/forecast?day={day}").text
        assert 'id="forecast-data"' in text
        assert "電源単価（30分単位）" in text
        assert "この日の加算額の内訳" not in text


def test_forecast_page_without_data(logged_in_client: TestClient) -> None:
    text = logged_in_client.get("/forecast?day=today").text

    assert "未取得" in text
    assert 'id="forecast-data"' not in text


def test_adjustment_create_and_delete(logged_in_client: TestClient, session) -> None:
    assert _add_adjustment(logged_in_client, category="SUBSIDY", amount="-4.5", note="国の支援").status_code == 303

    text = logged_in_client.get("/settings").text
    assert "補助金・値引き" in text
    assert "-4.50 円/kWh" in text
    assert "国の支援" in text

    adjustment_id = session.scalars(select(PriceAdjustment.id)).one()
    response = logged_in_client.post(
        f"/settings/adjustments/{adjustment_id}/delete",
        data={"csrf_token": csrf_token(logged_in_client)},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "国の支援" not in logged_in_client.get("/settings").text


@pytest.mark.parametrize(
    ("fields", "message"),
    [
        ({"effective_from": "2026-12-31", "effective_to": "2026-12-01"}, "適用終了日は適用開始日以降"),
        ({"amount": "abc"}, "金額"),
        ({"amount": "150"}, "金額"),
        ({"category": "UNKNOWN"}, "種類"),
        ({"effective_from": ""}, "適用開始日"),
    ],
)
def test_adjustment_validation(logged_in_client: TestClient, fields: dict[str, str], message: str) -> None:
    response = _add_adjustment(logged_in_client, **fields)

    assert response.status_code == 422
    assert message in response.text


def test_adjustment_requires_csrf(logged_in_client: TestClient) -> None:
    response = logged_in_client.post(
        "/settings/adjustments", data={"category": "SERVICE", "amount": "7", "effective_from": "2026-01-01"}
    )

    assert response.status_code == 403


def test_price_notice_can_be_resolved(logged_in_client: TestClient, provider: FakeProvider, session) -> None:
    check_price_notices(session, provider)  # 初回（既存のお知らせなし）
    provider.notices = [
        Notice("https://looop-denki.com/home/news/1/", "【重要】国の「電気・ガス料金支援」に関するお知らせ"),
        Notice("https://looop-denki.com/home/news/2/", "【重要】託送料金変更に伴う料金改定のお知らせ"),
    ]
    check_price_notices(session, provider)

    text = logged_in_client.get("/").text
    assert "電気・ガス料金支援" in text
    assert "警告" in text

    event_id = session.scalars(
        select(SystemEvent.id).where(SystemEvent.message.contains("料金支援"))
    ).one()
    logged_in_client.post(f"/events/{event_id}/resolve", data={"csrf_token": csrf_token(logged_in_client)})
    text = logged_in_client.get("/").text
    assert "電気・ガス料金支援" not in text
    assert "託送料金変更" in text

    logged_in_client.post("/events/resolve-price-notices", data={"csrf_token": csrf_token(logged_in_client)})
    assert "託送料金変更" not in logged_in_client.get("/").text


def test_errors_cannot_be_resolved_manually(logged_in_client: TestClient, provider: FakeProvider, session) -> None:
    provider.errors = [ForecastFetchError("timeout")]
    logged_in_client.post("/forecast/refresh", data={"csrf_token": csrf_token(logged_in_client), "next": "/"})
    event_id = session.scalars(select(SystemEvent.id)).one()

    logged_in_client.post(f"/events/{event_id}/resolve", data={"csrf_token": csrf_token(logged_in_client)})

    session.expire_all()
    assert session.get(SystemEvent, event_id).resolved_at is None


def test_dashboard_shows_plug_status(logged_in_client: TestClient) -> None:
    class Monitor:
        def snapshot(self) -> PlugSnapshot:
            return PlugSnapshot(
                status=PlugStatus(power="OFF", device_type="Plug Mini (JP)", raw={}),
                checked_at=datetime.now(UTC),
                error=None,
            )

    app.state.plug_monitor = Monitor()

    text = logged_in_client.get("/").text

    assert "OFF（蓄電池）" in text
    assert "確認" in text


def test_dashboard_shows_unconfirmed_plug_status(logged_in_client: TestClient) -> None:
    # 状態を確認する前（またはSwitchBotが未設定・取得失敗）は「未確認」と表示し、ON/OFF を推測しない
    text = logged_in_client.get("/").text

    assert "未確認" in text
    assert "ON（商用電源）" not in text.split("プラグの状態")[1].split("</dd>")[0]


# --- 運転スケジュール（Phase 4） ---
# FakeProvider の明日分：17:00〜19:00 が注意報、19:00〜20:00 が警報（運用時間 9:00〜18:00）


def _tomorrow_schedules(session) -> list[ControlSchedule]:
    tomorrow = datetime.now(JST).date() + timedelta(days=1)
    session.expire_all()
    return list(session.scalars(select(ControlSchedule).where(ControlSchedule.schedule_date == tomorrow).order_by(ControlSchedule.start_at)))


def test_schedules_are_generated_after_refresh(logged_in_client: TestClient, session) -> None:
    _fetch(logged_in_client)

    schedules = _tomorrow_schedules(session)
    assert [(s.source_level.value, s.enabled) for s in schedules] == [("ADVISORY", False), ("WARNING", True)]

    text = logged_in_client.get("/schedule?day=tomorrow").text
    assert "17:00〜18:00" in text and "でんき注意報" in text and "運用時間外を除外" in text
    assert "19:00〜20:00" in text and "でんき警報" in text
    assert "有効にする" in text and "無効にする" in text


def test_toggle_schedule(logged_in_client: TestClient, session) -> None:
    _fetch(logged_in_client)
    advisory = _tomorrow_schedules(session)[0]

    response = logged_in_client.post(
        f"/schedule/{advisory.id}/toggle",
        data={"enabled": "true", "csrf_token": csrf_token(logged_in_client)},
        follow_redirects=False,
    )

    assert response.headers["location"] == "/schedule?day=tomorrow&updated=toggle"
    advisory = _tomorrow_schedules(session)[0]
    assert advisory.enabled and advisory.manually_modified
    text = logged_in_client.get(response.headers["location"]).text
    assert "有効・無効を切り替えました" in text
    assert "手動・手動変更済み" in text


def test_toggle_requires_csrf(logged_in_client: TestClient, session) -> None:
    _fetch(logged_in_client)
    advisory = _tomorrow_schedules(session)[0]

    assert logged_in_client.post(f"/schedule/{advisory.id}/toggle", data={"enabled": "true"}).status_code == 403


def test_change_schedule_time(logged_in_client: TestClient, session) -> None:
    _fetch(logged_in_client)
    warning = _tomorrow_schedules(session)[1]

    response = logged_in_client.post(
        f"/schedule/{warning.id}/time",
        data={"start": "19:00", "end": "21:30", "csrf_token": csrf_token(logged_in_client)},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert "19:00〜21:30" in logged_in_client.get("/schedule?day=tomorrow").text


@pytest.mark.parametrize(
    ("index", "start", "end", "message"),
    [
        (0, "16:00", "19:30", "重なります"),
        (0, "18:00", "18:30", "運用時間"),
        (0, "17:15", "18:00", "30分単位"),
        (0, "aa", "18:00", "HH:MM"),
    ],
)
def test_change_schedule_time_validation(
    logged_in_client: TestClient, session, index: int, start: str, end: str, message: str
) -> None:
    _fetch(logged_in_client)
    schedule = _tomorrow_schedules(session)[index]

    response = logged_in_client.post(
        f"/schedule/{schedule.id}/time",
        data={"start": start, "end": end, "csrf_token": csrf_token(logged_in_client)},
    )

    assert response.status_code == 422
    assert message in response.text
    assert not _tomorrow_schedules(session)[index].manually_modified


def test_regenerate_keeps_manual_changes(logged_in_client: TestClient, session) -> None:
    _fetch(logged_in_client)
    advisory = _tomorrow_schedules(session)[0]
    logged_in_client.post(
        f"/schedule/{advisory.id}/toggle", data={"enabled": "true", "csrf_token": csrf_token(logged_in_client)}
    )

    response = logged_in_client.post(
        "/schedule/regenerate",
        data={"next": "/schedule?day=tomorrow", "csrf_token": csrf_token(logged_in_client)},
        follow_redirects=False,
    )

    assert response.headers["location"] == "/schedule?day=tomorrow&updated=regenerate"
    schedules = _tomorrow_schedules(session)
    assert schedules[0].id == advisory.id and schedules[0].enabled
    assert schedules[1].schedule_version == 2


def test_auto_enable_setting_enables_advisory(logged_in_client: TestClient, session) -> None:
    _fetch(logged_in_client)

    response = logged_in_client.post(
        "/settings",
        data={**VALID_SETTINGS, "operation_start": "09:00", "operation_end": "18:00", "auto_enable": "on", "csrf_token": csrf_token(logged_in_client)},
        headers={"HX-Request": "true"},
    )

    assert response.status_code == 200
    assert 'name="auto_enable" checked' in logged_in_client.get("/settings").text
    assert [s.enabled for s in _tomorrow_schedules(session)] == [True, True]


def test_forecast_page_shows_plan(logged_in_client: TestClient) -> None:
    _fetch(logged_in_client)

    text = logged_in_client.get("/forecast?day=tomorrow").text

    assert '"plan": [null' in text
    assert '"DISABLED"' in text and '"ENABLED"' in text


def test_schedule_page_without_forecast(logged_in_client: TestClient) -> None:
    assert "料金予報が未取得" in logged_in_client.get("/schedule?day=tomorrow").text


# --- Dry Run（Phase 5） ---


def test_history_shows_control_logs(logged_in_client: TestClient) -> None:
    app.state.control.initialize()

    text = logged_in_client.get("/history").text

    assert "起動時の安全側への初期化" in text
    assert "SAFETY" in text and "Dry Run" in text
    assert "実行するはずだった操作" in text


def test_history_date_filter(logged_in_client: TestClient) -> None:
    app.state.control.initialize()

    text = logged_in_client.get("/history?date=2020-01-01").text

    assert "起動時の安全側への初期化" not in text
    assert "この日の操作履歴はありません" in text


def test_dashboard_shows_dry_run_control_state(logged_in_client: TestClient) -> None:
    text = logged_in_client.get("/").text
    assert "SAFE" in text  # 初期化前
    assert "有効（Dry Run）" in text

    app.state.control.initialize()
    text = logged_in_client.get("/").text

    assert "GRID" in text
    assert "最終操作" in text and "Dry Run の推定値" in text


# --- 実制御・手動操作（Phase 6） ---


def _post(client: TestClient, path: str, **data: str) -> Response:
    return client.post(path, data={**data, "csrf_token": csrf_token(client)}, follow_redirects=False)


def test_manual_power_switches_to_manual_mode(logged_in_client: TestClient, session) -> None:
    response = _post(logged_in_client, "/control/plug/off")

    assert response.status_code == 303
    text = logged_in_client.get(response.headers["location"]).text
    assert "プラグを OFF にしました" in text and "Dry Run" in text
    assert "MANUAL" in text
    session.expire_all()
    log = session.scalars(select(ControlLog).order_by(ControlLog.id.desc())).first()
    assert (log.source.value, log.requested_state.value, log.result.value) == ("MANUAL", "OFF", "DRY_RUN")

    _post(logged_in_client, "/control/auto/start")
    assert app.state.control.control_mode == "AUTO"


def test_stop_and_start_auto_control(logged_in_client: TestClient) -> None:
    response = _post(logged_in_client, "/control/auto/stop")

    assert "自動制御を停止しました" in logged_in_client.get(response.headers["location"]).text
    assert app.state.control.control_mode == "MANUAL"
    _post(logged_in_client, "/control/auto/start")
    assert app.state.control.control_mode == "AUTO"


def test_manual_operations_require_csrf(logged_in_client: TestClient) -> None:
    assert logged_in_client.post("/control/plug/off").status_code == 403
    assert logged_in_client.post("/control/auto/stop").status_code == 403
    assert app.state.control.control_mode == "AUTO"


def test_real_control_cannot_be_enabled_while_dry_run_env(logged_in_client: TestClient) -> None:
    text = logged_in_client.get("/settings").text
    assert "DRY_RUN=false" in text and "実制御を開始</button>" not in text

    response = _post(logged_in_client, "/control/real-mode", enabled="true", confirmation="実制御を開始")

    assert "DRY_RUN=false" in logged_in_client.get(response.headers["location"]).text
    assert app.state.control.dry_run


def test_real_control_requires_confirmation_text(logged_in_client: TestClient) -> None:
    response = _post(logged_in_client, "/control/real-mode", enabled="true", confirmation="はい")

    assert "「実制御を開始」と入力してください" in logged_in_client.get(response.headers["location"]).text
    assert app.state.control.dry_run


def test_dashboard_shows_battery_not_configured(logged_in_client: TestClient) -> None:
    text = logged_in_client.get("/").text

    assert "ポータブル電源" in text and "未設定" in text
    assert "DRY RUN" in text
