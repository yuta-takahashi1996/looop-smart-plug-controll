from datetime import date, datetime, time, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import device as device_models
from app.models.schedule import ControlSchedule, EnabledBy, RepeatType, ScheduleRule, ScheduleSource
from app.models.settings import ControlSettings, load_control_settings, save_control_settings
from app.providers.base import DayPrices, FetchResult, ForecastLevel
from app.services import forecast as forecast_service
from app.services import optimizer
from app.services.forecast import PriceSlot
from app.services.optimizer import Draft, PlanParams, ScheduleChangeError, estimated_saving, plan_day
from tests.conftest import JST

pytestmark = pytest.mark.usefixtures("default_devices")

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
    # 19:00〜24:00（許可時間外・10コマ）の警報。制限を超えてもそのまま
    drafts = plan_day(make_slots({range(38, 48): W}), params(auto_enable=False))

    assert spans(drafts) == [(38, 48, W, True)]


@pytest.mark.parametrize("auto_enable", [False, True])
def test_advisory_enabled_follows_auto_enable(auto_enable: bool) -> None:
    drafts = plan_day(make_slots({range(30, 32): A}), params(auto_enable=auto_enable))

    assert spans(drafts) == [(30, 32, A, auto_enable)]


def test_advisory_is_clipped_to_operation_hours() -> None:
    # 17:00〜19:00 の注意報 → 許可終了 18:00 まで
    drafts = plan_day(make_slots({range(34, 38): A}), params())

    assert spans(drafts) == [(34, 36, A, False)]
    assert drafts[0].notes == ("許可時間外を除外",)


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
        ((8, 30), (10, 0), "許可時間"),
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


def _allow_off_all_day(session: Session) -> None:
    control = load_control_settings(session, get_settings())
    save_control_settings(session, control.model_copy(update={"off_all_day": True}))


def test_off_all_day_keeps_advisory_outside_hours() -> None:
    # 01:00〜03:00 と、日付をまたぐ扱いの 23:00〜24:00
    drafts = plan_day(make_slots({range(2, 6): A, range(46, 48): A}), params(off_all_day=True, auto_enable=True))

    assert spans(drafts) == [(2, 6, A, True), (46, 48, A, True)]
    assert all(not d.notes for d in drafts)


def test_off_all_day_keeps_continuous_limit() -> None:
    drafts = plan_day(make_slots({range(0, 10): A}), params(off_all_day=True))

    assert [d.length for d in drafts] == [4]


def test_off_all_day_uses_whole_day_for_charge_price() -> None:
    slots = make_slots({}, {0: 5.0, 20: 30.0})  # 最安は許可時間外の 00:00

    assert estimated_saving(slots, 20, 21, params(off_all_day=True)) > estimated_saving(slots, 20, 21, params())


def test_off_all_day_allows_time_change_outside_hours(session: Session, two_schedules: list[ControlSchedule]) -> None:
    _allow_off_all_day(session)
    advisory = two_schedules[0]

    optimizer.update_schedule(session, get_settings(), advisory, start_at=_at(8, 30), end_at=_at(10), now=BEFORE_DAY)

    assert advisory.start_at.astimezone(JST).time() == time(8, 30)


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


# --- 任意スケジュール（docs/07 §7.9） ---

C = ScheduleSource.CUSTOM


def _rule(session: Session, name: str = "任意", *, state: str = "OFF", start=(10, 0), end=(11, 0), **fields) -> ScheduleRule:
    """ルールを保存して当日・翌日（DAY）分を展開する。"""
    rule = ScheduleRule(
        name=name,
        target_state=state,
        repeat_type=fields.pop("repeat_type", RepeatType.DAILY),
        start_time=time(*start),
        end_time=time(*end),
        enabled=True,
        **fields,
    )
    rule.set_devices(device_models.forecast_schedule_device_ids(session))
    optimizer.save_rule(session, get_settings(), rule, now=BEFORE_DAY)
    return rule


def _spans(session: Session) -> list[tuple[time, time, str, str, bool]]:
    session.expire_all()
    return [
        (s.start_at.astimezone(JST).time(), s.end_at.astimezone(JST).time(), s.source_level.value, s.target_state, s.enabled)
        for s in optimizer.schedules_for(session, DAY)
    ]


def test_custom_off_is_expanded_without_forecast(session: Session) -> None:
    _rule(session, "昼", start=(12, 0), end=(13, 0))

    assert _spans(session) == [(time(12), time(13), "CUSTOM", "OFF", True)]
    schedule = optimizer.schedules_for(session, DAY)[0]
    assert "任意スケジュール「昼」" in schedule.reason and "未算出" in schedule.reason


def test_custom_once_and_weekly_apply_only_to_their_days(session: Session) -> None:
    _rule(session, "別の日", repeat_type=RepeatType.ONCE, target_date=DAY + timedelta(days=1))
    _rule(session, "曜日違い", repeat_type=RepeatType.WEEKLY, weekdays=str((DAY.weekday() + 1) % 7), start=(12, 0), end=(13, 0))
    _rule(session, "対象", repeat_type=RepeatType.WEEKLY, weekdays=str(DAY.weekday()), start=(14, 0), end=(15, 0))

    assert _spans(session) == [(time(14), time(15), "CUSTOM", "OFF", True)]


def test_forecast_schedules_avoid_custom_on(session: Session) -> None:
    _store(session, _levels({range(38, 40): W}))  # 19:00〜20:00 警報
    _rule(session, "在宅", state="ON", start=(19, 0), end=(19, 30))

    # 警報も任意 ON の時間帯には作らない（ON の前後には最低切り替え間隔を空けない）
    assert _spans(session) == [
        (time(19), time(19, 30), "CUSTOM", "ON", True),
        (time(19, 30), time(20), "WARNING", "OFF", True),
    ]


def test_custom_off_is_counted_first_in_daily_limit(session: Session) -> None:
    _store(session, _levels({range(28, 32): A}), [20.0 + i for i in range(48)])  # 14:00〜16:00 注意報
    _rule(session, "午前", start=(10, 0), end=(12, 0))  # 120分。1日の上限 180分の残りは 60分

    assert _spans(session) == [
        (time(10), time(12), "CUSTOM", "OFF", True),
        (time(15), time(16), "ADVISORY", "OFF", False),  # 単価の高い方の 60分に短縮
    ]


@pytest.mark.parametrize(
    ("fields", "message"),
    [
        (dict(start=(8, 0), end=(9, 0)), "許可時間"),
        (dict(start=(10, 0), end=(12, 30)), "最大連続OFF時間"),
        (dict(start=(11, 0), end=(10, 0)), "開始時刻を終了時刻より前"),
        (dict(start=(10, 15), end=(11, 0)), "30分単位"),
        (dict(repeat_type=RepeatType.ONCE, target_date=date(2030, 1, 14), start=(10, 0), end=(11, 0)), "現在より後"),
        (dict(repeat_type=RepeatType.WEEKLY, weekdays=""), "曜日を1つ以上"),
        (dict(start=(10, 30), end=(11, 30)), "重なります"),
        (dict(start=(11, 0), end=(11, 30), state="OFF"), None),  # 連続（間隔0）は許可
        (dict(start=(15, 0), end=(17, 0)), "1日の最大OFF時間"),
    ],
)
def test_custom_rule_validation(session: Session, fields: dict, message: str | None) -> None:
    _rule(session, "既存1", start=(10, 0), end=(11, 0))
    _rule(session, "既存2", start=(12, 0), end=(13, 0))
    state = fields.pop("state", "OFF")
    rule = ScheduleRule(
        name="新規",
        target_state=state,
        repeat_type=fields.pop("repeat_type", RepeatType.DAILY),
        start_time=time(*fields.pop("start", (14, 0))),
        end_time=time(*fields.pop("end", (15, 0))),
        enabled=True,
        **fields,
    )
    rule.set_devices(device_models.forecast_schedule_device_ids(session))

    errors = optimizer.validate_rule(session, get_settings(), rule, now=BEFORE_DAY)

    if message is None:
        assert errors == []
    else:
        assert any(message in e for e in errors), errors


def test_off_all_day_allows_custom_off_until_midnight(session: Session) -> None:
    _allow_off_all_day(session)
    rule = ScheduleRule(
        name="深夜", target_state="OFF", repeat_type=RepeatType.DAILY, start_time=time(23), end_time=time(0), enabled=True
    )
    rule.set_devices(device_models.forecast_schedule_device_ids(session))

    assert optimizer.validate_rule(session, get_settings(), rule, now=BEFORE_DAY) == []


def test_custom_on_is_not_limited(session: Session) -> None:
    rule = ScheduleRule(
        name="終日", target_state="ON", repeat_type=RepeatType.DAILY, start_time=time(0), end_time=time(0), enabled=True
    )
    rule.set_devices(device_models.forecast_schedule_device_ids(session))

    assert optimizer.validate_rule(session, get_settings(), rule, now=BEFORE_DAY) == []


def test_custom_rule_change_keeps_manually_toggled_instance(session: Session) -> None:
    rule = _rule(session, "昼", start=(12, 0), end=(13, 0))
    instance = optimizer.schedules_for(session, DAY)[0]
    optimizer.update_schedule(session, get_settings(), instance, enabled=False, now=BEFORE_DAY)

    rule.start_time, rule.end_time = time(14), time(15)
    optimizer.save_rule(session, get_settings(), rule, now=BEFORE_DAY)

    assert _spans(session) == [(time(12), time(13), "CUSTOM", "OFF", False)]


def test_custom_instance_time_cannot_be_changed(session: Session) -> None:
    _rule(session, "昼", start=(12, 0), end=(13, 0))
    instance = optimizer.schedules_for(session, DAY)[0]

    with pytest.raises(ScheduleChangeError, match="ルールで変更"):
        optimizer.update_schedule(session, get_settings(), instance, start_at=_at(13), end_at=_at(14), now=BEFORE_DAY)


def test_delete_rule_removes_pending_and_keeps_started(session: Session) -> None:
    rule = _rule(session, "昼", start=(12, 0), end=(13, 0))
    today_instance = next(s for s in optimizer.schedules_for(session, DAY))
    optimizer.update_schedule(session, get_settings(), today_instance, enabled=False, now=BEFORE_DAY)

    optimizer.delete_rule(session, get_settings(), rule, now=_at(12, 15))

    session.expire_all()
    remaining = optimizer.schedules_for(session, DAY)
    assert [(s.rule_id, s.start_at.astimezone(JST).time()) for s in remaining] == [(None, time(12))]  # 開始済みは残す
    assert optimizer.schedules_for(session, DAY + timedelta(days=1)) == []  # 翌日の未開始分は削除
    assert session.scalars(select(ScheduleRule)).all() == []


def test_rule_conflicting_with_modified_schedule_is_reported(session: Session) -> None:
    _store(session, _levels({range(20, 22): A}))  # 10:00〜11:00 注意報
    advisory = optimizer.regenerate(session, get_settings(), DAY, now=BEFORE_DAY)[0]
    optimizer.update_schedule(session, get_settings(), advisory, enabled=True, now=BEFORE_DAY)

    rule = ScheduleRule(
        name="重複", target_state="ON", repeat_type=RepeatType.DAILY, start_time=time(10, 30), end_time=time(11, 30), enabled=True
    )
    rule.set_devices(device_models.forecast_schedule_device_ids(session))
    messages = optimizer.save_rule(session, get_settings(), rule, now=BEFORE_DAY)

    assert any("01/15 10:30〜11:30" in m and "10:00〜11:00" in m for m in messages)
    assert [(s.source_level, s.target_state) for s in optimizer.schedules_for(session, DAY)] == [(A, "OFF")]


def test_custom_off_gap_validation(session: Session) -> None:
    control = load_control_settings(session, get_settings()).model_copy(update={"min_switch_interval_minutes": 60})
    save_control_settings(session, control)
    _rule(session, "既存", start=(12, 0), end=(13, 0))
    rule = ScheduleRule(
        name="近い", target_state="OFF", repeat_type=RepeatType.DAILY, start_time=time(13, 30), end_time=time(14), enabled=True
    )
    rule.set_devices(device_models.forecast_schedule_device_ids(session))

    errors = optimizer.validate_rule(session, get_settings(), rule, now=BEFORE_DAY)

    assert any("最低切り替え間隔（60分）" in e for e in errors)
    rule.target_state = "ON"  # ON との間隔は問わない
    assert optimizer.validate_rule(session, get_settings(), rule, now=BEFORE_DAY) == []
