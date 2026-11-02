"""SwitchBot の接続・状態取得・ON/OFF の動作確認（手動実行専用）。

    uv run python scripts/switchbot_check.py devices                 # デバイス一覧（SWITCHBOT_DEVICE_ID の確認用）
    uv run python scripts/switchbot_check.py [status]                # プラグの状態取得（読み取りのみ。省略時）
    uv run python scripts/switchbot_check.py verify-onoff --confirm  # OFF → 状態確認 → ON → 状態確認

verify-onoff は実際にプラグを切り替えるため、--confirm を付けたときだけ実行する。
docs/08 §8.3 の手順（操作前の状態取得 → コマンド送信 → 状態の再取得 → 期待状態との照合）で行い、
途中で失敗しても最後に必ず ON への復帰を試みる。DRY_RUN の設定に関わらず実機を操作する。
"""

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.clients.switchbot import PowerState, SwitchBotClient, SwitchBotError  # noqa: E402
from app.config import get_settings  # noqa: E402

# コマンド送信後、状態が反映されるまで待つ時間と確認回数
VERIFY_WAIT_SECONDS = 3
VERIFY_ATTEMPTS = 5


def _client() -> SwitchBotClient:
    settings = get_settings()
    return SwitchBotClient(
        settings.switchbot_token.get_secret_value(),
        settings.switchbot_secret.get_secret_value(),
        settings.switchbot_device_id,
    )


def _set_and_verify(client: SwitchBotClient, target: PowerState) -> bool:
    print(f"→ {target} コマンドを送信します")
    client.send_power_command(target)
    for attempt in range(1, VERIFY_ATTEMPTS + 1):
        time.sleep(VERIFY_WAIT_SECONDS)
        actual = client.get_status().power
        print(f"  状態確認 {attempt}/{VERIFY_ATTEMPTS}：{actual}")
        if actual == target:
            print(f"  ✓ {target} を確認しました")
            return True
    print(f"  ✗ {target} を確認できませんでした（成功扱いにしません）")
    return False


def cmd_devices(client: SwitchBotClient) -> int:
    devices = client.list_devices()
    if not devices:
        print("デバイスが見つかりません")
    for device in devices:
        print(f"{device.get('deviceId')}  {device.get('deviceType')}  {device.get('deviceName')}")
    return 0


def cmd_status(client: SwitchBotClient) -> int:
    status = client.get_status()
    print(f"デバイス種別：{status.device_type}")
    print(f"電源状態：{status.power}")
    return 0


def cmd_verify_onoff(client: SwitchBotClient, confirmed: bool) -> int:
    if not confirmed:
        print("プラグを実際に OFF → ON します。実行するには --confirm を付けてください。", file=sys.stderr)
        return 2

    before = client.get_status().power
    print(f"操作前の状態：{before}")
    ok = False
    try:
        ok = _set_and_verify(client, "OFF") and _set_and_verify(client, "ON")
    finally:
        # 途中で失敗しても ON（商用電源側）へ戻す
        try:
            if client.get_status().power != "ON":
                print("ON へ復帰させます")
                _set_and_verify(client, "ON")
        except SwitchBotError as exc:
            print(f"ON への復帰を確認できませんでした：{exc}。SwitchBot アプリで状態を確認してください。", file=sys.stderr)
            ok = False
    print("結果：成功" if ok else "結果：失敗")
    return 0 if ok else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "command", nargs="?", default="status", choices=["devices", "status", "verify-onoff"], help="省略時は status"
    )
    parser.add_argument("--confirm", action="store_true", help="verify-onoff で実際にプラグを操作する")
    args = parser.parse_args()

    client = _client()
    try:
        if args.command == "devices":
            return cmd_devices(client)
        if args.command == "status":
            return cmd_status(client)
        return cmd_verify_onoff(client, args.confirm)
    except SwitchBotError as exc:
        print(f"エラー：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
