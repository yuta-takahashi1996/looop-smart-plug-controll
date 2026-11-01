from datetime import timedelta
from typing import Literal

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from app.config import get_settings
from app.services import mock_data
from app.templating import templates

router = APIRouter()


@router.get("/forecast", response_class=HTMLResponse, name="forecast")
def forecast(request: Request, day: Literal["today", "tomorrow"] = "today") -> HTMLResponse:
    settings = get_settings()
    now = mock_data.now(settings)
    target = now.date() + timedelta(days=1 if day == "tomorrow" else 0)
    data = mock_data.get_forecast(settings, target)
    chart = {
        "labels": [slot.start.strftime("%H:%M") for slot in data.slots],
        "prices": [slot.price for slot in data.slots],
        "off": [slot.planned_state == "OFF" for slot in data.slots],
        # 当日表示のときのみ、現在時刻のコマ位置（小数）を渡す
        "nowIndex": (now.hour * 60 + now.minute) / 30 if day == "today" else None,
    }
    return templates.TemplateResponse(
        request,
        "forecast.html",
        {"day": day, "forecast": data, "chart": chart},
    )
