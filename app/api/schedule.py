"""運転スケジュール画面（docs/05-web-ui.md §5.5）。

でんき予報の注意報・警報から生成した OFF スケジュールを表示し、有効・無効の切り替えと時間変更を受け付ける。
"""

from datetime import date, datetime, time, timedelta
from typing import Annotated, Literal
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Form, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy.orm import Session

from app.api.auth import verify_csrf
from app.config import get_settings
from app.models.schedule import ControlSchedule
from app.models.settings import load_control_settings
from app.repositories.database import get_session
from app.services import forecast as forecast_service
from app.services import optimizer
from app.templating import templates

router = APIRouter()

Day = Literal["today", "tomorrow"]


def _target_day(day: Day) -> tuple[datetime, date]:
    now = datetime.now(ZoneInfo(get_settings().tz))
    return now, now.date() + timedelta(days=1 if day == "tomorrow" else 0)


def _render(
    request: Request,
    session: Session,
    day: Day,
    *,
    errors: list[str] | None = None,
    error_id: int | None = None,
    message: str | None = None,
    status_code: int = status.HTTP_200_OK,
) -> HTMLResponse:
    settings = get_settings()
    tz = ZoneInfo(settings.tz)
    now, target = _target_day(day)
    schedules = optimizer.schedules_for(session, target)
    forecast = forecast_service.get_day_forecast(session, settings.looop_area, target, tz)
    control = load_control_settings(session, settings)
    enabled_minutes = sum(int((s.end_at - s.start_at).total_seconds() // 60) for s in schedules if s.enabled)
    return templates.TemplateResponse(
        request,
        "schedule.html",
        {
            "day": day,
            "target": target,
            "now": now,
            "schedules": schedules,
            "has_forecast": bool(forecast.slots),
            "control": control,
            "enabled_minutes": enabled_minutes,
            "total_saving": round(sum(s.estimated_saving for s in schedules if s.enabled), 1),
            "errors": errors or [],
            "error_id": error_id,
            "message": message,
        },
        status_code=status_code,
    )


@router.get("/schedule", response_class=HTMLResponse, name="schedule")
def schedule_page(
    request: Request,
    day: Day = "today",
    updated: Literal["toggle", "time", "regenerate"] | None = None,
    session: Session = Depends(get_session),
) -> HTMLResponse:
    messages = {
        "toggle": "有効・無効を切り替えました。",
        "time": "時間を変更しました。",
        "regenerate": "スケジュールを再生成しました（手動で変更したものはそのまま残しています）。",
    }
    return _render(request, session, day, message=messages.get(updated or ""))


def _schedule_or_404(session: Session, schedule_id: int) -> ControlSchedule | None:
    return session.get(ControlSchedule, schedule_id)


def _day_of(schedule: ControlSchedule) -> Day:
    today = datetime.now(ZoneInfo(get_settings().tz)).date()
    return "tomorrow" if schedule.schedule_date > today else "today"


@router.post("/schedule/{schedule_id}/toggle", name="toggle_schedule", dependencies=[Depends(verify_csrf)])
def toggle_schedule(
    request: Request,
    schedule_id: int,
    enabled: Annotated[Literal["true", "false"], Form()],
    session: Session = Depends(get_session),
) -> Response:
    schedule = _schedule_or_404(session, schedule_id)
    if schedule is None:
        return RedirectResponse("/schedule", status_code=status.HTTP_303_SEE_OTHER)
    day = _day_of(schedule)
    try:
        optimizer.update_schedule(session, get_settings(), schedule, enabled=enabled == "true")
    except optimizer.ScheduleChangeError as exc:
        session.rollback()
        return _render(
            request,
            session,
            day,
            errors=exc.messages,
            error_id=schedule_id,
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        )
    return RedirectResponse(f"/schedule?day={day}&updated=toggle", status_code=status.HTTP_303_SEE_OTHER)


def _parse_time(value: str) -> time | None:
    try:
        return time.fromisoformat(value)
    except ValueError:
        return None


@router.post("/schedule/{schedule_id}/time", name="change_schedule_time", dependencies=[Depends(verify_csrf)])
def change_schedule_time(
    request: Request,
    schedule_id: int,
    start: Annotated[str, Form()],
    end: Annotated[str, Form()],
    session: Session = Depends(get_session),
) -> Response:
    schedule = _schedule_or_404(session, schedule_id)
    if schedule is None:
        return RedirectResponse("/schedule", status_code=status.HTTP_303_SEE_OTHER)
    day = _day_of(schedule)
    tz = ZoneInfo(get_settings().tz)
    start_time, end_time = _parse_time(start), _parse_time(end)
    errors = []
    if start_time is None or end_time is None:
        errors.append("時刻を HH:MM の形式で入力してください")
    else:
        base = datetime.combine(schedule.schedule_date, time(), tzinfo=tz)
        start_at = datetime.combine(schedule.schedule_date, start_time, tzinfo=tz)
        # 終了 00:00 は対象日の 24:00 とみなす
        if end_time == time():
            end_at = base + timedelta(days=1)
        else:
            end_at = datetime.combine(schedule.schedule_date, end_time, tzinfo=tz)
        try:
            optimizer.update_schedule(session, get_settings(), schedule, start_at=start_at, end_at=end_at)
        except optimizer.ScheduleChangeError as exc:
            session.rollback()
            errors = exc.messages
    if errors:
        return _render(
            request,
            session,
            day,
            errors=errors,
            error_id=schedule_id,
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        )
    return RedirectResponse(f"/schedule?day={day}&updated=time", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/schedule/regenerate", name="regenerate_schedule", dependencies=[Depends(verify_csrf)])
def regenerate_schedule(
    next: Annotated[str, Form()] = "/schedule", session: Session = Depends(get_session)
) -> RedirectResponse:
    """スケジュールの再生成（docs/05 §5.8）。手動で変更したスケジュールは残す。プラグは操作しない。"""
    settings = get_settings()
    today = datetime.now(ZoneInfo(settings.tz)).date()
    optimizer.regenerate_days(session, settings, [today, today + timedelta(days=1)])
    target = next if next in ("/", "/schedule", "/schedule?day=today", "/schedule?day=tomorrow") else "/schedule"
    separator = "&" if "?" in target else "?"
    return RedirectResponse(f"{target}{separator}updated=regenerate", status_code=status.HTTP_303_SEE_OTHER)
