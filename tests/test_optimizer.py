from datetime import date, datetime, time, timedelta

import pytest
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models.schedule import ControlSchedule, EnabledBy
from app.models.settings import ControlSettings, save_control_settings
from app.providers.base import DayPrices, FetchResult, ForecastLevel
from app.services import forecast as forecast_service
from app.services import optimizer
from app.services.forecast import PriceSlot
from app.services.optimizer import Draft, PlanParams, ScheduleChangeError, plan_day
from tests.conftest import JST

N, A, W = ForecastLevel.NORMAL, ForecastLevel.ADVISORY, ForecastLevel.WARNING
DAY = date(2030, 1, 15)  # 未来の日付（経過済みのコマがない）
BEFORE_DAY = datetime(2030, 1, 14, 12, 0, tzinfo=JST)


def params(**overrides) -> PlanParams:
    base = dict(
        operation_start=time(9, 0),
        operation_end=time(18, 0),
        max_continuous_slots=4,  # 120分
        max_daily_slots=6,  # 180分
        min_gap_slots=1,  # 30分
        auto_enable=False,
        round_trip_efficiency=0.85,
        average_power_w=150,
    )
    base.update(overrides)
    return PlanParams(**base)


def make_slots(levels: dict[range | int, ForecastLevel], prices: dict[int, float] | None = None) -> list[PriceSlot]:
    """指定したコマ以外は NORMAL・20円。prices でコマごとの単価を上書きする。"""
    level_list = [N] * 48
    for key, level in levels.items():
        for i in key if isinstance(key, range) else [key]:
            level_list[i] = level
    start = datetime.combine(DAY, time(), tzinfo=JST)
    return [
        PriceSlot(
            start=start + timedelta(minutes=30 * i),
            end=start + timedelta(minutes=30 * (i + 1)),
            energy_price=(prices or {}).get(i, 20.0),
            billing_price=(prices or {}).get(i, 20.0),
            level=level_list[i],
        )
        for i in range(48)
    ]


def spans(drafts: list[Draft]) -> list[tuple[int, int, ForecastLevel, bool]]:
    return [(d.start, d.end, d.level, d.enabled) for d in drafts]


# --- 生成（plan_day） ---


def test_warning_is_always_enabled_and_not_trimmed() -> None:
    # 19:00〜24:00（運用時間外・10コマ）の警報。制限を超えてもそのまま
    drafts = plan_day(make_slots({range(38, 48): W}), params(auto_enable=False))

    assert spans(drafts) == [(38, 48, W, True)]


@pytest.mark.parametrize("auto_enable", [False, True])
def test_advisory_enabled_follows_auto_enable(auto_enable: bool) -> None:
    drafts = plan_day(make_slots({range(30, 32): A}), params(auto_enable=auto_enable))

    assert spans(drafts) == [(30, 32, A, auto_enable)]


def test_advisory_is_clipped_to_operation_hours() -> None:
    # 17:00〜19:00 の注意報 → 運用終了 18:00 まで
    drafts = plan_day(make_slots({range(34, 38): A}), params())

    assert spans(drafts) == [(34, 36, A, False)]
    assert drafts[0].notes == ("運用時間外を除外",)


def test_advisory_outside_operation_hours_is_dropped() -> None:
    assert plan_day(make_slots({range(40, 44): A}), params()) == []


def test_advisory_longer_than_max_continuous_keeps_highest_priced_window() -> None:
    # 9:00〜12:00（6コマ）の注意報。10:30〜12:00 が高い → 最大4コマ（10:00〜12:00）
    slots = make_slots({range(18, 24): A}, {20: 40, 21: 45, 22: 50, 23: 48})

    drafts = plan_day(slots, params())

    assert spans(drafts) == [(20, 24, A, False)]
    assert "最大連続OFF時間に合わせて短縮" in drafts[0].notes


def test_daily_limit_prefers_higher_priced_advisory() -> None:
    # 2つの注意報（各4コマ）。1日の上限6コマ → 高い方を4コマ、残り2コマを安い方の最も高い区間に
    prices = {i: 30.0 for i in range(18, 22)} | {i: 40.0 for i in range(28, 32)} | {19: 35.0, 20: 36.0}
    slots = make_slots({range(18, 22): A, range(28, 32): A}, prices)

    drafts = plan_day(slots, params())

    assert spans(drafts) == [(19, 21, A, False), (28, 32, A, False)]
    assert sum(d.length for d in drafts) == 6
    assert "1日の最大OFF時間に合わせて短縮" in drafts[0].notes


def test_warning_time_counts_toward_daily_limit() -> None:
    # 警報6コマで1日の上限を使い切るため、注意報は作らない
    drafts = plan_day(make_slots({range(38, 44): W, range(20, 22): A}), params())

    assert spans(drafts) == [(38, 44, W, True)]


def test_adjacent_warning_and_advisory_are_split() -> None:
    drafts = plan_day(make_slots({range(30, 32): A, range(32, 34): W}), params())

    assert spans(drafts) == [(30, 32, A, False), (32, 34, W, True)]


def test_short_gap_drops_lower_priced_advisory() -> None:
    # 間隔1コマ（30分）で最低間隔2コマ（60分）未満 → 安い方の注意報を除外
    slots = make_slots({range(20, 22): A, range(23, 25): A}, {20: 30, 21: 30, 23: 50, 24: 50})

    drafts = plan_day(slots, params(min_gap_slots=2))

    assert spans(drafts) == [(23, 25, A, False)]


def test_short_gap_next_to_warning_drops_advisory() -> None:
    slots = make_slots({range(20, 22): A, range(23, 25): W})

    drafts = plan_day(slots, params(min_gap_slots=2))

    assert spans(drafts) == [(23, 25, W, True)]


def test_blocked_ranges_and_elapsed_slots_are_skipped() -> None:
    slots = make_slots({range(20, 30): A})

    # 20〜21 は経過済み、24〜25 は手動変更済みのスケジュール（前後1コマ＝23・26 も避ける）
    drafts = plan_day(slots, params(max_continuous_slots=10, max_daily_slots=10), blocked=[(24, 26)], first_available=22)

    assert spans(drafts) == [(22, 23, A, False), (27, 30, A, False)]


def test_used_slots_reduce_daily_budget() -> None:
    drafts = plan_day(make_slots({range(20, 24): A}), params(), used_slots=5)

    assert spans(drafts) == [(20, 21, A, False)]


def test_estimated_saving_uses_energy_price() -> None:
    # 9:00〜18:00 の最安 10円で充電、20円のコマ×2を回避。150W×1時間＝0.15kWh
    slots = make_slots({}, {18: 10.0})

    saving = optimizer.estimated_saving(slots, 30, 32, params())

    assert saving == pytest.approx(round(20 * 0.15 - 10 * 0.15 / 0.85, 1))


# --- 再生成（DB） ---


def _store(session: Session, levels: list[ForecastLevel], prices: list[float] | None = None) -> None:
    result = FetchResult(
        area="hokkaido",
        fetched_at=datetime.combine(DAY, time(6), tzinfo=JST),
        days=[DayPrices(DAY, prices or [20.0] * 48, levels)],
        source="test",
    )
    forecast_service.save_result(session, result, JST)


def _levels(spec: dict[range, ForecastLevel]) -> list[ForecastLevel]:
    levels = [N] * 48
    for r, level in spec.items():
        for i in r:
            levels[i] = level
    return levels


def _at(hour: int, minute: int = 0) -> datetime:
    return datetime.combine(DAY, time(hour, minute), tzinfo=JST)


def test_regenerate_creates_schedules(session: Session) -> None:
    _store(session, _levels({range(34, 36): A, range(38, 40): W}))

    schedules = optimizer.regenerate(session, get_settings(), DAY, now=BEFORE_DAY)

    assert [(s.start_at.astimezone(JST).time(), s.source_level, s.enabled) for s in schedules] == [
        (time(17, 0), A, False),
        (time(19, 0), W, True),
    ]
    assert all(s.enabled_by == EnabledBy.AUTO and not s.manually_modified for s in schedules)
    assert schedules[0].schedule_version == 1
    assert "でんき注意報" in schedules[0].reason


def test_regenerate_without_forecast_creates_nothing(session: Session) -> None:
    assert optimizer.regenerate(session, get_settings(), DAY, now=BEFORE_DAY) == []


def test_regenerate_keeps_manual_changes_and_replaces_others(session: Session) -> None:
    settings = get_settings()
    _store(session, _levels({range(20, 22): A, range(34, 36): A}))
    first = optimizer.regenerate(session, settings, DAY, now=BEFORE_DAY)
    optimizer.update_schedule(session, settings, first[0], enabled=True, now=BEFORE_DAY)

    # 予報が変わり、30〜32 が新たに注意報になった
    _store(session, _levels({range(20, 22): A, range(30, 32): A}))
    second = optimizer.regenerate(session, settings, DAY, now=BEFORE_DAY)

    assert [(s.start_at.astimezone(JST).time(), s.enabled, s.manually_modified) for s in second] == [
        (time(10, 0), True, True),  # 手動で有効にしたものは残る
        (time(15, 0), False, False),  # 新しく作られた
    ]
    assert second[1].schedule_version == 2


def test_regenerate_keeps_started_schedules(session: Session) -> None:
    settings = get_settings()
    _store(session, _levels({range(20, 22): A}))
    optimizer.regenerate(session, settings, DAY, now=BEFORE_DAY)

    _store(session, _levels({}))  # 注意報が消えた
    schedules = optimizer.regenerate(session, settings, DAY, now=_at(10, 15))

    assert [s.start_at.astimezone(JST).time() for s in schedules] == [time(10, 0)]


def test_regenerate_uses_saved_settings(session: Session) -> None:
    settings = get_settings()
    control = ControlSettings(
        auto_enable=True,
        operation_start=time(9),
        operation_end=time(18),
        round_trip_efficiency=0.85,
        max_continuous_off_minutes=120,
        max_daily_off_minutes=180,
        min_switch_interval_minutes=30,
        estimated_average_power_w=150,
        battery_min_percent=5,
        battery_capacity_wh=1070,
        battery_discharge_efficiency=0.85,
    )
    save_control_settings(session, control)
    _store(session, _levels({range(20, 22): A}))

    schedules = optimizer.regenerate(session, settings, DAY, now=BEFORE_DAY)

    assert schedules[0].enabled is True


# --- 手動変更 ---


@pytest.fixture
def two_schedules(session: Session) -> list[ControlSchedule]:
    # 10:00〜11:00 注意報、19:00〜20:00 警報
    _store(session, _levels({range(20, 22): A, range(38, 40): W}))
    return optimizer.regenerate(session, get_settings(), DAY, now=BEFORE_DAY)


def test_toggle_marks_manual(session: Session, two_schedules: list[ControlSchedule]) -> None:
    advisory, warning = two_schedules

    optimizer.update_schedule(session, get_settings(), advisory, enabled=True, now=BEFORE_DAY)
    optimizer.update_schedule(session, get_settings(), warning, enabled=False, now=BEFORE_DAY)

    assert (advisory.enabled, advisory.enabled_by, advisory.manually_modified) == (True, EnabledBy.MANUAL, True)
    assert (warning.enabled, warning.enabled_by) == (False, EnabledBy.MANUAL)


def test_change_time(session: Session, two_schedules: list[ControlSchedule]) -> None:
    advisory = two_schedules[0]

    optimizer.update_schedule(session, get_settings(), advisory, start_at=_at(9, 30), end_at=_at(11), now=BEFORE_DAY)

    assert advisory.start_at.astimezone(JST).time() == time(9, 30)
    assert advisory.manually_modified
    assert "時間を手動変更" in advisory.reason


def test_warning_time_can_exceed_limits(session: Session, two_schedules: list[ControlSchedule]) -> None:
    warning = two_schedules[1]

    optimizer.update_schedule(session, get_settings(), warning, start_at=_at(19), end_at=_at(23, 30), now=BEFORE_DAY)

    assert warning.end_at.astimezone(JST).time() == time(23, 30)


@pytest.mark.parametrize(
    ("start", "end", "message"),
    [
        ((10, 15), (11, 0), "30分単位"),
        ((11, 0), (10, 0), "開始を終了より前"),
        ((8, 30), (10, 0), "運用時間"),
        ((10, 0), (12, 30), "最大連続OFF時間"),
        ((18, 30), (19, 30), "重なります"),
    ],
)
def test_change_time_validation(
    session: Session, two_schedules: list[ControlSchedule], start: tuple, end: tuple, message: str
) -> None:
    with pytest.raises(ScheduleChangeError) as exc_info:
        optimizer.update_schedule(
            session, get_settings(), two_schedules[0], start_at=_at(*start), end_at=_at(*end), now=BEFORE_DAY
        )
    assert any(message in m for m in exc_info.value.messages)


def test_gap_validation(session: Session, two_schedules: list[ControlSchedule]) -> None:
    settings = get_settings()
    control = ControlSettings(
        auto_enable=False,
        operation_start=time(9),
        operation_end=time(20),
        round_trip_efficiency=0.85,
        max_continuous_off_minutes=120,
        max_daily_off_minutes=600,
        min_switch_interval_minutes=60,
        estimated_average_power_w=150,
        battery_min_percent=5,
        battery_capacity_wh=1070,
        battery_discharge_efficiency=0.85,
    )
    save_control_settings(session, control)
    advisory = two_schedules[0]

    with pytest.raises(ScheduleChangeError, match="最低切り替え間隔"):
        optimizer.update_schedule(session, settings, advisory, start_at=_at(17), end_at=_at(18, 30), now=BEFORE_DAY)
    # 連続（間隔0）は許可する
    optimizer.update_schedule(session, settings, advisory, start_at=_at(18), end_at=_at(19), now=BEFORE_DAY)


def test_enable_rejected_when_daily_limit_exceeded(session: Session) -> None:
    # 警報 3時間（6コマ）で1日の上限を使い切っている
    _store(session, _levels({range(20, 22): A, range(36, 42): W}))
    schedules = optimizer.regenerate(session, get_settings(), DAY, now=BEFORE_DAY)
    assert [s.source_level for s in schedules] == [W]  # 注意報は上限のため作られない

    _store(session, _levels({range(20, 22): A, range(36, 40): W}))
    advisory = next(s for s in optimizer.regenerate(session, get_settings(), DAY, now=BEFORE_DAY) if s.source_level == A)
    warning = next(s for s in optimizer.schedules_for(session, DAY) if s.source_level == W)
    optimizer.update_schedule(session, get_settings(), warning, start_at=_at(18), end_at=_at(21), now=BEFORE_DAY)

    with pytest.raises(ScheduleChangeError, match="1日の最大OFF時間"):
        optimizer.update_schedule(session, get_settings(), advisory, enabled=True, now=BEFORE_DAY)


def test_started_schedule_cannot_be_changed(session: Session, two_schedules: list[ControlSchedule]) -> None:
    with pytest.raises(ScheduleChangeError, match="開始済み"):
        optimizer.update_schedule(session, get_settings(), two_schedules[0], enabled=True, now=_at(10, 15))
