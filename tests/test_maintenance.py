from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import device as device_models
from app.models.forecast import ForecastPrice
from app.models.schedule import (
    ControlLog,
    ControlResult,
    ControlSchedule,
    ControlSource,
    DailySummary,
    EnabledBy,
    PlugState,
    ScheduleDevice,
    ScheduleDeviceStatus,
    ScheduleStatus,
)
from app.models.settings import EventCategory, EventLevel, SystemEvent, record_event
from app.providers.base import ForecastLevel
from app.services import maintenance
from tests.conftest import JST

pytestmark = pytest.mark.usefixtures("default_devices")

DAY = date(2030, 1, 15)


def at(day: date, hour: int, minute: int = 0) -> datetime:
    return datetime.combine(day, time(hour, minute), tzinfo=JST)


DEVICE_FIELDS = ("executed_start_at", "executed_end_at", "battery_start_percent", "battery_end_percent", "measured_power_w")


def add_schedule(session: Session, day: date, start: int, end: int, status: ScheduleStatus, **fields) -> ControlSchedule:
    """スケジュールを追加する。実行した時間帯・実測値は既定のプラグ（給電先は Jackery）の行に入れる。"""
    plug = device_models.active_switches(session)[0]
    device_fields = {key: fields.pop(key) for key in DEVICE_FIELDS if key in fields}
    measured = "battery_start_percent" in device_fields or "measured_power_w" in device_fields
    row = ScheduleDevice(
        device_id=plug.id,
        status=ScheduleDeviceStatus(status.value),
        power_source_id=plug.power_source_id if measured else None,
        **device_fields,
    )
    s = ControlSchedule(
        devices=[row],
        schedule_date=day,
        schedule_version=1,
        start_at=at(day, start),
        end_at=at(day, end),
        target_state="OFF",
        source_level=ForecastLevel.ADVISORY,
        enabled=True,
        enabled_by=EnabledBy.AUTO,
        manually_modified=False,
        reason="",
        estimated_saving=0,
        status=status,
        updated_at=datetime.now(UTC),
        **fields,
    )
    session.add(s)
    session.commit()
    return s


def add_log(session: Session, when: datetime, previous: PlugState | None, requested: PlugState, result=ControlResult.SUCCESS, dry_run=False) -> None:
    session.add(
        ControlLog(
            executed_at=when, source=ControlSource.AUTO, previous_state=previous, requested_state=requested,
            actual_state=requested, result=result, reason="", dry_run=dry_run,
        )
    )  # fmt: skip
    session.commit()


def test_summarize_day(session: Session) -> None:
    add_schedule(
        session, DAY, 10, 12, ScheduleStatus.DONE,
        executed_start_at=at(DAY, 10), executed_end_at=at(DAY, 12), executed_saving=12.3,
        battery_start_percent=100, battery_end_percent=60, measured_power_w=200.0,
    )  # fmt: skip
    add_schedule(
        session, DAY, 16, 17, ScheduleStatus.ABORTED,
        executed_start_at=at(DAY, 16), executed_end_at=at(DAY, 16, 30), executed_saving=1.2,
        battery_start_percent=60, battery_end_percent=50, measured_power_w=100.0,
    )  # fmt: skip
    add_schedule(session, DAY, 19, 20, ScheduleStatus.SKIPPED)
    add_log(session, at(DAY, 0), None, PlugState.ON)  # 初期化（切り替えに数えない）
    add_log(session, at(DAY, 10), PlugState.ON, PlugState.OFF)
    add_log(session, at(DAY, 12), PlugState.OFF, PlugState.ON)
    add_log(session, at(DAY, 16), PlugState.ON, PlugState.OFF, result=ControlResult.FAILED)
    add_log(session, at(DAY + timedelta(days=1), 0), PlugState.ON, PlugState.OFF)  # 翌日分は含めない

    summary = maintenance.summarize_day(session, get_settings(), DAY)

    assert summary.off_minutes == 150
    assert summary.switch_count == 3
    assert (summary.schedules_done, summary.schedules_aborted, summary.schedules_skipped) == (1, 1, 1)
    assert summary.estimated_saving == pytest.approx(13.5)
    assert summary.battery_used_percent == 50
    assert summary.measured_power_w == pytest.approx((200 * 120 + 100 * 30) / 150, abs=0.1)
    assert summary.failed_operations == 1
    assert summary.dry_run is False


def test_summarize_pending_skips_days_without_records(session: Session) -> None:
    today = DAY
    add_log(session, at(DAY - timedelta(days=3), 9), None, PlugState.ON, result=ControlResult.DRY_RUN, dry_run=True)
    add_log(session, at(DAY - timedelta(days=1), 9), None, PlugState.ON)

    days = maintenance.summarize_pending(session, get_settings(), today)

    assert days == [DAY - timedelta(days=3), DAY - timedelta(days=1)]
    assert session.get(DailySummary, DAY - timedelta(days=3)).dry_run is True
    # 前日分は毎回集計し直す（遅れて終わったスケジュールなどを反映するため）。それ以外は集計済みなら飛ばす
    assert maintenance.summarize_pending(session, get_settings(), today) == [DAY - timedelta(days=1)]


def test_cleanup_old_data(session: Session) -> None:
    settings = get_settings()  # 保持期間 400日
    now = at(DAY, 12)
    old, recent = now - timedelta(days=401), now - timedelta(days=10)
    add_log(session, old, PlugState.ON, PlugState.OFF)
    add_log(session, recent, PlugState.ON, PlugState.OFF)
    add_schedule(session, old.date(), 10, 11, ScheduleStatus.DONE)
    add_schedule(session, recent.date(), 10, 11, ScheduleStatus.DONE)
    for when in (old, recent):
        session.add(ForecastPrice(area="hokkaido", slot_start=when, slot_end=when + timedelta(minutes=30), energy_price=10, fetched_at=when, source="t"))
    session.commit()
    for category in (EventCategory.FORECAST, EventCategory.PRICE_NOTICE):
        event = record_event(session, EventLevel.INFO, category, "old")
        event.resolved_at = now - timedelta(days=91)
        session.commit()
    session.add(DailySummary(
        summary_date=old.date(), off_minutes=0, switch_count=0, schedules_done=0, schedules_aborted=0, schedules_skipped=0,
        estimated_saving=0, failed_operations=0, dry_run=False, updated_at=now,
    ))  # fmt: skip
    session.commit()

    deleted = maintenance.cleanup_old_data(session, settings, now)

    assert deleted == {"control_logs": 1, "control_schedules": 1, "forecast_prices": 1, "system_events": 1}
    assert session.scalar(select(func.count()).select_from(ControlLog)) == 1
    # お知らせ（重複検知に使う）と日次集計は残す
    assert session.scalars(select(SystemEvent.category)).all() == [EventCategory.PRICE_NOTICE]
    assert session.get(DailySummary, old.date()) is not None


def test_backup_database_keeps_generations(tmp_path: Path) -> None:
    import sqlite3

    db = tmp_path / "app.db"
    with sqlite3.connect(db) as conn:
        conn.execute("create table t (x)")
        conn.execute("insert into t values (1)")
    settings = get_settings().model_copy(update={"database_url": f"sqlite:///{db}", "backup_generations": 2})

    for offset in range(3):
        maintenance.backup_database(settings, DAY + timedelta(days=offset))

    backups = sorted(p.name for p in (tmp_path / "backups").iterdir())
    assert backups == ["app-20300116.db", "app-20300117.db"]
    with sqlite3.connect(tmp_path / "backups" / backups[-1]) as conn:
        assert conn.execute("select x from t").fetchone() == (1,)


def test_backup_is_skipped_for_in_memory_database() -> None:
    assert maintenance.backup_database(get_settings(), DAY) is None


def test_tuning_hints(session: Session) -> None:
    # 2時間・平均 200W・残量 40% 減（1070Wh の 40% ＝ 428Wh を使って 400Wh 出力 → 効率 約93%）
    add_schedule(
        session, DAY, 10, 12, ScheduleStatus.DONE,
        executed_start_at=at(DAY, 10), executed_end_at=at(DAY, 12),
        battery_start_percent=100, battery_end_percent=60, measured_power_w=200.0,
    )  # fmt: skip
    # 残量の減少が小さい回は効率の推定から除く
    add_schedule(
        session, DAY, 14, 15, ScheduleStatus.DONE,
        executed_start_at=at(DAY, 14), executed_end_at=at(DAY, 14, 30),
        battery_start_percent=60, battery_end_percent=58, measured_power_w=100.0,
    )  # fmt: skip

    hints = maintenance.tuning_hints(session, get_settings(), 1070, DAY)

    assert hints["count"] == 2
    assert hints["average_power_w"] == pytest.approx(round((200 * 2 + 100 * 0.5) / 2.5), abs=1)
    assert hints["efficiency_count"] == 1
    assert hints["efficiency_percent"] == round(400 / 428 * 100)


def test_tuning_hints_without_data(session: Session) -> None:
    hints = maintenance.tuning_hints(session, get_settings(), 1070, DAY)

    assert hints["average_power_w"] is None and hints["efficiency_percent"] is None
