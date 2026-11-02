"""料金予報の取得元（Forecast Provider）の共通定義。

取得方式が変わっても Forecast Service 側を変えずに差し替えられるよう、
取得元ごとの実装はこのインターフェースに合わせる。
"""

from dataclasses import dataclass
from datetime import date, datetime
from typing import Protocol


class ForecastFetchError(Exception):
    """取得失敗。retryable が True なら時間をおいて再試行してよい。"""

    def __init__(self, message: str, *, retryable: bool = True) -> None:
        super().__init__(message)
        self.retryable = retryable


@dataclass(frozen=True)
class DayPrices:
    """1日分の 30 分単位の電源料金単価（円/kWh、税込）。prices[0] が 0:00〜0:30。"""

    day: date
    prices: list[float]


@dataclass(frozen=True)
class FetchResult:
    area: str
    fetched_at: datetime
    days: list[DayPrices]  # 取得できた日のみ（明日分が未公開なら含まない）
    source: str


@dataclass(frozen=True)
class Notice:
    """取得元のお知らせ（単価改定・補助金など）。"""

    url: str
    title: str


class ForecastProvider(Protocol):
    def fetch(self, area: str) -> FetchResult: ...

    def fetch_notices(self) -> list[Notice]: ...
