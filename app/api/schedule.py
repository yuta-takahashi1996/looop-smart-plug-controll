"""運転スケジュール画面（docs/05-web-ui.md §5.5）。

でんき予報の注意報・警報から生成した OFF スケジュールと、任意スケジュールから展開した ON／OFF スケジュールを
表示し、有効・無効の切り替えと時間変更を受け付ける。任意スケジュールのルール（docs/07 §7.9）の登録・変更・削除も行う。
"""

from datetime import date, datetime, time, timedelta
from typing import Annotated, Literal
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Form, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.auth import verify_csrf
from app.config import get_settings
from app.models.schedule import ControlSchedule, RepeatType, ScheduleRule
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
    warnings: list[str] | None = None,
    rule_form: dict | None = None,
    rule_error_id: int | None = None,
    status_code: int = status.HTTP_200_OK,
) -> HTMLResponse:
    settings = get_settings()
    tz = ZoneInfo(settings.tz)
    now, target = _target_day(day)
    schedules = optimizer.schedules_for(session, target)
    forecast = forecast_service.get_day_forecast(session, settings.looop_area, target, tz)
    control = load_control_settings(session, settings)
    enabled_minutes = sum(
        int((s.end_at - s.start_at).total_seconds() // 60) for s in schedules if s.enabled and s.target_state == "OFF"
    )
    rules = [
        r
        for r in session.scalars(select(ScheduleRule).order_by(ScheduleRule.start_time, ScheduleRule.id))
        if r.repeat_type != RepeatType.ONCE or (r.target_date is not None and r.target_date >= now.date())
    ]
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
            "warnings": warnings or [],
            "rules": [(r, _repeat_label(r), _rule_values(r)) for r in rules],
            "rule_form": rule_form,
            "rule_error_id": rule_error_id,
            "new_rule": rule_form if rule_error_id is None and rule_form is not None else _blank_rule(target),
            "weekday_labels": optimizer.WEEKDAY_LABELS,
        },
        status_code=status_code,
    )


@router.get("/schedule", response_class=HTMLResponse, name="schedule")
def schedule_page(
    request: Request,
    day: Day = "today",
    updated: Literal["toggle", "time", "regenerate", "rule_created", "rule_updated", "rule_toggled", "rule_deleted"]
    | None = None,
    session: Session = Depends(get_session),
) -> HTMLResponse:
    messages = {
        "toggle": "有効・無効を切り替えました。",
        "time": "時間を変更しました。",
        "regenerate": "スケジュールを再生成しました（手動で変更したものはそのまま残しています）。",
        "rule_created": "任意スケジュールを追加しました。",
        "rule_updated": "任意スケジュールを変更しました。",
        "rule_toggled": "任意スケジュールの有効・無効を切り替えました。",
        "rule_deleted": "任意スケジュールを削除しました。",
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


# --- 任意スケジュールのルール（docs/07 §7.9） ---


def _repeat_label(rule: ScheduleRule) -> str:
    if rule.repeat_type == RepeatType.ONCE and rule.target_date is not None:
        return f"{rule.target_date:%m/%d}（{optimizer.WEEKDAY_LABELS[rule.target_date.weekday()]}）のみ"
    if rule.repeat_type == RepeatType.WEEKLY:
        return "毎週 " + "・".join(optimizer.WEEKDAY_LABELS[d] for d in sorted(rule.weekday_set))
    return "毎日"


def _rule_values(rule: ScheduleRule) -> dict:
    return {
        "name": rule.name,
        "target_state": rule.target_state,
        "repeat_type": rule.repeat_type.value,
        "target_date": rule.target_date.isoformat() if rule.target_date else "",
        "weekdays": sorted(rule.weekday_set),
        "start": rule.start_time.strftime("%H:%M"),
        "end": rule.end_time.strftime("%H:%M"),  # 00:00 は 24:00
    }


def _blank_rule(day: date) -> dict:
    return {
        "name": "",
        "target_state": "OFF",
        "repeat_type": RepeatType.ONCE.value,
        "target_date": day.isoformat(),
        "weekdays": [],
        "start": "",
        "end": "",
    }


def _parse_rule_time(value: str, *, end: bool = False) -> time | None:
    # 終了の 24:00（00:00）は対象日の終わりとみなす
    if end and value == "24:00":
        return time()
    return _parse_time(value)


def _apply_form(rule: ScheduleRule, values: dict) -> list[str]:
    """フォームの値をルールに設定する。形式の誤りはここで返し、内容の検証は optimizer.validate_rule で行う。"""
    errors = []
    start, end = _parse_rule_time(values["start"]), _parse_rule_time(values["end"], end=True)
    if start is None or end is None:
        errors.append("時刻を HH:MM の形式で入力してください")
    try:
        repeat = RepeatType(values["repeat_type"])
    except ValueError:
        repeat = None
        errors.append("繰り返しの指定が正しくありません")
    target_date = None
    if repeat == RepeatType.ONCE:
        try:
            target_date = date.fromisoformat(values["target_date"])
        except ValueError:
            errors.append("対象日を YYYY-MM-DD の形式で入力してください")
    weekdays = sorted({d for d in values["weekdays"] if 0 <= d <= 6})
    if errors:
        return errors
    assert start is not None and end is not None and repeat is not None
    rule.name = values["name"].strip()
    rule.target_state = values["target_state"]
    rule.repeat_type = repeat
    rule.target_date = target_date
    rule.weekdays = ",".join(map(str, weekdays)) if repeat == RepeatType.WEEKLY else None
    rule.start_time, rule.end_time = start, end
    return []


def _rule_values_from_form(
    name: str, target_state: str, repeat_type: str, target_date: str, weekdays: list[str], start: str, end: str
) -> dict:
    return {
        "name": name,
        "target_state": target_state,
        "repeat_type": repeat_type,
        "target_date": target_date,
        "weekdays": [int(d) for d in weekdays if d.isdigit()],
        "start": start,
        "end": end,
    }


def _save_rule(
    request: Request, session: Session, rule: ScheduleRule, form: dict, day: Day, updated: str
) -> Response:
    errors = _apply_form(rule, form)
    warnings: list[str] = []
    if not errors:
        try:
            warnings = optimizer.save_rule(session, get_settings(), rule)
        except optimizer.ScheduleChangeError as exc:
            errors = exc.messages
    if errors:
        session.rollback()
        return _render(
            request,
            session,
            day,
            errors=errors,
            rule_form=form,
            rule_error_id=rule.id,
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        )
    if warnings:
        messages = {"rule_created": "任意スケジュールを追加しました。", "rule_updated": "任意スケジュールを変更しました。"}
        return _render(request, session, day, message=messages[updated], warnings=warnings)
    return RedirectResponse(f"/schedule?day={day}&updated={updated}", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/schedule/rules", name="create_rule", dependencies=[Depends(verify_csrf)])
def create_rule(
    request: Request,
    name: Annotated[str, Form()] = "",
    target_state: Annotated[Literal["ON", "OFF"], Form()] = "OFF",
    repeat_type: Annotated[str, Form()] = RepeatType.ONCE.value,
    target_date: Annotated[str, Form()] = "",
    weekdays: Annotated[list[str], Form()] = [],  # noqa: B006
    start: Annotated[str, Form()] = "",
    end: Annotated[str, Form()] = "",
    day: Annotated[Day, Form()] = "today",
    session: Session = Depends(get_session),
) -> Response:
    form = _rule_values_from_form(name, target_state, repeat_type, target_date, weekdays, start, end)
    return _save_rule(request, session, ScheduleRule(enabled=True), form, day, "rule_created")


@router.post("/schedule/rules/{rule_id}", name="update_rule", dependencies=[Depends(verify_csrf)])
def update_rule(
    request: Request,
    rule_id: int,
    name: Annotated[str, Form()] = "",
    target_state: Annotated[Literal["ON", "OFF"], Form()] = "OFF",
    repeat_type: Annotated[str, Form()] = RepeatType.ONCE.value,
    target_date: Annotated[str, Form()] = "",
    weekdays: Annotated[list[str], Form()] = [],  # noqa: B006
    start: Annotated[str, Form()] = "",
    end: Annotated[str, Form()] = "",
    day: Annotated[Day, Form()] = "today",
    session: Session = Depends(get_session),
) -> Response:
    rule = session.get(ScheduleRule, rule_id)
    if rule is None:
        return RedirectResponse(f"/schedule?day={day}", status_code=status.HTTP_303_SEE_OTHER)
    form = _rule_values_from_form(name, target_state, repeat_type, target_date, weekdays, start, end)
    return _save_rule(request, session, rule, form, day, "rule_updated")


@router.post("/schedule/rules/{rule_id}/toggle", name="toggle_rule", dependencies=[Depends(verify_csrf)])
def toggle_rule(
    request: Request,
    rule_id: int,
    enabled: Annotated[Literal["true", "false"], Form()],
    day: Annotated[Day, Form()] = "today",
    session: Session = Depends(get_session),
) -> Response:
    rule = session.get(ScheduleRule, rule_id)
    if rule is None:
        return RedirectResponse(f"/schedule?day={day}", status_code=status.HTTP_303_SEE_OTHER)
    rule.enabled = enabled == "true"
    try:
        warnings = optimizer.save_rule(session, get_settings(), rule)
    except optimizer.ScheduleChangeError as exc:
        session.rollback()
        return _render(
            request, session, day, errors=exc.messages, rule_error_id=rule_id, status_code=status.HTTP_422_UNPROCESSABLE_CONTENT
        )
    if warnings:
        return _render(request, session, day, message="任意スケジュールの有効・無効を切り替えました。", warnings=warnings)
    return RedirectResponse(f"/schedule?day={day}&updated=rule_toggled", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/schedule/rules/{rule_id}/delete", name="delete_rule", dependencies=[Depends(verify_csrf)])
def delete_rule(
    rule_id: int, day: Annotated[Day, Form()] = "today", session: Session = Depends(get_session)
) -> RedirectResponse:
    rule = session.get(ScheduleRule, rule_id)
    if rule is not None:
        optimizer.delete_rule(session, get_settings(), rule)
    return RedirectResponse(f"/schedule?day={day}&updated=rule_deleted", status_code=status.HTTP_303_SEE_OTHER)
