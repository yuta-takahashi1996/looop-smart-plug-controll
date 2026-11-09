from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models.device import Device, DeviceCategory, DeviceType
from app.models.schedule import ControlLog
from app.repositories.database import get_session
from app.services import maintenance
from app.templating import templates

router = APIRouter()


def logs_for(session: Session, day: date, tz: ZoneInfo, device_id: int | None = None) -> list[ControlLog]:
    start = datetime.combine(day, time(), tzinfo=tz)
    query = select(ControlLog).where(ControlLog.executed_at >= start, ControlLog.executed_at < start + timedelta(days=1))
    if device_id is not None:
        query = query.where(ControlLog.device_id == device_id)
    return list(session.scalars(query.order_by(ControlLog.executed_at.desc(), ControlLog.id.desc())))


def _switches(session: Session) -> list[Device]:
    """絞り込みに使う制御機器（制御対象から外したものを含む。docs/17 §17.10）。"""
    return list(
        session.scalars(
            select(Device)
            .join(Device.device_type)
            .where(DeviceType.category == DeviceCategory.POWER_SWITCH)
            .order_by(Device.active.desc(), Device.sort_order, Device.id)
        ).unique()
    )


@router.get("/history", response_class=HTMLResponse, name="history")
def history(
    request: Request,
    date: date | None = None,
    month: Annotated[str | None, Query(pattern=r"^\d{4}-\d{2}$")] = None,
    device: str | None = None,  # 「すべて」は空文字で送られる
    session: Session = Depends(get_session),
) -> HTMLResponse:
    tz = ZoneInfo(get_settings().tz)
    device_id = int(device) if device and device.isdigit() else None
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
            "logs": logs_for(session, target, tz, device_id),
            "devices": (switches := _switches(session)),
            "device_names": {d.id: d.name for d in switches},
            "device_filter": device_id,
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
