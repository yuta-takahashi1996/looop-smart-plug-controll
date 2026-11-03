import base64
import json

import httpx
import pytest
from cryptography.hazmat.primitives import padding as sym_padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from app.clients import jackery
from app.clients.jackery import (
    JackeryAuthError,
    JackeryClient,
    JackeryError,
    JackeryNotConfiguredError,
    encrypt_login,
    parse_status,
)

AES_KEY = b"0123456789abcdef"
PROPERTIES = {"rb": 87, "ip": 0, "op": 152, "ot": 54, "it": 0, "bt": 263, "oac": 1, "acov": 1000}


def _decrypt(encrypted_b64: str) -> dict:
    decryptor = Cipher(algorithms.AES(AES_KEY), modes.ECB()).decryptor()
    padded = decryptor.update(base64.b64decode(encrypted_b64)) + decryptor.finalize()
    unpadder = sym_padding.PKCS7(128).unpadder()
    return json.loads(unpadder.update(padded) + unpadder.finalize())


class FakeJackery:
    """Jackery クラウドの最小限の模擬。"""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.login_response: dict = {"code": 0, "msg": "SUCCESS", "token": "TOKEN-1"}
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
            return httpx.Response(200, json={"code": 0, "data": {"properties": PROPERTIES}})
        return httpx.Response(404)


def _client(fake: FakeJackery, account: str = "fetch@example.com", password: str = "pw") -> JackeryClient:
    http = httpx.Client(base_url=jackery.BASE_URL, transport=httpx.MockTransport(fake))
    return JackeryClient(account, password, client=http, aes_key=AES_KEY)


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
