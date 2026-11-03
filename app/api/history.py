from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models.schedule import ControlLog
from app.repositories.database import get_session
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
def history(request: Request, date: date | None = None, session: Session = Depends(get_session)) -> HTMLResponse:
    tz = ZoneInfo(get_settings().tz)
    today = datetime.now(tz).date()
    target = min(date or today, today)
    return templates.TemplateResponse(
        request,
        "history.html",
        {"day": target, "today": today, "logs": logs_for(session, target, tz)},
    )
