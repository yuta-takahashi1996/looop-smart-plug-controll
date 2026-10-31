from datetime import date

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from app.config import get_settings
from app.services import mock_data
from app.templating import templates

router = APIRouter()


@router.get("/history", response_class=HTMLResponse, name="history")
def history(request: Request, date: date | None = None) -> HTMLResponse:
    settings = get_settings()
    today = mock_data.now(settings).date()
    target = min(date or today, today)
    return templates.TemplateResponse(
        request,
        "history.html",
        {"day": target, "today": today, "logs": mock_data.get_history(settings, target)},
    )
