"""料金予報の取得・検証・保存と、請求単価の算出（docs/06-forecast-provider.md）。

取得した電源料金単価は検証を通ったものだけを保存する。検証に失敗した日のデータは
保存せず、イベントとして記録する（不正確なデータで制御計画を作らないため）。
請求単価は「電源料金単価 ＋ 対象日に有効な加算額の合計」で都度算出する。
"""

import logging
import math
import re
import time as time_module
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.models.forecast import ForecastPrice, PriceAdjustment
from app.models.settings import (
    EventCategory,
    EventLevel,
    SystemEvent,
    SystemSetting,
    record_event,
    resolve_events,
)
from app.providers.base import DayPrices, FetchResult, ForecastFetchError, ForecastProvider

logger = logging.getLogger(__name__)

SLOTS_PER_DAY = 48
SLOT = timedelta(minutes=30)
# 電源料金単価として妥当な範囲（円/kWh）。範囲外は取得元の異常とみなす
MIN_PRICE, MAX_PRICE = 0.0, 1000.0

# 請求単価に関わる可能性があるお知らせのタイトル
PRICE_NOTICE_PATTERN = re.compile(r"料金支援|負担軽減|料金改定|託送|賦課金|値引|単価")
# お知らせの初回確認日時（この時点で公開済みのお知らせは通知しない）
NOTICE_BASELINE_KEY = "price_notices_baseline_at"


class ForecastValidationError(Exception):
    pass


@dataclass(frozen=True)
class PriceSlot:
    start: datetime
    end: datetime
    energy_price: float  # 電源料金単価（取得値）
    billing_price: float  # 請求単価 ＝ 電源料金単価 ＋ 加算額


@dataclass(frozen=True)
class DayForecast:
    day: date
    slots: list[PriceSlot]  # 未取得なら空
    adjustment_total: float  # 対象日の加算額合計（円/kWh）
    adjustments: list[PriceAdjustment]
    fetched_at: datetime | None


def validate_day(day_prices: DayPrices) -> list[float]:
    """48 コマ揃っていること、数値として妥当なことを確認する（docs/06 §6.4）。"""
    prices = day_prices.prices
    if len(prices) != SLOTS_PER_DAY:
        raise ForecastValidationError(
            f"{day_prices.day} のコマ数が {len(prices)} 件です（{SLOTS_PER_DAY} 件必要）"
        )
    validated = []
    for index, value in enumerate(prices):
        if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
            raise ForecastValidationError(f"{day_prices.day} の {index + 1} コマ目が数値ではありません: {value!r}")
        if not MIN_PRICE <= value <= MAX_PRICE:
            raise ForecastValidationError(f"{day_prices.day} の {index + 1} コマ目が範囲外です: {value}")
        validated.append(float(value))
    return validated


def _slot_starts(day: date, tz: ZoneInfo) -> list[datetime]:
    """対象日の 48 コマの開始時刻。重複・欠損なく 30 分刻みで連続する。"""
    start = datetime.combine(day, time(), tzinfo=tz)
    return [start + SLOT * index for index in range(SLOTS_PER_DAY)]


def save_result(session: Session, result: FetchResult, tz: ZoneInfo) -> list[date]:
    """検証を通った日のみ保存し、保存した日付を返す。同じコマは最新の取得値で上書きする。"""
    today = result.fetched_at.astimezone(tz).date()
    saved: list[date] = []
    for day_prices in result.days:
        try:
            if day_prices.day < today:
                raise ForecastValidationError(f"過去日（{day_prices.day}）のデータは保存しません")
            prices = validate_day(day_prices)
        except ForecastValidationError as exc:
            logger.warning("forecast validation failed: %s", exc)
            record_event(session, EventLevel.ERROR, EventCategory.FORECAST, f"料金データの検証に失敗しました：{exc}")
            continue

        starts = _slot_starts(day_prices.day, tz)
        existing = {
            row.slot_start: row
            for row in session.scalars(
                select(ForecastPrice).where(
                    ForecastPrice.area == result.area,
                    ForecastPrice.slot_start >= starts[0],
                    ForecastPrice.slot_start <= starts[-1],
                )
            )
        }
        for start, price in zip(starts, prices, strict=True):
            row = existing.get(start.astimezone(UTC))
            if row is None:
                row = ForecastPrice(area=result.area, slot_start=start)
                session.add(row)
            row.slot_end = start + SLOT
            row.energy_price = price
            row.fetched_at = result.fetched_at
            row.source = result.source
        session.commit()
        saved.append(day_prices.day)
    return saved


def fetch_and_store(
    session: Session,
    provider: ForecastProvider,
    area: str,
    tz: ZoneInfo,
    *,
    retry_count: int = 0,
    retry_wait_seconds: float = 0,
) -> list[date]:
    """料金予報を取得して保存する。取得に失敗した場合はイベントを記録して例外を送出する。"""
    result: FetchResult | None = None
    error: ForecastFetchError | None = None
    for attempt in range(retry_count + 1):
        if attempt:
            time_module.sleep(retry_wait_seconds)
        try:
            result = provider.fetch(area)
            break
        except ForecastFetchError as exc:
            error = exc
            logger.warning("forecast fetch failed (attempt %d): %s", attempt + 1, exc)
            if not exc.retryable:
                break
    if result is None:
        assert error is not None
        record_event(session, EventLevel.ERROR, EventCategory.FORECAST, str(error))
        raise error

    saved = save_result(session, result, tz)
    today = result.fetched_at.astimezone(tz).date()
    if today in saved:
        resolve_events(session, EventCategory.FORECAST)
    logger.info("forecast saved for %s (area=%s)", ", ".join(map(str, saved)) or "none", area)
    return saved


def has_forecast(session: Session, area: str, day: date, tz: ZoneInfo) -> bool:
    starts = _slot_starts(day, tz)
    count = len(
        session.scalars(
            select(ForecastPrice.id).where(
                ForecastPrice.area == area,
                ForecastPrice.slot_start >= starts[0],
                ForecastPrice.slot_start <= starts[-1],
            )
        ).all()
    )
    return count == SLOTS_PER_DAY


def adjustments_for(session: Session, day: date) -> list[PriceAdjustment]:
    return list(
        session.scalars(
            select(PriceAdjustment)
            .where(
                PriceAdjustment.effective_from <= day,
                or_(PriceAdjustment.effective_to.is_(None), PriceAdjustment.effective_to >= day),
            )
            .order_by(PriceAdjustment.category, PriceAdjustment.effective_from)
        )
    )


def get_day_forecast(session: Session, area: str, day: date, tz: ZoneInfo) -> DayForecast:
    """保存済みの予報と加算額から、対象日の請求単価を算出する。48 コマ揃わない日は未取得として扱う。"""
    adjustments = adjustments_for(session, day)
    total = round(sum(a.amount for a in adjustments), 4)
    starts = _slot_starts(day, tz)
    rows = list(
        session.scalars(
            select(ForecastPrice)
            .where(
                ForecastPrice.area == area,
                ForecastPrice.slot_start >= starts[0],
                ForecastPrice.slot_start <= starts[-1],
            )
            .order_by(ForecastPrice.slot_start)
        )
    )
    if len(rows) != SLOTS_PER_DAY:
        return DayForecast(day=day, slots=[], adjustment_total=total, adjustments=adjustments, fetched_at=None)
    slots = [
        PriceSlot(
            start=row.slot_start.astimezone(tz),
            end=row.slot_end.astimezone(tz),
            energy_price=row.energy_price,
            billing_price=round(row.energy_price + total, 2),
        )
        for row in rows
    ]
    return DayForecast(
        day=day,
        slots=slots,
        adjustment_total=total,
        adjustments=adjustments,
        fetched_at=max(row.fetched_at for row in rows).astimezone(tz),
    )


def check_price_notices(session: Session, provider: ForecastProvider) -> int:
    """単価に関わる新しいお知らせがあれば、確認を促すイベントとして記録する。通知した件数を返す。

    加算額の値は自動更新しない（お知らせは画像や本文で公開され、後から確定することもあるため）。
    """
    notices = [n for n in provider.fetch_notices() if PRICE_NOTICE_PATTERN.search(n.title)]
    first_check = session.get(SystemSetting, NOTICE_BASELINE_KEY) is None
    known = set(
        session.scalars(
            select(SystemEvent.reference).where(
                SystemEvent.category == EventCategory.PRICE_NOTICE,
                SystemEvent.reference.in_([n.url for n in notices]),
            )
        )
    )
    created = 0
    for notice in notices:
        if notice.url in known:
            continue
        event = record_event(
            session,
            EventLevel.WARNING,
            EventCategory.PRICE_NOTICE,
            f"単価に関わるお知らせ：{notice.title}",
            reference=notice.url,
        )
        if first_check:
            # 初回は既存のお知らせを確認済みとして記録し、以降の新着のみ通知する
            event.resolved_at = event.occurred_at
            session.commit()
        else:
            created += 1
    if first_check:
        now = datetime.now(UTC)
        session.add(SystemSetting(key=NOTICE_BASELINE_KEY, value=now.isoformat(), updated_at=now))
        session.commit()
    return created
