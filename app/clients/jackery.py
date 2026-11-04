"""Jackery クラウドからのポータブル電源の状態取得と充電モードの変更。

Jackery は公式 API を公開していないため、Jackery アプリが利用しているクラウド API
（https://iot.jackeryapp.com）を利用する。通信方式は以下を参考に独自に実装した。
- https://github.com/theak/jackery-homeassistant
- https://qiita.com/Hsky16/items/c163137265a87186ac39
- 充電モードの変更（MQTT）：https://github.com/jlopez/socketry/blob/main/docs/protocol.md
  （docs/jackery-charge-mode-research.md）

注意：
- 同じアカウントで同時にログインできるのは 1 台のみ。このシステムでログインすると、同じ
  アカウントのスマホアプリはログアウトされるため、取得専用のアカウントを作り、デバイスを
  共有して使う。
- 非公式 API のため予告なく使えなくなる可能性がある。取得できない場合は残量不明として扱う。
- 操作は充電モード（高速充電／静かな充電、プロパティ cs）の変更のみ。超急速充電（sfc）・出力の ON/OFF などは扱わない。
  変更は MQTT で送り、HTTP で再取得した値が期待どおりになった場合のみ成功とする（docs/08 §8.4）。
"""

import base64
import enum
import hashlib
import json
import logging
import ssl
import threading
import time as time_module
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import httpx
from cryptography.hazmat.primitives import padding as sym_padding
from cryptography.hazmat.primitives.asymmetric import padding as asym_padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.serialization import load_der_public_key

logger = logging.getLogger(__name__)

BASE_URL = "https://iot.jackeryapp.com"
MQTT_HOST = "emqx.jackeryapp.com"
MQTT_PORT = 8883
# MQTT ブローカーの自己署名 CA（Jackery アプリに同梱されているもの。socketry の定数と同一）
MQTT_CA_FILE = Path(__file__).with_name("jackery-ca.crt")
# 充電モード（cs）を変更するコマンドの actionId
CHARGE_SPEED_ACTION_ID = 10
# 変更後の確認：機器からの通知を待つ時間と、HTTP で再取得する間隔・回数
MQTT_WAIT_SECONDS = 10.0
# 接続してから送信するまで待つ時間。接続直後（特にログインし直した直後）に送ると、受領応答は成功でも
# コマンドが機器に届かないことがある（2026-11-04 の実機検証：直後の送信は 5/10 成功、15 秒待つと 3/3 成功。
# docs/jackery-charge-mode-research.md）
MQTT_SETTLE_SECONDS = 15.0
VERIFY_WAIT_SECONDS = 3.0
VERIFY_ATTEMPTS = 5
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


class JackeryCommandUnconfirmed(JackeryError):
    """コマンドを送ったが、再取得した値が期待どおりにならなかった（成功扱いにしない）。"""


class ChargeMode(enum.StrEnum):
    """充電モード（プロパティ cs）。超急速充電（sfc）は別の項目で、本アプリでは扱わない。"""

    FAST = "FAST"  # cs=0（高速充電。アプリの「高速充電モード」）
    SILENT = "SILENT"  # cs=1（静かな充電。アプリの「静かな充電モード」）

    @property
    def cs(self) -> int:
        return 0 if self is ChargeMode.FAST else 1

    @property
    def command_value(self) -> int | str:
        """コマンドで送る値。実機で反映を確認した形（高速充電は文字列 "0"、静かな充電は数値 1）で送る。
        数値の 0 で届かなかったのは接続直後の送信が捨てられていたためと考えられるが、未確認のため変えない。"""
        return "0" if self is ChargeMode.FAST else 1

    @property
    def label(self) -> str:
        return "高速充電" if self is ChargeMode.FAST else "静かな充電"

    @classmethod
    def from_cs(cls, value: Any) -> "ChargeMode | None":
        # cs=2（custom）など、高速充電・静かな充電以外は不明として扱う
        if isinstance(value, bool) or not isinstance(value, int):
            return None
        return {0: cls.FAST, 1: cls.SILENT}.get(value)


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
    charge_mode: ChargeMode | None
    raw: dict[str, Any]
    device_sn: str | None = None  # MQTT で操作するときの宛先（HTTP の deviceId とは別）


def _scaled(value: Any, divisor: float = 1.0) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return value / divisor


def _flag(value: Any) -> bool | None:
    return bool(value) if isinstance(value, int) and not isinstance(value, bool) and value in (0, 1) else None


def parse_status(properties: dict[str, Any], device_sn: str | None = None) -> BatteryStatus:
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
        charge_mode=ChargeMode.from_cs(properties.get("cs")),
        raw=properties,
        device_sn=device_sn,
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


def derive_mqtt_password(username: str, mqtt_password_b64: str) -> str:
    """MQTT の接続パスワード。ログイン応答の mqttPassWord（AES-256 鍵）で username を AES/CBC 暗号化する。"""
    key = base64.b64decode(mqtt_password_b64)
    padder = sym_padding.PKCS7(128).padder()
    plain = padder.update(username.encode()) + padder.finalize()
    encryptor = Cipher(algorithms.AES(key), modes.CBC(key[:16])).encryptor()
    return base64.b64encode(encryptor.update(plain) + encryptor.finalize()).decode()


@dataclass(frozen=True)
class MqttCredentials:
    client_id: str
    username: str
    password: str
    command_topic: str
    device_topic: str

    @property
    def subscribe_topics(self) -> list[str]:
        prefix = self.device_topic.rsplit("/", 1)[0]
        return [f"{prefix}/{name}" for name in ("device", "notice", "alert", "config")]


def build_command(device_sn: str, action_id: int, body: dict[str, Any], now_ms: int) -> str:
    """Jackery アプリと同じ形式のプロパティ変更コマンド。"""
    message: dict[str, Any] = {
        "deviceSn": device_sn,
        "id": now_ms,
        "version": 0,
        "messageType": "DevicePropertyChange",
        "actionId": action_id,
        "timestamp": now_ms,
        "body": body,
    }
    return json.dumps(message, separators=(",", ":"))


def parse_device_message(payload: bytes) -> tuple[str, dict[str, Any]] | None:
    """機器からのプロパティ変更通知を (deviceSn, properties) にする。対象外のメッセージは None。"""
    try:
        data = json.loads(payload)
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict) or data.get("messageType") != "DevicePropertyChange":
        return None
    sn, body = data.get("deviceSn"), data.get("body")
    if not isinstance(sn, str) or not isinstance(body, dict):
        return None
    return sn, {k: v for k, v in body.items() if k != "messageId"}


def describe_message(payload: bytes) -> str:
    """ログ・表示用。シリアル番号は伏せる。"""
    try:
        data = json.loads(payload)
    except (ValueError, TypeError):
        return f"(not json, {len(payload)} bytes)"
    if not isinstance(data, dict):
        return "(unknown)"
    shown = {k: v for k, v in data.items() if k not in ("deviceSn", "id", "timestamp")}
    return json.dumps(shown, ensure_ascii=False, separators=(",", ":"))


class MqttTransport(Protocol):
    def publish_and_wait(
        self,
        credentials: MqttCredentials,
        payload: str,
        matches: Callable[[str, dict[str, Any]], bool],
        timeout: float,
    ) -> bool:
        """コマンドを送り、matches に合う通知を timeout 秒まで待つ。通知を受けたら True。

        接続・認証・送信に失敗した場合は JackeryError（認証の失敗は JackeryAuthError）を送出する。
        """
        ...


class PahoMqttTransport:
    """paho-mqtt による接続。操作のたびに接続し、終わったら切断する（常時接続はしない）。"""

    def __init__(
        self,
        host: str = MQTT_HOST,
        port: int = MQTT_PORT,
        ca_file: Path = MQTT_CA_FILE,
        *,
        mqtt5: bool = True,
        settle_seconds: float = MQTT_SETTLE_SECONDS,
        sleep: Callable[[float], None] = time_module.sleep,
    ) -> None:
        self._host = host
        self._port = port
        self._ca_file = ca_file
        self._settle_seconds = settle_seconds
        self._sleep = sleep
        # MQTT 5 で接続する。3.1.1 では接続・購読はできるが、送信したコマンドが機器に届かない
        # （ブローカーはエラーを返さない。2026-11 に HTE1091000A で確認）。MQTT 5 では受領応答に理由コードも付く
        self._mqtt5 = mqtt5

    @contextmanager
    def _session(
        self, credentials: MqttCredentials, on_payload: Callable[[str, bytes], None], timeout: float
    ) -> Iterator[Any]:
        """接続して通知（device のほか notice / alert / config）を購読し、終わったら切断する。"""
        import paho.mqtt.client as mqtt

        connected = threading.Event()
        subscribed = threading.Event()
        connect_result: list[Any] = []

        def on_connect(client: Any, userdata: Any, flags: Any, reason_code: Any, properties: Any) -> None:
            connect_result.append(reason_code)
            connected.set()

        def on_subscribe(client: Any, userdata: Any, mid: Any, reason_codes: Any, properties: Any) -> None:
            subscribed.set()

        def on_message(client: Any, userdata: Any, message: Any) -> None:
            on_payload(message.topic, message.payload)

        def on_publish(client: Any, userdata: Any, mid: Any, reason_code: Any, properties: Any) -> None:
            logger.info("jackery mqtt: publish acknowledged by broker (reason: %s)", reason_code)
            if getattr(reason_code, "is_failure", False):
                rejected.append(str(reason_code))

        rejected: list[str] = []
        client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            client_id=credentials.client_id,
            protocol=mqtt.MQTTv5 if self._mqtt5 else mqtt.MQTTv311,
        )
        client.on_publish = on_publish
        client.rejected = rejected  # type: ignore[attr-defined]
        client.username_pw_set(credentials.username, credentials.password)
        context = ssl.create_default_context(cafile=str(self._ca_file))
        client.tls_set_context(context)
        client.on_connect = on_connect
        client.on_subscribe = on_subscribe
        client.on_message = on_message
        try:
            client.connect(self._host, self._port, keepalive=10)
        except (OSError, ssl.SSLError) as exc:
            raise JackeryError(f"Jackery の MQTT に接続できませんでした: {exc.__class__.__name__}") from exc
        client.loop_start()
        try:
            if not connected.wait(timeout):
                raise JackeryError("Jackery の MQTT に接続できませんでした（タイムアウト）")
            reason = connect_result[0]
            if reason.is_failure:
                if reason.value in (4, 5, 134, 135):  # 認証情報の誤り・権限なし
                    raise JackeryAuthError(f"Jackery の MQTT の認証に失敗しました（{reason}）")
                raise JackeryError(f"Jackery の MQTT に接続できませんでした（{reason}）")
            client.subscribe([(topic, 1) for topic in credentials.subscribe_topics])
            subscribed.wait(timeout)
            yield client
        finally:
            client.disconnect()
            client.loop_stop()

    def publish_and_wait(
        self,
        credentials: MqttCredentials,
        payload: str,
        matches: Callable[[str, dict[str, Any]], bool],
        timeout: float,
    ) -> bool:
        received = threading.Event()

        def on_payload(topic: str, raw: bytes) -> None:
            parsed = parse_device_message(raw) if topic == credentials.device_topic else None
            if parsed is None:
                # プロパティ変更以外（受領応答・お知らせ・警告など）。原因調査のため内容を残す
                logger.info("jackery mqtt: %s %s", topic.rsplit("/", 1)[-1], describe_message(raw))
                return
            properties = parsed[1]
            logger.info(
                "jackery mqtt: property change %s",
                {k: properties[k] for k in ("cs", "sfc", "ip", "acip") if k in properties} or sorted(properties),
            )
            if matches(*parsed):
                received.set()

        with self._session(credentials, on_payload, timeout) as client:
            self._sleep(self._settle_seconds)
            notified = False
            for attempt in range(2):  # 機器からの通知がなければ、同じ接続で1回だけ送り直す
                if attempt:
                    logger.info("jackery mqtt: no device notification; resending once")
                info = client.publish(credentials.command_topic, payload, qos=1)
                info.wait_for_publish(timeout)
                if not info.is_published():
                    raise JackeryError("Jackery の MQTT にコマンドを送信できませんでした")
                notified = received.wait(timeout)
                if client.rejected:
                    raise JackeryError(f"Jackery の MQTT がコマンドを拒否しました（{client.rejected[0]}）")
                if notified:
                    break
            return notified

    def listen(self, credentials: MqttCredentials, seconds: float, on_payload: Callable[[str, bytes], None]) -> None:
        """送信せず、通知を seconds 秒間受け取る（原因調査用）。"""
        with self._session(credentials, on_payload, 10.0):
            time_module.sleep(seconds)


class JackeryClient:
    def __init__(
        self,
        account: str,
        password: str,
        *,
        client: httpx.Client | None = None,
        timeout: float = 10.0,
        aes_key: bytes = APP_AES_KEY,
        mqtt: MqttTransport | None = None,
        sleep: Callable[[float], None] = time_module.sleep,
    ) -> None:
        self._account = account
        self._password = password
        self._client = client or httpx.Client(base_url=BASE_URL, timeout=timeout)
        self._aes_key = aes_key
        self._mqtt = mqtt or PahoMqttTransport()
        self._sleep = sleep
        self._token: str | None = None
        # MQTT の認証に使う（ログインのたびに更新される）
        self._user_id: str | None = None
        self._mqtt_password: str | None = None

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
        data = body.get("data") if isinstance(body.get("data"), dict) else {}
        user_id, mqtt_password = data.get("userId"), data.get("mqttPassWord")
        self._user_id = str(user_id) if user_id is not None else None
        self._mqtt_password = mqtt_password if isinstance(mqtt_password, str) and mqtt_password else None

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
        device = data.get("device") if isinstance(data.get("device"), dict) else {}
        sn = device.get("deviceSn")
        return parse_status(properties, sn if isinstance(sn, str) and sn else None)

    def _mqtt_credentials(self) -> MqttCredentials:
        if self._user_id is None or self._mqtt_password is None:
            self.login()
        if self._user_id is None or self._mqtt_password is None:
            raise JackeryError("ログイン応答に MQTT の認証情報が含まれていませんでした")
        username = f"{self._user_id}@{_device_udid(self._account)}"
        return MqttCredentials(
            client_id=f"{self._user_id}@APP",
            username=username,
            password=derive_mqtt_password(username, self._mqtt_password),
            command_topic=f"hb/app/{self._user_id}/command",
            device_topic=f"hb/app/{self._user_id}/device",
        )

    def watch(self, seconds: float, on_payload: Callable[[str, bytes], None]) -> None:
        """MQTT の通知を seconds 秒間受け取る（送信しない。原因調査用）。"""
        listen = getattr(self._mqtt, "listen", None)
        if listen is None:
            raise JackeryError("この MQTT の接続方式は通知の受信に対応していません")
        listen(self._mqtt_credentials(), seconds, on_payload)

    def set_charge_mode(
        self, device_id: str, mode: ChargeMode, *, body: dict[str, Any] | None = None
    ) -> BatteryStatus:
        """充電モードを変更し、HTTP で再取得して期待どおりか照合する。照合後の状態を返す。

        すでに目的のモードなら送信しない。照合できなければ JackeryCommandUnconfirmed（成功扱いにしない）。
        body はコマンドの中身を差し替える原因調査用（scripts/jackery_check.py の --body）。通常は指定しない。
        """
        status = self.get_status(device_id)
        if status.charge_mode == mode:
            return status
        if status.device_sn is None:
            raise JackeryError("ポータブル電源のシリアル番号を取得できませんでした")
        sn = status.device_sn
        command_body = body if body is not None else {"cs": mode.command_value}
        payload = build_command(sn, CHARGE_SPEED_ACTION_ID, command_body, int(time_module.time() * 1000))

        def matches(message_sn: str, properties: dict[str, Any]) -> bool:
            return message_sn == sn and properties.get("cs") == mode.cs

        logger.info("jackery charge mode: sending %s (current cs=%s)", json.dumps(command_body), status.raw.get("cs"))
        try:
            notified = self._mqtt.publish_and_wait(self._mqtt_credentials(), payload, matches, MQTT_WAIT_SECONDS)
        except JackeryAuthError:
            # mqttPassWord はログインのたびに変わるため、別の場所でログインされた場合に備えて1回だけやり直す
            self.login()
            notified = self._mqtt.publish_and_wait(self._mqtt_credentials(), payload, matches, MQTT_WAIT_SECONDS)
        logger.info("jackery charge mode: device notification with cs=%s %s", mode.cs, "received" if notified else "not received")

        # 機器からの通知の有無によらず、HTTP で再取得した値で照合する
        for attempt in range(VERIFY_ATTEMPTS):
            status = self.get_status(device_id)
            logger.info("jackery charge mode: verify %d/%d cs=%s", attempt + 1, VERIFY_ATTEMPTS, status.raw.get("cs"))
            if status.charge_mode == mode:
                return status
            self._sleep(VERIFY_WAIT_SECONDS)
        current = status.charge_mode.label if status.charge_mode else "不明"
        raise JackeryCommandUnconfirmed(f"充電モードが{mode.label}になりませんでした（現在：{current}）")
