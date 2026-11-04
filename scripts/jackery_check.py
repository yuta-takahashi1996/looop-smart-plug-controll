"""Jackery ポータブル電源の状態取得・充電モード変更の動作確認。

    uv run python scripts/jackery_check.py devices   # デバイス一覧（JACKERY_DEVICE_ID の確認用）
    uv run python scripts/jackery_check.py [status]  # 残量などの状態取得（省略時）
    uv run python scripts/jackery_check.py raw       # 取得した値をすべて表示（項目の調査用）
    uv run python scripts/jackery_check.py watch [秒数]  # MQTT の通知を表示する（送信しない。既定 120 秒）
    uv run python scripts/jackery_check.py charge-mode fast|silent --confirm
                                                     # 充電モードを変更する（実機の設定が変わる）

charge-mode 以外は読み取りのみ。charge-mode は DRY_RUN によらず送信するため、--confirm が必要。

取得専用のアカウント（JACKERY_ACCOUNT / JACKERY_PASSWORD）でログインする。
同じアカウントのスマホアプリはログアウトされるため、本アカウントは設定しないこと。
"""

import argparse
from datetime import datetime
import logging
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.clients.jackery import (  # noqa: E402
    ChargeMode,
    JackeryClient,
    JackeryError,
    PahoMqttTransport,
    describe_message,
)
from app.config import get_settings  # noqa: E402


def _fmt(value: object, unit: str = "") -> str:
    return "—" if value is None else f"{value}{unit}"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "command", nargs="?", default="status", choices=["devices", "device", "status", "raw", "watch", "charge-mode"]
    )
    parser.add_argument("mode", nargs="?", help="charge-mode の変更先（fast / silent）、watch の秒数")
    parser.add_argument("--confirm", action="store_true", help="charge-mode で実機の設定を変更することを確認する")
    parser.add_argument(
        "--body", help='charge-mode で送る中身を差し替える（原因調査用。cs と sfc=0 のみ。例：\'{"cs":"0"}\'）'
    )
    parser.add_argument("--mqtt311", action="store_true", help="MQTT 3.1.1 で接続する（比較用。コマンドは機器に届かない）")
    args = parser.parse_args()
    if args.command == "device":  # 打ち間違いを許容する
        args.command = "devices"
    if args.command == "charge-mode" and (args.mode not in ("fast", "silent") or not args.confirm):
        parser.error("charge-mode には fast / silent と --confirm を指定してください（実機の設定が変わります）")
    body = None
    if args.body is not None:
        try:
            body = json.loads(args.body)
        except ValueError:
            parser.error("--body は JSON で指定してください")
        # 充電モード以外の設定を変えないよう、送れる項目を制限する（超急速充電は無効のままにする値のみ）
        if not isinstance(body, dict) or "cs" not in body or set(body) - {"cs", "sfc"} or body.get("sfc", 0) != 0:
            parser.error("--body に指定できるのは cs と sfc=0 だけです")

    if args.command == "charge-mode":
        # 送信・機器からの通知・再取得の経過を表示する（原因調査用）
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
        logging.getLogger("httpx").setLevel(logging.WARNING)
    settings = get_settings()
    client = JackeryClient(
        settings.jackery_account, settings.jackery_password.get_secret_value(), mqtt=PahoMqttTransport(mqtt5=not args.mqtt311)
    )
    try:
        if args.command == "devices":
            devices = client.list_devices()
            if not devices:
                print("デバイスが見つかりません（共有が完了しているか確認してください）")
            for d in devices:
                print(f"{d.get('devId')}  {d.get('devName')}  model={d.get('productType') or d.get('devModel')}")
            return 0

        device_id = settings.jackery_device_id
        if not device_id:
            devices = client.list_devices()
            if len(devices) != 1:
                print("JACKERY_DEVICE_ID を設定してください（devices で確認できます）", file=sys.stderr)
                return 2
            device_id = str(devices[0].get("devId"))
        if args.command == "watch":
            seconds = float(args.mode or 120)
            sn = client.get_status(device_id).device_sn
            print(f"{seconds:.0f} 秒間、通知を表示します（送信はしません）。Jackery アプリで設定を変更してください")

            def show(topic: str, payload: bytes) -> None:
                try:
                    own = json.loads(payload).get("deviceSn") == sn
                except (ValueError, AttributeError):
                    own = False
                stamp = datetime.now().strftime("%H:%M:%S")
                print(f"{stamp} {topic.rsplit('/', 1)[-1]}{'' if own else '（他の機器）'} {describe_message(payload)}", flush=True)

            client.watch(seconds, show)
            status = client.get_status(device_id)
            print(f"終了時の HTTP の値：cs={status.raw.get('cs')} sfc={status.raw.get('sfc')}")
            return 0
        if args.command == "charge-mode":
            mode = ChargeMode.FAST if args.mode == "fast" else ChargeMode.SILENT
            before = client.get_status(device_id)
            print(f"変更前：{before.charge_mode.label if before.charge_mode else '不明'}（cs={before.raw.get('cs')}）")
            after = client.set_charge_mode(device_id, mode, body=body)
            print(f"変更後：{after.charge_mode.label if after.charge_mode else '不明'}（cs={after.raw.get('cs')}）")
            return 0
        status = client.get_status(device_id)
        if args.command == "raw":
            print(json.dumps(status.raw, ensure_ascii=False, indent=2, sort_keys=True))
            return 0
        print(f"残量：{_fmt(status.remaining_percent, '%')}")
        print(f"入力：{_fmt(status.input_power_w, ' W')}  出力：{_fmt(status.output_power_w, ' W')}")
        print(f"残り給電時間：{_fmt(status.remaining_output_hours, ' 時間')}  満充電まで：{_fmt(status.time_to_full_hours, ' 時間')}")
        print(f"電池温度：{_fmt(status.battery_temperature_c, ' ℃')}  AC出力：{_fmt(status.ac_output_on)}")
        mode = status.charge_mode.label if status.charge_mode else f"不明（cs={status.raw.get('cs')}）"
        print(f"充電モード：{mode}  超急速充電：{_fmt(status.raw.get('sfc'))}")
        return 0
    except JackeryError as exc:
        print(f"エラー：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
