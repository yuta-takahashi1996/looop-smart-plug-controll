"""料金予報（forecast_prices）と請求単価への加算額（price_adjustments）。

制御の判断には でんき予報 の電源料金単価と区分（注意報・警報）を使う。
請求単価（電源料金単価 ＋ 対象日に有効な加算額の合計）は、実際の支払単価の目安として表示するためだけに使う。
加算額はサービス料金・託送従量料金相当額・再エネ賦課金・国の補助金（値引き）などで、
改定や補助金の期間に合わせて適用期間付きで登録する。
"""

import enum
from datetime import date, datetime

from sqlalchemy import Date, Enum, Float, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.providers.base import ForecastLevel
from app.repositories.database import Base, UTCDateTime


class ForecastPrice(Base):
    __tablename__ = "forecast_prices"
    __table_args__ = (UniqueConstraint("area", "slot_start"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    area: Mapped[str] = mapped_column(String(32))
    slot_start: Mapped[datetime] = mapped_column(UTCDateTime, index=True)
    slot_end: Mapped[datetime] = mapped_column(UTCDateTime)
    # 電源料金単価（円/kWh、税込）。取得値をそのまま保存し、請求単価は都度算出する
    energy_price: Mapped[float] = mapped_column(Float)
    # でんき予報の区分（でんき日和・注意報・警報）
    level: Mapped[ForecastLevel] = mapped_column(
        Enum(ForecastLevel, native_enum=False), server_default=ForecastLevel.NORMAL.value
    )
    fetched_at: Mapped[datetime] = mapped_column(UTCDateTime)
    source: Mapped[str] = mapped_column(String(64))


class AdjustmentCategory(enum.StrEnum):
    SERVICE = "SERVICE"
    TRANSMISSION = "TRANSMISSION"
    RENEWABLE_LEVY = "RENEWABLE_LEVY"
    SUBSIDY = "SUBSIDY"
    OTHER = "OTHER"


ADJUSTMENT_CATEGORY_LABELS = {
    AdjustmentCategory.SERVICE: "サービス料金",
    AdjustmentCategory.TRANSMISSION: "託送従量料金相当額",
    AdjustmentCategory.RENEWABLE_LEVY: "再エネ賦課金",
    AdjustmentCategory.SUBSIDY: "補助金・値引き",
    AdjustmentCategory.OTHER: "その他",
}


class PriceAdjustment(Base):
    """請求単価への加算額（円/kWh、税込）。値引きは負の値で登録する。"""

    __tablename__ = "price_adjustments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    category: Mapped[AdjustmentCategory] = mapped_column(Enum(AdjustmentCategory, native_enum=False))
    amount: Mapped[float] = mapped_column(Float)
    # 使用日ベースの適用期間（終了日を含む）。終了日なしは期限なし
    effective_from: Mapped[date] = mapped_column(Date)
    effective_to: Mapped[date | None] = mapped_column(Date, nullable=True)
    note: Mapped[str] = mapped_column(Text, default="")
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime)
