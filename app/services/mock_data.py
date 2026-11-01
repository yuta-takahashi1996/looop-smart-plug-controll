"""Phase 2（Web UI基盤）用のモックデータ。

Looop 予報取得（Phase 3）や最適化エンジン（Phase 4）が実装されるまでの間、
画面表示を確認するためのデータを返す。値は日付から決定的に生成する。
実データへ切り替えたら、このモジュールは削除する。
"""

import hashlib
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Literal
from zoneinfo import ZoneInfo

from app.config import Settings

PlugState = Literal["ON", "OFF"]
ControlSource = Literal["AUTO", "MANUAL", "SAFETY"]
ControlMode = Literal["GRID", "BATTERY", "SAFE", "MANUAL"]

SLOT = timedelta(minutes=30)

# 時刻ごとの基準単価（円/kWh）。夜間・日中は安く、夕方にピークがある形
_HOURLY_BASE = [
    11, 10, 10, 9, 9, 10, 13, 17, 19, 16, 12, 10,
    9, 9, 11, 14, 22, 38, 46, 34, 24, 18, 15, 12,
]  # fmt: skip

# モック上の「高額時間帯」とみなす単価（円/kWh）
_OFF_THRESHOLD = 30.0


@dataclass(frozen=True)
class PriceSlot:
    start: datetime
    end: datetime
    price: float  # 電源料金単価（円/kWh）
    planned_state: PlugState


@dataclass(frozen=True)
class Forecast:
    day: date
    slots: list[PriceSlot]
    fetched_at: datetime | None
    error: str | None = None


@dataclass(frozen=True)
class ScheduleEntry:
    start: datetime
    end: datetime
    price: float  # 区間の平均予報単価
    target_state: PlugState
    reason: str
    estimated_saving: float  # 円
    status: Literal["DONE", "PENDING"]


@dataclass(frozen=True)
class ControlLogEntry:
    executed_at: datetime
    source: ControlSource
    previous_state: PlugState
    requested_state: PlugState
    actual_state: PlugState | None
    result: Literal["SUCCESS", "FAILED", "UNCONFIRMED"]
    reason: str
    error_message: str | None = None


@dataclass(frozen=True)
class DashboardSummary:
    current_price: float
    current_effective_price: float  # 往復効率を考慮した蓄電池利用時の実効単価
    next_price: float
    day_max_price: float
    day_min_price: float
    forecast_fetched_at: datetime | None
    auto_control_enabled: bool
    mode: ControlMode
    plug_state: PlugState
    next_switch: ScheduleEntry | None
    last_log: ControlLogEntry | None
    system_status: Literal["OK", "WARNING", "ERROR"]
    today_saving: float
    month_saving: float
    today_switch_count: int
    today_off_minutes: int


def now(settings: Settings) -> datetime:
    return datetime.now(ZoneInfo(settings.tz))


def _noise(day: date, index: int) -> float:
    digest = hashlib.sha256(f"{day.isoformat()}:{index}".encode()).digest()
    return (digest[0] / 255 - 0.5) * 6  # ±3円


def get_forecast(settings: Settings, day: date) -> Forecast:
    tz = ZoneInfo(settings.tz)
    slots = []
    for index in range(48):
        start = datetime.combine(day, time(), tzinfo=tz) + SLOT * index
        price = round(max(_HOURLY_BASE[index // 2] + _noise(day, index), 1.0), 2)
        in_operation = settings.operation_start <= start.time() < settings.operation_end
        planned: PlugState = "OFF" if in_operation and price >= _OFF_THRESHOLD else "ON"
        slots.append(PriceSlot(start=start, end=start + SLOT, price=price, planned_state=planned))

    today = now(settings).date()
    fetched_at = datetime.combine(min(day, today), time(6, 5), tzinfo=tz)
    if day > today:
        fetched_at = datetime.combine(today, time(10, 5), tzinfo=tz)
    return Forecast(day=day, slots=slots, fetched_at=fetched_at)


def get_schedule(settings: Settings, day: date) -> list[ScheduleEntry]:
    """予報の ON/OFF 予定を、状態が切り替わる区間ごとにまとめる。"""
    slots = get_forecast(settings, day).slots
    current = now(settings)
    entries: list[ScheduleEntry] = []
    group: list[PriceSlot] = []

    def flush() -> None:
        if not group:
            return
        state = group[0].planned_state
        avg = sum(s.price for s in group) / len(group)
        if state == "OFF":
            kwh = settings.estimated_average_power_w / 1000 * 0.5 * len(group)
            charge_cost = kwh / settings.round_trip_efficiency * min(s.price for s in slots)
            saving = round(avg * kwh - charge_cost, 1)
            reason = f"高額時間帯（平均 {avg:.1f}円/kWh）のため蓄電池へ切り替え"
        else:
            saving = 0.0
            reason = "単価が低いため商用電源を利用"
        entries.append(
            ScheduleEntry(
                start=group[0].start,
                end=group[-1].end,
                price=round(avg, 1),
                target_state=state,
                reason=reason,
                estimated_saving=saving,
                status="DONE" if group[0].start <= current else "PENDING",
            )
        )

    for slot in slots:
        if not settings.operation_start <= slot.start.time() < settings.operation_end:
            continue
        if group and group[-1].planned_state != slot.planned_state:
            flush()
            group = []
        group.append(slot)
    flush()
    return entries


def get_history(settings: Settings, day: date) -> list[ControlLogEntry]:
    tz = ZoneInfo(settings.tz)
    current = now(settings)
    logs: list[ControlLogEntry] = [
        ControlLogEntry(
            executed_at=datetime.combine(day, settings.operation_start, tzinfo=tz),
            source="SAFETY",
            previous_state="ON",
            requested_state="ON",
            actual_state="ON",
            result="SUCCESS",
            reason="運用開始時の状態確認",
        )
    ]
    previous: PlugState = "ON"
    for entry in get_schedule(settings, day):
        if entry.target_state == previous:
            continue
        logs.append(
            ControlLogEntry(
                executed_at=entry.start,
                source="AUTO",
                previous_state=previous,
                requested_state=entry.target_state,
                actual_state=entry.target_state,
                result="SUCCESS",
                reason=entry.reason,
            )
        )
        previous = entry.target_state
    logs.append(
        ControlLogEntry(
            executed_at=datetime.combine(day, time(12, 10), tzinfo=tz),
            source="MANUAL",
            previous_state="ON",
            requested_state="ON",
            actual_state=None,
            result="UNCONFIRMED",
            reason="Web UI からの手動操作",
            error_message="状態取得がタイムアウトしました（モック）",
        )
    )
    logs.append(
        ControlLogEntry(
            executed_at=datetime.combine(day, settings.operation_end, tzinfo=tz),
            source="SAFETY",
            previous_state=previous,
            requested_state="ON",
            actual_state="ON",
            result="SUCCESS",
            reason="運用終了時刻のため ON へ復帰",
        )
    )
    return sorted((log for log in logs if log.executed_at <= current), key=lambda log: log.executed_at, reverse=True)


def get_dashboard_summary(settings: Settings) -> DashboardSummary:
    current = now(settings)
    forecast = get_forecast(settings, current.date())
    index = (current.hour * 60 + current.minute) // 30
    slot = forecast.slots[index]
    next_slot = forecast.slots[index + 1] if index + 1 < 48 else get_forecast(settings, current.date() + timedelta(days=1)).slots[0]
    min_price = min(s.price for s in forecast.slots)

    schedule = get_schedule(settings, current.date())
    next_switch = next((e for e in schedule if e.start > current), None)
    history = get_history(settings, current.date())
    done_off = [e for e in schedule if e.target_state == "OFF" and e.start <= current]
    off_minutes = sum(int((min(e.end, current) - e.start).total_seconds() // 60) for e in done_off)
    today_saving = round(sum(e.estimated_saving for e in done_off), 1)

    return DashboardSummary(
        current_price=slot.price,
        current_effective_price=round(min_price / settings.round_trip_efficiency, 2),
        next_price=next_slot.price,
        day_max_price=max(s.price for s in forecast.slots),
        day_min_price=min_price,
        forecast_fetched_at=forecast.fetched_at,
        auto_control_enabled=True,
        mode="BATTERY" if slot.planned_state == "OFF" else "GRID",
        plug_state=slot.planned_state,
        next_switch=next_switch,
        last_log=history[0] if history else None,
        system_status="OK",
        today_saving=today_saving,
        month_saving=round(today_saving + 18.4 * (current.day - 1), 1),
        today_switch_count=sum(1 for log in history if log.source == "AUTO"),
        today_off_minutes=off_minutes,
    )
