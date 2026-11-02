import base64
import hashlib
import hmac
import json

import httpx
import pytest
from sqlalchemy.orm import Session

from app.clients import switchbot
from app.clients.switchbot import (
    SwitchBotAuthError,
    SwitchBotClient,
    SwitchBotError,
    SwitchBotNotConfiguredError,
    build_auth_headers,
)
from app.models.settings import EventCategory, unresolved_events
from app.repositories.database import SessionLocal
from app.services.controller import PlugMonitor

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
