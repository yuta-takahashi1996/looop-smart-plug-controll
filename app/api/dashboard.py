from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from app.config import get_settings
from app.templating import templates

router = APIRouter()


@router.get("/", response_class=HTMLResponse, name="dashboard")
def dashboard(request: Request) -> HTMLResponse:
    settings = get_settings()
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "app_env": settings.app_env,
            "dry_run": settings.dry_run,
            "looop_area": settings.looop_area,
            "operation_start": settings.operation_start,
            "operation_end": settings.operation_end,
        },
    )
