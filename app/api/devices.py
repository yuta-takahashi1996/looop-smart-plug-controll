"""制御機器画面（/devices、docs/17-multi-device.md §17.5）。

左に SwitchBot API で取得した機器の一覧、右に制御対象（devices の active=true の制御機器）を表示し、
「保存」で制御対象を確定する。機器一覧は API の呼び出し上限（1日10,000回）のため、未取得か24時間以上前の
場合と「再取得」の操作時だけ取得し、メモリに保持する（DB には制御対象の機器だけを保存する）。
制御対象から外すときは docs/17 §17.5.3 の検証を行い、開始前のスケジュール・ルール・でんき予報の対象機器から外す。
"""

import threading
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo
from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, Depends, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from pydantic import BaseModel, Field, ValidationError, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.auth import CSRF_FORM_FIELD, verify_csrf
from app.clients.switchbot import SwitchBotClient, SwitchBotError
from app.config import get_settings
from app.models import device as device_models
from app.models.device import Device, SwitchBotDevice, Vendor
from app.models.schedule import (
    ControlResult,
    ControlSchedule,
    ControlSource,
    PlugState,
    ScheduleDeviceStatus,
    ScheduleRule,
    ScheduleStatus,
)
from app.repositories.database import get_session
from app.services import optimizer
from app.templating import templates

router = APIRouter()

LIST_MAX_AGE = timedelta(hours=24)


class DeviceListCache:
    """SwitchBot の機器一覧（GET /devices の deviceList）のメモリ上の保持。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.items: list[dict[str, Any]] | None = None
        self.fetched_at: datetime | None = None
        self.error: str | None = None

    def get(self, client: SwitchBotClient, *, force: bool = False, now: datetime | None = None) -> list[dict[str, Any]] | None:
        """一覧を返す。未取得・24時間以上前・force のときだけ API を呼ぶ。取得できなければ None（error に理由）。"""
        now = now or datetime.now(UTC)
        with self._lock:
            stale = self.fetched_at is None or now - self.fetched_at >= LIST_MAX_AGE
            if not (force or stale or self.items is None):
                return self.items
            try:
                self.items = client.list_devices()
                self.error = None
            except SwitchBotError as exc:
                self.items, self.error = None, str(exc)
            self.fetched_at = now
            return self.items


@dataclass(frozen=True)
class ActiveRow:
    device: Device
    listed: SwitchBotDevice | None  # 一覧の該当機器（一覧を取得できない・見つからない場合は None）
    state: PlugState | None  # 制御上の状態（Dry Run では想定の状態）
    confirm: str  # 外すときの確認事項（docs/17 §17.5.3）


def _back(message: str, *, error: bool = False) -> RedirectResponse:
    key = "error" if error else "message"
    return RedirectResponse(f"/devices?{key}={quote(message)}", status_code=status.HTTP_303_SEE_OTHER)


def _listing(request: Request, session: Session, *, force: bool = False) -> tuple[list[SwitchBotDevice] | None, str | None]:
    client: SwitchBotClient = request.app.state.switchbot
    cache: DeviceListCache = request.app.state.switchbot_devices
    if not client.credentials_configured:  # 認証情報が未設定なら API を呼ばない
        return None, "SWITCHBOT_TOKEN / SWITCHBOT_SECRET が未設定です（環境変数で設定してください）"
    items = cache.get(client, force=force)
    if items is None:
        return None, cache.error
    return device_models.ensure_switchbot_types(session, items), None


def running_with(session: Session, device_id: int) -> list[ControlSchedule]:
    """機器を対象に含む実行中のスケジュール（その機器がまだ終わっていないもの）。"""
    running = session.scalars(select(ControlSchedule).where(ControlSchedule.status == ScheduleStatus.RUNNING))
    return [
        s
        for s in running
        if (sd := s.device(device_id)) is not None
        and sd.status in (ScheduleDeviceStatus.PENDING, ScheduleDeviceStatus.RUNNING, ScheduleDeviceStatus.RETURNED)
    ]


def _pending_with(session: Session, device_id: int) -> list[ControlSchedule]:
    """機器を対象に含む開始前のスケジュール。"""
    now = datetime.now(UTC)
    pending = session.scalars(
        select(ControlSchedule).where(ControlSchedule.status == ScheduleStatus.PENDING, ControlSchedule.start_at > now)
    )
    return [s for s in pending if device_id in s.device_ids]


def _rules_with(session: Session, device_id: int) -> list[ScheduleRule]:
    return [r for r in session.scalars(select(ScheduleRule)) if device_id in r.device_ids]


def _confirm_text(session: Session, device: Device, state: PlugState | None, forecast_ids: set[int]) -> str:
    parts = []
    if state == PlugState.OFF:
        parts.append("OFF 中のため ON に戻してから外します")
    pending, rules = len(_pending_with(session, device.id)), len(_rules_with(session, device.id))
    if pending:
        parts.append(f"開始前のスケジュール {pending}件から外します")
    if rules:
        parts.append(f"任意スケジュールのルール {rules}件から外します")
    if device.id in forecast_ids:
        parts.append("でんき予報のスケジュールの対象機器から外します")
    return "、".join(parts)


def _render(
    request: Request,
    session: Session,
    *,
    message: str | None = None,
    errors: list[str] | None = None,
    edit_errors: dict[int, list[str]] | None = None,
    status_code: int = status.HTTP_200_OK,
) -> HTMLResponse:
    listing, list_error = _listing(request, session)
    control = request.app.state.control
    switches = device_models.active_switches(session)
    by_external = {d.external_id: d for d in listing or []}
    active_ids = {d.external_id for d in switches}
    forecast_ids = set(device_models.forecast_schedule_device_ids(session))
    candidates = [d for d in listing or [] if d.external_id not in active_ids]
    context = {
        # 左ペインには ON／OFF に対応する種別だけを表示し、非対応の機器は件数と折りたたみの一覧にする（docs/17 §17.5.1）
        "available": [d for d in candidates if d.device_type.power_control],
        "unsupported": [d for d in candidates if not d.device_type.power_control],
        "active_rows": [
            ActiveRow(
                d,
                by_external.get(d.external_id),
                control.device_state(d.id),
                _confirm_text(session, d, control.device_state(d.id), forecast_ids),
            )
            for d in switches
        ],
        "listed": listing is not None,
        "list_error": list_error,
        "fetched_at": request.app.state.switchbot_devices.fetched_at,
        "portable_powers": device_models.portable_powers(session),
        "max_devices": get_settings().max_devices_per_schedule,
        "message": message,
        "errors": errors or [],
        "edit_errors": edit_errors or {},
    }
    return templates.TemplateResponse(request, "devices.html", context, status_code=status_code)


@router.get("/devices", response_class=HTMLResponse, name="devices")
def devices_page(
    request: Request, message: str | None = None, error: str | None = None, session: Session = Depends(get_session)
) -> HTMLResponse:
    return _render(request, session, message=message, errors=[error] if error else None)


@router.post("/devices/refresh", name="refresh_devices", dependencies=[Depends(verify_csrf)])
def refresh_devices(request: Request, session: Session = Depends(get_session)) -> RedirectResponse:
    listing, error = _listing(request, session, force=True)
    if listing is None:
        return _back(f"機器の一覧を取得できませんでした：{error}", error=True)
    return _back(f"機器の一覧を取得しました（{len(listing)}台）")


@router.post("/devices", name="save_devices", dependencies=[Depends(verify_csrf)])
async def save_devices(request: Request, session: Session = Depends(get_session)) -> Response:
    """制御対象を保存する。外す機器は docs/17 §17.5.3 の検証を行う（実行中は外せない、OFF 中は ON に戻してから外す）。"""
    form = await request.form()
    requested = [str(v) for v in form.getlist("active") if str(v)]
    control = request.app.state.control
    current = {d.external_id: d for d in device_models.active_switches(session)}
    added = [ext for ext in dict.fromkeys(requested) if ext not in current]
    removed = [d for ext, d in current.items() if ext not in requested]

    errors: list[str] = []
    listing: list[SwitchBotDevice] | None = None
    if added:
        listing, list_error = _listing(request, session)
        if listing is None:
            errors.append(f"機器の一覧を取得できないため、制御対象を追加できません：{list_error}")
    by_external = {d.external_id: d for d in listing or []}
    for ext in added if listing is not None else []:
        item = by_external.get(ext)
        if item is None:
            errors.append(f"SwitchBot の機器一覧に見つからない機器です（{ext}）。再取得してください")
        elif not item.device_type.power_control:
            errors.append(f"{item.name}：ON／OFF の制御に対応していない種別です（{item.type_code}）")
        elif not item.cloud_enabled:
            errors.append(f"{item.name}：クラウドサービスが無効です（SwitchBot アプリで有効にしてください）")

    for device in removed:
        if running_with(session, device.id):
            errors.append(f"{device.name}：実行中のスケジュールが終わってから外してください")
    if errors:
        return _render(request, session, errors=errors, status_code=status.HTTP_422_UNPROCESSABLE_CONTENT)

    # 本システムが OFF にしている機器は ON に戻してから外す（ON にできなければ外さない）
    control.bind_devices(session)
    for device in removed:
        if control.device_state(device.id) != PlugState.OFF:
            continue
        log = control.request(
            session, PlugState.ON, ControlSource.MANUAL, f"制御対象から外すため ON に戻す（{device.name}）", device_id=device.id
        )
        if log.result not in (ControlResult.SUCCESS, ControlResult.DRY_RUN):
            return _render(
                request,
                session,
                errors=[f"{device.name}：ON に戻せなかったため外しませんでした（{log.error_message}）"],
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            )

    now = datetime.now(UTC)
    forecast_ids = device_models.forecast_schedule_device_ids(session)
    for device in removed:
        device.active = False
        device.updated_at = now
        # 開始前のスケジュール・ルールから外す。0台になったスケジュールは未実行、ルールは無効にする
        for schedule in _pending_with(session, device.id):
            schedule.devices = [sd for sd in schedule.devices if sd.device_id != device.id]
            if not schedule.devices:
                schedule.status, schedule.status_reason = ScheduleStatus.SKIPPED, "未実行（対象機器がありません）"
            schedule.updated_at = now
        for rule in _rules_with(session, device.id):
            rule.set_devices([i for i in rule.device_ids if i != device.id])
            if not rule.devices:
                rule.enabled = False
            rule.updated_at = now
    for ext in added:
        item = by_external[ext]
        device = device_models.find_device(session, Vendor.SWITCHBOT, ext)
        if device is None:
            # 名前の初期値は SwitchBot アプリでの名前。給電先は未設定、想定平均負荷は空欄（全体の設定値）
            device = Device(device_type_id=item.device_type.id, external_id=ext, name=item.name[:50], created_at=now)
            session.add(device)
        device.device_type_id = item.device_type.id
        device.active = True
        device.sort_order = device_models.next_sort_order(session)
        device.updated_at = now
    session.commit()
    removed_ids = {d.id for d in removed}
    if removed_ids & set(forecast_ids):
        device_models.set_forecast_schedule_device_ids(session, [i for i in forecast_ids if i not in removed_ids])

    if removed:
        # 手動で変更していない開始前のスケジュールに、対象機器の変更を反映する（推定節約額を含む）
        settings = get_settings()
        today = datetime.now(ZoneInfo(settings.tz)).date()
        optimizer.regenerate_days(session, settings, [today, today + timedelta(days=1)])
    control.bind_devices(session)
    for device in removed:
        request.app.state.plug_monitor.reset(device.id)
    parts = [f"追加 {len(added)}台"] if added else []
    parts += [f"外した機器 {len(removed)}台"] if removed else []
    return _back(f"制御対象を保存しました（{'・'.join(parts) or '変更なし'}）")


class DeviceEdit(BaseModel):
    name: str = Field(min_length=1, max_length=50)
    note: str | None = Field(default=None, max_length=200)
    power_source_id: int | None = None
    estimated_power_w: float | None = Field(default=None, ge=0)

    @field_validator("note", "power_source_id", "estimated_power_w", mode="before")
    @classmethod
    def _empty_to_none(cls, value: object) -> object:
        return None if isinstance(value, str) and not value.strip() else value


_EDIT_LABELS = {"name": "名前", "note": "説明", "power_source_id": "給電先", "estimated_power_w": "想定平均負荷"}


@router.post("/devices/{device_id}", name="edit_device", dependencies=[Depends(verify_csrf)])
async def edit_device(device_id: int, request: Request, session: Session = Depends(get_session)) -> Response:
    """制御対象の名前・説明・給電先のポータブル電源・想定平均負荷を変更する（docs/17 §17.5.1）。"""
    device = session.get(Device, device_id)
    if device is None or not device.active:
        return _back("制御対象の機器が見つかりません", error=True)
    form = {k: v.strip() for k, v in (await request.form()).items() if isinstance(v, str)}
    form.pop(CSRF_FORM_FIELD, None)
    errors: list[str] = []
    try:
        data = DeviceEdit.model_validate({key: form.get(key, "") for key in _EDIT_LABELS})
    except ValidationError as exc:
        for error in exc.errors():
            field = error["loc"][0] if error["loc"] else None
            errors.append(f"{_EDIT_LABELS.get(str(field), '')}：{error['msg'].removeprefix('Value error, ')}")
        data = None

    control = request.app.state.control
    if data is not None:
        if data.power_source_id is not None and data.power_source_id not in {p.id for p in device_models.portable_powers(session)}:
            errors.append("給電先：登録済みのポータブル電源から選んでください")
        changing_source = data.power_source_id != device.power_source_id
        if changing_source and running_with(session, device.id):
            errors.append("給電先：実行中のスケジュールが終わってから変更してください")
    if errors or data is None:
        return _render(request, session, edit_errors={device.id: errors}, status_code=status.HTTP_422_UNPROCESSABLE_CONTENT)

    device.name = data.name
    device.note = data.note
    device.power_source_id = data.power_source_id
    device.estimated_power_w = data.estimated_power_w
    device.updated_at = datetime.now(UTC)
    session.commit()
    control.bind_devices(session)
    return _back(f"{device.name} を保存しました")
