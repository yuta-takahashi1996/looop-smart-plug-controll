import base64
import hashlib
import hmac
import json
from datetime import date, datetime, time, timedelta

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.clients import switchbot
from app.clients.switchbot import (
    SwitchBotAuthError,
    SwitchBotClient,
    SwitchBotError,
    SwitchBotNotConfiguredError,
    build_auth_headers,
)
from app.config import get_settings
from app.models.schedule import ControlLog, ControlResult, ControlSchedule, ControlSource, PlugState, ScheduleStatus
from app.models.settings import EventCategory, unresolved_events
from app.providers.base import DayPrices, FetchResult, ForecastLevel
from app.repositories.database import SessionLocal
from app.services import forecast as forecast_service
from app.services import optimizer
from app.services.controller import ControlService, PlugMonitor
from tests.conftest import JST

# --- SwitchBot クライアント ---


def _client(handler, *, token: str = "token", secret: str = "secret", device_id: str = "DEVICE1") -> SwitchBotClient:
    http = httpx.Client(base_url=switchbot.BASE_URL, transport=httpx.MockTransport(handler))
    return SwitchBotClient(token, secret, device_id, client=http)


def _ok(body: dict | None = None) -> httpx.Response:
    return httpx.Response(200, json={"statusCode": 100, "body": body or {}, "message": "success"})


def test_auth_headers_are_signed_with_hmac_sha256() -> None:
    headers = build_auth_headers("token", "secret", t="1700000000000", nonce="abc")

    expected = base64.b64encode(hmac.new(b"secret", b"token1700000000000abc", hashlib.sha256).digest()).decode()
    assert headers["Authorization"] == "token"
    assert headers["sign"] == expected
    assert headers["t"] == "1700000000000"
    assert headers["nonce"] == "abc"


@pytest.mark.parametrize(("power", "expected"), [("on", "ON"), ("off", "OFF"), ("ON", "ON")])
def test_get_status(power: str, expected: str) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return _ok({"deviceId": "DEVICE1", "deviceType": "Plug Mini (JP)", "power": power})

    status = _client(handler).get_status()

    assert status.power == expected
    assert status.device_type == "Plug Mini (JP)"
    assert requests[0].method == "GET"
    assert requests[0].url.path == "/v1.1/devices/DEVICE1/status"
    assert requests[0].headers["Authorization"] == "token"
    assert "sign" in requests[0].headers


def test_get_status_rejects_unknown_power_value() -> None:
    with pytest.raises(SwitchBotError, match="電源状態"):
        _client(lambda r: _ok({"power": ""})).get_status()


@pytest.mark.parametrize(("state", "command"), [("ON", "turnOn"), ("OFF", "turnOff")])
def test_send_power_command_uses_explicit_commands(state: str, command: str) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return _ok()

    _client(handler).send_power_command(state)  # type: ignore[arg-type]

    assert requests[0].method == "POST"
    assert requests[0].url.path == "/v1.1/devices/DEVICE1/commands"
    assert json.loads(requests[0].content) == {"command": command, "parameter": "default", "commandType": "command"}


def test_toggle_is_not_supported() -> None:
    with pytest.raises(KeyError):
        _client(lambda r: _ok()).send_power_command("TOGGLE")  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("response", "error", "message"),
    [
        (httpx.Response(401, json={"message": "Unauthorized"}), SwitchBotAuthError, "認証"),
        (httpx.Response(429), SwitchBotError, "上限"),
        (httpx.Response(500), SwitchBotError, "HTTP 500"),
        (httpx.Response(200, json={"statusCode": 161, "body": {}, "message": "device offline"}), SwitchBotError, "オフライン"),
        (httpx.Response(200, json={"statusCode": 152, "body": {}}), SwitchBotError, "見つかりません"),
        (httpx.Response(200, text="not json"), SwitchBotError, "解釈"),
    ],
)
def test_api_errors(response: httpx.Response, error: type[Exception], message: str) -> None:
    with pytest.raises(error, match=message):
        _client(lambda r: response).get_status()


def test_network_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("timeout", request=request)

    with pytest.raises(SwitchBotError, match="接続できませんでした"):
        _client(handler).get_status()


@pytest.mark.parametrize(
    "kwargs", [{"token": ""}, {"secret": ""}, {"device_id": ""}]
)
def test_not_configured(kwargs: dict[str, str]) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("未設定のときは通信しない")

    client = _client(handler, **kwargs)

    assert not client.configured
    with pytest.raises(SwitchBotNotConfiguredError):
        client.get_status()


def test_error_messages_do_not_contain_credentials() -> None:
    with pytest.raises(SwitchBotAuthError) as exc_info:
        _client(lambda r: httpx.Response(401), token="SECRET-TOKEN", secret="SECRET-KEY").get_status()
    assert "SECRET" not in str(exc_info.value)


# --- プラグ状態の監視 ---


class FakeSwitchBot:
    def __init__(self) -> None:
        self.configured = True
        self.results: list[switchbot.PlugStatus | Exception] = []
        self.calls = 0

    def get_status(self) -> switchbot.PlugStatus:
        self.calls += 1
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    def send_power_command(self, state: str) -> None:
        raise AssertionError("監視ではコマンドを送信しない")


def _status(power: str) -> switchbot.PlugStatus:
    return switchbot.PlugStatus(power=power, device_type="Plug Mini (JP)", raw={})  # type: ignore[arg-type]


def test_monitor_keeps_latest_status(session: Session) -> None:
    fake = FakeSwitchBot()
    fake.results = [_status("ON"), _status("OFF")]
    monitor = PlugMonitor(fake, SessionLocal)  # type: ignore[arg-type]

    assert monitor.snapshot().status is None  # 確認前は未確認
    monitor.refresh()
    assert monitor.snapshot().status.power == "ON"
    monitor.refresh()
    snapshot = monitor.snapshot()
    assert snapshot.status.power == "OFF"
    assert snapshot.error is None
    assert snapshot.checked_at is not None


def test_monitor_records_error_once_and_resolves_on_recovery(session: Session) -> None:
    fake = FakeSwitchBot()
    error = SwitchBotError("デバイスがオフラインです")
    fake.results = [_status("ON"), error, error, _status("ON")]
    monitor = PlugMonitor(fake, SessionLocal)  # type: ignore[arg-type]

    monitor.refresh()
    monitor.refresh()
    # 失敗時は古い状態を現在の状態として残さない
    assert monitor.snapshot().status is None
    assert monitor.snapshot().error == "デバイスがオフラインです"
    monitor.refresh()
    assert [e.category for e in unresolved_events(session)] == [EventCategory.SWITCHBOT]  # 同じエラーは1回だけ

    monitor.refresh()
    session.expire_all()
    assert unresolved_events(session) == []
    assert monitor.snapshot().status.power == "ON"


def test_monitor_without_credentials_does_not_call_api(session: Session) -> None:
    fake = FakeSwitchBot()
    fake.configured = False
    monitor = PlugMonitor(fake, SessionLocal)  # type: ignore[arg-type]

    snapshot = monitor.refresh()

    assert fake.calls == 0
    assert snapshot.status is None
    assert "未設定" in snapshot.error
    assert unresolved_events(session) == []


# --- Control Service（Dry Run でのスケジュール実行） ---

DAY = date(2030, 1, 15)
N, A, W = ForecastLevel.NORMAL, ForecastLevel.ADVISORY, ForecastLevel.WARNING


def _at(hour: int, minute: int = 0, second: int = 5) -> datetime:
    return datetime.combine(DAY, time(hour, minute, second), tzinfo=JST)


def _prepare(session: Session, spec: dict[range, ForecastLevel], enable: bool = True) -> list[ControlSchedule]:
    levels = [N] * 48
    for r, level in spec.items():
        for i in r:
            levels[i] = level
    prices = [20.0] * 48
    result = FetchResult("hokkaido", datetime.combine(DAY, time(6), tzinfo=JST), [DayPrices(DAY, prices, levels)], "test")
    forecast_service.save_result(session, result, JST)
    before = datetime.combine(DAY, time(), tzinfo=JST) - timedelta(hours=1)
    schedules = optimizer.regenerate(session, get_settings(), DAY, now=before)
    if enable:
        for s in schedules:
            if not s.enabled:
                optimizer.update_schedule(session, get_settings(), s, enabled=True, now=before)
    return optimizer.schedules_for(session, DAY)


def _logs(session: Session) -> list[tuple[str, str | None, str, str]]:
    session.expire_all()
    return [
        (log.source.value, log.previous_state.value if log.previous_state else None, log.requested_state.value, log.result.value)
        for log in session.scalars(select(ControlLog).order_by(ControlLog.id))
    ]


def _schedules(session: Session) -> list[ControlSchedule]:
    session.expire_all()
    return optimizer.schedules_for(session, DAY)


@pytest.fixture
def control() -> ControlService:
    return ControlService(get_settings(), SessionLocal)


def test_initializes_to_on_once(session: Session, control: ControlService) -> None:
    control.run_cycle(_at(8))
    control.run_cycle(_at(8, 1))

    assert _logs(session) == [("SAFETY", None, "ON", "DRY_RUN")]
    assert control.mode == "GRID"


def test_runs_enabled_schedule_without_sending_commands(session: Session, control: ControlService) -> None:
    _prepare(session, {range(34, 36): A})  # 17:00〜18:00 の注意報を有効にする

    for t in [_at(16, 59), _at(17, 0), _at(17, 30), _at(17, 59), _at(18, 0)]:
        control.run_cycle(t)

    assert _logs(session) == [
        ("SAFETY", None, "ON", "DRY_RUN"),
        ("AUTO", "ON", "OFF", "DRY_RUN"),
        ("AUTO", "OFF", "ON", "DRY_RUN"),
    ]
    s = _schedules(session)[0]
    assert s.status == ScheduleStatus.DONE
    assert s.executed_start_at == _at(17, 0)
    assert s.executed_end_at == _at(18, 0, 0)
    # 17:00:05〜18:00 の実行分の推定節約額（計画値とほぼ同じ）
    assert s.executed_saving == pytest.approx(s.estimated_saving, abs=0.1)
    session.expire_all()
    off_log = session.scalars(select(ControlLog).where(ControlLog.requested_state == PlugState.OFF)).one()
    assert off_log.schedule_id == s.id and off_log.dry_run and off_log.actual_state is None


def test_disabled_schedule_is_not_executed(session: Session, control: ControlService) -> None:
    _prepare(session, {range(34, 36): A}, enable=False)

    for t in [_at(17), _at(17, 30), _at(18)]:
        control.run_cycle(t)

    assert _logs(session) == [("SAFETY", None, "ON", "DRY_RUN")]
    s = _schedules(session)[0]
    assert s.status == ScheduleStatus.SKIPPED and "無効" in s.status_reason


def test_warning_outside_operation_hours_is_not_started_without_battery(session: Session, control: ControlService) -> None:
    _prepare(session, {range(38, 40): W})  # 19:00〜20:00（運用時間外）

    control.run_cycle(_at(19))

    assert _logs(session) == [("SAFETY", None, "ON", "DRY_RUN")]
    s = _schedules(session)[0]
    assert s.status == ScheduleStatus.ABORTED and "運用時間外" in s.status_reason


def test_warning_is_stopped_at_continuous_limit_without_battery(session: Session, control: ControlService) -> None:
    _prepare(session, {range(20, 26): W})  # 10:00〜13:00 の警報（生成時は切り詰めない）

    for t in [_at(10), _at(11), _at(11, 59), _at(12, 0, 5), _at(12, 30)]:
        control.run_cycle(t)

    assert _logs(session) == [
        ("SAFETY", None, "ON", "DRY_RUN"),
        ("AUTO", "ON", "OFF", "DRY_RUN"),
        ("SAFETY", "OFF", "ON", "DRY_RUN"),
    ]
    s = _schedules(session)[0]
    assert s.status == ScheduleStatus.ABORTED and "最大連続OFF時間" in s.status_reason
    assert s.executed_end_at == _at(12, 0, 5)


def test_adjacent_schedules_do_not_switch_between(session: Session, control: ControlService) -> None:
    _prepare(session, {range(32, 34): A, range(34, 36): W})  # 16:00〜17:00 注意報、17:00〜18:00 警報

    for t in [_at(16), _at(17), _at(18)]:
        control.run_cycle(t)

    assert [log[2] for log in _logs(session)] == ["ON", "OFF", "ON"]
    assert [s.status for s in _schedules(session)] == [ScheduleStatus.DONE, ScheduleStatus.DONE]


def test_schedule_missed_while_stopped_is_skipped(session: Session, control: ControlService) -> None:
    _prepare(session, {range(20, 22): A})

    control.run_cycle(_at(12))

    s = _schedules(session)[0]
    assert s.status == ScheduleStatus.SKIPPED and "停止" in s.status_reason
    assert [log[2] for log in _logs(session)] == ["ON"]


def test_shutdown_returns_to_on(session: Session, control: ControlService) -> None:
    _prepare(session, {range(20, 22): A})
    control.run_cycle(_at(10))

    control.shutdown(_at(10, 15))

    assert _logs(session)[-1] == ("SAFETY", "OFF", "ON", "DRY_RUN")
    s = _schedules(session)[0]
    assert s.status == ScheduleStatus.ABORTED and s.status_reason == "システムの終了"
    assert s.executed_end_at == _at(10, 15)


def test_control_logs_are_dry_run(session: Session, control: ControlService) -> None:
    control.initialize(_at(8))

    log = session.scalars(select(ControlLog)).one()
    assert (log.source, log.result, log.dry_run) == (ControlSource.SAFETY, ControlResult.DRY_RUN, True)


def test_initialize_failure_does_not_raise(session: Session) -> None:
    def broken_session() -> Session:
        raise RuntimeError("database is not ready")

    control = ControlService(get_settings(), broken_session)

    control.initialize(_at(8))  # 例外を外へ出さない

    assert control.mode == "SAFE"
