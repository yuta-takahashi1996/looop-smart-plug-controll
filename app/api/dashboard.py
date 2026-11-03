from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from typing import Literal
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.auth import verify_csrf
from app.api.forecast import PriceView, chart_context, get_price_view, price_of
from app.config import Settings, get_settings
from app.models.schedule import ControlLog, ControlSchedule, PlugState, ScheduleStatus
from app.models.settings import EventCategory, EventLevel, SystemEvent, load_control_settings, unresolved_events
from app.providers.base import ForecastLevel
from app.repositories.database import get_session
from app.services import forecast as forecast_service
from app.services import optimizer, safety
from app.services.controller import PlugSnapshot
from app.templating import templates

router = APIRouter()


@dataclass(frozen=True)
class PriceSummary:
    """電力情報。価格は表示モード（電源料金単価／請求単価）に応じた値。予報が未取得なら current は None。"""

    view: PriceView
    current: forecast_service.PriceSlot | None
    current_price: float | None
    next_price: float | None
    day_max: float | None
    day_min: float | None
    battery_effective_price: float | None  # 当日最安の単価で充電した場合の実効単価（往復効率込み）
    next_alert: forecast_service.PriceSlot | None  # 本日これから最初の注意報・警報のコマ
    adjustment_total: float
    has_adjustments: bool
    fetched_at: datetime | None


def _price_summary(session: Session, settings: Settings, now: datetime, view: PriceView) -> PriceSummary:
    tz = ZoneInfo(settings.tz)
    area = settings.looop_area
    today = forecast_service.get_day_forecast(session, area, now.date(), tz)
    if not today.slots:
        return PriceSummary(
            view, None, None, None, None, None, None, None, today.adjustment_total, bool(today.adjustments), None
        )

    index = (now.hour * 60 + now.minute) // 30
    if index + 1 < len(today.slots):
        next_slot = today.slots[index + 1]
    else:
        tomorrow = forecast_service.get_day_forecast(session, area, now.date() + timedelta(days=1), tz)
        next_slot = tomorrow.slots[0] if tomorrow.slots else None
    prices = [price_of(slot, view) for slot in today.slots]
    efficiency = load_control_settings(session, settings).round_trip_efficiency
    return PriceSummary(
        view=view,
        current=today.slots[index],
        current_price=prices[index],
        next_price=price_of(next_slot, view) if next_slot else None,
        day_max=max(prices),
        day_min=min(prices),
        battery_effective_price=round(min(prices) / efficiency, 2),
        next_alert=next(
            (s for s in today.slots[index + 1 :] if s.level in (ForecastLevel.ADVISORY, ForecastLevel.WARNING)), None
        ),
        adjustment_total=today.adjustment_total,
        has_adjustments=bool(today.adjustments),
        fetched_at=today.fetched_at,
    )


def _schedule_summary(session: Session, now: datetime) -> dict:
    """次に実行する（有効な）OFF 予定と、まだ開始していない無効の注意報スケジュールの件数。"""
    schedules = optimizer.schedules_for(session, now.date()) + optimizer.schedules_for(
        session, now.date() + timedelta(days=1)
    )
    upcoming = [s for s in schedules if s.end_at > now]
    return {
        "next_off": next((s for s in upcoming if s.enabled), None),
        "pending_advisories": sum(
            1 for s in upcoming if not s.enabled and s.start_at > now and s.source_level == ForecastLevel.ADVISORY
        ),
    }


@dataclass(frozen=True)
class ControlSummary:
    dry_run: bool
    mode: str  # SAFE / GRID / BATTERY（Dry Run では想定の状態）
    last_log: ControlLog | None
    today_saving: float  # 実行した（Dry Run では実行するはずだった）OFF の推定節約額
    month_saving: float
    today_switch_count: int
    today_off_minutes: int


def _executed_saving(session: Session, settings: Settings, schedule: ControlSchedule, now: datetime) -> float:
    if schedule.executed_saving is not None:
        return schedule.executed_saving
    if schedule.status != ScheduleStatus.RUNNING or schedule.executed_start_at is None:
        return 0.0
    # 実行中は現在までの分
    forecast = forecast_service.get_day_forecast(session, settings.looop_area, schedule.schedule_date, ZoneInfo(settings.tz))
    if not forecast.slots:
        return 0.0
    params = optimizer.PlanParams.from_settings(load_control_settings(session, settings))
    return optimizer.saving_between(forecast.slots, schedule.executed_start_at, now, params)


def _control_summary(request: Request, session: Session, settings: Settings, now: datetime) -> ControlSummary:
    control = request.app.state.control
    today = optimizer.schedules_for(session, now.date())
    month = list(
        session.scalars(
            select(ControlSchedule).where(
                ControlSchedule.schedule_date >= now.date().replace(day=1), ControlSchedule.schedule_date <= now.date()
            )
        )
    )
    day_start = datetime.combine(now.date(), time(), tzinfo=now.tzinfo)
    today_logs = list(session.scalars(select(ControlLog).where(ControlLog.executed_at >= day_start)))
    return ControlSummary(
        dry_run=control.dry_run,
        mode=control.mode,
        last_log=session.scalars(select(ControlLog).order_by(ControlLog.executed_at.desc(), ControlLog.id.desc())).first(),
        today_saving=round(sum(_executed_saving(session, settings, s, now) for s in today), 1),
        month_saving=round(sum(_executed_saving(session, settings, s, now) for s in month), 1),
        today_switch_count=sum(
            1 for log in today_logs if log.previous_state is not None and log.previous_state != log.requested_state
        ),
        today_off_minutes=safety.off_minutes(today, now),
    )


@dataclass(frozen=True)
class BatterySummary:
    configured: bool
    percent: int | None
    output_w: float | None
    read_at: datetime | None
    error: str | None
    eta: datetime | None  # OFF 中に下限へ達する見込み時刻
    min_percent: int


def _battery_summary(request: Request, session: Session, settings: Settings, now: datetime) -> BatterySummary:
    control = request.app.state.control
    battery = control.battery
    reading = battery.last
    limits = load_control_settings(session, settings)
    eta = None
    if reading is not None and control.state == PlugState.OFF:
        power = max(reading.output_w, battery.average_output_w() or 0.0)
        eta = safety.time_to_min_percent(reading, power, limits)
    return BatterySummary(
        configured=battery.configured,
        percent=reading.percent if reading else None,
        output_w=reading.output_w if reading else None,
        read_at=reading.at if reading else None,
        error=battery.last_error,
        eta=eta,
        min_percent=limits.battery_min_percent,
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
        "prices": _price_summary(session, settings, now, get_price_view(request)),
        "plug": plug,
        "events": events,
        "system_status": _system_status(events),
        "control": _control_summary(request, session, settings, now),
        "control_mode": request.app.state.control.control_mode.value,
        "battery": _battery_summary(request, session, settings, now),
        **_schedule_summary(session, now),
    }


@router.get("/", response_class=HTMLResponse, name="dashboard")
def dashboard(
    request: Request,
    day: Literal["today", "tomorrow"] = "today",
    refresh: Literal["ok", "error"] | None = None,
    message: str | None = None,
    error: str | None = None,
    session: Session = Depends(get_session),
) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            **_context(request, session),
            **chart_context(request, session, day),
            "refresh_result": refresh,
            "flash_message": message,
            "flash_error": error,
        },
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
