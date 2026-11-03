from datetime import UTC, date, datetime, time, timedelta

import pytest

from app.models.schedule import ControlSchedule, EnabledBy
from app.models.settings import ControlSettings
from app.providers.base import ForecastLevel
from app.services import safety
from tests.conftest import JST

DAY = date(2030, 1, 15)
CONTROL = ControlSettings(
    auto_enable=False,
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


def at(hour: int, minute: int = 0) -> datetime:
    return datetime.combine(DAY, time(hour, minute), tzinfo=JST)


def schedule(level: ForecastLevel, start: datetime, end: datetime, executed_start: datetime | None = None) -> ControlSchedule:
    return ControlSchedule(
        schedule_date=DAY,
        schedule_version=1,
        start_at=start,
        end_at=end,
        source_level=level,
        enabled=True,
        enabled_by=EnabledBy.AUTO,
        reason="",
        estimated_saving=0,
        executed_start_at=executed_start,
        updated_at=datetime.now(UTC),
    )


def check(s: ControlSchedule, now: datetime, **kwargs) -> safety.OffDecision:
    options = {"has_forecast": True, "day_off_minutes": 0, **kwargs}
    return safety.check_off(s, now, CONTROL, **options)


def test_allowed_within_limits() -> None:
    assert check(schedule(ForecastLevel.ADVISORY, at(16), at(18)), at(16, 0)).allowed


def test_no_off_without_forecast() -> None:
    decision = check(schedule(ForecastLevel.WARNING, at(16), at(18)), at(16), has_forecast=False, battery_known=True)

    assert not decision.allowed and "予報" in decision.reason


@pytest.mark.parametrize("level", [ForecastLevel.ADVISORY, ForecastLevel.WARNING])
def test_operation_hours(level: ForecastLevel) -> None:
    decision = check(schedule(level, at(18), at(20)), at(18, 0))

    assert not decision.allowed and "運用時間外" in decision.reason


def test_continuous_limit() -> None:
    s = schedule(ForecastLevel.WARNING, at(10), at(14), executed_start=at(10))

    assert check(s, at(11, 59)).allowed
    decision = check(s, at(12, 0))
    assert not decision.allowed and "最大連続OFF時間" in decision.reason


def test_daily_limit() -> None:
    decision = check(schedule(ForecastLevel.ADVISORY, at(15), at(16)), at(15), day_off_minutes=180)

    assert not decision.allowed and "1日の最大OFF時間" in decision.reason


def test_warning_ignores_limits_when_battery_known() -> None:
    s = schedule(ForecastLevel.WARNING, at(18), at(23), executed_start=at(18))

    assert check(s, at(22), day_off_minutes=600, battery_known=True).allowed


def test_advisory_is_limited_even_when_battery_known() -> None:
    assert not check(schedule(ForecastLevel.ADVISORY, at(18), at(19)), at(18), battery_known=True).allowed


def test_off_minutes_counts_running_schedule() -> None:
    done = schedule(ForecastLevel.ADVISORY, at(10), at(11), executed_start=at(10))
    done.executed_end_at = at(11)
    running = schedule(ForecastLevel.ADVISORY, at(15), at(16), executed_start=at(15))

    assert safety.off_minutes([done, running, schedule(ForecastLevel.ADVISORY, at(17), at(18))], at(15, 20)) == 80


# --- ポータブル電源の残量 ---


def reading(percent: int, watts: float, when: datetime) -> safety.BatteryReading:
    return safety.BatteryReading(percent, watts, when)


def test_battery_below_min_is_rejected() -> None:
    decision = safety.check_battery(reading(5, 0, at(10)), at(10), CONTROL, average_output_w=None, end_at=None)

    assert not decision.allowed and "下限" in decision.reason


def test_battery_projection_until_next_check() -> None:
    # 150W ÷ 0.85 ÷ 1070Wh ≒ 16.5%/h → 5分で約1.4%
    assert safety.check_battery(reading(7, 150, at(10)), at(10), CONTROL, average_output_w=None, end_at=None).allowed
    decision = safety.check_battery(reading(6, 150, at(10)), at(10), CONTROL, average_output_w=None, end_at=None)
    assert not decision.allowed and "150W" in decision.reason


def test_battery_projection_uses_larger_of_current_and_average() -> None:
    decision = safety.check_battery(reading(7, 50, at(10)), at(10), CONTROL, average_output_w=300, end_at=None)

    assert not decision.allowed and "300W" in decision.reason


def test_battery_projection_stops_at_schedule_end() -> None:
    # 終了まで1分なら、5分先ではなく終了時で判定する
    assert safety.check_battery(reading(6, 150, at(10)), at(10), CONTROL, average_output_w=None, end_at=at(10, 1)).allowed


def test_battery_known_expires_after_10_minutes() -> None:
    r = reading(50, 100, at(10))

    assert safety.battery_known(r, at(10, 10))
    assert not safety.battery_known(r, at(10, 11))
    assert not safety.battery_known(None, at(10))


def test_time_to_min_percent() -> None:
    eta = safety.time_to_min_percent(reading(21.5, 150, at(10)), 150, CONTROL)  # type: ignore[arg-type]

    assert eta is not None and abs((eta - at(11)).total_seconds()) < 60
