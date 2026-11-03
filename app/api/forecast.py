from datetime import datetime, timedelta
from typing import Annotated, Literal
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Form, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.api.auth import verify_csrf
from app.config import get_settings
from app.models.settings import EventCategory, unresolved_events
from app.providers.base import ForecastFetchError
from app.repositories.database import get_session
from app.services import forecast as forecast_service
from app.services import optimizer
from app.templating import templates

router = APIRouter()

# 翌日分の予報が公開される目安（でんき予報ページの注記より）
TOMORROW_PUBLISH_HOUR = 16

# 単価の表示モード。制御の判断は常に電源料金単価で行い、請求単価は実際の支払単価の目安として表示する
PriceView = Literal["energy", "billing"]
PRICE_VIEW_SESSION_KEY = "price_view"
PRICE_VIEW_LABELS: dict[str, str] = {"energy": "電源単価", "billing": "請求単価"}

# 表示モードの切り替え・再取得の後に戻ってよいパス
_RETURN_PATHS = ("/", "/forecast", "/forecast?day=today", "/forecast?day=tomorrow")


def get_price_view(request: Request) -> PriceView:
    return "billing" if request.session.get(PRICE_VIEW_SESSION_KEY) == "billing" else "energy"


def price_of(slot: forecast_service.PriceSlot, view: PriceView) -> float:
    return slot.billing_price if view == "billing" else slot.energy_price


def _safe_return(next_path: str, default: str = "/forecast") -> str:
    return next_path if next_path in _RETURN_PATHS else default


@router.get("/forecast", response_class=HTMLResponse, name="forecast")
def forecast(
    request: Request,
    day: Literal["today", "tomorrow"] = "today",
    refresh: Literal["ok", "error"] | None = None,
    session: Session = Depends(get_session),
) -> HTMLResponse:
    settings = get_settings()
    tz = ZoneInfo(settings.tz)
    now = datetime.now(tz)
    target = now.date() + timedelta(days=1 if day == "tomorrow" else 0)
    data = forecast_service.get_day_forecast(session, settings.looop_area, target, tz)
    view = get_price_view(request)
    plan: list[str | None] = [None] * len(data.slots)
    for schedule in optimizer.schedules_for(session, target):
        for i, slot in enumerate(data.slots):
            if schedule.start_at <= slot.start < schedule.end_at:
                plan[i] = "ENABLED" if schedule.enabled else "DISABLED"
    chart = {
        "label": PRICE_VIEW_LABELS[view],
        "prices": [price_of(slot, view) for slot in data.slots],
        "levels": [slot.level.value for slot in data.slots],
        "plan": plan,  # OFF 予定（ENABLED：有効、DISABLED：無効、None：予定なし）
        "labels": [slot.start.strftime("%H:%M") for slot in data.slots],
        # 当日表示のときのみ、現在時刻のコマ位置（小数）を渡す
        "nowIndex": (now.hour * 60 + now.minute) / 30 if day == "today" else None,
    }
    errors = [e for e in unresolved_events(session) if e.category == EventCategory.FORECAST]
    return templates.TemplateResponse(
        request,
        "forecast.html",
        {
            "day": day,
            "target": target,
            "forecast": data,
            "chart": chart,
            "view": view,
            "prices": chart["prices"],
            "errors": errors,
            "before_publish": day == "tomorrow" and now.hour < TOMORROW_PUBLISH_HOUR,
            "refresh_result": refresh,
        },
    )


@router.post("/forecast/refresh", name="refresh_forecast", dependencies=[Depends(verify_csrf)])
def refresh_forecast(
    request: Request,
    next: Annotated[str, Form()] = "/forecast",
    session: Session = Depends(get_session),
) -> RedirectResponse:
    """料金予報の手動再取得（docs/05 §5.8）。取得後にスケジュールを再生成する。プラグは操作しないため MANUAL モードには移行しない。"""
    settings = get_settings()
    try:
        saved = forecast_service.fetch_and_store(
            session, request.app.state.forecast_provider, settings.looop_area, ZoneInfo(settings.tz)
        )
        optimizer.regenerate_days(session, settings, saved)
        result = "ok"
    except ForecastFetchError:
        result = "error"
    target = _safe_return(next)
    separator = "&" if "?" in target else "?"
    return RedirectResponse(f"{target}{separator}refresh={result}", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/price-view", name="set_price_view", dependencies=[Depends(verify_csrf)])
def set_price_view(
    request: Request, view: Annotated[PriceView, Form()], next: Annotated[str, Form()] = "/"
) -> RedirectResponse:
    """単価の表示モード（電源単価／請求単価）を切り替える。表示のみで、制御の判断には影響しない。"""
    request.session[PRICE_VIEW_SESSION_KEY] = view
    return RedirectResponse(_safe_return(next, "/"), status_code=status.HTTP_303_SEE_OTHER)
