from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from app.config import get_settings
from app.services import mock_data
from app.templating import templates

router = APIRouter()


@router.get("/schedule", response_class=HTMLResponse, name="schedule")
def schedule(request: Request) -> HTMLResponse:
    settings = get_settings()
    today = mock_data.now(settings).date()
    entries = mock_data.get_schedule(settings, today)
    return templates.TemplateResponse(
        request,
        "schedule.html",
        {
            "day": today,
            "entries": entries,
            "total_saving": round(sum(e.estimated_saving for e in entries), 1),
            "operation_start": settings.operation_start,
            "operation_end": settings.operation_end,
        },
    )
