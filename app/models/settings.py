"""システム設定（system_settings）。

Web UI から変更できる制御条件を key/value で保存する。
秘匿情報（SwitchBot の認証情報など）は保存せず、環境変数のみで管理する。
Dry Run の解除は明示的な確認操作が必要なため、ここでは扱わない。
"""

from datetime import UTC, datetime, time

from pydantic import BaseModel, Field, model_validator
from sqlalchemy import DateTime, String, Text, select
from sqlalchemy.orm import Mapped, Session, mapped_column

from app.config import Settings
from app.repositories.database import Base


class SystemSetting(Base):
    __tablename__ = "system_settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ControlSettings(BaseModel):
    """Web UI から変更できる制御条件。制約は app/config.py の Settings に揃える。"""

    operation_start: time
    operation_end: time
    min_saving_spread: float = Field(ge=0)
    round_trip_efficiency: float = Field(gt=0, le=1)
    max_continuous_off_minutes: int = Field(ge=0)
    max_daily_off_minutes: int = Field(ge=0)
    min_switch_interval_minutes: int = Field(ge=0)
    estimated_average_power_w: float = Field(ge=0)

    @model_validator(mode="after")
    def _check_consistency(self) -> "ControlSettings":
        if self.operation_start >= self.operation_end:
            raise ValueError("運用開始時刻は運用終了時刻より前にしてください")
        if self.max_continuous_off_minutes > self.max_daily_off_minutes:
            raise ValueError("最大連続OFF時間は1日の最大OFF時間以下にしてください")
        return self


def load_control_settings(session: Session, settings: Settings) -> ControlSettings:
    """保存済みの値を優先し、未保存の項目は環境変数の値を使う。"""
    values = {name: getattr(settings, name) for name in ControlSettings.model_fields}
    stored = session.scalars(
        select(SystemSetting).where(SystemSetting.key.in_(ControlSettings.model_fields))
    )
    values.update({row.key: row.value for row in stored})
    return ControlSettings.model_validate(values)


def save_control_settings(session: Session, data: ControlSettings) -> None:
    now = datetime.now(UTC)
    for key, value in data.model_dump(mode="json").items():
        session.merge(SystemSetting(key=key, value=str(value), updated_at=now))
    session.commit()
