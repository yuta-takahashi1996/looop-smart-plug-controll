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
from app.templating import templates

router = APIRouter()

# 翌日分の予報が公開される目安（でんき予報ページの注記より）
TOMORROW_PUBLISH_HOUR = 16


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
    chart = {
        "labels": [slot.start.strftime("%H:%M") for slot in data.slots],
        "billing": [slot.billing_price for slot in data.slots],
        "energy": [slot.energy_price for slot in data.slots],
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
    """料金予報の手動再取得（docs/05 §5.8）。プラグは操作しないため MANUAL モードには移行しない。"""
    settings = get_settings()
    try:
        forecast_service.fetch_and_store(
            session, request.app.state.forecast_provider, settings.looop_area, ZoneInfo(settings.tz)
        )
        result = "ok"
    except ForecastFetchError:
        result = "error"
    target = next if next in ("/", "/forecast", "/forecast?day=today", "/forecast?day=tomorrow") else "/forecast"
    separator = "&" if "?" in target else "?"
    return RedirectResponse(f"{target}{separator}refresh={result}", status_code=status.HTTP_303_SEE_OTHER)
