"""制御スケジュールの生成と手動変更の検証（docs/07-control-logic.md §7.4〜7.6）。

でんき予報の注意報・警報が連続するコマから OFF スケジュールを作る。
- 警報：常に有効。生成時には OFF 時間の制限で切り詰めない（残量を取得できない間の制限は実行時に適用する）。
- 注意報：AUTO_ENABLE が有効なら有効。運用時間・最大連続OFF時間・1日の最大OFF時間に収まるよう、
  電源単価の高いコマを優先して切り詰める。
- 最低切り替え間隔：スケジュール同士の間隔は 0（連続）か最低切り替え間隔以上とする。
手動で変更したスケジュールと、開始済みのスケジュールは再生成で上書きしない。
"""

import math
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.models.schedule import ControlSchedule, EnabledBy
from app.models.settings import ControlSettings, load_control_settings
from app.providers.base import ForecastLevel
from app.services import forecast as forecast_service
from app.services.forecast import PriceSlot

SLOT_MINUTES = 30
SLOT = timedelta(minutes=SLOT_MINUTES)
OFF_LEVELS = (ForecastLevel.ADVISORY, ForecastLevel.WARNING)
LEVEL_LABELS = {ForecastLevel.ADVISORY: "でんき注意報", ForecastLevel.WARNING: "でんき警報"}


@dataclass(frozen=True)
class PlanParams:
    operation_start: time
    operation_end: time
    max_continuous_slots: int
    max_daily_slots: int
    min_gap_slots: int  # スケジュール間の最低間隔（コマ数）
    auto_enable: bool
    round_trip_efficiency: float
    average_power_w: float

    @classmethod
    def from_settings(cls, control: ControlSettings) -> "PlanParams":
        return cls(
            operation_start=control.operation_start,
            operation_end=control.operation_end,
            max_continuous_slots=control.max_continuous_off_minutes // SLOT_MINUTES,
            max_daily_slots=control.max_daily_off_minutes // SLOT_MINUTES,
            min_gap_slots=math.ceil(control.min_switch_interval_minutes / SLOT_MINUTES),
            auto_enable=control.auto_enable,
            round_trip_efficiency=control.round_trip_efficiency,
            average_power_w=control.estimated_average_power_w,
        )


@dataclass(frozen=True)
class Draft:
    """生成するスケジュール。start / end はコマ番号（end は含まない）。"""

    start: int
    end: int
    level: ForecastLevel
    enabled: bool
    notes: tuple[str, ...] = ()

    @property
    def length(self) -> int:
        return self.end - self.start


# --- 計算（DB に依存しない） ---


def in_operation(slot: PriceSlot, params: PlanParams) -> bool:
    """コマ全体が運用時間内か（23:30〜24:00 のコマは終了が翌日になるため、運用時間外として扱う）。"""
    return (
        slot.end.date() == slot.start.date()
        and params.operation_start <= slot.start.time()
        and slot.end.time() <= params.operation_end
    )


def estimated_saving(slots: list[PriceSlot], start: int, end: int, params: PlanParams) -> float:
    """OFF 時間帯の電源単価で回避できる購入費 − 運用時間内の最安の電源単価で充電する費用（docs/07 §7.2）。"""
    if end <= start:
        return 0.0
    operation_prices = [s.energy_price for s in slots if in_operation(s, params)] or [s.energy_price for s in slots]
    kwh_per_slot = params.average_power_w / 1000 * SLOT_MINUTES / 60
    avoided = sum(s.energy_price for s in slots[start:end]) * kwh_per_slot
    charge = min(operation_prices) * kwh_per_slot * (end - start) / params.round_trip_efficiency
    return round(avoided - charge, 1)


def saving_between(slots: list[PriceSlot], start_at: datetime, end_at: datetime, params: PlanParams) -> float:
    """実行した時間帯（コマの途中から・途中までを含む）の推定節約額。計算方法は estimated_saving と同じ。"""
    weights = []
    for slot in slots:
        overlap = (min(slot.end, end_at) - max(slot.start, start_at)).total_seconds()
        weights.append(max(0.0, overlap) / SLOT.total_seconds())
    if sum(weights) == 0:
        return 0.0
    operation_prices = [s.energy_price for s in slots if in_operation(s, params)] or [s.energy_price for s in slots]
    kwh_per_slot = params.average_power_w / 1000 * SLOT_MINUTES / 60
    avoided = sum(s.energy_price * w for s, w in zip(slots, weights, strict=True)) * kwh_per_slot
    charge = min(operation_prices) * kwh_per_slot * sum(weights) / params.round_trip_efficiency
    return round(avoided - charge, 1)


def average_price(slots: list[PriceSlot], start: int, end: int) -> float:
    return sum(s.energy_price for s in slots[start:end]) / (end - start)


def _best_window(slots: list[PriceSlot], start: int, end: int, length: int) -> tuple[int, int]:
    """[start, end) のうち、電源単価の合計が最大になる長さ length の区間（同点は早い方）。"""
    best = start
    best_sum = sum(s.energy_price for s in slots[start : start + length])
    current = best_sum
    for i in range(start + 1, end - length + 1):
        current += slots[i + length - 1].energy_price - slots[i - 1].energy_price
        if current > best_sum + 1e-9:
            best, best_sum = i, current
    return best, best + length


def _runs(slots: list[PriceSlot], available: list[bool]) -> list[tuple[int, int, ForecastLevel]]:
    """注意報・警報が同じ区分で連続する区間。"""
    runs = []
    i = 0
    while i < len(slots):
        level = slots[i].level
        if not available[i] or level not in OFF_LEVELS:
            i += 1
            continue
        j = i
        while j < len(slots) and available[j] and slots[j].level == level:
            j += 1
        runs.append((i, j, level))
        i = j
    return runs


def plan_day(
    slots: list[PriceSlot],
    params: PlanParams,
    *,
    blocked: list[tuple[int, int]] = (),
    first_available: int = 0,
    used_slots: int = 0,
) -> list[Draft]:
    """1日分（48 コマ）のスケジュールを計画する。

    blocked: 残すスケジュール（手動変更済み・開始済み）のコマ範囲。この範囲と最低間隔の内側には作らない。
    first_available: これより前のコマには作らない（当日の経過済みのコマ）。
    used_slots: 残すスケジュールのうち有効なものの OFF コマ数（1日の最大OFF時間に含める）。
    """
    available = [i >= first_available for i in range(len(slots))]
    for b_start, b_end in blocked:
        for i in range(max(0, b_start - params.min_gap_slots), min(len(slots), b_end + params.min_gap_slots)):
            available[i] = False

    drafts: list[Draft] = []
    candidates: list[Draft] = []
    for start, end, level in _runs(slots, available):
        if level == ForecastLevel.WARNING:
            drafts.append(Draft(start, end, level, enabled=True))
            continue
        # 注意報：運用時間内に限り、最大連続OFF時間に収める
        inside = [i for i in range(start, end) if in_operation(slots[i], params)]
        if not inside:
            continue
        notes = ("運用時間外を除外",) if len(inside) < end - start else ()
        c_start, c_end = inside[0], inside[-1] + 1
        if c_end - c_start > params.max_continuous_slots:
            if params.max_continuous_slots <= 0:
                continue
            c_start, c_end = _best_window(slots, c_start, c_end, params.max_continuous_slots)
            notes += ("最大連続OFF時間に合わせて短縮",)
        candidates.append(Draft(c_start, c_end, level, enabled=params.auto_enable, notes=notes))

    # 1日の最大OFF時間：警報・残すスケジュールを差し引いた残りを、電源単価の高い注意報から割り当てる
    remaining = params.max_daily_slots - used_slots - sum(d.length for d in drafts)
    min_length = max(1, params.min_gap_slots)
    for candidate in sorted(candidates, key=lambda d: (-average_price(slots, d.start, d.end), d.start)):
        if remaining <= 0:
            break
        if candidate.length <= remaining:
            drafts.append(candidate)
            remaining -= candidate.length
        elif remaining >= min_length:
            start, end = _best_window(slots, candidate.start, candidate.end, remaining)
            drafts.append(Draft(start, end, candidate.level, candidate.enabled, candidate.notes + ("1日の最大OFF時間に合わせて短縮",)))
            remaining = 0

    return _enforce_gaps(slots, sorted(drafts, key=lambda d: d.start), blocked, params.min_gap_slots)


def _enforce_gaps(
    slots: list[PriceSlot], drafts: list[Draft], blocked: list[tuple[int, int]], min_gap: int
) -> list[Draft]:
    """間隔が 0 より大きく最低切り替え間隔未満になる注意報のスケジュールを、単価の低い方から除外する。"""
    while True:
        ranges = sorted([(d.start, d.end, d) for d in drafts] + [(s, e, None) for s, e in blocked], key=lambda r: r[0])
        conflict = None
        for (a_start, a_end, a), (b_start, b_end, b) in zip(ranges, ranges[1:]):
            gap = b_start - a_end
            if 0 < gap < min_gap:
                conflict = (a, b)
                break
        if conflict is None:
            return drafts
        removable = [d for d in conflict if d is not None and d.level == ForecastLevel.ADVISORY]
        if not removable:
            return drafts  # 警報同士・残すスケジュールとの組み合わせは変更しない
        drafts = [d for d in drafts if d is not min(removable, key=lambda d: average_price(slots, d.start, d.end))]


def describe(slots: list[PriceSlot], draft: Draft) -> str:
    reason = f"{LEVEL_LABELS[draft.level]}（平均 {average_price(slots, draft.start, draft.end):.1f}円/kWh）"
    return reason + "".join(f"・{note}" for note in draft.notes)


# --- DB との連携 ---


def _day_start(day: date, tz: ZoneInfo) -> datetime:
    return datetime.combine(day, time(), tzinfo=tz)


def _slot_range(schedule: ControlSchedule, tz: ZoneInfo) -> tuple[int, int]:
    base = _day_start(schedule.schedule_date, tz)
    return (
        int((schedule.start_at - base) / SLOT),
        int((schedule.end_at - base) / SLOT),
    )


def schedules_for(session: Session, day: date) -> list[ControlSchedule]:
    return list(
        session.scalars(
            select(ControlSchedule).where(ControlSchedule.schedule_date == day).order_by(ControlSchedule.start_at)
        )
    )


def regenerate(session: Session, settings: Settings, day: date, now: datetime | None = None) -> list[ControlSchedule]:
    """対象日のスケジュールを最新の予報で作り直す。予報が未取得の日は何もしない（新規の OFF を作らない）。"""
    tz = ZoneInfo(settings.tz)
    now = now or datetime.now(tz)
    forecast = forecast_service.get_day_forecast(session, settings.looop_area, day, tz)
    if not forecast.slots:
        return schedules_for(session, day)
    params = PlanParams.from_settings(load_control_settings(session, settings))

    existing = schedules_for(session, day)
    kept = [s for s in existing if s.manually_modified or s.start_at <= now]
    replaced = [s.id for s in existing if s not in kept]
    if replaced:
        session.execute(delete(ControlSchedule).where(ControlSchedule.id.in_(replaced)))

    blocked = [_slot_range(s, tz) for s in kept]
    elapsed = sum(1 for slot in forecast.slots if slot.start <= now)
    drafts = plan_day(
        forecast.slots,
        params,
        blocked=blocked,
        first_available=elapsed,
        used_slots=sum(e - s for (s, e), k in zip(blocked, kept) if k.enabled),
    )
    version = (session.scalar(select(func.max(ControlSchedule.schedule_version)).where(ControlSchedule.schedule_date == day)) or 0) + 1
    timestamp = datetime.now(UTC)
    for draft in drafts:
        session.add(
            ControlSchedule(
                schedule_date=day,
                schedule_version=version,
                start_at=forecast.slots[draft.start].start,
                end_at=forecast.slots[draft.end - 1].end,
                target_state="OFF",
                source_level=draft.level,
                enabled=draft.enabled,
                enabled_by=EnabledBy.AUTO,
                manually_modified=False,
                reason=describe(forecast.slots, draft),
                estimated_saving=estimated_saving(forecast.slots, draft.start, draft.end, params),
                updated_at=timestamp,
            )
        )
    session.commit()
    return schedules_for(session, day)


def regenerate_days(session: Session, settings: Settings, days: list[date]) -> None:
    for day in days:
        regenerate(session, settings, day)


# --- 手動変更 ---


class ScheduleChangeError(Exception):
    def __init__(self, messages: list[str]) -> None:
        super().__init__("; ".join(messages))
        self.messages = messages


def _validate(
    session: Session,
    settings: Settings,
    schedule: ControlSchedule,
    start_at: datetime,
    end_at: datetime,
    enabled: bool,
    now: datetime,
) -> list[str]:
    tz = ZoneInfo(settings.tz)
    params = PlanParams.from_settings(load_control_settings(session, settings))
    day_start = _day_start(schedule.schedule_date, tz)
    errors: list[str] = []

    if schedule.start_at <= now:
        return ["開始済みのスケジュールは変更できません"]
    if (start_at - day_start) % SLOT or (end_at - day_start) % SLOT:
        errors.append("時刻は30分単位で指定してください")
    if not day_start <= start_at < end_at <= day_start + timedelta(days=1):
        errors.append("開始・終了時刻は対象日の範囲内で、開始を終了より前にしてください")
    if start_at <= now:
        errors.append("開始時刻は現在より後にしてください")
    if errors:
        return errors

    gap_minutes = params.min_gap_slots * SLOT_MINUTES
    others = [s for s in schedules_for(session, schedule.schedule_date) if s.id != schedule.id]
    for other in others:
        if start_at < other.end_at and other.start_at < end_at:
            errors.append(f"{other.start_at.astimezone(tz):%H:%M}〜{other.end_at.astimezone(tz):%H:%M} のスケジュールと重なります")
            continue
        gap = min(abs(start_at - other.end_at), abs(other.start_at - end_at))
        if timedelta(0) < gap < timedelta(minutes=gap_minutes):
            errors.append(
                f"{other.start_at.astimezone(tz):%H:%M}〜{other.end_at.astimezone(tz):%H:%M} のスケジュールとの間隔が"
                f"最低切り替え間隔（{gap_minutes}分）未満です"
            )

    if schedule.source_level == ForecastLevel.ADVISORY:
        local_start, local_end = start_at.astimezone(tz), end_at.astimezone(tz)
        end_time = local_end.time() if local_end.date() == local_start.date() else time(23, 59, 59)
        if local_start.time() < params.operation_start or end_time > params.operation_end:
            errors.append(
                f"でんき注意報のスケジュールは運用時間（{params.operation_start:%H:%M}〜{params.operation_end:%H:%M}）内にしてください"
            )
        length = int((end_at - start_at) / SLOT)
        if length > params.max_continuous_slots:
            errors.append(f"でんき注意報のスケジュールは最大連続OFF時間（{params.max_continuous_slots * SLOT_MINUTES}分）以内にしてください")
        if enabled:
            total = length + sum(int((s.end_at - s.start_at) / SLOT) for s in others if s.enabled)
            if total > params.max_daily_slots:
                errors.append(
                    f"有効なスケジュールの合計が1日の最大OFF時間（{params.max_daily_slots * SLOT_MINUTES}分）を超えます"
                    f"（合計 {total * SLOT_MINUTES}分）"
                )
    return errors


def update_schedule(
    session: Session,
    settings: Settings,
    schedule: ControlSchedule,
    *,
    start_at: datetime | None = None,
    end_at: datetime | None = None,
    enabled: bool | None = None,
    now: datetime | None = None,
) -> ControlSchedule:
    """有効・無効の切り替え、または時間変更。変更したスケジュールは手動変更済みとし、再生成で上書きしない。"""
    tz = ZoneInfo(settings.tz)
    now = now or datetime.now(tz)
    new_start = start_at or schedule.start_at
    new_end = end_at or schedule.end_at
    new_enabled = schedule.enabled if enabled is None else enabled

    errors = _validate(session, settings, schedule, new_start, new_end, new_enabled, now)
    if errors:
        raise ScheduleChangeError(errors)

    if enabled is not None and enabled != schedule.enabled:
        schedule.enabled = enabled
        schedule.enabled_by = EnabledBy.MANUAL
    if start_at is not None or end_at is not None:
        forecast = forecast_service.get_day_forecast(session, settings.looop_area, schedule.schedule_date, tz)
        if forecast.slots:
            params = PlanParams.from_settings(load_control_settings(session, settings))
            base = _day_start(schedule.schedule_date, tz)
            s, e = int((new_start - base) / SLOT), int((new_end - base) / SLOT)
            schedule.estimated_saving = estimated_saving(forecast.slots, s, e, params)
            schedule.reason = f"{LEVEL_LABELS[schedule.source_level]}（平均 {average_price(forecast.slots, s, e):.1f}円/kWh）・時間を手動変更"
        schedule.start_at, schedule.end_at = new_start, new_end
    schedule.manually_modified = True
    schedule.updated_at = datetime.now(UTC)
    session.commit()
    return schedule
