"""SwitchBot OpenAPI v1.1 クライアント（docs/08-switchbot-api.md）。

状態取得と明示的な ON/OFF（turnOn / turnOff）のみを扱う。toggle は期待状態との照合が
できないため使用しない。認証情報は環境変数から受け取り、ログや画面には出さない。
"""

import base64
import hashlib
import hmac
import time
import uuid
from dataclasses import dataclass
from typing import Any, Literal

import httpx

BASE_URL = "https://api.switch-bot.com/v1.1"
SUCCESS = 100

PowerState = Literal["ON", "OFF"]

_COMMANDS: dict[PowerState, str] = {"ON": "turnOn", "OFF": "turnOff"}

# statusCode の意味（SwitchBot API ドキュメントより）
_STATUS_MESSAGES = {
    151: "デバイス種別が操作に対応していません",
    152: "デバイスが見つかりません",
    160: "コマンドに対応していません",
    161: "デバイスがオフラインです",
    171: "ハブがオフラインです",
    190: "デバイス内部エラー、またはリクエストが不正です",
}


class SwitchBotError(Exception):
    """通信・API エラー。再試行の対象。"""


class SwitchBotAuthError(SwitchBotError):
    """認証エラー。設定を直すまで再試行しても成功しない。"""


class SwitchBotNotConfiguredError(SwitchBotError):
    """認証情報またはデバイスIDが未設定。"""


@dataclass(frozen=True)
class PlugStatus:
    power: PowerState
    device_type: str | None
    raw: dict[str, Any]


def build_auth_headers(token: str, secret: str, *, t: str | None = None, nonce: str | None = None) -> dict[str, str]:
    """HMAC-SHA256 による署名ヘッダーを作る。"""
    t = t or str(int(time.time() * 1000))
    nonce = nonce or uuid.uuid4().hex
    digest = hmac.new(secret.encode(), f"{token}{t}{nonce}".encode(), hashlib.sha256).digest()
    return {
        "Authorization": token,
        "sign": base64.b64encode(digest).decode(),
        "t": t,
        "nonce": nonce,
        "Content-Type": "application/json; charset=utf8",
    }


class SwitchBotClient:
    def __init__(
        self,
        token: str,
        secret: str,
        device_id: str,
        *,
        client: httpx.Client | None = None,
        timeout: float = 10.0,
    ) -> None:
        self._token = token
        self._secret = secret
        self.device_id = device_id
        self._client = client or httpx.Client(base_url=BASE_URL, timeout=timeout)

    @property
    def configured(self) -> bool:
        return bool(self.credentials_configured and self.device_id)

    @property
    def credentials_configured(self) -> bool:
        """認証情報が設定されているか（機器一覧の取得には機器 ID は不要）。"""
        return bool(self._token and self._secret)

    def _request(self, method: str, path: str, json: dict[str, Any] | None = None) -> Any:
        if not (self._token and self._secret):
            raise SwitchBotNotConfiguredError("SWITCHBOT_TOKEN / SWITCHBOT_SECRET が未設定です")
        try:
            response = self._client.request(
                method, path, json=json, headers=build_auth_headers(self._token, self._secret)
            )
        except httpx.HTTPError as exc:
            raise SwitchBotError(f"SwitchBot API に接続できませんでした: {exc.__class__.__name__}") from exc

        if response.status_code in (401, 403):
            raise SwitchBotAuthError("SwitchBot API の認証に失敗しました（トークン・シークレットを確認してください）")
        if response.status_code == 429:
            raise SwitchBotError("SwitchBot API の呼び出し回数の上限に達しました")
        if response.status_code >= 400:
            raise SwitchBotError(f"SwitchBot API がエラーを返しました（HTTP {response.status_code}）")
        try:
            body = response.json()
        except ValueError as exc:
            raise SwitchBotError("SwitchBot API の応答を解釈できませんでした") from exc

        status_code = body.get("statusCode")
        if status_code != SUCCESS:
            detail = _STATUS_MESSAGES.get(status_code, body.get("message", "不明なエラー"))
            raise SwitchBotError(f"SwitchBot API エラー（statusCode {status_code}）：{detail}")
        return body.get("body")

    def _require_device(self, device_id: str | None) -> str:
        device_id = device_id or self.device_id
        if not device_id:
            raise SwitchBotNotConfiguredError("制御対象の機器が未設定です（制御機器画面で選んでください）")
        return device_id

    def list_devices(self) -> list[dict[str, Any]]:
        body = self._request("GET", "/devices") or {}
        return list(body.get("deviceList", []))

    def get_status(self, device_id: str | None = None) -> PlugStatus:
        """機器の状態を取得する。device_id を省略した場合は生成時の機器（確認用スクリプト向け）。"""
        body = self._request("GET", f"/devices/{self._require_device(device_id)}/status") or {}
        power = str(body.get("power", "")).upper()
        if power not in ("ON", "OFF"):
            raise SwitchBotError(f"プラグの電源状態を取得できませんでした（power={body.get('power')!r}）")
        return PlugStatus(power=power, device_type=body.get("deviceType"), raw=body)  # type: ignore[arg-type]

    def send_power_command(self, state: PowerState, device_id: str | None = None) -> None:
        """明示的な ON/OFF のみ送信する。送信後の状態は呼び出し側で再取得して照合すること。"""
        command = _COMMANDS[state]  # toggle などは送らない（KeyError）
        self._request(
            "POST",
            f"/devices/{self._require_device(device_id)}/commands",
            json={"command": command, "parameter": "default", "commandType": "command"},
        )
