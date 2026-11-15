from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any
from urllib.parse import quote
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.auth import CSRF_FORM_FIELD, verify_csrf
from app.api.control import REAL_CONTROL_CONFIRMATION
from app.api.devices import running_with
from app.clients.portable_power import PortablePowerClient, PortablePowerError
from app.config import get_settings
from app.models import device as device_models
from app.models.device import Device, PortablePowerSettings, Vendor
from app.models.forecast import AdjustmentCategory, PriceAdjustment
from app.models.schedule import ChargeModeOption, PlugState
from app.models.settings import ControlSettings, load_control_settings, save_control_settings
from app.repositories.database import get_session
from app.services import forecast as forecast_service
from app.services import maintenance, optimizer
from app.templating import templates

router = APIRouter()

# 画面に表示する項目（docs/05-web-ui.md §5.7）。グループごとに (キー, ラベル, 単位, input 属性, 説明)
GROUPS: list[tuple[str, str, list[tuple[str, str, str, dict[str, Any], str]]]] = [
    (
        "自動 OFF を許可する時間帯",
        "でんき注意報と任意 OFF のスケジュールは、この時間帯の中でだけ実行します。でんき警報は残量を取得できる間は時間外も実行し、取得できない間は時間外に ON に戻します。",
        [
            ("operation_start", "許可開始時刻", "", {"type": "time"}, ""),
            ("operation_end", "許可終了時刻", "", {"type": "time"}, "この時刻を過ぎると ON に戻します。"),
        ],
    ),
    (
        "OFF 時間の制限",
        "蓄電池を使い切らないための上限です。でんき注意報には常に、でんき警報には残量を取得できない間だけ適用します。",
        [
            ("max_continuous_off_minutes", "最大連続OFF時間", "分", {"type": "number", "min": 0, "step": 1}, "1回の OFF の上限。"),
            ("max_daily_off_minutes", "1日の最大OFF時間", "分", {"type": "number", "min": 0, "step": 1}, "警報の OFF も含めた1日の合計の上限。"),
            (
                "min_switch_interval_minutes",
                "最低切り替え間隔",
                "分",
                {"type": "number", "min": 0, "step": 1},
                "短い間隔で ON/OFF を繰り返さないための、スケジュール同士の最小の間隔。",
            ),
        ],
    ),
    (
        "ポータブル電源の初期値",
        "ポータブル電源を追加するときの初期値です。登録済みのポータブル電源は、下の「ポータブル電源」でそれぞれの値を設定します。",
        [
            ("battery_min_percent", "残量の下限", "%", {"type": "number", "min": 0, "max": 100, "step": 1}, ""),
            ("battery_capacity_wh", "容量", "Wh", {"type": "number", "min": 1, "step": 1}, "種別の公称容量がない場合に使います。"),
            (
                "round_trip_efficiency",
                "変換効率",
                "%",
                {"type": "number", "min": 1, "max": 100, "step": 1},
                "給電先が未設定の機器の推定節約額と、ダッシュボードの蓄電池利用時の実効単価にも使います。",
            ),
        ],
    ),
    (
        "推定節約額",
        "画面に表示する推定節約額の計算に使います（制御の判断には使いません）。",
        [
            (
                "estimated_average_power_w",
                "想定平均負荷",
                "W",
                {"type": "number", "min": 0, "step": 1},
                "OFF 中に蓄電池から給電する家電の平均消費電力。",
            ),
        ],
    ),
]
# 選択肢で入力する項目（テンプレートで個別に表示する）
CHARGE_MODE_FIELDS = [
    ("charge_mode_start", "開始時"),
    ("charge_mode_end", "終了時"),
]
CHARGE_MODE_CHOICES = [(option.value, option.label) for option in ChargeModeOption]
FIELDS: list[tuple[str, str, str, dict[str, Any]]] = [
    (key, label, unit, attrs) for _, _, fields in GROUPS for key, label, unit, attrs, _ in fields
]
_LABELS = {key: label for key, label, _, _ in FIELDS} | {key: f"充電モード（{label}）" for key, label in CHARGE_MODE_FIELDS}


def _to_form_values(data: ControlSettings) -> dict[str, str]:
    """画面表示用の文字列へ変換する（往復効率は％で表示する）。"""
    return {
        "auto_enable": "on" if data.auto_enable else "",
        "operation_start": data.operation_start.strftime("%H:%M"),
        "operation_end": data.operation_end.strftime("%H:%M"),
        "off_all_day": "on" if data.off_all_day else "",
        "round_trip_efficiency": f"{data.round_trip_efficiency * 100:g}",
        "max_continuous_off_minutes": str(data.max_continuous_off_minutes),
        "max_daily_off_minutes": str(data.max_daily_off_minutes),
        "min_switch_interval_minutes": str(data.min_switch_interval_minutes),
        "estimated_average_power_w": f"{data.estimated_average_power_w:g}",
        "battery_min_percent": str(data.battery_min_percent),
        "battery_capacity_wh": f"{data.battery_capacity_wh:g}",
        "charge_mode_start": data.charge_mode_start.value,
        "charge_mode_end": data.charge_mode_end.value,
    }


def _parse_form(form: dict[str, str]) -> ControlSettings:
    values: dict[str, Any] = {key: form.get(key, "").strip() for key in _LABELS}
    # チェックボックスは未チェックだと送信されない
    values["auto_enable"] = form.get("auto_enable") == "on"
    values["off_all_day"] = form.get("off_all_day") == "on"
    try:
        values["round_trip_efficiency"] = float(values["round_trip_efficiency"]) / 100  # 画面では％で入力する
    except ValueError:
        pass  # 数値でない場合は ControlSettings の検証でエラーにする
    return ControlSettings.model_validate(values)


def _error_messages(exc: ValidationError) -> list[str]:
    messages = []
    for error in exc.errors():
        field = error["loc"][0] if error["loc"] else None
        message = error["msg"].removeprefix("Value error, ")
        messages.append(f"{_LABELS[field]}：{message}" if field in _LABELS else message)
    return messages


def _adjustments_context(session: Session) -> dict[str, Any]:
    settings = get_settings()
    today = datetime.now(ZoneInfo(settings.tz)).date()
    active = forecast_service.adjustments_for(session, today)
    return {
        "adjustments": list(
            session.scalars(
                select(PriceAdjustment).order_by(PriceAdjustment.effective_from.desc(), PriceAdjustment.category)
            )
        ),
        "active_adjustment_ids": {a.id for a in active},
        "adjustment_total_today": round(sum(a.amount for a in active), 2),
        "today": today,
        "categories": list(AdjustmentCategory),
    }


def _render(
    request: Request,
    session: Session,
    values: dict[str, str],
    *,
    saved: bool = False,
    errors: list[str] | None = None,
    adjustment_form: dict[str, str] | None = None,
    adjustment_errors: list[str] | None = None,
    flash_message: str | None = None,
    flash_error: str | None = None,
    status_code: int = status.HTTP_200_OK,
    forecast_device_ids: list[int] | None = None,
) -> HTMLResponse:
    settings = get_settings()
    control = load_control_settings(session, settings)
    context: dict[str, Any] = {
        "switches": device_models.active_switches(session),
        "forecast_device_ids": (
            device_models.forecast_schedule_device_ids(session) if forecast_device_ids is None else forecast_device_ids
        ),
        "max_devices": settings.max_devices_per_schedule,
        "groups": GROUPS,
        "charge_mode_fields": CHARGE_MODE_FIELDS,
        "charge_mode_choices": CHARGE_MODE_CHOICES,
        "values": values,
        "saved": saved,
        "errors": errors or [],
        "hints": maintenance.tuning_hints(
            session, settings, control.battery_capacity_wh, datetime.now(ZoneInfo(settings.tz)).date()
        ),
    }
    context |= _portable_power_context(request, session, values)
    # HTMX からの送信ではフォーム部分のみ返す
    if request.headers.get("HX-Request"):
        template = "components/settings_form.html"
    else:
        template = "settings.html"
        context |= _adjustments_context(session)
        context["adjustment_form"] = adjustment_form or {}
        context["adjustment_errors"] = adjustment_errors or []
        context["flash_message"] = flash_message
        context["flash_error"] = flash_error
        context["can_enable_real_control"] = request.app.state.control.can_enable_real_control
        context["real_control_confirmation"] = REAL_CONTROL_CONFIRMATION
    return templates.TemplateResponse(request, template, context, status_code=status_code)


def _current_values(session: Session) -> dict[str, str]:
    return _to_form_values(load_control_settings(session, get_settings()))


@router.get("/settings", response_class=HTMLResponse, name="settings")
def settings_page(
    request: Request,
    message: str | None = None,
    error: str | None = None,
    session: Session = Depends(get_session),
) -> HTMLResponse:
    return _render(request, session, _current_values(session), flash_message=message, flash_error=error)


@router.post("/settings", response_class=HTMLResponse, dependencies=[Depends(verify_csrf)])
async def update_settings(request: Request, session: Session = Depends(get_session)) -> HTMLResponse:
    raw = await request.form()
    form = {key: value for key, value in raw.items() if isinstance(value, str)}
    form.pop(CSRF_FORM_FIELD, None)
    # 充電モードの既定値は「ポータブル電源」の欄で保存する（docs/17 §17.8.2）。この画面の送信では現在の値を引き継ぐ
    current = load_control_settings(session, get_settings())
    for key, _ in CHARGE_MODE_FIELDS:
        form.setdefault(key, getattr(current, key).value)
    # でんき予報のスケジュールの対象機器（docs/17 §17.6.1）。0台なら、でんき予報のスケジュールを作らない
    device_ids = list(dict.fromkeys(int(v) for v in raw.getlist("forecast_device_ids") if isinstance(v, str) and v.isdigit()))
    device_errors = optimizer.validate_device_ids(session, get_settings(), device_ids) if device_ids else []
    try:
        data = _parse_form(form)
    except ValidationError as exc:
        data, errors = None, _error_messages(exc)
    else:
        errors = []
    errors += [f"でんき予報のスケジュールの対象機器：{e}" for e in device_errors]
    if data is None or errors:
        return _render(
            request,
            session,
            {key: form.get(key, "") for key in _LABELS} | {"auto_enable": form.get("auto_enable", ""), "off_all_day": form.get("off_all_day", "")},
            errors=errors,
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            forecast_device_ids=device_ids,
        )
    save_control_settings(session, data)
    device_models.set_forecast_schedule_device_ids(session, device_ids)
    # 変更した条件を、手動で変更していない未開始のスケジュールへ反映する
    today = datetime.now(ZoneInfo(get_settings().tz)).date()
    optimizer.regenerate_days(session, get_settings(), [today, today + timedelta(days=1)])
    return _render(request, session, _to_form_values(data), saved=True)


class AdjustmentInput(BaseModel):
    category: AdjustmentCategory
    amount: float = Field(ge=-100, le=100)
    effective_from: date
    effective_to: date | None = None
    note: str = Field(default="", max_length=200)

    @field_validator("effective_to", mode="before")
    @classmethod
    def _empty_to_none(cls, value: object) -> object:
        return None if value == "" else value

    @model_validator(mode="after")
    def _check_period(self) -> "AdjustmentInput":
        if self.effective_to is not None and self.effective_to < self.effective_from:
            raise ValueError("適用終了日は適用開始日以降にしてください")
        return self


_ADJUSTMENT_LABELS = {
    "category": "種類",
    "amount": "金額",
    "effective_from": "適用開始日",
    "effective_to": "適用終了日",
    "note": "メモ",
}


@router.post("/settings/adjustments", name="create_adjustment", dependencies=[Depends(verify_csrf)])
async def create_adjustment(request: Request, session: Session = Depends(get_session)) -> Response:
    form = {key: value for key, value in (await request.form()).items() if isinstance(value, str)}
    form.pop(CSRF_FORM_FIELD, None)
    try:
        data = AdjustmentInput.model_validate({key: form.get(key, "").strip() for key in _ADJUSTMENT_LABELS})
    except ValidationError as exc:
        messages = []
        for error in exc.errors():
            field = error["loc"][0] if error["loc"] else None
            message = error["msg"].removeprefix("Value error, ")
            messages.append(f"{_ADJUSTMENT_LABELS[field]}：{message}" if field in _ADJUSTMENT_LABELS else message)
        return _render(
            request,
            session,
            _current_values(session),
            adjustment_form=form,
            adjustment_errors=messages,
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        )
    session.add(PriceAdjustment(**data.model_dump(), updated_at=datetime.now(UTC)))
    session.commit()
    return RedirectResponse("/settings#adjustments", status_code=status.HTTP_303_SEE_OTHER)


@router.post(
    "/settings/adjustments/{adjustment_id}/delete", name="delete_adjustment", dependencies=[Depends(verify_csrf)]
)
def delete_adjustment(adjustment_id: int, session: Session = Depends(get_session)) -> RedirectResponse:
    adjustment = session.get(PriceAdjustment, adjustment_id)
    if adjustment is not None:
        session.delete(adjustment)
        session.commit()
    return RedirectResponse("/settings#adjustments", status_code=status.HTTP_303_SEE_OTHER)



# --- ポータブル電源（docs/17-multi-device.md §17.8.2） ---


@dataclass(frozen=True)
class PortablePowerRow:
    device: Device
    settings: PortablePowerSettings
    linked: list[Device]  # 紐づく制御機器（制御対象のもの）
    hints: dict
    delete_confirm: str  # 削除の確認ダイアログに出す内容


def _portable_power_clients(request: Request) -> dict[Vendor, PortablePowerClient]:
    return getattr(request.app.state, "portable_power_clients", {})


def configured_vendors(request: Request) -> list[Vendor]:
    """アカウントが設定されているメーカー（未設定のメーカーは選択肢に出さない）。"""
    return [vendor for vendor, client in _portable_power_clients(request).items() if client.configured]


def _portable_power_context(request: Request, session: Session, values: dict[str, str]) -> dict[str, Any]:
    settings = get_settings()
    today = datetime.now(ZoneInfo(settings.tz)).date()
    switches = device_models.active_switches(session)
    rows = []
    for device in device_models.portable_powers(session):
        pp = device.portable_power
        if pp is None:
            continue
        linked = [d for d in switches if d.power_source_id == device.id]
        confirm = f"紐づくプラグ {len(linked)}台の給電先を未設定に戻してから削除します。" if linked else ""
        rows.append(
            PortablePowerRow(
                device,
                pp,
                linked,
                maintenance.tuning_hints(session, settings, pp.capacity_wh, today, power_source_id=device.id),
                confirm,
            )
        )
    return {
        "portable_power_rows": rows,
        "portable_power_switches": switches,
        "portable_power_vendors": configured_vendors(request),
        "charge_mode_values": {key: values.get(key, "") for key, _ in CHARGE_MODE_FIELDS},
    }


def _back(message: str, *, error: bool = False) -> RedirectResponse:
    key = "error" if error else "message"
    return RedirectResponse(f"/settings?{key}={quote(message)}#portable-powers", status_code=status.HTTP_303_SEE_OTHER)


def _regenerate(session: Session) -> None:
    """推定節約額（給電先の変換効率）を、手動で変更していない開始前のスケジュールへ反映する。"""
    settings = get_settings()
    today = datetime.now(ZoneInfo(settings.tz)).date()
    optimizer.regenerate_days(session, settings, [today, today + timedelta(days=1)])


@router.post("/settings/charge-mode", name="save_charge_mode_defaults", dependencies=[Depends(verify_csrf)])
async def save_charge_mode_defaults(request: Request, session: Session = Depends(get_session)) -> RedirectResponse:
    """でんき予報のスケジュールの充電モードの既定値（全体で1つ。docs/17 §17.8.3 の D-6）。"""
    form = await request.form()
    current = load_control_settings(session, get_settings())
    values = current.model_dump()
    for key, label in CHARGE_MODE_FIELDS:
        raw = form.get(key)
        if raw not in {option.value for option in ChargeModeOption}:
            return _back(f"充電モード（{label}）：選択肢から選んでください", error=True)
        values[key] = ChargeModeOption(str(raw))
    save_control_settings(session, ControlSettings.model_validate(values))
    _regenerate(session)
    return _back("充電モードの既定値を保存しました")


class PortablePowerInput(BaseModel):
    name: str = Field(min_length=1, max_length=50)
    capacity_wh: float = Field(gt=0, le=100000)
    min_percent: int = Field(ge=0, le=100)
    efficiency_percent: float = Field(ge=1, le=100)
    charge_mode_enabled: bool = False
    note: str | None = Field(default=None, max_length=200)

    @field_validator("note", mode="before")
    @classmethod
    def _empty_to_none(cls, value: object) -> object:
        return None if isinstance(value, str) and not value.strip() else value


_PORTABLE_POWER_LABELS = {
    "name": "名前",
    "capacity_wh": "容量",
    "min_percent": "残量の下限",
    "efficiency_percent": "変換効率",
    "note": "説明",
}


def _parse_portable_power(form: Any) -> tuple[PortablePowerInput | None, list[int], list[str]]:
    """入力値・紐づくプラグ（devices.id）・エラー。"""
    values = {key: str(form.get(key) or "").strip() for key in _PORTABLE_POWER_LABELS}
    values["charge_mode_enabled"] = form.get("charge_mode_enabled") == "on"
    linked = list(dict.fromkeys(int(v) for v in form.getlist("linked_ids") if isinstance(v, str) and v.isdigit()))
    try:
        return PortablePowerInput.model_validate(values), linked, []
    except ValidationError as exc:
        errors = []
        for error in exc.errors():
            field = str(error["loc"][0]) if error["loc"] else ""
            errors.append(f"{_PORTABLE_POWER_LABELS.get(field, field)}：{error['msg'].removeprefix('Value error, ')}")
        return None, linked, errors


def _check_name(session: Session, name: str, exclude_id: int | None) -> list[str]:
    duplicated = any(p.name == name and p.id != exclude_id for p in device_models.portable_powers(session))
    return ["名前：ほかのポータブル電源と同じ名前は使えません"] if duplicated else []


def _link_changes(session: Session, source: Device | None, linked: list[int]) -> tuple[list[Device], list[Device], list[str]]:
    """紐づくプラグの変更（新たに紐づける機器・外す機器・エラー）。実行中のスケジュールの対象機器は変更できない。"""
    switches = {d.id: d for d in device_models.active_switches(session)}
    errors = [f"紐づくプラグ：制御対象の機器から選んでください（{i}）" for i in linked if i not in switches]
    current = {d.id for d in switches.values() if source is not None and d.power_source_id == source.id}
    added = [switches[i] for i in linked if i in switches and i not in current]
    removed = [switches[i] for i in current if i not in linked]
    for device in added + removed:
        if running_with(session, device.id):
            errors.append(f"紐づくプラグ：{device.name} は実行中のスケジュールが終わってから変更してください")
    return added, removed, errors


def _apply(session: Session, device: Device, data: PortablePowerInput, added: list[Device], removed: list[Device]) -> None:
    now = datetime.now(UTC)
    device.name = data.name
    device.note = data.note
    device.updated_at = now
    pp = device.portable_power
    assert pp is not None
    pp.capacity_wh = data.capacity_wh
    pp.min_percent = data.min_percent
    pp.efficiency = data.efficiency_percent / 100
    # 種別が充電モードの変更に対応していなければ切り替えない
    pp.charge_mode_enabled = data.charge_mode_enabled and device.device_type.charge_mode
    for switch in added:
        switch.power_source_id, switch.updated_at = device.id, now
    for switch in removed:
        switch.power_source_id, switch.updated_at = None, now
    session.commit()


@router.get("/settings/portable-powers/new", response_class=HTMLResponse, name="new_portable_power")
def new_portable_power(request: Request, vendor: str = "", session: Session = Depends(get_session)) -> HTMLResponse:
    """「＋ポータブル電源を追加」：メーカーのアカウントの機器一覧（未登録のもの）を取得して追加の入力欄を返す（HTMX）。"""
    context: dict[str, Any] = {"vendor": vendor, "candidates": [], "error": None}
    client = _portable_power_clients(request).get(Vendor(vendor)) if vendor in Vendor.__members__ else None
    if client is None or not client.configured:
        context["error"] = "このメーカーのアカウントが未設定です（環境変数で設定してください）"
    else:
        try:
            listed = client.list_devices()
        except PortablePowerError as exc:
            context["error"] = f"機器の一覧を取得できませんでした：{exc}"
        else:
            registered = {d.external_id for d in device_models.portable_powers(session) if d.device_type.vendor == vendor}
            context["candidates"] = [d for d in listed if d.external_id not in registered]
            device_type = device_models.portable_power_type(session, Vendor(vendor))
            control = load_control_settings(session, get_settings())
            context["defaults"] = {
                "capacity_wh": f"{(device_type.default_capacity_wh if device_type else None) or control.battery_capacity_wh:g}",
                "min_percent": str(control.battery_min_percent),
                "efficiency_percent": f"{control.round_trip_efficiency * 100:g}",
                "charge_mode": bool(device_type and device_type.charge_mode),
            }
            context["switches"] = device_models.active_switches(session)
    return templates.TemplateResponse(request, "components/portable_power_new.html", context)


@router.post("/settings/portable-powers", name="create_portable_power", dependencies=[Depends(verify_csrf)])
async def create_portable_power(request: Request, session: Session = Depends(get_session)) -> RedirectResponse:
    form = await request.form()
    vendor_raw = str(form.get("vendor") or "")
    external_id = str(form.get("external_id") or "").strip()
    data, linked, errors = _parse_portable_power(form)
    client = _portable_power_clients(request).get(Vendor(vendor_raw)) if vendor_raw in Vendor.__members__ else None
    if client is None or not client.configured:
        return _back("このメーカーのアカウントが未設定です（環境変数で設定してください）", error=True)
    vendor = Vendor(vendor_raw)
    if not external_id:
        errors.append("機器：一覧から選んでください")
    else:
        try:
            listed = {d.external_id for d in client.list_devices()}
        except PortablePowerError as exc:
            return _back(f"機器の一覧を取得できないため追加できません：{exc}", error=True)
        if external_id not in listed:
            errors.append("機器：アカウントの機器一覧に見つかりません。一覧を取得し直してください")
        existing = device_models.find_device(session, vendor, external_id)
        if existing is not None and existing.active:
            errors.append("機器：すでに登録されています")
    if data is not None:
        errors += _check_name(session, data.name, None)
    added, removed, link_errors = _link_changes(session, None, linked)
    errors += link_errors
    if errors or data is None:
        return _back("ポータブル電源を追加できませんでした：" + "、".join(errors), error=True)

    device = device_models.find_device(session, vendor, external_id)
    now = datetime.now(UTC)
    if device is None:
        device = device_models.register_portable_power(session, get_settings(), external_id, data.name, vendor=vendor, now=now)
    else:
        # 削除済み（active=false）の機器を登録し直す
        device.active = True
        device.sort_order = device_models.next_sort_order(session)
        if device.portable_power is None:
            control = load_control_settings(session, get_settings())
            session.add(
                PortablePowerSettings(
                    device_id=device.id,
                    capacity_wh=control.battery_capacity_wh,
                    min_percent=control.battery_min_percent,
                    efficiency=control.round_trip_efficiency,
                )
            )
            session.flush()
            session.refresh(device)
    _apply(session, device, data, added, removed)
    request.app.state.control.bind_devices(session)
    _regenerate(session)
    return _back(f"{device.name} を追加しました")


@router.post("/settings/portable-powers/{device_id}", name="edit_portable_power", dependencies=[Depends(verify_csrf)])
async def edit_portable_power(device_id: int, request: Request, session: Session = Depends(get_session)) -> RedirectResponse:
    device = session.get(Device, device_id)
    if device is None or not device.active or device.portable_power is None:
        return _back("ポータブル電源が見つかりません", error=True)
    data, linked, errors = _parse_portable_power(await request.form())
    if data is not None:
        errors += _check_name(session, data.name, device.id)
    added, removed, link_errors = _link_changes(session, device, linked)
    errors += link_errors
    if errors or data is None:
        return _back(f"{device.name} を保存できませんでした：" + "、".join(errors), error=True)
    _apply(session, device, data, added, removed)
    request.app.state.control.bind_devices(session)
    _regenerate(session)
    return _back(f"{device.name} を保存しました")


@router.post("/settings/portable-powers/{device_id}/delete", name="delete_portable_power", dependencies=[Depends(verify_csrf)])
def delete_portable_power(device_id: int, request: Request, session: Session = Depends(get_session)) -> RedirectResponse:
    """紐づくプラグの給電先を未設定に戻してから削除する（active=false）。OFF 中の機器の給電先は削除できない。"""
    device = session.get(Device, device_id)
    if device is None or not device.active:
        return _back("ポータブル電源が見つかりません", error=True)
    control = request.app.state.control
    control.bind_devices(session)
    linked = [d for d in device_models.active_switches(session) if d.power_source_id == device.id]
    off = [d.name for d in linked if control.device_state(d.id) == PlugState.OFF or running_with(session, d.id)]
    if off:
        return _back(f"{'・'.join(off)} が OFF 中（実行中のスケジュールの対象）のため、{device.name} は削除できません", error=True)
    now = datetime.now(UTC)
    for switch in linked:
        switch.power_source_id, switch.updated_at = None, now
    device.active = False
    device.updated_at = now
    session.commit()
    control.bind_devices(session)
    _regenerate(session)
    suffix = f"（紐づくプラグ {len(linked)}台の給電先を未設定にしました）" if linked else ""
    return _back(f"{device.name} を削除しました{suffix}")
