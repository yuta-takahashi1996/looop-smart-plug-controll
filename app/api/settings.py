from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.auth import CSRF_FORM_FIELD, verify_csrf
from app.api.control import REAL_CONTROL_CONFIRMATION
from app.config import get_settings
from app.models import device as device_models
from app.models.forecast import AdjustmentCategory, PriceAdjustment
from app.models.schedule import ChargeModeOption
from app.models.settings import ControlSettings, load_control_settings, save_control_settings
from app.repositories.database import get_session
from app.services import forecast as forecast_service
from app.services import maintenance, optimizer
from app.templating import templates

router = APIRouter()

# 画面に表示する項目（docs/05-web-ui.md §5.7）。グループごとに (キー, ラベル, 単位, input 属性, 説明)
GROUPS: list[tuple[str, str, list[tuple[str, str, str, dict[str, Any], str]]]] = [
    (
        "運用時間",
        "でんき注意報のスケジュールはこの時間内に限って作ります（でんき警報は残量を取得できる間は時間外も実行します）。",
        [
            ("operation_start", "運用開始時刻", "", {"type": "time"}, ""),
            ("operation_end", "運用終了時刻", "", {"type": "time"}, "この時刻を過ぎると ON に戻します。"),
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
        "ポータブル電源",
        "残量が下限を下回る前（次の取得時の予測を含む）に ON に戻します。",
        [
            ("battery_min_percent", "残量の下限", "%", {"type": "number", "min": 0, "max": 100, "step": 1}, ""),
            ("battery_capacity_wh", "容量", "Wh", {"type": "number", "min": 1, "step": 1}, "Jackery 1000 New は 1070Wh。"),
            (
                "round_trip_efficiency",
                "変換効率",
                "%",
                {"type": "number", "min": 1, "max": 100, "step": 1},
                "残量の減り方の予測と、推定節約額（充電に必要な電力量）の計算に使います。低いほど安全側です。",
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
    values["auto_enable"] = form.get("auto_enable") == "on"  # チェックボックスは未チェックだと送信されない
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
            {key: form.get(key, "") for key in _LABELS} | {"auto_enable": form.get("auto_enable", "")},
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
