"""制御スケジュール（control_schedules）と制御履歴（control_logs）。docs/10-database.md §10.2・§10.3。

スケジュールはでんき予報の注意報・警報から生成した OFF の予定で、有効（enabled）なものだけを実行する。
手動で変更（有効・無効の切り替え、時間変更）したものは manually_modified とし、再生成で上書きしない。
制御履歴は Control Service が行った（Dry Run では行うはずだった）すべての状態変更の記録。
"""

import enum
from datetime import date, datetime

from sqlalchemy import Boolean, Date, Enum, Float, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.providers.base import ForecastLevel
from app.repositories.database import Base, UTCDateTime


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
    source_level: Mapped[ForecastLevel] = mapped_column(Enum(ForecastLevel, native_enum=False))
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
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime)


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
