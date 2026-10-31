from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from app.config import get_settings
from app.services import mock_data
from app.templating import templates

router = APIRouter()


@router.get("/", response_class=HTMLResponse, name="dashboard")
def dashboard(request: Request) -> HTMLResponse:
    settings = get_settings()
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {"summary": mock_data.get_dashboard_summary(settings), "now": mock_data.now(settings)},
    )


@router.get("/partials/dashboard/status", response_class=HTMLResponse, name="dashboard_status")
def dashboard_status(request: Request) -> HTMLResponse:
    """HTMX による定期的な部分更新（docs/05-web-ui.md §5.9）。保存済みデータのみ参照する。"""
    settings = get_settings()
    return templates.TemplateResponse(
        request,
        "components/dashboard_status.html",
        {"summary": mock_data.get_dashboard_summary(settings), "now": mock_data.now(settings)},
    )
