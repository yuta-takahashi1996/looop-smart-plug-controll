"""制御スケジュール（control_schedules）と制御履歴（control_logs）。docs/10-database.md §10.2・§10.3。

スケジュールはでんき予報の注意報・警報から生成した OFF の予定と、任意スケジュールのルール（schedule_rules、
docs/10 §10.9）から展開した ON／OFF の予定で、有効（enabled）なものだけを実行する。
手動で変更（有効・無効の切り替え、時間変更）したものは manually_modified とし、再生成で上書きしない。
制御履歴は Control Service が行った（Dry Run では行うはずだった）すべての状態変更の記録。
"""

import enum
from datetime import date, datetime, time

from sqlalchemy import Boolean, Date, Enum, Float, Integer, String, Text, Time
from sqlalchemy.orm import Mapped, mapped_column

from app.repositories.database import Base, UTCDateTime


class ScheduleSource(enum.StrEnum):
    """スケジュールの生成元。ADVISORY / WARNING は ForecastLevel と同じ値。"""

    ADVISORY = "ADVISORY"  # でんき注意報
    WARNING = "WARNING"  # でんき警報
    CUSTOM = "CUSTOM"  # 任意スケジュール（docs/07 §7.9）


class RepeatType(enum.StrEnum):
    ONCE = "ONCE"  # 1回のみ（target_date）
    DAILY = "DAILY"  # 毎日
    WEEKLY = "WEEKLY"  # 曜日指定（weekdays）


class EnabledBy(enum.StrEnum):
    AUTO = "AUTO"  # 生成時の自動判定（警報は常に有効、注意報は AUTO_ENABLE）
    MANUAL = "MANUAL"  # ユーザーが切り替えた


class ScheduleStatus(enum.StrEnum):
    PENDING = "PENDING"  # 未実行
    RUNNING = "RUNNING"  # 実行中（OFF）
    DONE = "DONE"  # 終了時刻まで実行した
    ABORTED = "ABORTED"  # 安全制御で途中で ON に戻した、または開始できなかった
    SKIPPED = "SKIPPED"  # 実行しなかった（無効だった、システムが停止していた）


class ControlSource(enum.StrEnum):
    AUTO = "AUTO"  # スケジュールによる自動制御
    MANUAL = "MANUAL"  # Web UI からの手動操作（Phase 6）
    SAFETY = "SAFETY"  # 安全制御（起動・終了時の ON 復帰、制限超過など）


class PlugState(enum.StrEnum):
    ON = "ON"  # 商用電源（GRID）
    OFF = "OFF"  # 蓄電池（BATTERY）


class ControlResult(enum.StrEnum):
    SUCCESS = "SUCCESS"  # 送信後の状態確認で期待状態と一致した（Phase 6）
    FAILED = "FAILED"
    UNCONFIRMED = "UNCONFIRMED"  # 送信したが状態を確認できなかった
    DRY_RUN = "DRY_RUN"  # Dry Run のため送信していない


class ControlSchedule(Base):
    __tablename__ = "control_schedules"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    schedule_date: Mapped[date] = mapped_column(Date, index=True)  # 対象日（表示用のタイムゾーン）
    schedule_version: Mapped[int] = mapped_column(Integer)  # 対象日の生成回数
    start_at: Mapped[datetime] = mapped_column(UTCDateTime)
    end_at: Mapped[datetime] = mapped_column(UTCDateTime)
    target_state: Mapped[str] = mapped_column(String(8), default="OFF")
    source_level: Mapped[ScheduleSource] = mapped_column(Enum(ScheduleSource, native_enum=False))
    rule_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)  # 任意スケジュールの展開元
    enabled: Mapped[bool] = mapped_column(Boolean)
    enabled_by: Mapped[EnabledBy] = mapped_column(Enum(EnabledBy, native_enum=False))
    manually_modified: Mapped[bool] = mapped_column(Boolean, default=False)
    reason: Mapped[str] = mapped_column(Text)
    estimated_saving: Mapped[float] = mapped_column(Float)  # 円（電源単価で算出、docs/07 §7.2）
    status: Mapped[ScheduleStatus] = mapped_column(
        Enum(ScheduleStatus, native_enum=False), default=ScheduleStatus.PENDING
    )
    # 実行結果（推定節約額の検証用）。実際に OFF にした（Dry Run では OFF にするはずだった）時間帯と、その時間帯の推定節約額
    executed_start_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    executed_end_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    executed_saving: Mapped[float | None] = mapped_column(Float, nullable=True)
    status_reason: Mapped[str | None] = mapped_column(Text, nullable=True)  # 中断・スキップの理由
    # 実測値（制御パラメータ調整の材料）。ポータブル電源の残量を取得できた場合のみ
    battery_start_percent: Mapped[int | None] = mapped_column(Integer, nullable=True)
    battery_end_percent: Mapped[int | None] = mapped_column(Integer, nullable=True)
    measured_power_w: Mapped[float | None] = mapped_column(Float, nullable=True)  # OFF 中の平均消費電力
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime)


class ScheduleRule(Base):
    """任意スケジュールのルール（docs/10-database.md §10.9）。当日・翌日分を control_schedules に展開する。"""

    __tablename__ = "schedule_rules"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(50))
    target_state: Mapped[str] = mapped_column(String(8))  # ON / OFF
    repeat_type: Mapped[RepeatType] = mapped_column(Enum(RepeatType, native_enum=False))
    target_date: Mapped[date | None] = mapped_column(Date, nullable=True)  # ONCE のみ
    weekdays: Mapped[str | None] = mapped_column(String(16), nullable=True)  # WEEKLY のみ。月=0〜日=6 のカンマ区切り
    # 表示用のタイムゾーンの時刻（30分単位）。end_time の 00:00 は 24:00 とみなす
    start_time: Mapped[time] = mapped_column(Time)
    end_time: Mapped[time] = mapped_column(Time)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime)

    @property
    def weekday_set(self) -> set[int]:
        return {int(d) for d in (self.weekdays or "").split(",") if d}

    @property
    def start_minute(self) -> int:
        return self.start_time.hour * 60 + self.start_time.minute

    @property
    def end_minute(self) -> int:
        return self.end_time.hour * 60 + self.end_time.minute or 24 * 60

    def applies_to(self, day: date) -> bool:
        """day に適用されるか（無効なルールは適用しない）。"""
        if not self.enabled:
            return False
        if self.repeat_type == RepeatType.ONCE:
            return self.target_date == day
        if self.repeat_type == RepeatType.WEEKLY:
            return day.weekday() in self.weekday_set
        return True


class ControlLog(Base):
    __tablename__ = "control_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    executed_at: Mapped[datetime] = mapped_column(UTCDateTime, index=True)
    source: Mapped[ControlSource] = mapped_column(Enum(ControlSource, native_enum=False))
    previous_state: Mapped[PlugState | None] = mapped_column(Enum(PlugState, native_enum=False), nullable=True)
    requested_state: Mapped[PlugState] = mapped_column(Enum(PlugState, native_enum=False))
    actual_state: Mapped[PlugState | None] = mapped_column(Enum(PlugState, native_enum=False), nullable=True)
    result: Mapped[ControlResult] = mapped_column(Enum(ControlResult, native_enum=False))
    reason: Mapped[str] = mapped_column(Text)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    dry_run: Mapped[bool] = mapped_column(Boolean)
    schedule_id: Mapped[int | None] = mapped_column(Integer, nullable=True)  # スケジュールによる操作の場合


class DailySummary(Base):
    """日次集計（docs/10-database.md §10.7）。前日分を毎日 0:05 に集計する（当日分は画面で都度集計）。"""

    __tablename__ = "daily_summaries"

    summary_date: Mapped[date] = mapped_column(Date, primary_key=True)
    off_minutes: Mapped[int] = mapped_column(Integer)  # 実行した OFF 時間の合計
    switch_count: Mapped[int] = mapped_column(Integer)  # 状態が変わった操作の回数
    schedules_done: Mapped[int] = mapped_column(Integer)
    schedules_aborted: Mapped[int] = mapped_column(Integer)
    schedules_skipped: Mapped[int] = mapped_column(Integer)  # 無効・停止中などで実行しなかった
    estimated_saving: Mapped[float] = mapped_column(Float)  # 実行分の推定節約額の合計（円）
    battery_used_percent: Mapped[int | None] = mapped_column(Integer, nullable=True)  # 実測できた OFF の残量減少の合計
    measured_power_w: Mapped[float | None] = mapped_column(Float, nullable=True)  # 実測できた OFF の平均消費電力
    failed_operations: Mapped[int] = mapped_column(Integer)  # 失敗・未確認の操作の件数
    dry_run: Mapped[bool] = mapped_column(Boolean)  # Dry Run の記録を含むか
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime)
