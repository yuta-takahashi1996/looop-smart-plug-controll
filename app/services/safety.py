"""実行時の安全判定（docs/07-control-logic.md §7.5・§7.7、docs/14-safety.md）。

スケジュールの OFF を開始・継続してよいかを、実行のたびに判定する。生成時に制限内に収めた
スケジュールでも、ここで改めて確認する（手動変更や予報の更新、停止からの復帰などに備える）。
"""

from dataclasses import dataclass
from datetime import datetime, timedelta

from app.models.schedule import ControlSchedule
from app.models.settings import ControlSettings
from app.providers.base import ForecastLevel


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
) -> OffDecision:
    """schedule の OFF を now（表示用のタイムゾーンの日時）の時点で開始・継続してよいか。

    battery_known: ポータブル電源の残量を取得できているか（Phase 6 で連携する。それまでは常に False）。
    """
    if not has_forecast:
        return OffDecision(False, "料金予報が未取得のため OFF にしません")

    # 注意報は常に、警報は残量を取得できない間だけ、OFF 時間の制限を実行時にも適用する
    if schedule.source_level == ForecastLevel.ADVISORY or not battery_known:
        if not control.operation_start <= now.time() < control.operation_end:
            return OffDecision(False, "運用時間外のため ON に戻します")
        if schedule.executed_start_at is not None:
            continuous = int((now - schedule.executed_start_at).total_seconds() // 60)
            if continuous >= control.max_continuous_off_minutes:
                return OffDecision(False, f"最大連続OFF時間（{control.max_continuous_off_minutes}分）に達しました")
        if day_off_minutes >= control.max_daily_off_minutes:
            return OffDecision(False, f"1日の最大OFF時間（{control.max_daily_off_minutes}分）に達しました")
    return OffDecision(True)

