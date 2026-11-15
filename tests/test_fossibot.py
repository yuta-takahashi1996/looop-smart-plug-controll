from collections.abc import Callable

import pytest

from app.clients import fossibot
from app.clients.fossibot import (
    FUNC_READ_HOLDING,
    FUNC_READ_INPUT,
    FUNC_WRITE_SINGLE,
    FossibotClient,
    FossibotError,
    crc16,
    parse_message,
    read_frame,
    silent_charging_frame,
)
from app.clients.portable_power import ChargeMode, PortablePowerUnconfirmed

MAC = "70AF0906A6CE"


def response_frame(func: int, registers: list[int]) -> bytes:
    """機器の応答（ヘッダー6バイト＋データ＋CRC）。"""
    body = bytes([0x11, func, 0, 0, 0, len(registers)]) + b"".join(r.to_bytes(2, "big") for r in registers)
    crc = crc16(body)
    return body + bytes([crc >> 8, crc & 0xFF])


def inputs(soc: int = 942, input_w: int = 0, output_w: int = 0) -> list[int]:
    regs = [0] * 80
    regs[56], regs[6], regs[39] = soc, input_w, output_w
    return regs


def holdings(silent: int = 0) -> list[int]:
    regs = [0] * 80
    regs[57], regs[67] = silent, 1000
    return regs


class FakeDevice:
    """ブローカーとその先の F2400 を模擬する。送信された要求に同期して応答する。"""

    def __init__(self) -> None:
        self.on_message: Callable[[str, bytes], None] | None = None
        self.connected = True
        self.published: list[tuple[str, bytes]] = []
        self.inputs = inputs()
        self.holdings = holdings()
        self.respond_input = True
        self.respond_holding = True
        self.ignore_writes = False
        self.ignore_first_requests = 0
        self.stopped = False

    def start(self, on_message: Callable[[str, bytes], None]) -> None:
        self.on_message = on_message

    def wait_connected(self, timeout: float) -> bool:
        return self.connected

    def stop(self) -> None:
        self.stopped = True

    def push(self, kind: str, payload: bytes, mac: str = MAC) -> None:
        assert self.on_message is not None
        self.on_message(f"{mac}/device/response/{kind}", payload)

    def publish(self, topic: str, payload: bytes) -> None:
        if not self.connected:
            raise FossibotError("MQTT ブローカーに接続していません")
        self.published.append((topic, payload))
        if self.ignore_first_requests > 0:
            self.ignore_first_requests -= 1
            return
        func = payload[1]
        if func == FUNC_READ_INPUT and self.respond_input:
            self.push("client/04", response_frame(FUNC_READ_INPUT, self.inputs))
        elif func == FUNC_READ_HOLDING and self.respond_holding:
            self.push("client/data", response_frame(FUNC_READ_HOLDING, self.holdings))
        elif func == FUNC_WRITE_SINGLE:
            self.push("client/data", payload)  # 書き込みの応答は送った内容と同じ
            if not self.ignore_writes:
                register = int.from_bytes(payload[2:4], "big")
                self.holdings[register] = int.from_bytes(payload[4:6], "big")


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def device() -> FakeDevice:
    return FakeDevice()


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def client(device: FakeDevice, clock: Clock) -> FossibotClient:
    c = FossibotClient("broker", transport=device, sleep=clock.sleep, clock=clock, request_timeout=0.01)
    c.start()
    return c


def test_frames_match_device_examples() -> None:
    # 実機で応答を確認したフレーム（docs/fossibot-research.md）
    assert read_frame(FUNC_READ_HOLDING).hex() == "1103000000506647"
    assert silent_charging_frame(True).hex() == "110600390001979a"
    assert silent_charging_frame(False)[:6].hex() == "110600390000"


def test_read_frame_rejects_write() -> None:
    with pytest.raises(ValueError):
        read_frame(FUNC_WRITE_SINGLE)


def test_parse_message_checks_crc() -> None:
    payload = response_frame(FUNC_READ_INPUT, inputs())
    assert parse_message(f"{MAC}/device/response/client/04", payload).registers == inputs()
    broken = payload[:-1] + bytes([payload[-1] ^ 0xFF])
    assert parse_message(f"{MAC}/device/response/client/04", broken).registers is None
    assert parse_message(f"{MAC}/client/request/data", payload) is None


def test_read_status(client: FossibotClient, device: FakeDevice) -> None:
    device.inputs = inputs(soc=948, input_w=310, output_w=125)
    device.holdings = holdings(silent=1)
    status = client.read(MAC.lower())
    assert status.remaining_percent == 94  # 切り捨て
    assert status.input_power_w == 310
    assert status.output_power_w == 125
    assert status.charge_mode is ChargeMode.SILENT
    assert {t for t, _ in device.published} == {f"{MAC}/client/request/data"}
    assert all(p[1] in (FUNC_READ_INPUT, FUNC_READ_HOLDING) for _, p in device.published)


def test_read_retries_when_device_does_not_respond(client: FossibotClient, device: FakeDevice) -> None:
    device.ignore_first_requests = 1
    assert client.read(MAC).remaining_percent == 94
    assert len(device.published) == 3  # 入力（無応答）→入力→保持


def test_read_uses_recent_pushed_values(client: FossibotClient, device: FakeDevice, clock: Clock) -> None:
    device.push("client/04", response_frame(FUNC_READ_INPUT, inputs(soc=500)))
    device.respond_input = False
    clock.now += 60
    assert client.read(MAC).remaining_percent == 50


def test_read_fails_without_fresh_values(client: FossibotClient, device: FakeDevice, clock: Clock) -> None:
    device.push("client/04", response_frame(FUNC_READ_INPUT, inputs(soc=500)))
    device.respond_input = False
    clock.now += fossibot.CACHED_INPUT_SECONDS + 1
    with pytest.raises(FossibotError, match="応答がありません"):
        client.read(MAC)


def test_read_without_holding_has_unknown_charge_mode(client: FossibotClient, device: FakeDevice) -> None:
    device.respond_holding = False
    status = client.read(MAC)
    assert status.remaining_percent == 94
    assert status.charge_mode is None


def test_invalid_soc_is_unknown(client: FossibotClient, device: FakeDevice) -> None:
    device.inputs = inputs(soc=1500)
    assert client.read(MAC).remaining_percent is None


def test_read_fails_when_broker_disconnected(client: FossibotClient, device: FakeDevice) -> None:
    device.connected = False
    with pytest.raises(FossibotError):
        client.read(MAC)


def test_list_devices_from_received_messages(client: FossibotClient, device: FakeDevice, clock: Clock) -> None:
    assert client.list_devices() == []
    device.push("client/04", response_frame(FUNC_READ_INPUT, inputs()))
    device.push("state", b"\x01", mac="AABBCCDDEEFF")
    listed = client.list_devices()
    assert [d.external_id for d in listed] == ["70AF0906A6CE", "AABBCCDDEEFF"]
    assert listed[0].name == "FOSSiBOT F2400"
    clock.now += fossibot.DEVICE_SEEN_SECONDS + 1
    assert client.list_devices() == []


def test_set_charge_mode_writes_and_verifies(client: FossibotClient, device: FakeDevice) -> None:
    status = client.set_charge_mode(MAC, ChargeMode.SILENT)
    assert status.charge_mode is ChargeMode.SILENT
    writes = [p for _, p in device.published if p[1] == FUNC_WRITE_SINGLE]
    assert writes == [silent_charging_frame(True)]
    client.set_charge_mode(MAC, ChargeMode.FAST)
    assert device.holdings[57] == 0


def test_set_charge_mode_does_not_send_when_already_set(client: FossibotClient, device: FakeDevice) -> None:
    device.holdings = holdings(silent=1)
    assert client.set_charge_mode(MAC, ChargeMode.SILENT).charge_mode is ChargeMode.SILENT
    assert not [p for _, p in device.published if p[1] == FUNC_WRITE_SINGLE]


def test_set_charge_mode_is_unconfirmed_when_value_does_not_change(client: FossibotClient, device: FakeDevice) -> None:
    device.ignore_writes = True
    with pytest.raises(PortablePowerUnconfirmed):
        client.set_charge_mode(MAC, ChargeMode.SILENT)


def test_set_charge_mode_requires_current_value(client: FossibotClient, device: FakeDevice) -> None:
    device.respond_holding = False
    with pytest.raises(FossibotError, match="変更前"):
        client.set_charge_mode(MAC, ChargeMode.SILENT)
    assert not [p for _, p in device.published if p[1] == FUNC_WRITE_SINGLE]


def test_not_configured() -> None:
    c = FossibotClient("")
    assert not c.configured
    c.start()  # 何もしない
    with pytest.raises(FossibotError, match="FOSSIBOT_MQTT_HOST"):
        c.read(MAC)


def test_stop(client: FossibotClient, device: FakeDevice) -> None:
    client.stop()
    assert device.stopped
