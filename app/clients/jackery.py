"""Jackery クラウドからのポータブル電源の状態取得（読み取り専用）。

Jackery は公式 API を公開していないため、Jackery アプリが利用しているクラウド API
（https://iot.jackeryapp.com）を利用する。通信方式は以下を参考に独自に実装した。
- https://github.com/theak/jackery-homeassistant
- https://qiita.com/Hsky16/items/c163137265a87186ac39

注意：
- 同じアカウントで同時にログインできるのは 1 台のみ。このシステムでログインすると、同じ
  アカウントのスマホアプリはログアウトされるため、取得専用のアカウントを作り、デバイスを
  共有して使う。
- 非公式 API のため予告なく使えなくなる可能性がある。取得できない場合は残量不明として扱う。
- 状態の取得のみを行い、ポータブル電源の操作はしない。
"""

import base64
import hashlib
import json
import uuid
from dataclasses import dataclass
from typing import Any

import httpx
from cryptography.hazmat.primitives import padding as sym_padding
from cryptography.hazmat.primitives.asymmetric import padding as asym_padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.serialization import load_der_public_key

BASE_URL = "https://iot.jackeryapp.com"
SUCCESS = 0
# ログインし直せば回復するエラー：トークンの期限切れ、別の場所（同じアカウント）でのログイン
RELOGIN_CODES = (10402, 10403)

# Jackery アプリに埋め込まれているログイン用の RSA 公開鍵
_LOGIN_PUBLIC_KEY = (
    "MIGfMA0GCSqGSIb3DQEBAQUAA4GNADCBiQKBgQCVmzgJy/4XolxPnkfu32YtJqYGFLYqf9/rnVgURJED+8J9J3Pccd6+9L97/+7COZE5OkejsgO"
    "kqeLNC9C3r5mhpE4zk/HStss7Q8/5DqkGD1annQ+eoICo3oi0dITZ0Qll56Dowb8lXi6WHViVDdih/oeUwVJY89uJNtTWrz7t7QIDAQAB"
)

# ログイン情報の暗号化に使う AES 鍵。Jackery アプリに埋め込まれている固定値で、秘密ではない。
# RSA で暗号化した鍵も送るが、サーバーはこの固定鍵で復号しており、別の鍵ではログインできない
# （"Mandatory parameter is null" になる。2026-11 に確認）。通信自体は HTTPS で保護される。
APP_AES_KEY = b"1234567890123456"

# Jackery アプリと同じ形式のヘッダー（異なるとログインできない場合がある）
_APP_HEADERS = {
    "app_version": "1.0.5",
    "sys_version": "17.2",
    "platform": "1",
    "accept": "*/*",
    "accept-language": "en-US",
    "user-agent": "DxPowerProject/1.0.5 (com.hb.jackery; build:2; iOS 17.2.0) Alamofire/5.8.0",
    "model": "iPad Pro (12.9-inch) (3rd generation)",
}


class JackeryError(Exception):
    """通信・API エラー。"""


class JackeryAuthError(JackeryError):
    """ログインできない（アカウント・パスワードの誤り、ロックなど）。"""


class JackeryNotConfiguredError(JackeryError):
    """アカウントまたはパスワードが未設定。"""


@dataclass(frozen=True)
class BatteryStatus:
    """ポータブル電源の状態。取得できなかった項目は None。"""

    remaining_percent: int | None  # 残量（%）
    input_power_w: float | None
    output_power_w: float | None
    remaining_output_hours: float | None  # アプリ表示の残り時間（参考値。制御には使わない）
    time_to_full_hours: float | None
    battery_temperature_c: float | None
    ac_output_on: bool | None
    raw: dict[str, Any]


def _scaled(value: Any, divisor: float = 1.0) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return value / divisor


def _flag(value: Any) -> bool | None:
    return bool(value) if isinstance(value, int) and not isinstance(value, bool) and value in (0, 1) else None


def parse_status(properties: dict[str, Any]) -> BatteryStatus:
    remaining = properties.get("rb")
    return BatteryStatus(
        remaining_percent=remaining if isinstance(remaining, int) and not isinstance(remaining, bool) else None,
        input_power_w=_scaled(properties.get("ip")),
        output_power_w=_scaled(properties.get("op")),
        # 1/10 時間単位。1000 New で ot=539 のときアプリの表示が 53.9h であることを確認（2026-11）。
        # 機種によって単位が異なるとの報告があり、AC 入力中などは実際の給電可能時間と一致しないため、
        # 表示用の参考値とし、制御の判断には使わない
        remaining_output_hours=_scaled(properties.get("ot"), 10),
        time_to_full_hours=_scaled(properties.get("it"), 10),
        battery_temperature_c=_scaled(properties.get("bt"), 10),
        ac_output_on=_flag(properties.get("oac")),
        raw=properties,
    )


def encrypt_login(payload: dict[str, Any], aes_key: bytes) -> tuple[str, str]:
    """ログイン情報を AES（ECB/PKCS7）で暗号化し、AES 鍵を RSA 公開鍵で暗号化する。"""
    padder = sym_padding.PKCS7(128).padder()
    plain = padder.update(json.dumps(payload, ensure_ascii=False).encode()) + padder.finalize()
    encryptor = Cipher(algorithms.AES(aes_key), modes.ECB()).encryptor()  # noqa: S305 アプリの仕様
    encrypted = encryptor.update(plain) + encryptor.finalize()
    public_key = load_der_public_key(base64.b64decode(_LOGIN_PUBLIC_KEY))
    wrapped_key = public_key.encrypt(aes_key, asym_padding.PKCS1v15())  # type: ignore[union-attr]
    return base64.b64encode(encrypted).decode(), base64.b64encode(wrapped_key).decode()


def _device_udid(account: str) -> str:
    """アカウントごとに固定の端末ID（アプリと同じ形式）。ログインのたびに別端末と扱われないようにする。"""
    digest = bytearray(hashlib.md5(f"looop-smart-plug-controller:{account}".encode()).digest())  # noqa: S324
    return "2" + uuid.UUID(bytes=bytes(digest), version=3).hex


class JackeryClient:
    def __init__(
        self,
        account: str,
        password: str,
        *,
        client: httpx.Client | None = None,
        timeout: float = 10.0,
        aes_key: bytes = APP_AES_KEY,
    ) -> None:
        self._account = account
        self._password = password
        self._client = client or httpx.Client(base_url=BASE_URL, timeout=timeout)
        self._aes_key = aes_key
        self._token: str | None = None

    @property
    def configured(self) -> bool:
        return bool(self._account and self._password)

    def login(self) -> None:
        if not self.configured:
            raise JackeryNotConfiguredError("JACKERY_ACCOUNT / JACKERY_PASSWORD が未設定です")
        payload = {
            "account": self._account,
            "loginType": 2,
            "macId": _device_udid(self._account),
            "password": self._password,
            "phone": "",
            "registerAppId": "com.hbxn.jackery",
            "verificationCode": "",
        }
        encrypted, wrapped_key = encrypt_login(payload, self._aes_key)
        try:
            response = self._client.post(
                "/v1/auth/login",
                params={"aesEncryptData": encrypted, "rsaForAesKey": wrapped_key},
                headers=_APP_HEADERS,
                files={"file": ("", b"", "")},
            )
            response.raise_for_status()
            body = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise JackeryError(f"Jackery にログインできませんでした: {exc.__class__.__name__}") from exc
        if body.get("code") != SUCCESS or not body.get("token"):
            raise JackeryAuthError(f"Jackery のログインに失敗しました：{body.get('msg', '不明なエラー')}")
        self._token = body["token"]

    def _get(self, path: str, params: dict[str, str] | None = None) -> Any:
        if self._token is None:
            self.login()
        body = self._get_once(path, params)
        if body.get("code") in RELOGIN_CODES:
            # 同時ログインは1台までのため、取得専用アカウントで別の場所からログインされると無効になる
            self.login()
            body = self._get_once(path, params)
        if body.get("code") != SUCCESS:
            raise JackeryError(f"Jackery API エラー（code {body.get('code')}）：{body.get('msg', '不明なエラー')}")
        return body.get("data")

    def _get_once(self, path: str, params: dict[str, str] | None) -> dict[str, Any]:
        try:
            response = self._client.get(path, params=params, headers={**_APP_HEADERS, "token": self._token or ""})
            response.raise_for_status()
            body = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise JackeryError(f"Jackery API に接続できませんでした: {exc.__class__.__name__}") from exc
        if not isinstance(body, dict):
            raise JackeryError("Jackery API の応答を解釈できませんでした")
        return body

    def list_devices(self) -> list[dict[str, Any]]:
        data = self._get("/v1/device/bind/list")
        return list(data) if isinstance(data, list) else []

    def get_status(self, device_id: str) -> BatteryStatus:
        data = self._get("/v1/device/property", {"deviceId": device_id})
        properties = data.get("properties") if isinstance(data, dict) else None
        if not isinstance(properties, dict):
            raise JackeryError("ポータブル電源の状態を取得できませんでした")
        return parse_status(properties)
