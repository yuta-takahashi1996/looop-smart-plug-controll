"""FOSSiBOT（SYDPOWER）のポータブル電源のクライアント（docs/17-multi-device.md §17.9.4、docs/08 §8.5）。

自前の MQTT ブローカー（Compose の Mosquitto）経由で、Modbus のフレームを送受信する。機器は BrightEMS アプリの
「ローカルMQTT Broker設定」でブローカーに接続させておく。クラウド API・アカウントは使わない（メーカーが遮断している）。

- ブローカーには起動時に接続し続け（切断時は自動で再接続）、`+/device/response/#` を購読する。
  機器は要求がなくても約1分ごとに計測値（入力レジスタ）を送ってくる。
- 状態は入力レジスタ（04）・保持レジスタ（03）の読み出しを要求して取得する。要求は1つずつ直列に送る。
- 書き込みは AC 静音充電（保持レジスタ 57）の 0／1 だけ。ファームウェアは書き込む値を検証しないため、
  他のレジスタ・値は送れないようにしている。
"""

import logging
import queue
import threading
import time as time_module
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from app.clients.portable_power import (
    ChargeMode,
    PortablePowerError,
    PortablePowerInfo,
    PortablePowerUnconfirmed,
)

logger = logging.getLogger(__name__)

MODBUS_ADDRESS = 0x11  # SYDPOWER の機器の Modbus アドレス（17）
READ_COUNT = 80
FUNC_READ_HOLDING = 0x03  # 設定値（応答は client/data）
FUNC_READ_INPUT = 0x04  # 計測値（応答は client/04）
FUNC_WRITE_SINGLE = 0x06

# 入力レジスタ（計測値）
IREG_TOTAL_INPUT_W = 6
IREG_TOTAL_OUTPUT_W = 39
IREG_SOC = 56  # 0.1% 単位
# 保持レジスタ（設定値）
HREG_SILENT_CHARGING = 57  # 下位バイトが 1 なら AC 静音充電 ON

SUBSCRIBE_TOPIC = "+/device/response/#"
DEVICE_SEEN_SECONDS = 600  # 機器一覧に出す、最後にメッセージを受信してからの時間
CACHED_INPUT_SECONDS = 180  # 読み出しに応答がないときに使う、機器から届いた計測値の古さの上限
REQUEST_ATTEMPTS = 3
REQUEST_TIMEOUT_SECONDS = 5.0
REQUEST_GAP_SECONDS = 1.0  # 前の要求の直後に送ると応答しないことがあるため空ける
VERIFY_ATTEMPTS = 3
VERIFY_INTERVAL_SECONDS = 2.0
CONNECT_WAIT_SECONDS = 5.0


class FossibotError(PortablePowerError):
    pass


def crc16(data: bytes) -> int:
    """Modbus の CRC16。"""
    crc = 0xFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc


def _frame(func: int, register: int, value: int) -> bytes:
    body = bytes([MODBUS_ADDRESS, func, register >> 8, register & 0xFF, value >> 8, value & 0xFF])
    crc = crc16(body)
    return body + bytes([crc >> 8, crc & 0xFF])  # SYDPOWER は CRC の上位バイトが先


def read_frame(func: int) -> bytes:
    """レジスタ 0 から READ_COUNT 個を読み出す要求。"""
    if func not in (FUNC_READ_HOLDING, FUNC_READ_INPUT):
        raise ValueError(f"unsupported function: {func}")
    return _frame(func, 0, READ_COUNT)


def silent_charging_frame(enabled: bool) -> bytes:
    """AC 静音充電の書き込み要求。書き込みはこれ以外に作らない。"""
    return _frame(FUNC_WRITE_SINGLE, HREG_SILENT_CHARGING, 1 if enabled else 0)


def normalize_mac(value: str) -> str:
    return value.replace(":", "").strip().upper()


@dataclass(frozen=True)
class Response:
    mac: str
    kind: str  # トピックの末尾（client/04、client/data、state など）
    func: int | None
    registers: list[int] | None  # 読み出しの応答で CRC が一致したもの


def parse_message(topic: str, payload: bytes) -> Response | None:
    parts = topic.split("/", 3)
    if len(parts) < 4 or parts[1:3] != ["device", "response"]:
        return None
    mac, kind = normalize_mac(parts[0]), parts[3]
    func = payload[1] if kind.startswith("client/") and len(payload) >= 8 else None
    registers = None
    if func in (FUNC_READ_HOLDING, FUNC_READ_INPUT) and crc16(payload[:-2]) == int.from_bytes(payload[-2:], "big"):
        # ヘッダー6バイト（アドレス・機能・開始・個数）＋データ＋CRC
        data = payload[6:-2]
        registers = [int.from_bytes(data[i : i + 2], "big") for i in range(0, len(data) - 1, 2)]
    return Response(mac, kind, func, registers)


def _reg(registers: list[int] | None, index: int) -> int | None:
    return registers[index] if registers is not None and index < len(registers) else None


def charge_mode_from_registers(holdings: list[int] | None) -> ChargeMode | None:
    raw = _reg(holdings, HREG_SILENT_CHARGING)
    if raw is None:
        return None
    return {0: ChargeMode.FAST, 1: ChargeMode.SILENT}.get(raw & 0xFF)


@dataclass(frozen=True)
class FossibotStatus:
    """取得した状態（PowerStatus の形）。取得できなかった項目は None。"""

    remaining_percent: int | None
    input_power_w: float | None
    output_power_w: float | None
    charge_mode: ChargeMode | None
    inputs: list[int] | None = field(default=None, repr=False)
    holdings: list[int] | None = field(default=None, repr=False)


def parse_status(inputs: list[int] | None, holdings: list[int] | None) -> FossibotStatus:
    soc = _reg(inputs, IREG_SOC)
    input_w = _reg(inputs, IREG_TOTAL_INPUT_W)
    output_w = _reg(inputs, IREG_TOTAL_OUTPUT_W)
    return FossibotStatus(
        # 0.1% 単位。判定は安全側になるよう切り捨てる
        remaining_percent=soc // 10 if soc is not None and 0 <= soc <= 1000 else None,
        input_power_w=float(input_w) if input_w is not None else None,
        output_power_w=float(output_w) if output_w is not None else None,
        charge_mode=charge_mode_from_registers(holdings),
        inputs=inputs,
        holdings=holdings,
    )


class MqttTransport(Protocol):
    def start(self, on_message: Callable[[str, bytes], None]) -> None:
        """接続して SUBSCRIBE_TOPIC を購読する（切断時は再接続して購読し直す）。"""
        ...

    def wait_connected(self, timeout: float) -> bool: ...

    def publish(self, topic: str, payload: bytes) -> None:
        """送信する。接続していなければ FossibotError。"""
        ...

    def stop(self) -> None: ...


class PahoMqttTransport:
    """paho-mqtt によるブローカーへの常時接続（MQTT 3.1.1、匿名）。"""

    def __init__(self, host: str, port: int) -> None:
        self._host = host
        self._port = port
        self._client: Any = None
        self._connected = threading.Event()

    def start(self, on_message: Callable[[str, bytes], None]) -> None:
        import paho.mqtt.client as mqtt

        def on_connect(client: Any, userdata: Any, flags: Any, reason_code: Any, properties: Any) -> None:
            if getattr(reason_code, "is_failure", False):
                logger.warning("fossibot mqtt: connection refused (%s)", reason_code)
                return
            client.subscribe(SUBSCRIBE_TOPIC, qos=1)
            self._connected.set()
            logger.info("fossibot mqtt: connected to %s:%s", self._host, self._port)

        def on_disconnect(client: Any, userdata: Any, flags: Any, reason_code: Any, properties: Any) -> None:
            self._connected.clear()
            logger.warning("fossibot mqtt: disconnected (%s)", reason_code)

        client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2, client_id=f"looop_{uuid.uuid4().hex[:12]}", protocol=mqtt.MQTTv311
        )
        client.on_connect = on_connect
        client.on_disconnect = on_disconnect
        client.on_message = lambda c, u, message: on_message(message.topic, message.payload)
        client.reconnect_delay_set(min_delay=1, max_delay=60)
        client.connect_async(self._host, self._port, keepalive=60)
        client.loop_start()
        self._client = client

    def wait_connected(self, timeout: float) -> bool:
        return self._connected.wait(timeout)

    def publish(self, topic: str, payload: bytes) -> None:
        if self._client is None or not self._connected.is_set():
            raise FossibotError("MQTT ブローカーに接続していません")
        info = self._client.publish(topic, payload, qos=1)
        if info.rc != 0:
            raise FossibotError(f"MQTT の送信に失敗しました（rc={info.rc}）")

    def stop(self) -> None:
        if self._client is not None:
            self._client.disconnect()
            self._client.loop_stop()
            self._client = None
        self._connected.clear()


class FossibotClient:
    """ポータブル電源の共通の形（app/clients/portable_power.py の PortablePowerClient）のクライアント。"""

    def __init__(
        self,
        host: str,
        port: int = 1883,
        *,
        transport: MqttTransport | None = None,
        sleep: Callable[[float], None] = time_module.sleep,
        clock: Callable[[], float] = time_module.monotonic,
        request_timeout: float = REQUEST_TIMEOUT_SECONDS,
        request_gap: float = REQUEST_GAP_SECONDS,
        verify_interval: float = VERIFY_INTERVAL_SECONDS,
        connect_wait: float = CONNECT_WAIT_SECONDS,
    ) -> None:
        self._host = host
        self._transport = transport if transport is not None else (PahoMqttTransport(host, port) if host else None)
        self._sleep = sleep
        self._clock = clock
        self._request_timeout = request_timeout
        self._request_gap = request_gap
        self._verify_interval = verify_interval
        self._connect_wait = connect_wait
        self._started = False
        self._start_lock = threading.Lock()
        self._request_lock = threading.Lock()  # 要求は1つずつ
        self._state_lock = threading.Lock()
        self._seen: dict[str, float] = {}
        self._latest_inputs: dict[str, tuple[float, list[int]]] = {}
        self._responses: queue.Queue[Response] = queue.Queue()

    @property
    def configured(self) -> bool:
        return bool(self._host) and self._transport is not None

    def start(self) -> None:
        """ブローカーに接続する（何度呼んでもよい）。"""
        if not self.configured:
            return
        with self._start_lock:
            if self._started:
                return
            assert self._transport is not None
            self._transport.start(self._on_message)
            self._started = True
            if not self._transport.wait_connected(self._connect_wait):
                logger.warning("fossibot mqtt: not connected yet (%s)", self._host)

    def stop(self) -> None:
        with self._start_lock:
            if self._started and self._transport is not None:
                self._transport.stop()
            self._started = False

    def _on_message(self, topic: str, payload: bytes) -> None:
        res = parse_message(topic, payload)
        if res is None:
            return
        now = self._clock()
        with self._state_lock:
            self._seen[res.mac] = now
            if res.func == FUNC_READ_INPUT and res.registers:
                self._latest_inputs[res.mac] = (now, res.registers)
        self._responses.put(res)

    def _ensure_started(self) -> None:
        if not self.configured:
            raise FossibotError("FOSSIBOT_MQTT_HOST が未設定です")
        self.start()

    def list_devices(self) -> list[PortablePowerInfo]:
        """直近にブローカーへメッセージを送ってきた機器（機種は分からないため F2400 として扱う）。"""
        self._ensure_started()
        now = self._clock()
        with self._state_lock:
            macs = sorted(mac for mac, at in self._seen.items() if now - at <= DEVICE_SEEN_SECONDS)
        return [PortablePowerInfo(mac, "FOSSiBOT F2400", "F2400") for mac in macs]

    def _drain(self) -> None:
        while True:
            try:
                self._responses.get_nowait()
            except queue.Empty:
                return

    def _wait_for(self, mac: str, func: int) -> list[int] | None:
        deadline = self._clock() + self._request_timeout
        while (left := deadline - self._clock()) > 0:
            try:
                res = self._responses.get(timeout=left)
            except queue.Empty:
                return None
            if res.mac == mac and res.func == func and res.registers:
                return res.registers
        return None

    def _publish(self, mac: str, payload: bytes) -> None:
        assert self._transport is not None
        self._transport.publish(f"{mac}/client/request/data", payload)

    def _request(self, mac: str, func: int, attempts: int = REQUEST_ATTEMPTS) -> list[int] | None:
        """読み出しを要求して応答のレジスタを返す（_request_lock を持って呼ぶ）。応答がなければ None。"""
        for _ in range(attempts):
            self._sleep(self._request_gap)
            self._drain()
            try:
                self._publish(mac, read_frame(func))
            except FossibotError as exc:
                logger.info("fossibot: request failed (%s)", exc)
                continue
            registers = self._wait_for(mac, func)
            if registers is not None:
                return registers
        return None

    def _read_locked(self, mac: str, holdings: list[int] | None = None) -> FossibotStatus:
        inputs = self._request(mac, FUNC_READ_INPUT)
        if inputs is None:
            with self._state_lock:
                cached = self._latest_inputs.get(mac)
            if cached is not None and self._clock() - cached[0] <= CACHED_INPUT_SECONDS:
                inputs = cached[1]
        if inputs is None:
            raise FossibotError("機器から応答がありません（ブローカーへの接続とアプリのローカルMQTT設定を確認してください）")
        if holdings is None:
            holdings = self._request(mac, FUNC_READ_HOLDING)
        return parse_status(inputs, holdings)

    def read(self, external_id: str) -> FossibotStatus:
        self._ensure_started()
        with self._request_lock:
            return self._read_locked(normalize_mac(external_id))

    def set_charge_mode(self, external_id: str, mode: ChargeMode) -> FossibotStatus:
        """AC 静音充電を切り替え（静かな充電＝ON、高速充電＝OFF）、読み直して照合する。"""
        self._ensure_started()
        mac = normalize_mac(external_id)
        with self._request_lock:
            holdings = self._request(mac, FUNC_READ_HOLDING)
            if holdings is None or charge_mode_from_registers(holdings) is None:
                raise FossibotError("変更前の充電モードを取得できませんでした")
            if charge_mode_from_registers(holdings) is not mode:
                self._publish(mac, silent_charging_frame(mode is ChargeMode.SILENT))
                for _ in range(VERIFY_ATTEMPTS):
                    self._sleep(self._verify_interval)
                    holdings = self._request(mac, FUNC_READ_HOLDING, attempts=1)
                    if charge_mode_from_registers(holdings) is mode:
                        break
                else:
                    raise PortablePowerUnconfirmed(f"再取得した充電モードが{mode.label}になりませんでした")
            return self._read_locked(mac, holdings)
