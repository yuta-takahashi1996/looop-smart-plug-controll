from datetime import UTC, date, datetime
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.auth import CSRF_FORM_FIELD, verify_csrf
from app.config import get_settings
from app.models.forecast import AdjustmentCategory, PriceAdjustment
from app.models.settings import ControlSettings, load_control_settings, save_control_settings
from app.repositories.database import get_session
from app.services import forecast as forecast_service
from app.templating import templates

router = APIRouter()

# 画面に表示する項目（docs/05-web-ui.md §5.7）。(キー, ラベル, 単位, input 属性)
FIELDS: list[tuple[str, str, str, dict[str, Any]]] = [
    ("operation_start", "運用開始時刻", "", {"type": "time"}),
    ("operation_end", "運用終了時刻", "", {"type": "time"}),
    ("min_saving_spread", "最低節約単価差", "円/kWh", {"type": "number", "min": 0, "step": 0.1}),
    ("round_trip_efficiency", "充放電往復効率", "%", {"type": "number", "min": 1, "max": 100, "step": 1}),
    ("max_continuous_off_minutes", "最大連続OFF時間", "分", {"type": "number", "min": 0, "step": 1}),
    ("max_daily_off_minutes", "1日の最大OFF時間", "分", {"type": "number", "min": 0, "step": 1}),
    ("min_switch_interval_minutes", "最低切り替え間隔", "分", {"type": "number", "min": 0, "step": 1}),
    ("estimated_average_power_w", "想定平均負荷", "W", {"type": "number", "min": 0, "step": 1}),
]
_LABELS = {key: label for key, label, _, _ in FIELDS}


def _to_form_values(data: ControlSettings) -> dict[str, str]:
    """画面表示用の文字列へ変換する（往復効率は％で表示する）。"""
    return {
        "operation_start": data.operation_start.strftime("%H:%M"),
        "operation_end": data.operation_end.strftime("%H:%M"),
        "min_saving_spread": f"{data.min_saving_spread:g}",
        "round_trip_efficiency": f"{data.round_trip_efficiency * 100:g}",
        "max_continuous_off_minutes": str(data.max_continuous_off_minutes),
        "max_daily_off_minutes": str(data.max_daily_off_minutes),
        "min_switch_interval_minutes": str(data.min_switch_interval_minutes),
        "estimated_average_power_w": f"{data.estimated_average_power_w:g}",
    }


def _parse_form(form: dict[str, str]) -> ControlSettings:
    values: dict[str, Any] = {key: form.get(key, "").strip() for key in _LABELS}
    try:
        values["round_trip_efficiency"] = float(values["round_trip_efficiency"]) / 100
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
    status_code: int = status.HTTP_200_OK,
) -> HTMLResponse:
    context: dict[str, Any] = {"fields": FIELDS, "values": values, "saved": saved, "errors": errors or []}
    # HTMX からの送信ではフォーム部分のみ返す
    if request.headers.get("HX-Request"):
        template = "components/settings_form.html"
    else:
        template = "settings.html"
        context |= _adjustments_context(session)
        context["adjustment_form"] = adjustment_form or {}
        context["adjustment_errors"] = adjustment_errors or []
    return templates.TemplateResponse(request, template, context, status_code=status_code)


def _current_values(session: Session) -> dict[str, str]:
    return _to_form_values(load_control_settings(session, get_settings()))


@router.get("/settings", response_class=HTMLResponse, name="settings")
def settings_page(request: Request, session: Session = Depends(get_session)) -> HTMLResponse:
    return _render(request, session, _current_values(session))


@router.post("/settings", response_class=HTMLResponse, dependencies=[Depends(verify_csrf)])
async def update_settings(request: Request, session: Session = Depends(get_session)) -> HTMLResponse:
    form = {key: value for key, value in (await request.form()).items() if isinstance(value, str)}
    form.pop(CSRF_FORM_FIELD, None)
    try:
        data = _parse_form(form)
    except ValidationError as exc:
        return _render(
            request,
            session,
            {key: form.get(key, "") for key in _LABELS},
            errors=_error_messages(exc),
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        )
    save_control_settings(session, data)
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
