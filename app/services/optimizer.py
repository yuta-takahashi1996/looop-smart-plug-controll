"""制御スケジュールの生成と手動変更の検証（docs/07-control-logic.md §7.4〜7.6）。

でんき予報の注意報・警報が連続するコマから OFF スケジュールを作る。
- 警報：常に有効。生成時には OFF 時間の制限で切り詰めない（残量を取得できない間の制限は実行時に適用する）。
- 注意報：AUTO_ENABLE が有効なら有効。運用時間・最大連続OFF時間・1日の最大OFF時間に収まるよう、
  電源単価の高いコマを優先して切り詰める。
- 最低切り替え間隔：スケジュール同士の間隔は 0（連続）か最低切り替え間隔以上とする。
手動で変更したスケジュールと、開始済みのスケジュールは再生成で上書きしない。
開始・終了時の充電モード（docs/07 §7.10）は、でんき予報のスケジュールは設定の既定値、任意スケジュールはルールの値とする。

任意スケジュール（docs/07 §7.9）：ルール（schedule_rules）を当日・翌日分の ON／OFF スケジュールに展開する。
予報の有無によらず展開し、でんき予報のスケジュールより優先する（予報のスケジュールは任意スケジュールを避けて作る）。
任意 OFF にはでんき注意報と同じ制限（運用時間・最大連続OFF時間・1日の最大OFF時間・最低切り替え間隔）を適用する。

対象機器（docs/17 §17.6）：でんき予報のスケジュールは設定の「でんき予報のスケジュールの対象機器」、任意スケジュールは
ルールの対象機器で作る（制御対象の機器だけ。0台なら作らない）。推定節約額は対象機器ごとの想定平均負荷で算出して合計する。
"""

import math
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import device as device_models
from app.models.device import Device
from app.models.schedule import (
    ChargeModeOption,
    ControlSchedule,
    EnabledBy,
    RepeatType,
    ScheduleDevice,
    ScheduleDeviceStatus,
    ScheduleRule,
    ScheduleSource,
    ScheduleStatus,
)
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


def device_params(params: PlanParams, device: Device | None) -> PlanParams:
    """機器の想定平均負荷（未設定なら全体の設定値）で計算するための値（docs/17 §17.7.5）。"""
    if device is None or device.estimated_power_w is None:
        return params
    return replace(params, average_power_w=device.estimated_power_w)


def devices_saving(slots: list[PriceSlot], start: int, end: int, params: PlanParams, devices: list[Device]) -> float:
    """対象機器ごとの推定節約額の合計。"""
    return round(sum(estimated_saving(slots, start, end, device_params(params, d)) for d in devices), 1)


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
    excluded: list[tuple[int, int]] = (),
    first_available: int = 0,
    used_slots: int = 0,
) -> list[Draft]:
    """1日分（48 コマ）のスケジュールを計画する。

    blocked: 残す OFF のスケジュール（手動変更済み・開始済み・任意 OFF）のコマ範囲。この範囲と最低間隔の内側には作らない。
    excluded: 任意 ON のスケジュールのコマ範囲。この範囲には作らない（切り替えは起きないため最低間隔は空けない）。
    first_available: これより前のコマには作らない（当日の経過済みのコマ）。
    used_slots: 残すスケジュールのうち有効なものの OFF コマ数（1日の最大OFF時間に含める）。
    """
    available = [i >= first_available for i in range(len(slots))]
    for b_start, b_end in blocked:
        for i in range(max(0, b_start - params.min_gap_slots), min(len(slots), b_end + params.min_gap_slots)):
            available[i] = False
    for e_start, e_end in excluded:
        for i in range(max(0, e_start), min(len(slots), e_end)):
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


def forecast_devices(session: Session) -> list[Device]:
    """でんき予報のスケジュールの対象機器（制御対象のものだけ。設定の順）。"""
    active = {d.id: d for d in device_models.active_switches(session)}
    return [active[i] for i in device_models.forecast_schedule_device_ids(session) if i in active]


def _schedule_devices(devices: list[Device]) -> list[ScheduleDevice]:
    return [ScheduleDevice(device_id=d.id, status=ScheduleDeviceStatus.PENDING) for d in devices]


def schedules_for(session: Session, day: date) -> list[ControlSchedule]:
    return list(
        session.scalars(
            select(ControlSchedule).where(ControlSchedule.schedule_date == day).order_by(ControlSchedule.start_at)
        )
    )


def regenerate(session: Session, settings: Settings, day: date, now: datetime | None = None) -> list[ControlSchedule]:
    """対象日のスケジュールを作り直す。

    任意スケジュールはルールから展開し直す（予報の有無によらない）。でんき予報のスケジュールは予報が未取得の日は
    作らない（新規の OFF を作らない）。手動変更済み・開始済みのスケジュールは残す。
    """
    tz = ZoneInfo(settings.tz)
    now = now or datetime.now(tz)
    forecast = forecast_service.get_day_forecast(session, settings.looop_area, day, tz)
    control = load_control_settings(session, settings)
    params = PlanParams.from_settings(control)

    existing = schedules_for(session, day)
    kept = [s for s in existing if s.manually_modified or s.start_at <= now]
    # 予報が未取得の日は、予報のスケジュールを消さない（作り直せないため）
    replaced = [
        s.id for s in existing if s not in kept and (forecast.slots or s.source_level == ScheduleSource.CUSTOM)
    ]
    if replaced:
        # ORM で削除する（対象機器の行も一緒に消し、セッション内に古い行を残さない）
        for schedule in existing:
            if schedule.id in replaced:
                session.delete(schedule)
        session.flush()
        existing = [s for s in existing if s.id not in replaced]

    version = (session.scalar(select(func.max(ControlSchedule.schedule_version)).where(ControlSchedule.schedule_date == day)) or 0) + 1
    timestamp = datetime.now(UTC)
    custom = _expand_rules(session, day, tz, now, existing, params, forecast.slots)
    for schedule in custom:
        schedule.schedule_version, schedule.updated_at = version, timestamp
        session.add(schedule)

    targets = forecast_devices(session)
    # 対象機器が未設定の場合は、でんき予報のスケジュールを作らない（docs/17 §17.6.1）
    if forecast.slots and targets:
        planned = kept + custom
        ranges = [_slot_range(s, tz) for s in planned]
        elapsed = sum(1 for slot in forecast.slots if slot.start <= now)
        drafts = plan_day(
            forecast.slots,
            params,
            blocked=[r for r, s in zip(ranges, planned) if s.target_state == "OFF"],
            excluded=[r for r, s in zip(ranges, planned) if s.target_state == "ON"],
            first_available=elapsed,
            used_slots=sum(e - s for (s, e), p in zip(ranges, planned) if p.enabled and p.target_state == "OFF"),
        )
        for draft in drafts:
            session.add(
                ControlSchedule(
                    schedule_date=day,
                    schedule_version=version,
                    start_at=forecast.slots[draft.start].start,
                    end_at=forecast.slots[draft.end - 1].end,
                    target_state="OFF",
                    source_level=ScheduleSource(draft.level.value),
                    enabled=draft.enabled,
                    enabled_by=EnabledBy.AUTO,
                    manually_modified=False,
                    reason=describe(forecast.slots, draft),
                    estimated_saving=devices_saving(forecast.slots, draft.start, draft.end, params, targets),
                    charge_mode_start=control.charge_mode_start,
                    charge_mode_end=control.charge_mode_end,
                    updated_at=timestamp,
                    devices=_schedule_devices(targets),
                )
            )
    session.commit()
    return schedules_for(session, day)


def regenerate_days(session: Session, settings: Settings, days: list[date], now: datetime | None = None) -> None:
    for day in days:
        regenerate(session, settings, day, now=now)


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
    if schedule.source_level == ScheduleSource.CUSTOM and (start_at, end_at) != (schedule.start_at, schedule.end_at):
        return ["任意スケジュールの時間はルールで変更してください"]
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
        if "ON" in (schedule.target_state, other.target_state):
            continue  # 最低切り替え間隔は OFF のスケジュール同士に適用する
        gap = min(abs(start_at - other.end_at), abs(other.start_at - end_at))
        if timedelta(0) < gap < timedelta(minutes=gap_minutes):
            errors.append(
                f"{other.start_at.astimezone(tz):%H:%M}〜{other.end_at.astimezone(tz):%H:%M} のスケジュールとの間隔が"
                f"最低切り替え間隔（{gap_minutes}分）未満です"
            )

    # でんき注意報と任意 OFF は制限の範囲内に限る（警報・任意 ON は制限を受けない）
    if schedule.target_state == "OFF" and schedule.source_level in (ScheduleSource.ADVISORY, ScheduleSource.CUSTOM):
        label = LEVEL_LABELS.get(schedule.source_level, "任意 OFF")
        local_start, local_end = start_at.astimezone(tz), end_at.astimezone(tz)
        end_time = local_end.time() if local_end.date() == local_start.date() else time(23, 59, 59)
        if local_start.time() < params.operation_start or end_time > params.operation_end:
            errors.append(
                f"{label}のスケジュールは運用時間（{params.operation_start:%H:%M}〜{params.operation_end:%H:%M}）内にしてください"
            )
        length = int((end_at - start_at) / SLOT)
        if length > params.max_continuous_slots:
            errors.append(f"{label}のスケジュールは最大連続OFF時間（{params.max_continuous_slots * SLOT_MINUTES}分）以内にしてください")
        if enabled:
            total = length + sum(int((s.end_at - s.start_at) / SLOT) for s in others if s.enabled and s.target_state == "OFF")
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
            schedule.estimated_saving = devices_saving(forecast.slots, s, e, params, [d.device for d in schedule.devices])
            schedule.reason = f"{LEVEL_LABELS[schedule.source_level]}（平均 {average_price(forecast.slots, s, e):.1f}円/kWh）・時間を手動変更"
        schedule.start_at, schedule.end_at = new_start, new_end
    schedule.manually_modified = True
    schedule.updated_at = datetime.now(UTC)
    session.commit()
    return schedule


def validate_device_ids(session: Session, settings: Settings, device_ids: list[int]) -> list[str]:
    """対象機器の検証：制御対象の制御機器から1台以上、上限（MAX_DEVICES_PER_SCHEDULE）以下（docs/17 §17.6）。"""
    ids = list(dict.fromkeys(device_ids))
    if not ids:
        return ["対象機器を1台以上選んでください"]
    errors = []
    if len(ids) > settings.max_devices_per_schedule:
        errors.append(f"対象機器は{settings.max_devices_per_schedule}台までです")
    active = {d.id for d in device_models.active_switches(session)}
    if any(i not in active for i in ids):
        errors.append("制御対象の機器から選んでください")
    return errors


def update_schedule_devices(
    session: Session, settings: Settings, schedule: ControlSchedule, device_ids: list[int], *, now: datetime | None = None
) -> ControlSchedule:
    """開始前のスケジュールの対象機器を変更する（docs/17 §17.6.1）。手動変更済みとし、再生成で上書きしない。"""
    tz = ZoneInfo(settings.tz)
    now = now or datetime.now(tz)
    if schedule.start_at <= now or schedule.status != ScheduleStatus.PENDING:
        raise ScheduleChangeError(["開始済みのスケジュールの対象機器は変更できません"])
    errors = validate_device_ids(session, settings, device_ids)
    if errors:
        raise ScheduleChangeError(errors)
    ids = list(dict.fromkeys(device_ids))
    current = {d.device_id: d for d in schedule.devices}
    schedule.devices = [current.get(i) or ScheduleDevice(device_id=i, status=ScheduleDeviceStatus.PENDING) for i in ids]
    session.flush()
    if schedule.target_state == "OFF":
        forecast = forecast_service.get_day_forecast(session, settings.looop_area, schedule.schedule_date, tz)
        if forecast.slots:
            s, e = _slot_range(schedule, tz)
            params = PlanParams.from_settings(load_control_settings(session, settings))
            schedule.estimated_saving = devices_saving(forecast.slots, s, e, params, [session.get(Device, i) for i in ids])
    schedule.manually_modified = True
    schedule.updated_at = datetime.now(UTC)
    session.commit()
    return schedule


def update_charge_modes(
    schedule: ControlSchedule,
    *,
    start: ChargeModeOption | None = None,
    end: ChargeModeOption | None = None,
    now: datetime,
    session: Session,
) -> ControlSchedule:
    """開始・終了時の充電モードを個別に変更する（docs/07 §7.10）。手動変更済みとし、再生成で上書きしない。

    開始時のモードは開始前のみ、終了時のモードは終了前（実行中を含む）のみ変更できる。
    """
    errors = []
    not_started = schedule.status == ScheduleStatus.PENDING and schedule.start_at > now
    if start is not None and start != schedule.charge_mode_start and not not_started:
        errors.append("開始済みのスケジュールの開始時の充電モードは変更できません")
    if end is not None and end != schedule.charge_mode_end and not (
        schedule.status in (ScheduleStatus.PENDING, ScheduleStatus.RUNNING) and schedule.end_at > now
    ):
        errors.append("終了したスケジュールの充電モードは変更できません")
    if errors:
        raise ScheduleChangeError(errors)
    if start is not None:
        schedule.charge_mode_start = start
    if end is not None:
        schedule.charge_mode_end = end
    schedule.manually_modified = True
    schedule.updated_at = datetime.now(UTC)
    session.commit()
    return schedule


# --- 任意スケジュール（docs/07 §7.9） ---

WEEKDAY_LABELS = "月火水木金土日"
RULE_NAME_MAX = 50


def rule_range(rule: ScheduleRule, day: date, tz: ZoneInfo) -> tuple[datetime, datetime]:
    base = _day_start(day, tz)
    return base + timedelta(minutes=rule.start_minute), base + timedelta(minutes=rule.end_minute)


def _minutes_label(start: int, end: int) -> str:
    return f"{start // 60:02d}:{start % 60:02d}〜{end // 60:02d}:{end % 60:02d}"


def _off_limit_errors(start: int, end: int, params: PlanParams) -> list[str]:
    """任意 OFF の時間帯がでんき注意報と同じ制限（運用時間・最大連続OFF時間）に収まるか。start / end は分。"""
    errors = []
    op_start = params.operation_start.hour * 60 + params.operation_start.minute
    op_end = params.operation_end.hour * 60 + params.operation_end.minute
    if start < op_start or end > op_end:
        errors.append(
            f"OFF の任意スケジュールは運用時間（{params.operation_start:%H:%M}〜{params.operation_end:%H:%M}）内にしてください"
        )
    max_minutes = params.max_continuous_slots * SLOT_MINUTES
    if end - start > max_minutes:
        errors.append(f"OFF の任意スケジュールは最大連続OFF時間（{max_minutes}分）以内にしてください")
    return errors


def _conflicts(
    start_at: datetime, end_at: datetime, target_state: str, schedules: list[ControlSchedule], min_gap: timedelta
) -> list[ControlSchedule]:
    """重なる、または OFF 同士で間隔が最低切り替え間隔未満になるスケジュール。"""
    found = []
    for s in schedules:
        if start_at < s.end_at and s.start_at < end_at:
            found.append(s)
        elif target_state == s.target_state == "OFF":
            gap = min(abs(start_at - s.end_at), abs(s.start_at - end_at))
            if timedelta(0) < gap < min_gap:
                found.append(s)
    return found


def _expand_rules(
    session: Session,
    day: date,
    tz: ZoneInfo,
    now: datetime,
    existing: list[ControlSchedule],
    params: PlanParams,
    slots: list[PriceSlot],
) -> list[ControlSchedule]:
    """対象日に適用されるルールを展開する（未保存の ControlSchedule を返す）。

    既に展開済み（手動変更・開始済みで残したもの）のルール、開始時刻を過ぎたもの、残すスケジュールと重なるものは
    展開しない。展開時点の設定で制限を超える OFF は無効として展開する。
    """
    rules = [r for r in session.scalars(select(ScheduleRule).order_by(ScheduleRule.start_time)) if r.applies_to(day)]
    expanded = {s.rule_id for s in existing if s.rule_id is not None}
    off_slots = sum(
        int((s.end_at - s.start_at) / SLOT)
        for s in existing
        if s.source_level == ScheduleSource.CUSTOM and s.enabled and s.target_state == "OFF"
    )
    created: list[ControlSchedule] = []
    active = {d.id: d for d in device_models.active_switches(session)}
    for rule in rules:
        start_at, end_at = rule_range(rule, day, tz)
        targets = [active[i] for i in rule.device_ids if i in active]
        if rule.id in expanded or start_at <= now or not targets:
            continue
        if _conflicts(start_at, end_at, rule.target_state, existing, params.min_gap_slots * SLOT):
            continue  # 保存時の画面で知らせる（unexpanded_messages）
        notes: list[str] = []
        length = (rule.end_minute - rule.start_minute) // SLOT_MINUTES
        if rule.target_state == "OFF":
            notes = _off_limit_errors(rule.start_minute, rule.end_minute, params)
            if not notes and off_slots + length > params.max_daily_slots:
                notes.append(f"任意 OFF の合計が1日の最大OFF時間（{params.max_daily_slots * SLOT_MINUTES}分）を超えます")
            if not notes:
                off_slots += length
        base = _day_start(day, tz)
        s_index, e_index = rule.start_minute // SLOT_MINUTES, rule.end_minute // SLOT_MINUTES
        reason = f"任意スケジュール「{rule.name}」（{rule.target_state}）"
        saving = 0.0
        if rule.target_state == "OFF":
            if slots:
                reason += f"（平均 {average_price(slots, s_index, e_index):.1f}円/kWh）"
                saving = devices_saving(slots, s_index, e_index, params, targets)
            else:
                reason += "・料金予報が未取得のため推定節約額は未算出"
        if notes:
            reason += "・制限超過のため無効：" + "、".join(notes)
        created.append(
            ControlSchedule(
                schedule_date=day,
                start_at=base + timedelta(minutes=rule.start_minute),
                end_at=base + timedelta(minutes=rule.end_minute),
                target_state=rule.target_state,
                source_level=ScheduleSource.CUSTOM,
                rule_id=rule.id,
                enabled=not notes,
                enabled_by=EnabledBy.AUTO,
                manually_modified=False,
                reason=reason,
                estimated_saving=saving,
                charge_mode_start=rule.charge_mode_start,
                charge_mode_end=rule.charge_mode_end,
                devices=_schedule_devices(targets),
            )
        )
    return created


def validate_rule(session: Session, settings: Settings, rule: ScheduleRule, now: datetime | None = None) -> list[str]:
    """ルールの登録・変更時の検証（docs/07 §7.9）。rule は未保存の値でよい。"""
    tz = ZoneInfo(settings.tz)
    now = now or datetime.now(tz)
    params = PlanParams.from_settings(load_control_settings(session, settings))
    errors: list[str] = []
    if not rule.name.strip():
        errors.append("名前を入力してください")
    elif len(rule.name) > RULE_NAME_MAX:
        errors.append(f"名前は{RULE_NAME_MAX}文字以内にしてください")
    if rule.target_state not in ("ON", "OFF"):
        errors.append("内容は ON か OFF を指定してください")
    start, end = rule.start_minute, rule.end_minute
    if start % SLOT_MINUTES or end % SLOT_MINUTES:
        errors.append("時刻は30分単位で指定してください")
    if start >= end:
        errors.append("開始時刻を終了時刻より前にしてください")
    if rule.repeat_type == RepeatType.ONCE:
        if rule.target_date is None:
            errors.append("対象日を指定してください")
        elif start < end and rule_range(rule, rule.target_date, tz)[0] <= now:
            errors.append("開始時刻は現在より後にしてください")
    if rule.repeat_type == RepeatType.WEEKLY and not rule.weekday_set:
        errors.append("曜日を1つ以上選んでください")
    if rule.enabled:  # 無効のルール（対象機器が0台になって無効にしたものなど）は対象機器なしでも保存できる
        errors += validate_device_ids(session, settings, rule.device_ids)
    if errors:
        return errors
    if rule.target_state == "OFF":
        errors += _off_limit_errors(start, end, params)
    if not rule.enabled:
        return errors

    with session.no_autoflush:
        others = [r for r in session.scalars(select(ScheduleRule)) if r.id != rule.id and r.enabled]
    today = now.date()
    if rule.repeat_type == RepeatType.ONCE:
        assert rule.target_date is not None
        days = [rule.target_date]
    else:
        # 繰り返しは1週間分と、他の1回のみのルールの対象日で確認する
        days = [today + timedelta(days=i) for i in range(7)]
        days += [r.target_date for r in others if r.target_date is not None and r.target_date >= today]
    gap = params.min_gap_slots * SLOT_MINUTES
    length = end - start
    daily_error = None
    for day in sorted(set(days)):
        if not rule.applies_to(day):
            continue
        same_day = [r for r in others if r.applies_to(day)]
        for other in same_day:
            label = f"「{other.name}」（{_minutes_label(other.start_minute, other.end_minute)}）"
            if start < other.end_minute and other.start_minute < end:
                errors.append(f"任意スケジュール{label}と重なります")
            elif rule.target_state == other.target_state == "OFF":
                interval = min(abs(start - other.end_minute), abs(other.start_minute - end))
                if 0 < interval < gap:
                    errors.append(f"任意スケジュール{label}との間隔が最低切り替え間隔（{gap}分）未満です")
        if rule.target_state == "OFF" and daily_error is None:
            total = length + sum(r.end_minute - r.start_minute for r in same_day if r.target_state == "OFF")
            if total > params.max_daily_slots * SLOT_MINUTES:
                daily_error = (
                    f"{day:%m/%d}（{WEEKDAY_LABELS[day.weekday()]}）の任意 OFF の合計が"
                    f"1日の最大OFF時間（{params.max_daily_slots * SLOT_MINUTES}分）を超えます（合計 {total}分）"
                )
    if daily_error:
        errors.append(daily_error)
    return list(dict.fromkeys(errors))


def unexpanded_messages(session: Session, settings: Settings, rule: ScheduleRule, now: datetime | None = None) -> list[str]:
    """当日・翌日に適用されるのに、開始済み・手動変更済みのスケジュールと重なって展開しなかった日と時間帯。"""
    tz = ZoneInfo(settings.tz)
    now = now or datetime.now(tz)
    min_gap = PlanParams.from_settings(load_control_settings(session, settings)).min_gap_slots * SLOT
    messages = []
    for day in (now.date(), now.date() + timedelta(days=1)):
        if not rule.applies_to(day):
            continue
        start_at, end_at = rule_range(rule, day, tz)
        schedules = schedules_for(session, day)
        if start_at <= now or any(s.rule_id == rule.id for s in schedules):
            continue
        blockers = _conflicts(start_at, end_at, rule.target_state, schedules, min_gap)
        if blockers:
            ranges = "、".join(f"{b.start_at.astimezone(tz):%H:%M}〜{b.end_at.astimezone(tz):%H:%M}" for b in blockers)
            messages.append(
                f"{day:%m/%d} {_minutes_label(rule.start_minute, rule.end_minute)} は、開始済み・手動変更済みの"
                f"スケジュール（{ranges}）と重なる（または間隔が最低切り替え間隔未満の）ため展開しませんでした"
            )
    return messages


def save_rule(session: Session, settings: Settings, rule: ScheduleRule, now: datetime | None = None) -> list[str]:
    """ルールを保存し、当日・翌日分を展開し直す。展開できなかった日のメッセージを返す。"""
    tz = ZoneInfo(settings.tz)
    now = now or datetime.now(tz)
    errors = validate_rule(session, settings, rule, now)
    if errors:
        raise ScheduleChangeError(errors)
    timestamp = datetime.now(UTC)
    if rule.id is None:
        rule.created_at = timestamp
        session.add(rule)
    rule.updated_at = timestamp
    session.commit()
    regenerate_days(session, settings, [now.date(), now.date() + timedelta(days=1)], now=now)
    return unexpanded_messages(session, settings, rule, now)


def delete_rule(session: Session, settings: Settings, rule: ScheduleRule, now: datetime | None = None) -> None:
    """ルールを削除する。展開した未開始のスケジュールは（手動変更済みも含めて）削除し、開始済みのものは残す。"""
    tz = ZoneInfo(settings.tz)
    now = now or datetime.now(tz)
    for schedule in session.scalars(select(ControlSchedule).where(ControlSchedule.rule_id == rule.id)):
        if schedule.start_at > now:
            session.delete(schedule)
        else:
            schedule.rule_id = None
    session.delete(rule)
    session.commit()
    regenerate_days(session, settings, [now.date(), now.date() + timedelta(days=1)], now=now)
