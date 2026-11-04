import base64
import json

import httpx
import pytest
from cryptography.hazmat.primitives import padding as sym_padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from app.clients import jackery
from app.clients.jackery import (
    ChargeMode,
    JackeryAuthError,
    JackeryClient,
    JackeryCommandUnconfirmed,
    JackeryError,
    JackeryNotConfiguredError,
    MqttCredentials,
    derive_mqtt_password,
    encrypt_login,
    parse_device_message,
    parse_status,
)

AES_KEY = b"0123456789abcdef"
MQTT_KEY = bytes(range(32))
MQTT_KEY_B64 = base64.b64encode(MQTT_KEY).decode()
PROPERTIES = {"rb": 87, "ip": 0, "op": 152, "ot": 54, "it": 0, "bt": 263, "oac": 1, "acov": 1000, "cs": 0, "sfc": 0}


def _decrypt(encrypted_b64: str) -> dict:
    decryptor = Cipher(algorithms.AES(AES_KEY), modes.ECB()).decryptor()
    padded = decryptor.update(base64.b64decode(encrypted_b64)) + decryptor.finalize()
    unpadder = sym_padding.PKCS7(128).unpadder()
    return json.loads(unpadder.update(padded) + unpadder.finalize())


class FakeJackery:
    """Jackery クラウドの最小限の模擬。"""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.login_response: dict = {
            "code": 0,
            "msg": "SUCCESS",
            "token": "TOKEN-1",
            "data": {"userId": 4242, "mqttPassWord": MQTT_KEY_B64},
        }
        self.properties = dict(PROPERTIES)
        self.expire_next = False
        self.expire_code = 10402
        self.logins = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.path == "/v1/auth/login":
            self.logins += 1
            body = dict(self.login_response)
            if body.get("token"):
                body["token"] = f"TOKEN-{self.logins}"
            return httpx.Response(200, json=body)
        if self.expire_next:
            self.expire_next = False
            return httpx.Response(200, json={"code": self.expire_code, "msg": "token expired"})
        if request.url.path == "/v1/device/bind/list":
            return httpx.Response(200, json={"code": 0, "data": [{"devId": "123", "devName": "Jackery 1000 New"}]})
        if request.url.path == "/v1/device/property":
            return httpx.Response(
                200, json={"code": 0, "data": {"device": {"deviceSn": "SN0001"}, "properties": dict(self.properties)}}
            )
        return httpx.Response(404)


class FakeMqtt:
    """MQTT の模擬。コマンドを受けると（apply が True なら）機器の cs を変更して通知する。"""

    def __init__(self, fake: FakeJackery) -> None:
        self.fake = fake
        self.sent: list[tuple[MqttCredentials, dict]] = []
        self.apply = True
        self.auth_errors = 0  # 指定回数だけ認証エラーにする

    def publish_and_wait(self, credentials, payload, matches, timeout) -> bool:  # noqa: ANN001
        if self.auth_errors:
            self.auth_errors -= 1
            raise JackeryAuthError("not authorized")
        message = json.loads(payload)
        self.sent.append((credentials, message))
        if not self.apply:
            return False
        # 機器は文字列の値も数値として反映し、数値で通知する
        applied = {k: int(v) for k, v in message["body"].items()}
        self.fake.properties.update(applied)
        return matches(message["deviceSn"], applied)


def _client(
    fake: FakeJackery, account: str = "fetch@example.com", password: str = "pw", mqtt: FakeMqtt | None = None
) -> JackeryClient:
    http = httpx.Client(base_url=jackery.BASE_URL, transport=httpx.MockTransport(fake))
    return JackeryClient(account, password, client=http, aes_key=AES_KEY, mqtt=mqtt, sleep=lambda _: None)


def test_default_key_is_app_key() -> None:
    # サーバーはアプリの固定鍵で復号するため、既定値はこの鍵でなければならない
    assert jackery.APP_AES_KEY == b"1234567890123456"


def test_encrypt_login_roundtrip() -> None:
    encrypted, wrapped = encrypt_login({"account": "a", "password": "日本語"}, AES_KEY)

    assert _decrypt(encrypted) == {"account": "a", "password": "日本語"}
    assert len(base64.b64decode(wrapped)) == 128  # RSA 1024bit で暗号化した AES 鍵


def test_login_sends_encrypted_credentials_and_uses_token() -> None:
    fake = FakeJackery()
    client = _client(fake)

    status = client.get_status("123")

    login, request = fake.requests
    payload = _decrypt(login.url.params["aesEncryptData"])
    assert payload["account"] == "fetch@example.com"
    assert payload["password"] == "pw"
    assert payload["macId"].startswith("2") and len(payload["macId"]) == 33
    assert "pw" not in str(login.url)  # 平文のパスワードを送らない
    assert request.headers["token"] == "TOKEN-1"
    assert request.url.params["deviceId"] == "123"
    assert status.remaining_percent == 87


def test_device_id_is_stable_per_account() -> None:
    fake = FakeJackery()
    _client(fake).login()
    _client(fake).login()
    first, second = (_decrypt(r.url.params["aesEncryptData"])["macId"] for r in fake.requests)

    assert first == second


@pytest.mark.parametrize("code", [10402, 10403])  # トークンの期限切れ／別の場所でのログイン
def test_relogin_when_token_expired(code: int) -> None:
    fake = FakeJackery()
    client = _client(fake)
    client.list_devices()
    fake.expire_next = True
    fake.expire_code = code

    devices = client.list_devices()

    assert devices[0]["devId"] == "123"
    assert fake.logins == 2
    assert fake.requests[-1].headers["token"] == "TOKEN-2"


def test_parse_status_scales_values() -> None:
    status = parse_status(PROPERTIES)

    assert status.remaining_percent == 87
    assert status.output_power_w == 152
    assert status.remaining_output_hours == pytest.approx(5.4)
    assert status.battery_temperature_c == pytest.approx(26.3)
    assert status.ac_output_on is True


@pytest.mark.parametrize("properties", [{}, {"rb": "87"}, {"rb": True}, {"rb": None}])
def test_parse_status_treats_unknown_values_as_missing(properties: dict) -> None:
    assert parse_status(properties).remaining_percent is None


def test_login_failure() -> None:
    fake = FakeJackery()
    fake.login_response = {"code": 10001, "msg": "account or password error"}

    with pytest.raises(JackeryAuthError, match="account or password error"):
        _client(fake).list_devices()


@pytest.mark.parametrize(
    "response",
    [httpx.Response(500), httpx.Response(200, text="not json"), httpx.Response(200, json={"code": 5000, "msg": "x"})],
)
def test_api_errors(response: httpx.Response) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/auth/login":
            return httpx.Response(200, json={"code": 0, "token": "T"})
        return response

    http = httpx.Client(base_url=jackery.BASE_URL, transport=httpx.MockTransport(handler))
    with pytest.raises(JackeryError):
        JackeryClient("a", "b", client=http).get_status("123")


def test_missing_properties() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/auth/login":
            return httpx.Response(200, json={"code": 0, "token": "T"})
        return httpx.Response(200, json={"code": 0, "data": {}})

    http = httpx.Client(base_url=jackery.BASE_URL, transport=httpx.MockTransport(handler))
    with pytest.raises(JackeryError, match="状態を取得できませんでした"):
        JackeryClient("a", "b", client=http).get_status("123")


@pytest.mark.parametrize(("account", "password"), [("", "pw"), ("a@example.com", "")])
def test_not_configured(account: str, password: str) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("未設定のときは通信しない")

    http = httpx.Client(base_url=jackery.BASE_URL, transport=httpx.MockTransport(handler))
    client = JackeryClient(account, password, client=http)

    assert not client.configured
    with pytest.raises(JackeryNotConfiguredError):
        client.list_devices()


# --- 充電モード（docs/07 §7.10） ---


@pytest.mark.parametrize(("cs", "expected"), [(0, ChargeMode.FAST), (1, ChargeMode.SILENT), (2, None), (None, None), (True, None)])
def test_parse_charge_mode(cs: object, expected: ChargeMode | None) -> None:
    assert parse_status({"cs": cs}).charge_mode == expected


def test_status_includes_device_serial() -> None:
    assert _client(FakeJackery()).get_status("123").device_sn == "SN0001"


def test_mqtt_password_is_username_encrypted_with_login_key() -> None:
    password = derive_mqtt_password("4242@MAC", MQTT_KEY_B64)

    decryptor = Cipher(algorithms.AES(MQTT_KEY), modes.CBC(MQTT_KEY[:16])).decryptor()
    padded = decryptor.update(base64.b64decode(password)) + decryptor.finalize()
    unpadder = sym_padding.PKCS7(128).unpadder()
    assert unpadder.update(padded) + unpadder.finalize() == b"4242@MAC"


def test_set_charge_mode_sends_command_and_verifies() -> None:
    fake = FakeJackery()
    mqtt = FakeMqtt(fake)
    client = _client(fake, mqtt=mqtt)

    status = client.set_charge_mode("123", ChargeMode.SILENT)

    assert status.charge_mode == ChargeMode.SILENT
    credentials, message = mqtt.sent[0]
    assert credentials.client_id == "4242@APP"
    assert credentials.username.startswith("4242@2")  # ログイン時と同じ端末ID
    assert credentials.command_topic == "hb/app/4242/command"
    assert credentials.device_topic == "hb/app/4242/device"
    assert message["deviceSn"] == "SN0001"
    assert message["messageType"] == "DevicePropertyChange"
    assert message["actionId"] == 10
    assert message["body"] == {"cs": 1}
    assert "sfc" not in message["body"]  # 超急速充電は扱わない


def test_set_charge_mode_does_not_send_when_already_set() -> None:
    fake = FakeJackery()
    mqtt = FakeMqtt(fake)

    status = _client(fake, mqtt=mqtt).set_charge_mode("123", ChargeMode.FAST)

    assert status.charge_mode == ChargeMode.FAST
    assert mqtt.sent == []


def test_set_charge_mode_is_unconfirmed_when_value_does_not_change() -> None:
    fake = FakeJackery()
    mqtt = FakeMqtt(fake)
    mqtt.apply = False

    with pytest.raises(JackeryCommandUnconfirmed, match="静かな充電になりませんでした（現在：高速充電）"):
        _client(fake, mqtt=mqtt).set_charge_mode("123", ChargeMode.SILENT)
    assert len(mqtt.sent) == 1


def test_set_charge_mode_relogins_once_on_mqtt_auth_error() -> None:
    fake = FakeJackery()
    mqtt = FakeMqtt(fake)
    mqtt.auth_errors = 1
    client = _client(fake, mqtt=mqtt)

    client.set_charge_mode("123", ChargeMode.SILENT)

    assert fake.logins == 2
    assert len(mqtt.sent) == 1


def test_set_charge_mode_requires_mqtt_credentials() -> None:
    fake = FakeJackery()
    fake.login_response = {"code": 0, "token": "T"}

    with pytest.raises(JackeryError, match="MQTT の認証情報"):
        _client(fake, mqtt=FakeMqtt(fake)).set_charge_mode("123", ChargeMode.SILENT)


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        (b'{"messageType":"DevicePropertyChange","deviceSn":"SN","body":{"cs":1,"messageId":9}}', ("SN", {"cs": 1})),
        (b'{"messageType":"DeviceOnlineChange","deviceSn":"SN","body":{"online":1}}', None),
        (b"not json", None),
        (b'{"messageType":"DevicePropertyChange","deviceSn":"SN","body":[]}', None),
    ],
)
def test_parse_device_message(payload: bytes, expected: object) -> None:
    assert parse_device_message(payload) == expected


def test_mqtt_ca_file_is_bundled() -> None:
    assert jackery.MQTT_CA_FILE.read_text().startswith("-----BEGIN CERTIFICATE-----")


def test_describe_message_hides_serial() -> None:
    text = jackery.describe_message(b'{"deviceSn":"SN0001","id":1,"timestamp":1,"messageType":"DevicePropertyChange","body":{"cs":0}}')

    assert "SN0001" not in text
    assert '"body":{"cs":0}' in text


def test_mqtt_subscribes_to_all_notifications() -> None:
    credentials = MqttCredentials("1@APP", "1@x", "pw", "hb/app/1/command", "hb/app/1/device")

    assert credentials.subscribe_topics == ["hb/app/1/device", "hb/app/1/notice", "hb/app/1/alert", "hb/app/1/config"]



def test_mqtt_uses_version_5_by_default() -> None:
    # 3.1.1 ではコマンドが機器に届かない（docs/08 §8.4）
    assert jackery.PahoMqttTransport()._mqtt5 is True


def test_fast_mode_is_sent_as_string_zero() -> None:
    # 実機で反映を確認した形で送る（docs/08 §8.4）
    fake = FakeJackery()
    fake.properties["cs"] = 1
    mqtt = FakeMqtt(fake)

    status = _client(fake, mqtt=mqtt).set_charge_mode("123", ChargeMode.FAST)

    assert status.charge_mode == ChargeMode.FAST
    assert mqtt.sent[0][1]["body"] == {"cs": "0"}
