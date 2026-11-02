from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.auth import verify_csrf
from app.config import Settings, get_settings
from app.models.settings import EventCategory, EventLevel, SystemEvent, load_control_settings, unresolved_events
from app.repositories.database import get_session
from app.services import forecast as forecast_service
from app.services import mock_data
from app.services.controller import PlugSnapshot
from app.templating import templates

router = APIRouter()


@dataclass(frozen=True)
class PriceSummary:
    """電力情報（請求単価 ＝ 電源料金単価 ＋ 加算額）。予報が未取得なら current は None。"""

    current: forecast_service.PriceSlot | None
    next: forecast_service.PriceSlot | None
    day_max: float | None
    day_min: float | None
    battery_effective_price: float | None  # 当日最安の請求単価で充電した場合の実効単価（往復効率込み）
    adjustment_total: float
    has_adjustments: bool
    fetched_at: datetime | None


def _price_summary(session: Session, settings: Settings, now: datetime) -> PriceSummary:
    tz = ZoneInfo(settings.tz)
    area = settings.looop_area
    today = forecast_service.get_day_forecast(session, area, now.date(), tz)
    if not today.slots:
        return PriceSummary(None, None, None, None, None, today.adjustment_total, bool(today.adjustments), None)

    index = (now.hour * 60 + now.minute) // 30
    if index + 1 < len(today.slots):
        next_slot = today.slots[index + 1]
    else:
        tomorrow = forecast_service.get_day_forecast(session, area, now.date() + timedelta(days=1), tz)
        next_slot = tomorrow.slots[0] if tomorrow.slots else None
    prices = [slot.billing_price for slot in today.slots]
    efficiency = load_control_settings(session, settings).round_trip_efficiency
    return PriceSummary(
        current=today.slots[index],
        next=next_slot,
        day_max=max(prices),
        day_min=min(prices),
        battery_effective_price=round(min(prices) / efficiency, 2),
        adjustment_total=today.adjustment_total,
        has_adjustments=bool(today.adjustments),
        fetched_at=today.fetched_at,
    )


def _system_status(events: list[SystemEvent]) -> str:
    if any(e.level == EventLevel.ERROR for e in events):
        return "ERROR"
    if any(e.level == EventLevel.WARNING for e in events):
        return "WARNING"
    return "OK"


def _context(request: Request, session: Session) -> dict:
    settings = get_settings()
    now = datetime.now(ZoneInfo(settings.tz))
    events = unresolved_events(session)
    plug: PlugSnapshot = request.app.state.plug_monitor.snapshot()
    return {
        "now": now,
        "tz": ZoneInfo(settings.tz),
        "prices": _price_summary(session, settings, now),
        "plug": plug,
        "events": events,
        "system_status": _system_status(events),
        "control": mock_data.get_control_summary(settings),
    }


@router.get("/", response_class=HTMLResponse, name="dashboard")
def dashboard(
    request: Request, refresh: Literal["ok", "error"] | None = None, session: Session = Depends(get_session)
) -> HTMLResponse:
    return templates.TemplateResponse(
        request, "dashboard.html", {**_context(request, session), "refresh_result": refresh}
    )


@router.get("/partials/dashboard/status", response_class=HTMLResponse, name="dashboard_status")
def dashboard_status(request: Request, session: Session = Depends(get_session)) -> HTMLResponse:
    """HTMX による定期的な部分更新（docs/05-web-ui.md §5.9）。保存済みデータのみ参照する。"""
    return templates.TemplateResponse(request, "components/dashboard_status.html", _context(request, session))


@router.post("/events/{event_id}/resolve", name="resolve_event", dependencies=[Depends(verify_csrf)])
def resolve_event(event_id: int, session: Session = Depends(get_session)) -> RedirectResponse:
    """お知らせを確認済みにする。エラーは原因が解消したときに自動で解決されるため対象外。"""
    event = session.get(SystemEvent, event_id)
    if event is not None and event.category == EventCategory.PRICE_NOTICE and event.resolved_at is None:
        event.resolved_at = datetime.now(UTC)
        session.commit()
    return RedirectResponse("/", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/events/resolve-price-notices", name="resolve_price_notices", dependencies=[Depends(verify_csrf)])
def resolve_price_notices(session: Session = Depends(get_session)) -> RedirectResponse:
    now = datetime.now(UTC)
    for event in session.scalars(
        select(SystemEvent).where(
            SystemEvent.category == EventCategory.PRICE_NOTICE, SystemEvent.resolved_at.is_(None)
        )
    ):
        event.resolved_at = now
    session.commit()
    return RedirectResponse("/", status_code=status.HTTP_303_SEE_OTHER)
