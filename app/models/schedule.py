"""制御スケジュール（control_schedules、docs/10-database.md §10.2）。

でんき予報の注意報・警報から生成した OFF の予定。有効（enabled）なものだけを実行する。
手動で変更（有効・無効の切り替え、時間変更）したものは manually_modified とし、再生成で上書きしない。
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
    PENDING = "PENDING"  # 未実行（実行は Phase 5 以降）


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
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime)
