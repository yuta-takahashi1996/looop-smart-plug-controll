from typing import Any

from fastapi import APIRouter, Depends, Request, status
from fastapi.responses import HTMLResponse
from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.api.auth import CSRF_FORM_FIELD, verify_csrf
from app.config import get_settings
from app.models.settings import ControlSettings, load_control_settings, save_control_settings
from app.repositories.database import get_session
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


def _render(
    request: Request,
    values: dict[str, str],
    *,
    saved: bool = False,
    errors: list[str] | None = None,
    status_code: int = status.HTTP_200_OK,
) -> HTMLResponse:
    # HTMX からの送信ではフォーム部分のみ返す
    template = "components/settings_form.html" if request.headers.get("HX-Request") else "settings.html"
    return templates.TemplateResponse(
        request,
        template,
        {"fields": FIELDS, "values": values, "saved": saved, "errors": errors or []},
        status_code=status_code,
    )


@router.get("/settings", response_class=HTMLResponse, name="settings")
def settings_page(request: Request, session: Session = Depends(get_session)) -> HTMLResponse:
    return _render(request, _to_form_values(load_control_settings(session, get_settings())))


@router.post("/settings", response_class=HTMLResponse, dependencies=[Depends(verify_csrf)])
async def update_settings(request: Request, session: Session = Depends(get_session)) -> HTMLResponse:
    form = {key: value for key, value in (await request.form()).items() if isinstance(value, str)}
    form.pop(CSRF_FORM_FIELD, None)
    try:
        data = _parse_form(form)
    except ValidationError as exc:
        return _render(
            request,
            {key: form.get(key, "") for key in _LABELS},
            errors=_error_messages(exc),
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        )
    save_control_settings(session, data)
    return _render(request, _to_form_values(data), saved=True)
