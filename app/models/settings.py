"""システム設定（system_settings）とシステムイベント（system_events）。

Web UI から変更できる制御条件を key/value で保存する。
秘匿情報（SwitchBot の認証情報など）は保存せず、環境変数のみで管理する。
Dry Run の解除は明示的な確認操作が必要なため、ここでは扱わない。
"""

import enum
from datetime import UTC, datetime, time

from pydantic import BaseModel, Field, model_validator
from sqlalchemy import DateTime, Enum, Integer, String, Text, select
from sqlalchemy.orm import Mapped, Session, mapped_column

from app.config import Settings
from app.repositories.database import Base, UTCDateTime


class SystemSetting(Base):
    __tablename__ = "system_settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ControlSettings(BaseModel):
    """Web UI から変更できる制御条件。制約は app/config.py の Settings に揃える。"""

    auto_enable: bool
    operation_start: time
    operation_end: time
    round_trip_efficiency: float = Field(gt=0, le=1)
    max_continuous_off_minutes: int = Field(ge=0)
    max_daily_off_minutes: int = Field(ge=0)
    min_switch_interval_minutes: int = Field(ge=0)
    estimated_average_power_w: float = Field(ge=0)
    battery_min_percent: int = Field(ge=0, le=100)
    battery_capacity_wh: float = Field(gt=0)

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


def get_value(session: Session, key: str) -> str | None:
    """system_settings の任意の値（制御モードなど、制御条件以外の保持値）。"""
    row = session.get(SystemSetting, key)
    return row.value if row is not None else None


def set_value(session: Session, key: str, value: str) -> None:
    session.merge(SystemSetting(key=key, value=value, updated_at=datetime.now(UTC)))
    session.commit()


class EventLevel(enum.StrEnum):
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"


class EventCategory(enum.StrEnum):
    FORECAST = "FORECAST"  # 料金予報の取得・検証
    SWITCHBOT = "SWITCHBOT"  # SwitchBot API との通信・コマンドの失敗
    BATTERY = "BATTERY"  # ポータブル電源（Jackery）の残量取得
    PRICE_NOTICE = "PRICE_NOTICE"  # 単価に関わる Looop のお知らせ（加算額の確認が必要）


class SystemEvent(Base):
    """エラー・イベント履歴。未解決のものは Web UI に表示する。"""

    __tablename__ = "system_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    occurred_at: Mapped[datetime] = mapped_column(UTCDateTime, index=True)
    level: Mapped[EventLevel] = mapped_column(Enum(EventLevel, native_enum=False))
    category: Mapped[EventCategory] = mapped_column(Enum(EventCategory, native_enum=False))
    message: Mapped[str] = mapped_column(Text)
    # 重複検知用のキー（お知らせの URL など）
    reference: Mapped[str | None] = mapped_column(String(512), nullable=True, index=True)
    resolved_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)


def record_event(
    session: Session,
    level: EventLevel,
    category: EventCategory,
    message: str,
    *,
    reference: str | None = None,
) -> SystemEvent:
    event = SystemEvent(
        occurred_at=datetime.now(UTC),
        level=level,
        category=category,
        message=message,
        reference=reference,
    )
    session.add(event)
    session.commit()
    return event


def resolve_events(session: Session, category: EventCategory) -> None:
    """カテゴリの未解決イベントを解決済みにする（例：取得失敗後に取得が成功したとき）。"""
    now = datetime.now(UTC)
    for event in session.scalars(
        select(SystemEvent).where(SystemEvent.category == category, SystemEvent.resolved_at.is_(None))
    ):
        event.resolved_at = now
    session.commit()


def unresolved_events(session: Session) -> list[SystemEvent]:
    return list(
        session.scalars(
            select(SystemEvent)
            .where(SystemEvent.resolved_at.is_(None))
            .order_by(SystemEvent.occurred_at.desc())
        )
    )
