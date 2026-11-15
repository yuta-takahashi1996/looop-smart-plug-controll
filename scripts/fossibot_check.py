"""FOSSiBOT F2400 の状態取得・AC 静音充電の変更の動作確認（Phase 12 の調査用）。

    uv run python scripts/fossibot_check.py devices [秒数]  # ブローカーに届く機器（MAC）の一覧（既定 90 秒待つ）
    uv run python scripts/fossibot_check.py [status]        # 残量・入出力・AC 静音充電などの取得（省略時）
    uv run python scripts/fossibot_check.py raw             # 取得したレジスタの値をすべて表示（項目の調査用）
    uv run python scripts/fossibot_check.py watch [秒数]    # 機器からのメッセージを表示する（送信しない。既定 120 秒）
    uv run python scripts/fossibot_check.py silent on|off --confirm
                                                            # AC 静音充電を変更する（実機の設定が変わる）

silent 以外は読み取りのみ（status / raw は Modbus の読み出し要求を送る。設定は変わらない）。

BrightEMS アプリの「ローカルMQTT Broker設定」で、機器の接続先を自前のブローカーにしておく（docs/fossibot-research.md）。
接続先は FOSSIBOT_MQTT_HOST / FOSSIBOT_MQTT_PORT（既定 1883）か --host / --port、
機器は FOSSIBOT_DEVICE_ID（MAC アドレス。コロンなし）か --mac で指定する。省略時は最初に届いた機器を使う。
"""

import argparse
import os
import queue
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import datetime

import paho.mqtt.client as mqtt

MODBUS_ADDRESS = 17  # SYDPOWER の機器の Modbus アドレス（0x11）
READ_COUNT = 80
FUNC_READ_HOLDING = 0x03  # 設定値
FUNC_READ_INPUT = 0x04  # 計測値
FUNC_WRITE_SINGLE = 0x06

# 入力レジスタ（計測値）
IREG_AC_INPUT_W = 3
IREG_DC_INPUT_W = 4
IREG_TYPEC_INPUT_W = 5
IREG_TOTAL_INPUT_W = 6
IREG_AC_OUTPUT_W = 20
IREG_TOTAL_OUTPUT_W = 39
IREG_SLAVE_1 = 53
IREG_SLAVE_2 = 55
IREG_SOC = 56  # 0.1% 単位
# 保持レジスタ（設定値）
HREG_SILENT_CHARGING = 57  # 下位バイトが 1 なら ON
HREG_DISCHARGE_MIN = 66  # 0.1% 単位
HREG_CHARGE_MAX = 67  # 0.1% 単位


def crc16(data: bytes) -> int:
    crc = 0xFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc


def frame(func: int, register: int, value: int) -> bytes:
    """読み出し（value＝個数）・書き込み（value＝値）のフレーム。CRC は上位バイトが先。"""
    body = bytes([MODBUS_ADDRESS, func, register >> 8, register & 0xFF, value >> 8, value & 0xFF])
    crc = crc16(body)
    return body + bytes([crc >> 8, crc & 0xFF])


@dataclass
class Response:
    mac: str
    kind: str  # topic の末尾（client/04、client/data、state など）
    payload: bytes
    func: int | None = None
    registers: list[int] | None = None  # 読み出しの応答のみ

    @property
    def crc_ok(self) -> bool:
        return len(self.payload) >= 4 and crc16(self.payload[:-2]) == int.from_bytes(self.payload[-2:], "big")


def parse(topic: str, payload: bytes) -> Response | None:
    parts = topic.split("/", 3)
    if len(parts) < 4 or parts[1:3] != ["device", "response"]:
        return None
    res = Response(mac=parts[0], kind=parts[3], payload=payload)
    if res.kind.startswith("client/") and len(payload) >= 8:
        res.func = payload[1]
        # 読み出しの応答：ヘッダー6バイト（アドレス・機能・開始・個数）＋データ＋CRC
        if res.func in (FUNC_READ_HOLDING, FUNC_READ_INPUT) and res.crc_ok:
            data = payload[6:-2]
            res.registers = [int.from_bytes(data[i : i + 2], "big") for i in range(0, len(data) - 1, 2)]
    return res


class Session:
    def __init__(self, host: str, port: int) -> None:
        self.messages: queue.Queue[Response] = queue.Queue()
        self.client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2, client_id=f"looop_check_{uuid.uuid4().hex[:12]}", protocol=mqtt.MQTTv311
        )
        self.client.on_message = self._on_message
        self.client.connect(host, port, keepalive=60)
        self.client.subscribe("+/device/response/#", qos=1)
        self.client.loop_start()

    def _on_message(self, _client, _userdata, msg) -> None:
        res = parse(msg.topic, msg.payload)
        if res is not None:
            self.messages.put(res)

    def close(self) -> None:
        self.client.loop_stop()
        self.client.disconnect()

    def send(self, mac: str, data: bytes) -> None:
        self.client.publish(f"{mac}/client/request/data", data, qos=1).wait_for_publish(10)

    def wait(self, accept, timeout: float) -> Response | None:
        deadline = time.monotonic() + timeout
        while (left := deadline - time.monotonic()) > 0:
            try:
                res = self.messages.get(timeout=left)
            except queue.Empty:
                break
            if accept(res):
                return res
        return None

    def read(self, mac: str, func: int, attempts: int = 3, timeout: float = 5) -> list[int] | None:
        # 前の要求の直後に送ると応答しないことがあるため、間隔を空けて再送する
        kind = "client/04" if func == FUNC_READ_INPUT else "client/data"
        for _ in range(attempts):
            time.sleep(1)
            self.send(mac, frame(func, 0, READ_COUNT))
            res = self.wait(lambda r: r.mac == mac and r.kind == kind and r.func == func and r.registers, timeout)
            if res:
                return res.registers
        return None


def _get(regs: list[int] | None, index: int) -> int | None:
    return regs[index] if regs is not None and index < len(regs) else None


def _pct(raw: int | None) -> str:
    return "—" if raw is None else f"{raw / 10:.1f}%"


def _slave(raw: int | None) -> str:
    # 0＝未接続。接続時は SOC＋10 を 0.1% 単位で表す（10〜1010）
    return "—" if raw is None else ("未接続" if raw == 0 else f"{(raw - 10) / 10:.1f}%")


def _w(raw: int | None) -> str:
    return "—" if raw is None else f"{raw} W"


def _silent(regs: list[int] | None) -> bool | None:
    raw = _get(regs, HREG_SILENT_CHARGING)
    return None if raw is None else (raw & 0xFF) == 1


def _label(silent: bool | None) -> str:
    return "不明" if silent is None else ("ON（静かな充電）" if silent else "OFF（高速充電）")


def describe(res: Response) -> str:
    if res.registers is not None:
        regs = res.registers
        if res.func == FUNC_READ_INPUT:
            return (
                f"計測値 残量={_pct(_get(regs, IREG_SOC))} 入力={_w(_get(regs, IREG_TOTAL_INPUT_W))} "
                f"出力={_w(_get(regs, IREG_TOTAL_OUTPUT_W))}"
            )
        return f"設定値 AC静音充電={_label(_silent(regs))}"
    if res.kind == "state":
        return f"状態 {res.payload.hex()}"
    return f"{res.kind} {len(res.payload)} バイト crc={'OK' if res.crc_ok else 'NG'} {res.payload.hex()[:40]}"


def resolve_mac(session: Session, mac: str | None, timeout: float = 90) -> str | None:
    if mac:
        return mac.replace(":", "").upper()
    print(f"機器からのメッセージを待っています（最大 {timeout:.0f} 秒。--mac で省略できます）", file=sys.stderr)
    res = session.wait(lambda r: True, timeout)
    return res.mac if res else None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", nargs="?", default="status", choices=["devices", "status", "raw", "watch", "silent"])
    parser.add_argument("arg", nargs="?", help="devices・watch の秒数、silent の on / off")
    parser.add_argument("--host", default=os.environ.get("FOSSIBOT_MQTT_HOST"), help="ブローカーのホスト")
    parser.add_argument("--port", type=int, default=int(os.environ.get("FOSSIBOT_MQTT_PORT") or 1883))
    parser.add_argument("--mac", default=os.environ.get("FOSSIBOT_DEVICE_ID"), help="機器の MAC アドレス")
    parser.add_argument("--confirm", action="store_true", help="silent で実機の設定を変更することを確認する")
    args = parser.parse_args()
    if not args.host:
        parser.error("FOSSIBOT_MQTT_HOST か --host でブローカーを指定してください")
    if args.command == "silent" and (args.arg not in ("on", "off") or not args.confirm):
        parser.error("silent には on / off と --confirm を指定してください（実機の設定が変わります）")

    try:
        session = Session(args.host, args.port)
    except OSError as exc:
        print(f"エラー：ブローカーに接続できません（{args.host}:{args.port}）：{exc}", file=sys.stderr)
        return 1
    try:
        if args.command == "devices":
            seconds = float(args.arg or 90)
            print(f"{seconds:.0f} 秒間、届いたメッセージから機器を集めます（送信はしません）")
            seen: dict[str, Response] = {}
            deadline = time.monotonic() + seconds
            while (left := deadline - time.monotonic()) > 0:
                res = session.wait(lambda r: True, left)
                if res is None:
                    break
                if res.mac not in seen:
                    print(f"{res.mac}  （最初のメッセージ：{describe(res)}）", flush=True)
                seen[res.mac] = res
            if not seen:
                print("機器が見つかりません（アプリのローカルMQTT Broker設定・接続先を確認してください）")
            return 0

        if args.command == "watch":
            seconds = float(args.arg or 120)
            print(f"{seconds:.0f} 秒間、機器からのメッセージを表示します（送信はしません）")
            deadline = time.monotonic() + seconds
            while (left := deadline - time.monotonic()) > 0:
                res = session.wait(lambda r: True, left)
                if res is not None:
                    print(f"{datetime.now():%H:%M:%S} {res.mac} {describe(res)}", flush=True)
            return 0

        mac = resolve_mac(session, args.mac)
        if mac is None:
            print("エラー：機器からのメッセージが届きません", file=sys.stderr)
            return 1

        if args.command == "silent":
            want = args.arg == "on"
            before = _silent(session.read(mac, FUNC_READ_HOLDING))
            print(f"変更前：{_label(before)}")
            if before is None:
                print("エラー：変更前の値を取得できないため送信しません", file=sys.stderr)
                return 1
            if before == want:
                print("すでに指定の状態です（送信しません）")
                return 0
            session.send(mac, frame(FUNC_WRITE_SINGLE, HREG_SILENT_CHARGING, 1 if want else 0))
            ack = session.wait(lambda r: r.mac == mac and r.func == FUNC_WRITE_SINGLE, 10)
            print(f"書き込みの応答：{'あり ' + ack.payload.hex() if ack else 'なし（10秒）'}")
            # 送信後は読み直して照合する（照合できなければ成功扱いにしない）
            after = None
            for _ in range(3):
                time.sleep(2)
                after = _silent(session.read(mac, FUNC_READ_HOLDING))
                if after == want:
                    break
            print(f"変更後：{_label(after)}")
            if after != want:
                print("エラー：再取得した値が指定の状態と一致しません", file=sys.stderr)
                return 1
            return 0

        inputs = session.read(mac, FUNC_READ_INPUT)
        holdings = session.read(mac, FUNC_READ_HOLDING)
        if inputs is None and holdings is None:
            print("エラー：機器から応答がありません", file=sys.stderr)
            return 1
        if args.command == "raw":
            for name, regs in (("入力レジスタ", inputs), ("保持レジスタ", holdings)):
                print(f"{name}：" + ("取得できません" if regs is None else ""))
                for i, v in enumerate(regs or []):
                    if v:
                        print(f"  {i:3d}: {v:6d}  (0x{v:04x})")
            return 0
        print(f"機器：{mac}")
        print(f"残量：{_pct(_get(inputs, IREG_SOC))}  拡張1：{_slave(_get(inputs, IREG_SLAVE_1))}  拡張2：{_slave(_get(inputs, IREG_SLAVE_2))}")
        print(
            f"入力：{_w(_get(inputs, IREG_TOTAL_INPUT_W))}（AC {_w(_get(inputs, IREG_AC_INPUT_W))}・"
            f"DC {_w(_get(inputs, IREG_DC_INPUT_W))}・USB-C {_w(_get(inputs, IREG_TYPEC_INPUT_W))}）"
        )
        print(f"出力：{_w(_get(inputs, IREG_TOTAL_OUTPUT_W))}（AC {_w(_get(inputs, IREG_AC_OUTPUT_W))}）")
        print(f"AC 静音充電：{_label(_silent(holdings))}")
        print(f"放電の下限：{_pct(_get(holdings, HREG_DISCHARGE_MIN))}  充電の上限：{_pct(_get(holdings, HREG_CHARGE_MAX))}")
        return 0
    finally:
        session.close()


if __name__ == "__main__":
    sys.exit(main())
