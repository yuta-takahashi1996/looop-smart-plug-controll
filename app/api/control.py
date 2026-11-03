"""手動操作と実制御の切り替え（docs/05-web-ui.md §5.8、docs/14-safety.md §14.3）。

状態変更はすべて Control Service（app.state.control）を経由する。プラグの手動操作と自動制御の停止は
MANUAL モードへ移行し、自動制御で上書きしない。安全制御は MANUAL 中も継続する。
"""

from datetime import UTC, datetime
from typing import Annotated, Literal
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, Request, status
from fastapi.responses import RedirectResponse

from app.api.auth import verify_csrf
from app.models.schedule import PlugState

router = APIRouter()

# 実制御を有効にするときに入力してもらう確認文言
REAL_CONTROL_CONFIRMATION = "実制御を開始"


def _back(path: str, message: str, *, error: bool = False) -> RedirectResponse:
    key = "error" if error else "message"
    return RedirectResponse(f"{path}?{key}={quote(message)}", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/control/plug/{target}", name="manual_power", dependencies=[Depends(verify_csrf)])
def manual_power(request: Request, target: Literal["on", "off"]) -> RedirectResponse:
    """プラグの手動 ON/OFF。MANUAL モードへ移行する。"""
    control = request.app.state.control
    try:
        log = control.manual_power(PlugState.ON if target == "on" else PlugState.OFF)
    except ValueError as exc:
        return _back("/", f"OFF にできません：{exc}", error=True)
    if log.result.value in ("FAILED", "UNCONFIRMED"):
        return _back("/", f"プラグを {log.requested_state} にできませんでした：{log.error_message}", error=True)
    suffix = "（Dry Run：コマンドは送信していません）" if log.dry_run else ""
    return _back("/", f"プラグを {log.requested_state} にしました。自動制御を停止しています{suffix}")


@router.post("/control/auto/{action}", name="auto_control", dependencies=[Depends(verify_csrf)])
def auto_control(request: Request, action: Literal["start", "stop"]) -> RedirectResponse:
    control = request.app.state.control
    if action == "start":
        control.start_auto()
        return _back("/", "自動制御を再開しました。次の制御サイクル（1分以内）からスケジュールに従います")
    control.stop_auto()
    return _back("/", "自動制御を停止しました（プラグの状態は変えていません）")


@router.post("/control/real-mode", name="real_mode", dependencies=[Depends(verify_csrf)])
def real_mode(
    request: Request,
    enabled: Annotated[Literal["true", "false"], Form()],
    confirmation: Annotated[str, Form()] = "",
) -> RedirectResponse:
    """実制御の有効・無効。有効にするには DRY_RUN=false と確認文言の入力が必要。Dry Run へはいつでも戻せる。"""
    control = request.app.state.control
    if enabled == "false":
        control.set_real_control(False)
        return _back("/settings", "Dry Run に戻しました。SwitchBot へのコマンドは送信しません")
    if confirmation.strip() != REAL_CONTROL_CONFIRMATION:
        return _back("/settings", f"確認のため「{REAL_CONTROL_CONFIRMATION}」と入力してください", error=True)
    try:
        control.set_real_control(True)
    except ValueError as exc:
        return _back("/settings", str(exc), error=True)
    return _back("/settings", "実制御を開始しました。次の制御サイクルから SwitchBot へコマンドを送信します")


@router.post("/control/refresh/{target}", name="refresh_status", dependencies=[Depends(verify_csrf)])
def refresh_status(request: Request, target: Literal["plug", "battery"]) -> RedirectResponse:
    """プラグ・ポータブル電源の状態を今すぐ取得する（読み取りのみ。制御はしない）。"""
    if target == "plug":
        snapshot = request.app.state.plug_monitor.refresh()
        if snapshot.status is None:
            return _back("/", f"プラグの状態を取得できませんでした：{snapshot.error}", error=True)
        return _back("/", f"プラグの状態を取得しました（{snapshot.status.power}）")
    battery = request.app.state.control.battery
    reading = battery.read(datetime.now(UTC))
    if reading is None:
        return _back("/", f"ポータブル電源の状態を取得できませんでした：{battery.last_error}", error=True)
    return _back("/", f"ポータブル電源の状態を取得しました（残量 {reading.percent}%・消費 {reading.output_w:.0f}W）")
