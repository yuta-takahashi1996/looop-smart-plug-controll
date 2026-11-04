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
from app.clients.jackery import ChargeMode, JackeryCommandUnconfirmed, JackeryError
from app.clients.switchbot import (
    SwitchBotAuthError,
    SwitchBotClient,
    SwitchBotError,
    SwitchBotNotConfiguredError,
    build_auth_headers,
)
from app.config import get_settings
from app.models.schedule import (
    ChargeModeOption,
    ControlLog,
    ControlResult,
    ControlSchedule,
    ControlSource,
    PlugState,
    RepeatType,
    ScheduleRule,
    ScheduleStatus,
)
from app.models.settings import EventCategory, SystemEvent, set_value, unresolved_events
from app.providers.base import DayPrices, FetchResult, ForecastLevel
from app.repositories.database import SessionLocal
from app.services import forecast as forecast_service
from app.services import optimizer
from app.services import safety
from app.services.controller import LEGACY_CHARGE_MODE_SCHEDULE_KEY, BatteryMonitor, ControlService, PlugMonitor
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


def _custom_rule(session: Session, state: str, start: int, end: int) -> None:
    rule = ScheduleRule(
        name=f"任意{state}", target_state=state, repeat_type=RepeatType.DAILY, start_time=time(start), end_time=time(end), enabled=True
    )
    optimizer.save_rule(session, get_settings(), rule, now=datetime.combine(DAY, time(), tzinfo=JST) - timedelta(hours=1))


def test_custom_off_runs_without_forecast(session: Session, control: ControlService) -> None:
    _custom_rule(session, "OFF", 12, 13)  # 予報は未取得

    for t in [_at(11, 59), _at(12), _at(12, 30), _at(13)]:
        control.run_cycle(t)

    assert [log[2] for log in _logs(session)] == ["ON", "OFF", "ON"]
    s = _schedules(session)[0]
    assert s.status == ScheduleStatus.DONE and s.executed_start_at == _at(12)
    assert s.executed_saving is None  # 予報がないため算出しない


def test_custom_off_is_limited_like_advisory(session: Session, control: ControlService) -> None:
    _custom_rule(session, "OFF", 12, 13)
    s = _schedules(session)[0]
    s.start_at, s.end_at = _at(17, 0, 0), _at(19, 0, 0)  # 運用時間外にずれた場合も実行時に止める
    session.commit()

    for t in [_at(17, 30), _at(18)]:
        control.run_cycle(t)

    s = _schedules(session)[0]
    assert s.status == ScheduleStatus.ABORTED and "運用時間外" in s.status_reason
    assert [log[2] for log in _logs(session)] == ["ON", "OFF", "ON"]


def test_custom_on_keeps_plug_on(session: Session, control: ControlService) -> None:
    _custom_rule(session, "OFF", 12, 13)
    _custom_rule(session, "ON", 13, 14)

    for t in [_at(12), _at(13), _at(13, 30), _at(14)]:
        control.run_cycle(t)

    logs = _logs(session)
    assert [log[2] for log in logs] == ["ON", "OFF", "ON"]
    session.expire_all()
    on_log = session.scalars(select(ControlLog).order_by(ControlLog.id.desc())).first()
    assert "任意スケジュール「任意ON」" in on_log.reason and on_log.source == ControlSource.AUTO
    on_schedule = _schedules(session)[1]
    assert on_schedule.status == ScheduleStatus.DONE
    assert on_schedule.executed_start_at is None and on_schedule.executed_saving is None  # OFF 時間・節約額に含めない


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


# --- 実制御（Phase 6）：SwitchBot・Jackery は模擬する ---


class FakePlug:
    """コマンドを受けると、指定回数の状態取得の後に状態が変わるプラグ。"""

    def __init__(self, power: str = "ON") -> None:
        self.configured = True
        self.power = power
        self.commands: list[str] = []
        self.ignore_commands = False  # コマンドを無視する（状態が変わらない）
        self.status_error = False  # 状態取得に失敗する

    def get_status(self) -> switchbot.PlugStatus:
        if self.status_error:
            raise SwitchBotError("デバイスがオフラインです")
        return switchbot.PlugStatus(power=self.power, device_type="Plug Mini (JP)", raw={})  # type: ignore[arg-type]

    def send_power_command(self, state: str) -> None:
        assert state in ("ON", "OFF")  # toggle は使わない
        self.commands.append(state)
        if not self.ignore_commands:
            self.power = state


class FakeBattery(BatteryMonitor):
    def __init__(self, readings: list[tuple[int, float] | None]) -> None:
        super().__init__(None, "", SessionLocal)
        self.readings = readings
        self.reads = 0

    @property
    def configured(self) -> bool:
        return True

    def read(self, now: datetime) -> safety.BatteryReading | None:
        self.reads += 1
        value = self.readings.pop(0) if len(self.readings) > 1 else self.readings[0]
        if value is None:
            return None
        reading = safety.BatteryReading(value[0], value[1], now)
        self.last = reading
        self.samples.append(value[1])
        return reading


def _real_control(plug: FakePlug, battery: BatteryMonitor | None = None) -> ControlService:
    settings = get_settings().model_copy(update={"dry_run": False})
    control = ControlService(settings, SessionLocal, switchbot=plug, battery=battery, sleep=lambda _: None)  # type: ignore[arg-type]
    control.set_real_control(True)
    return control


def _results(session: Session) -> list[tuple[str, str, str | None, str]]:
    session.expire_all()
    return [
        (log.source.value, log.requested_state.value, log.actual_state.value if log.actual_state else None, log.result.value)
        for log in session.scalars(select(ControlLog).order_by(ControlLog.id))
    ]


def test_real_control_requires_env_and_confirmation(session: Session) -> None:
    control = ControlService(get_settings(), SessionLocal, switchbot=FakePlug(), sleep=lambda _: None)  # type: ignore[arg-type]

    assert control.dry_run
    assert not control.can_enable_real_control
    with pytest.raises(ValueError, match="DRY_RUN=false"):
        control.set_real_control(True)

    settings = get_settings().model_copy(update={"dry_run": False})
    control = ControlService(settings, SessionLocal, switchbot=FakePlug(), sleep=lambda _: None)  # type: ignore[arg-type]
    assert control.dry_run  # 画面で有効にするまでは Dry Run
    control.set_real_control(True)
    assert not control.dry_run
    control.set_real_control(False)
    assert control.dry_run


def test_real_control_sends_command_and_verifies(session: Session) -> None:
    plug = FakePlug("ON")
    control = _real_control(plug)
    _prepare(session, {range(20, 22): A})

    control.run_cycle(_at(10))  # 初期化（すでに ON なので送信しない）→ OFF
    control.run_cycle(_at(11))  # 終了 → ON

    assert plug.commands == ["OFF", "ON"]
    assert _results(session) == [
        ("SAFETY", "ON", "ON", "SUCCESS"),
        ("AUTO", "OFF", "OFF", "SUCCESS"),
        ("AUTO", "ON", "ON", "SUCCESS"),
    ]


def test_initialization_turns_plug_on(session: Session) -> None:
    plug = FakePlug("OFF")
    control = _real_control(plug)

    control.initialize(_at(8))

    assert plug.commands == ["ON"]
    log = session.scalars(select(ControlLog)).one()
    assert (log.previous_state, log.actual_state, log.result) == (PlugState.OFF, PlugState.ON, ControlResult.SUCCESS)


def test_failed_command_is_not_treated_as_success(session: Session) -> None:
    plug = FakePlug("ON")
    plug.ignore_commands = True
    control = _real_control(plug)
    _prepare(session, {range(20, 24): A})

    control.run_cycle(_at(10))

    assert plug.commands == ["OFF", "OFF"]  # 1回だけ送り直す
    assert _results(session)[-1] == ("AUTO", "OFF", "ON", "FAILED")
    assert control.state == PlugState.ON
    assert [e.category for e in unresolved_events(session)] == [EventCategory.SWITCHBOT]

    control.run_cycle(_at(10, 2))  # 5分間は同じ要求を繰り返さない
    assert plug.commands == ["OFF", "OFF"]
    plug.ignore_commands = False
    control.run_cycle(_at(10, 6))
    assert plug.commands == ["OFF", "OFF", "OFF"]
    session.expire_all()
    assert unresolved_events(session) == []


def test_unconfirmed_state_returns_to_safe(session: Session) -> None:
    plug = FakePlug("ON")
    control = _real_control(plug)
    _prepare(session, {range(20, 24): A})
    control.run_cycle(_at(10))
    plug.status_error = True

    control.request(session, PlugState.ON, ControlSource.AUTO, "test", now=_at(10, 1))

    assert _results(session)[-1] == ("AUTO", "ON", None, "UNCONFIRMED")
    assert control.state is None and control.mode == "SAFE"


def test_manual_operation_switches_to_manual_mode(session: Session) -> None:
    plug = FakePlug("ON")
    control = _real_control(plug)
    _prepare(session, {range(20, 24): A})
    control.run_cycle(_at(10))  # 自動で OFF

    control.manual_power(PlugState.ON, _at(10, 5))

    assert control.mode == "MANUAL"
    assert _schedules(session)[0].status == ScheduleStatus.ABORTED
    control.run_cycle(_at(10, 10))  # MANUAL の間はスケジュールを実行しない
    assert plug.commands == ["OFF", "ON"]
    assert _results(session)[-1][0] == "MANUAL"

    # 再起動しても MANUAL を引き継ぐ
    assert _real_control(plug).control_mode == "MANUAL"

    control.start_auto()
    assert control.control_mode == "AUTO"


def test_stop_auto_keeps_plug_state(session: Session) -> None:
    plug = FakePlug("ON")
    control = _real_control(plug)
    _prepare(session, {range(20, 24): A})
    control.run_cycle(_at(10))

    control.stop_auto(_at(10, 5))

    assert plug.commands == ["OFF"]
    assert control.mode == "MANUAL" and control.state == PlugState.OFF


def test_manual_off_without_battery_returns_on_after_max_continuous(session: Session) -> None:
    plug = FakePlug("ON")
    control = _real_control(plug, FakeBattery([None]))
    control.initialize(_at(8))

    control.manual_power(PlugState.OFF, _at(9))
    control.run_cycle(_at(10, 59))
    assert control.state == PlugState.OFF
    control.run_cycle(_at(11, 0))

    assert control.state == PlugState.ON
    assert _results(session)[-1] == ("SAFETY", "ON", "ON", "SUCCESS")
    assert control.mode == "MANUAL"


def test_manual_off_is_rejected_when_battery_low(session: Session) -> None:
    control = _real_control(FakePlug("ON"), FakeBattery([(4, 150.0)]))

    with pytest.raises(ValueError, match="下限"):
        control.manual_power(PlugState.OFF, _at(9))


def test_schedule_is_not_started_when_battery_projection_is_low(session: Session) -> None:
    # 残量6%・消費電力 300W：5分で約2.75%減り、下限5%を下回る見込み
    plug = FakePlug("ON")
    control = _real_control(plug, FakeBattery([(6, 300.0)]))
    _prepare(session, {range(20, 24): A})

    control.run_cycle(_at(10))

    assert plug.commands == []
    s = _schedules(session)[0]
    assert s.status == ScheduleStatus.ABORTED and "下限" in s.status_reason


def test_running_schedule_is_stopped_before_battery_runs_out(session: Session) -> None:
    plug = FakePlug("ON")
    battery = FakeBattery([(50, 150.0), (6, 150.0)])
    control = _real_control(plug, battery)
    _prepare(session, {range(20, 24): A})

    control.run_cycle(_at(10))  # 開始時に取得（50%）→ OFF
    control.run_cycle(_at(10, 2))  # 5分以内は取得しない
    assert battery.reads == 1
    control.run_cycle(_at(10, 5))  # 5分ごとに取得（6%）→ 150W で5分後に約4.6%となり下限を下回る見込み

    assert plug.commands == ["OFF", "ON"]
    assert _results(session)[-1][0] == "SAFETY"
    s = _schedules(session)[0]
    assert s.status == ScheduleStatus.ABORTED and "下限" in s.status_reason


def test_warning_runs_outside_operation_hours_when_battery_known(session: Session) -> None:
    plug = FakePlug("ON")
    control = _real_control(plug, FakeBattery([(90, 150.0)]))
    _prepare(session, {range(38, 40): W})  # 19:00〜20:00（運用時間外）

    control.run_cycle(_at(19))

    assert plug.commands == ["OFF"]
    assert _schedules(session)[0].status == ScheduleStatus.RUNNING


def test_measured_values_are_recorded_per_schedule(session: Session) -> None:
    plug = FakePlug("ON")
    battery = FakeBattery([(90, 150.0), (88, 170.0), (86, 160.0), (85, 150.0)])
    control = _real_control(plug, battery)
    _prepare(session, {range(20, 22): A})  # 10:00〜11:00

    control.run_cycle(_at(10))  # 開始時に取得（90%・150W）
    control.run_cycle(_at(10, 5))  # 88%・170W
    control.run_cycle(_at(10, 10))  # 86%・160W
    control.run_cycle(_at(11))  # 終了時に取得（85%）

    s = _schedules(session)[0]
    assert s.status == ScheduleStatus.DONE
    assert (s.battery_start_percent, s.battery_end_percent) == (90, 85)
    assert s.measured_power_w == pytest.approx((150 + 170 + 160 + 150) / 4, abs=0.1)


# --- 充電モード（docs/07 §7.10）：スケジュールの開始時に静かな充電、終了時に高速充電 ---


class ChargeBattery(FakeBattery):
    """充電モードの変更を記録する模擬。fail に例外を入れると変更に失敗する。"""

    def __init__(self) -> None:
        super().__init__([(100, 100.0)])
        self.modes: list[ChargeMode] = []
        self.fail: JackeryError | None = None

    def set_charge_mode(self, mode: ChargeMode, now: datetime) -> None:
        self.modes.append(mode)
        if self.fail is not None:
            raise self.fail
        self.charge_mode, self.charge_mode_at = mode, now


def _charge_events(session: Session) -> list[str]:
    session.expire_all()
    return [
        e.message
        for e in session.scalars(
            select(SystemEvent).where(SystemEvent.category == EventCategory.CHARGE_MODE).order_by(SystemEvent.id)
        )
    ]


def test_charge_mode_follows_schedule_in_dry_run(session: Session) -> None:
    battery = ChargeBattery()
    control = ControlService(get_settings(), SessionLocal, battery=battery)
    _prepare(session, {range(34, 36): A})

    control.run_cycle(_at(16, 59))
    control.run_cycle(_at(17))
    assert control.charge_mode == ChargeMode.SILENT  # Dry Run の想定
    control.run_cycle(_at(18))

    assert battery.modes == []  # Dry Run では送信しない
    assert control.charge_mode == ChargeMode.FAST
    assert _charge_events(session) == [
        "（Dry Run）充電モードを静かな充電にしました（AUTO：スケジュール開始）",
        "（Dry Run）充電モードを高速充電にしました（AUTO：スケジュール終了）",
    ]
    assert unresolved_events(session) == []  # 変更の記録はお知らせに出さない


def test_charge_mode_is_sent_at_schedule_start_and_end(session: Session) -> None:
    battery = ChargeBattery()
    control = _real_control(FakePlug("ON"), battery)
    _prepare(session, {range(32, 34): A, range(34, 36): W})  # 連続するスケジュールの間では切り替えない

    for t in [_at(15, 59), _at(16), _at(16, 30), _at(17), _at(18)]:
        control.run_cycle(t)

    assert battery.modes == [ChargeMode.SILENT, ChargeMode.FAST]
    assert control.last_charge_change is not None
    assert control.last_charge_change.result == ControlResult.SUCCESS


def test_custom_on_schedule_also_switches_charge_mode(session: Session) -> None:
    battery = ChargeBattery()
    control = _real_control(FakePlug("ON"), battery)
    _custom_rule(session, "ON", 10, 11)

    for t in [_at(9, 59), _at(10), _at(11)]:
        control.run_cycle(t)

    assert battery.modes == [ChargeMode.SILENT, ChargeMode.FAST]


def test_aborted_schedule_returns_charge_mode_to_fast(session: Session) -> None:
    battery = ChargeBattery()
    control = _real_control(FakePlug("ON"), battery)
    _prepare(session, {range(20, 24): A})

    control.run_cycle(_at(10))
    control.stop_auto(_at(10, 30))

    assert battery.modes == [ChargeMode.SILENT, ChargeMode.FAST]


def test_charge_mode_failure_does_not_stop_plug_control_and_is_retried(session: Session) -> None:
    battery = ChargeBattery()
    battery.fail = JackeryCommandUnconfirmed("充電モードが静かな充電になりませんでした（現在：高速充電）")
    plug = FakePlug("ON")
    control = _real_control(plug, battery)
    _prepare(session, {range(20, 24): A})

    control.run_cycle(_at(10))

    assert plug.commands == ["OFF"]  # プラグの制御は続ける
    assert control.last_charge_change is not None and control.last_charge_change.result == ControlResult.UNCONFIRMED
    assert [e.category for e in unresolved_events(session)] == [EventCategory.CHARGE_MODE]

    control.run_cycle(_at(10, 2))  # 5分間は再試行しない
    assert battery.modes == [ChargeMode.SILENT]
    control.run_cycle(_at(10, 5, 10))  # 再試行も失敗（エラーは記録し直さない）
    assert battery.modes == [ChargeMode.SILENT] * 2
    assert len(unresolved_events(session)) == 1

    battery.fail = None
    control.run_cycle(_at(10, 11))
    assert battery.modes == [ChargeMode.SILENT] * 3
    session.expire_all()
    assert unresolved_events(session) == []


def test_charge_mode_is_restored_after_restart(session: Session) -> None:
    battery = ChargeBattery()
    control = _real_control(FakePlug("ON"), battery)
    _prepare(session, {range(20, 22): A})
    control.run_cycle(_at(10))
    assert battery.modes == [ChargeMode.SILENT]

    # スケジュールの実行中に停止し、終了後に再起動した
    restarted = _real_control(FakePlug("ON"), battery)
    restarted.run_cycle(_at(12))

    assert battery.modes == [ChargeMode.SILENT, ChargeMode.FAST]


def _set_charge_modes(session: Session, start: ChargeModeOption, end: ChargeModeOption) -> None:
    for s in _schedules(session):
        s.charge_mode_start, s.charge_mode_end = start, end
    session.commit()


def test_charge_mode_follows_each_schedule_setting(session: Session) -> None:
    battery = ChargeBattery()
    control = _real_control(FakePlug("ON"), battery)
    _prepare(session, {range(20, 22): A})
    _set_charge_modes(session, ChargeModeOption.FAST, ChargeModeOption.SILENT)

    for t in [_at(10), _at(11)]:
        control.run_cycle(t)

    assert battery.modes == [ChargeMode.FAST, ChargeMode.SILENT]


def test_keep_charge_mode_sends_nothing(session: Session) -> None:
    battery = ChargeBattery()
    control = _real_control(FakePlug("ON"), battery)
    _prepare(session, {range(20, 22): A})
    _set_charge_modes(session, ChargeModeOption.SILENT, ChargeModeOption.KEEP)

    for t in [_at(10), _at(11)]:
        control.run_cycle(t)

    assert battery.modes == [ChargeMode.SILENT]


def test_adjacent_schedules_send_only_the_next_start_mode(session: Session) -> None:
    battery = ChargeBattery()
    control = _real_control(FakePlug("ON"), battery)
    _prepare(session, {range(32, 34): A, range(34, 36): W})
    first, second = _schedules(session)
    first.charge_mode_start, first.charge_mode_end = ChargeModeOption.SILENT, ChargeModeOption.FAST
    second.charge_mode_start, second.charge_mode_end = ChargeModeOption.KEEP, ChargeModeOption.FAST
    session.commit()

    for t in [_at(16), _at(17)]:
        control.run_cycle(t)
    # 次のスケジュールが「変更しない」なら、終了するスケジュールの設定（高速充電）を使う
    assert battery.modes == [ChargeMode.SILENT, ChargeMode.FAST]


def test_running_schedule_end_mode_can_be_changed(session: Session) -> None:
    battery = ChargeBattery()
    control = _real_control(FakePlug("ON"), battery)
    _prepare(session, {range(20, 22): A})
    control.run_cycle(_at(10))

    s = _schedules(session)[0]
    optimizer.update_charge_modes(s, end=ChargeModeOption.KEEP, now=_at(10, 10), session=session)
    with pytest.raises(optimizer.ScheduleChangeError, match="開始済み"):
        optimizer.update_charge_modes(s, start=ChargeModeOption.FAST, now=_at(10, 10), session=session)
    control.run_cycle(_at(11))

    assert battery.modes == [ChargeMode.SILENT]


def test_legacy_running_flag_restores_default_end_mode(session: Session) -> None:
    set_value(session, LEGACY_CHARGE_MODE_SCHEDULE_KEY, "true")  # 旧形式で実行中として記録していた
    battery = ChargeBattery()
    control = _real_control(FakePlug("ON"), battery)

    control.run_cycle(_at(12))
    control.run_cycle(_at(12, 1))

    assert battery.modes == [ChargeMode.FAST]


def test_manual_charge_mode(session: Session) -> None:
    battery = ChargeBattery()
    control = _real_control(FakePlug("ON"), battery)

    change = control.manual_charge_mode(ChargeMode.SILENT, _at(9))

    assert battery.modes == [ChargeMode.SILENT]
    assert (change.source, change.result) == (ControlSource.MANUAL, ControlResult.SUCCESS)
    assert control.control_mode == "AUTO"  # プラグの制御モードは変えない
    assert _charge_events(session) == ["充電モードを静かな充電にしました（MANUAL：手動操作（静かな充電））"]


def test_manual_charge_mode_requires_jackery(session: Session) -> None:
    control = ControlService(get_settings(), SessionLocal)

    with pytest.raises(ValueError, match="Jackery"):
        control.manual_charge_mode(ChargeMode.SILENT)
