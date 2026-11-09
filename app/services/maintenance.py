"""長期運用のための定期処理（Phase 7）。

- 日次集計：前日分の実績を daily_summaries に保存する（毎日 0:05、起動時は直近の未集計分も）。
- バックアップ：SQLite のオンラインバックアップで data/backups/ に日付ごとに保存し、指定世代を残す。
- 古いデータの削除：保持期間を過ぎた制御履歴・スケジュール・料金予報と、解決済みのイベントを削除する。
  日次集計は削除しない（長期の実績として残す）。
"""

import logging
import sqlite3
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from sqlalchemy import delete, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from app.config import Settings
from app.models.forecast import ForecastPrice
from app.models.schedule import ControlLog, ControlResult, ControlSchedule, DailySummary, ScheduleDevice, ScheduleStatus
from app.models.settings import EventCategory, SystemEvent
from app.services import optimizer, safety

logger = logging.getLogger(__name__)

SUMMARY_LOOKBACK_DAYS = 7  # 起動時に未集計の日を探す範囲
EVENT_RETENTION_DAYS = 90  # 解決済みイベントの保持期間


def measured_rows(schedules: list[ControlSchedule]) -> list[ScheduleDevice]:
    """実測値のある機器ごとの実行結果。同じスケジュールで同じ給電先の機器は同じ値のため、1件だけ数える。"""
    rows: dict[tuple[int, int], ScheduleDevice] = {}
    for s in schedules:
        for sd in s.devices:
            if sd.battery_start_percent is None and sd.measured_power_w is None:
                continue
            rows.setdefault((s.id, sd.power_source_id or -sd.device_id), sd)
    return list(rows.values())


def _day_range(day: date, tz: ZoneInfo) -> tuple[datetime, datetime]:
    start = datetime.combine(day, time(), tzinfo=tz)
    return start, start + timedelta(days=1)


def summarize_day(session: Session, settings: Settings, day: date) -> DailySummary:
    """対象日の実績を集計して保存する（同じ日を再集計した場合は上書きする）。"""
    tz = ZoneInfo(settings.tz)
    start, end = _day_range(day, tz)
    schedules = optimizer.schedules_for(session, day)
    logs = list(session.scalars(select(ControlLog).where(ControlLog.executed_at >= start, ControlLog.executed_at < end)))

    rows = measured_rows(schedules)
    measured = [s for s in rows if s.battery_start_percent is not None and s.battery_end_percent is not None]
    powered = [s for s in rows if s.measured_power_w is not None and s.executed_start_at and s.executed_end_at]
    weight = sum((s.executed_end_at - s.executed_start_at).total_seconds() for s in powered)  # type: ignore[operator]

    summary = session.get(DailySummary, day) or DailySummary(summary_date=day)
    summary.off_minutes = safety.off_minutes(schedules, end)
    summary.switch_count = sum(1 for log in logs if log.previous_state is not None and log.previous_state != log.requested_state)
    summary.schedules_done = sum(1 for s in schedules if s.status == ScheduleStatus.DONE)
    summary.schedules_aborted = sum(1 for s in schedules if s.status == ScheduleStatus.ABORTED)
    summary.schedules_skipped = sum(1 for s in schedules if s.status == ScheduleStatus.SKIPPED)
    summary.estimated_saving = round(sum(s.executed_saving or 0.0 for s in schedules), 1)
    summary.battery_used_percent = (
        sum(s.battery_start_percent - s.battery_end_percent for s in measured) if measured else None  # type: ignore[operator]
    )
    summary.measured_power_w = (
        round(
            sum(s.measured_power_w * (s.executed_end_at - s.executed_start_at).total_seconds() for s in powered)  # type: ignore[operator]
            / weight,
            1,
        )
        if weight
        else None
    )
    summary.failed_operations = sum(1 for log in logs if log.result in (ControlResult.FAILED, ControlResult.UNCONFIRMED))
    summary.dry_run = any(log.dry_run for log in logs)
    summary.updated_at = datetime.now(UTC)
    session.add(summary)
    session.commit()
    return summary


def summarize_pending(session: Session, settings: Settings, today: date) -> list[date]:
    """前日分を集計し直し、直近の未集計の日も集計する。集計した日付を返す。"""
    done = set(session.scalars(select(DailySummary.summary_date)))
    days = []
    for offset in range(SUMMARY_LOOKBACK_DAYS, 0, -1):
        day = today - timedelta(days=offset)
        if offset == 1 or day not in done:
            # 記録がまったくない日（運用開始前など）は作らない
            tz = ZoneInfo(settings.tz)
            start, end = _day_range(day, tz)
            has_records = session.scalar(
                select(ControlLog.id).where(ControlLog.executed_at >= start, ControlLog.executed_at < end).limit(1)
            ) or optimizer.schedules_for(session, day)
            if has_records:
                summarize_day(session, settings, day)
                days.append(day)
    return days


def monthly_summaries(session: Session, year: int, month: int) -> list[DailySummary]:
    first = date(year, month, 1)
    next_month = date(year + (month == 12), month % 12 + 1, 1)
    return list(
        session.scalars(
            select(DailySummary)
            .where(DailySummary.summary_date >= first, DailySummary.summary_date < next_month)
            .order_by(DailySummary.summary_date)
        )
    )


def _sqlite_path(database_url: str) -> Path | None:
    url = make_url(database_url)
    if url.get_backend_name() != "sqlite" or url.database in (None, "", ":memory:"):
        return None
    return Path(url.database)


def backup_database(settings: Settings, today: date) -> Path | None:
    """SQLite をオンラインバックアップし、古い世代を削除する。SQLite 以外・インメモリでは何もしない。"""
    source = _sqlite_path(settings.database_url)
    if source is None or settings.backup_generations <= 0:
        return None
    backup_dir = source.parent / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    target = backup_dir / f"app-{today:%Y%m%d}.db"
    with sqlite3.connect(source) as src, sqlite3.connect(target) as dst:
        src.backup(dst)
    backups = sorted(backup_dir.glob("app-*.db"))
    for old in backups[: -settings.backup_generations]:
        old.unlink()
    logger.info("database backed up to %s (%d generations kept)", target, min(len(backups), settings.backup_generations))
    return target


def cleanup_old_data(session: Session, settings: Settings, now: datetime) -> dict[str, int]:
    """保持期間を過ぎたデータを削除する。削除した件数を返す。"""
    tz = ZoneInfo(settings.tz)
    cutoff = now - timedelta(days=settings.data_retention_days)
    cutoff_date = cutoff.astimezone(tz).date()
    deleted = {
        "control_logs": session.execute(delete(ControlLog).where(ControlLog.executed_at < cutoff)).rowcount,
        "control_schedules": session.execute(
            delete(ControlSchedule).where(ControlSchedule.schedule_date < cutoff_date)
        ).rowcount,
        "forecast_prices": session.execute(delete(ForecastPrice).where(ForecastPrice.slot_start < cutoff)).rowcount,
        "system_events": session.execute(
            delete(SystemEvent).where(
                SystemEvent.resolved_at.is_not(None),
                SystemEvent.resolved_at < now - timedelta(days=EVENT_RETENTION_DAYS),
                # お知らせは重複検知（同じお知らせを再通知しない）に使うため残す
                SystemEvent.category != EventCategory.PRICE_NOTICE,
            )
        ).rowcount,
    }
    session.commit()
    # WAL を本体に反映して縮め、クエリプランの統計を更新する
    if _sqlite_path(settings.database_url) is not None:
        session.execute(text("PRAGMA wal_checkpoint(TRUNCATE)"))
        session.execute(text("PRAGMA optimize"))
    if any(deleted.values()):
        logger.info("old data deleted: %s", deleted)
    return deleted


TUNING_LOOKBACK_DAYS = 30
TUNING_MIN_DROP_PERCENT = 5  # 残量は整数％のため、減少が小さい回は効率の推定から除く


def tuning_hints(session: Session, settings: Settings, capacity_wh: float, today: date) -> dict:
    """制御パラメータ調整の目安：直近の OFF の実測から、平均消費電力と変換効率を推定する。"""
    recent = list(
        session.scalars(
            select(ControlSchedule).where(ControlSchedule.schedule_date >= today - timedelta(days=TUNING_LOOKBACK_DAYS))
        )
    )
    schedules = [
        sd for sd in measured_rows(recent) if sd.measured_power_w is not None and sd.executed_start_at and sd.executed_end_at
    ]
    hours = [(s.executed_end_at - s.executed_start_at).total_seconds() / 3600 for s in schedules]  # type: ignore[operator]
    total_hours = sum(hours)
    average_power = (
        round(sum(s.measured_power_w * h for s, h in zip(schedules, hours)) / total_hours, 0) if total_hours else None  # type: ignore[operator]
    )
    usable = [
        (s, h)
        for s, h in zip(schedules, hours)
        if s.battery_start_percent is not None
        and s.battery_end_percent is not None
        and s.battery_start_percent - s.battery_end_percent >= TUNING_MIN_DROP_PERCENT
    ]
    output_wh = sum(s.measured_power_w * h for s, h in usable)  # type: ignore[operator]
    battery_wh = sum((s.battery_start_percent - s.battery_end_percent) / 100 * capacity_wh for s, _ in usable)  # type: ignore[operator]
    efficiency = round(min(output_wh / battery_wh, 1.0) * 100) if battery_wh else None
    return {
        "count": len(schedules),
        "average_power_w": average_power,
        "efficiency_count": len(usable),
        "efficiency_percent": efficiency,
        "days": TUNING_LOOKBACK_DAYS,
    }
