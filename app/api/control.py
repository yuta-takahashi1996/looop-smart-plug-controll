"""手動操作と実制御の切り替え（docs/05-web-ui.md §5.8、docs/14-safety.md §14.3）。

状態変更はすべて Control Service（app.state.control）を経由する。プラグの手動操作と自動制御の停止は
MANUAL モードへ移行し、自動制御で上書きしない。安全制御は MANUAL 中も継続する。
"""

from datetime import UTC, datetime
from typing import Annotated, Literal
from urllib.parse import quote

from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Form, Request, status
from fastapi.responses import RedirectResponse, Response
from sqlalchemy.orm import Session

from app.api.auth import verify_csrf
from app.api.dashboard import status_context
from app.clients.jackery import ChargeMode
from app.config import get_settings
from app.models.schedule import PlugState
from app.repositories.database import get_session
from app.templating import templates

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


@router.post("/control/charge-mode/{mode}", name="charge_mode", dependencies=[Depends(verify_csrf)])
def charge_mode(request: Request, mode: Literal["fast", "silent"]) -> RedirectResponse:
    """ポータブル電源の充電モード（高速充電／静かな充電）の手動変更。超急速充電は扱わない（docs/07 §7.10）。"""
    control = request.app.state.control
    target = ChargeMode.FAST if mode == "fast" else ChargeMode.SILENT
    try:
        change = control.manual_charge_mode(target)
    except ValueError as exc:
        return _back("/", str(exc), error=True)
    if change.error is not None:
        return _back("/", f"充電モードを{target.label}にできませんでした：{change.error}", error=True)
    suffix = "（Dry Run：コマンドは送信していません）" if change.result.value == "DRY_RUN" else ""
    return _back("/", f"充電モードを{target.label}にしました{suffix}")


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
def refresh_status(
    request: Request, target: Literal["plug", "battery"], session: Session = Depends(get_session)
) -> Response:
    """プラグ・ポータブル電源の状態を今すぐ取得する（読み取りのみ。制御はしない）。

    HTMX からの要求では、ダッシュボードの状態表示（部分テンプレート）だけを返して差し替える。
    """
    if target == "plug":
        snapshot = request.app.state.plug_monitor.refresh()
        ok = snapshot.status is not None
        message = (
            f"{snapshot.checked_at.astimezone(_tz()):%H:%M:%S} に取得しました" if ok else f"取得できませんでした：{snapshot.error}"
        )
    else:
        battery = request.app.state.control.battery
        reading = battery.read(datetime.now(UTC))
        ok = reading is not None
        message = f"{reading.at.astimezone(_tz()):%H:%M:%S} に取得しました" if ok else f"取得できませんでした：{battery.last_error}"

    if request.headers.get("HX-Request"):
        return templates.TemplateResponse(
            request,
            "components/dashboard_status.html",
            {**status_context(request, session), "refresh_target": target, "refresh_ok": ok, "refresh_message": message},
        )
    label = "プラグの状態" if target == "plug" else "ポータブル電源の状態"
    return _back("/", f"{label}を{message}", error=not ok)


def _tz() -> ZoneInfo:
    return ZoneInfo(get_settings().tz)
