"""実行時の安全判定（docs/07-control-logic.md §7.5・§7.7、docs/14-safety.md）。

スケジュールの OFF を開始・継続してよいかを、実行のたびに判定する。生成時に制限内に収めた
スケジュールでも、ここで改めて確認する（手動変更や予報の更新、停止からの復帰などに備える）。
"""

from dataclasses import dataclass
from datetime import datetime, timedelta

from app.models.schedule import ControlSchedule, ScheduleSource
from app.models.settings import ControlSettings


@dataclass(frozen=True)
class OffDecision:
    allowed: bool
    reason: str | None = None


def off_minutes(schedules: list[ControlSchedule], now: datetime) -> int:
    """実行済み（実行中を含む）の OFF 時間の合計（分）。"""
    total = timedelta()
    for s in schedules:
        if s.executed_start_at is not None:
            total += (s.executed_end_at or now) - s.executed_start_at
    return int(total.total_seconds() // 60)


def check_off(
    schedule: ControlSchedule,
    now: datetime,
    control: ControlSettings,
    *,
    has_forecast: bool,
    day_off_minutes: int,
    battery_known: bool = False,
    started_at: datetime | None = None,
) -> OffDecision:
    """schedule の OFF を now（表示用のタイムゾーンの日時）の時点で開始・継続してよいか。

    判定は制御機器ごとに行う（docs/17 §17.7.3）。
    battery_known: その機器の給電先のポータブル電源の残量を取得できているか（給電先が未設定なら False）。
    started_at: その機器を OFF にした時刻（最大連続OFF時間の判定に使う。未開始なら None）。
    任意スケジュールはユーザーが指定した時間帯のため、予報が未取得でも実行する（docs/14 §14.2）。
    """
    if not has_forecast and schedule.source_level != ScheduleSource.CUSTOM:
        return OffDecision(False, "料金予報が未取得のため OFF にしません")

    # 注意報・任意 OFF は常に、警報は残量を取得できない間だけ、OFF 時間の制限を実行時にも適用する
    if schedule.source_level != ScheduleSource.WARNING or not battery_known:
        if not control.off_all_day and not control.operation_start <= now.time() < control.operation_end:
            return OffDecision(False, "自動 OFF の許可時間外のため ON に戻します")
        if started_at is not None:
            continuous = int((now - started_at).total_seconds() // 60)
            if continuous >= control.max_continuous_off_minutes:
                return OffDecision(False, f"最大連続OFF時間（{control.max_continuous_off_minutes}分）に達しました")
        if day_off_minutes >= control.max_daily_off_minutes:
            return OffDecision(False, f"1日の最大OFF時間（{control.max_daily_off_minutes}分）に達しました")
    return OffDecision(True)


# --- ポータブル電源の残量（docs/07 §7.7） ---

BATTERY_CHECK_INTERVAL = timedelta(minutes=5)  # OFF 中の取得間隔
BATTERY_STALE_AFTER = timedelta(minutes=10)  # これより古い値は「取得できない」とみなす


@dataclass(frozen=True)
class BatteryReading:
    percent: int  # 残量（%）
    output_w: float  # 出力電力（家電の消費電力）
    at: datetime


def battery_known(reading: BatteryReading | None, now: datetime) -> bool:
    return reading is not None and now - reading.at <= BATTERY_STALE_AFTER


@dataclass(frozen=True)
class BatteryLimits:
    """ポータブル電源ごとの判定の設定（portable_power_settings。docs/17 §17.4.4）。"""

    min_percent: int  # 残量の下限（%）
    capacity_wh: float  # 容量（Wh）
    efficiency: float  # 変換効率（0〜1）

    @classmethod
    def from_control(cls, control: ControlSettings) -> "BatteryLimits":
        """全体の設定値（設定がないポータブル電源の代わりに使う）。"""
        return cls(control.battery_min_percent, control.battery_capacity_wh, control.round_trip_efficiency)


def drain_percent_per_hour(power_w: float, limits: BatteryLimits) -> float:
    # 出力（家電の消費電力）より、変換ロスの分だけ多く電池から減る。効率は節約額の計算と共用（安全側に低めになる）
    return power_w / limits.efficiency / limits.capacity_wh * 100


def projected_percent(reading: BatteryReading, power_w: float, until: datetime, limits: BatteryLimits) -> float:
    hours = max(0.0, (until - reading.at).total_seconds() / 3600)
    return reading.percent - drain_percent_per_hour(power_w, limits) * hours


def check_battery(
    reading: BatteryReading,
    now: datetime,
    limits: BatteryLimits,
    *,
    average_output_w: float | None,
    end_at: datetime | None,
) -> OffDecision:
    """残量と、次の取得時（終了の方が早ければ終了時）の予測残量が下限を下回らないか。

    消費電力は今回値と OFF 中の平均の大きい方を使い、予測を安全側に寄せる。
    """
    if reading.percent <= limits.min_percent:
        return OffDecision(False, f"ポータブル電源の残量（{reading.percent}%）が下限（{limits.min_percent}%）以下です")
    power = max(reading.output_w, average_output_w or 0.0)
    horizon = now + BATTERY_CHECK_INTERVAL
    if end_at is not None:
        horizon = min(horizon, end_at)
    projected = projected_percent(reading, power, horizon, limits)
    if projected < limits.min_percent:
        return OffDecision(
            False,
            f"消費電力 {power:.0f}W が続くと {horizon:%H:%M} に残量が {projected:.1f}% になり、"
            f"下限（{limits.min_percent}%）を下回るため",
        )
    return OffDecision(True)


def time_to_min_percent(reading: BatteryReading, power_w: float, limits: BatteryLimits) -> datetime | None:
    """このままの消費電力で下限に達する見込み時刻（画面表示用）。"""
    drain = drain_percent_per_hour(power_w, limits)
    if drain <= 0:
        return None
    return reading.at + timedelta(hours=(reading.percent - limits.min_percent) / drain)
