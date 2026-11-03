from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models.schedule import ControlLog
from app.repositories.database import get_session
from app.services import maintenance
from app.templating import templates

router = APIRouter()


def logs_for(session: Session, day: date, tz: ZoneInfo) -> list[ControlLog]:
    start = datetime.combine(day, time(), tzinfo=tz)
    return list(
        session.scalars(
            select(ControlLog)
            .where(ControlLog.executed_at >= start, ControlLog.executed_at < start + timedelta(days=1))
            .order_by(ControlLog.executed_at.desc(), ControlLog.id.desc())
        )
    )


@router.get("/history", response_class=HTMLResponse, name="history")
def history(
    request: Request,
    date: date | None = None,
    month: Annotated[str | None, Query(pattern=r"^\d{4}-\d{2}$")] = None,
    session: Session = Depends(get_session),
) -> HTMLResponse:
    tz = ZoneInfo(get_settings().tz)
    today = datetime.now(tz).date()
    target = min(date or today, today)
    year, mon = (int(part) for part in month.split("-")) if month else (today.year, today.month)
    if not 1 <= mon <= 12:
        year, mon = today.year, today.month
    summaries = maintenance.monthly_summaries(session, year, mon)
    return templates.TemplateResponse(
        request,
        "history.html",
        {
            "day": target,
            "today": today,
            "logs": logs_for(session, target, tz),
            "month": f"{year:04d}-{mon:02d}",
            "summaries": summaries,
            "totals": {
                "off_minutes": sum(s.off_minutes for s in summaries),
                "switch_count": sum(s.switch_count for s in summaries),
                "schedules_done": sum(s.schedules_done for s in summaries),
                "schedules_aborted": sum(s.schedules_aborted for s in summaries),
                "estimated_saving": round(sum(s.estimated_saving for s in summaries), 1),
                "failed_operations": sum(s.failed_operations for s in summaries),
            },
        },
    )
