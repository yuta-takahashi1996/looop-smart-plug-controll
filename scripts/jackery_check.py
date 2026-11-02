"""Jackery ポータブル電源の状態取得の動作確認（読み取りのみ）。

    uv run python scripts/jackery_check.py devices   # デバイス一覧（JACKERY_DEVICE_ID の確認用）
    uv run python scripts/jackery_check.py [status]  # 残量などの状態取得（省略時）
    uv run python scripts/jackery_check.py raw       # 取得した値をすべて表示（項目の調査用）

取得専用のアカウント（JACKERY_ACCOUNT / JACKERY_PASSWORD）でログインする。
同じアカウントのスマホアプリはログアウトされるため、本アカウントは設定しないこと。
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.clients.jackery import JackeryClient, JackeryError  # noqa: E402
from app.config import get_settings  # noqa: E402


def _fmt(value: object, unit: str = "") -> str:
    return "—" if value is None else f"{value}{unit}"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", nargs="?", default="status", choices=["devices", "device", "status", "raw"])
    args = parser.parse_args()
    if args.command == "device":  # 打ち間違いを許容する
        args.command = "devices"

    settings = get_settings()
    client = JackeryClient(settings.jackery_account, settings.jackery_password.get_secret_value())
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
        status = client.get_status(device_id)
        if args.command == "raw":
            print(json.dumps(status.raw, ensure_ascii=False, indent=2, sort_keys=True))
            return 0
        print(f"残量：{_fmt(status.remaining_percent, '%')}")
        print(f"入力：{_fmt(status.input_power_w, ' W')}  出力：{_fmt(status.output_power_w, ' W')}")
        print(f"残り給電時間：{_fmt(status.remaining_output_hours, ' 時間')}  満充電まで：{_fmt(status.time_to_full_hours, ' 時間')}")
        print(f"電池温度：{_fmt(status.battery_temperature_c, ' ℃')}  AC出力：{_fmt(status.ac_output_on)}")
        return 0
    except JackeryError as exc:
        print(f"エラー：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
