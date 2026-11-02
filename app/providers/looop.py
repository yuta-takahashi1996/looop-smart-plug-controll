"""Looopでんき でんき予報 からの料金取得（docs/06-forecast-provider.md）。

でんき予報ページ（https://looop-denki.com/home/denkiforecast/）が利用している
`GET /api/prices?select_area=<エリアコード>` を利用する（ログイン不要）。

レスポンスは {"0": 昨日, "1": 今日, "2": 明日, "label": [...], "timelist": [...]} の形で、
各日に price_data（48 コマの電源料金単価）と level（でんき予報の区分。-0.5：でんき日和、
0：なし、0.5：でんき注意報、1：でんき警報）が入る。明日分が未公開の場合は disabled_flg、
データ欠損時は defrect_flg が付く。レスポンスに日付は含まれないため、取得時点の日本時間の
日付から対象日を決める。日付の境界をまたぐ取り違えを防ぐため、0 時前後は取得しない。
"""

import html
import re
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

import httpx

from app.providers.base import DayPrices, FetchResult, ForecastFetchError, ForecastLevel, Notice

BASE_URL = "https://looop-denki.com"
SOURCE = "looop-denki-forecast"
JST = ZoneInfo("Asia/Tokyo")

AREA_CODES = {
    "hokkaido": "01",
    "tohoku": "02",
    "tokyo": "03",
    "chubu": "04",
    "hokuriku": "05",
    "kansai": "06",
    "chugoku": "07",
    "shikoku": "08",
    "kyushu": "09",
    "okinawa": "10",
}

# レスポンスのキーと、取得日からの日数
_DAY_KEYS = {"1": 0, "2": 1}

# level の値とでんき予報の区分（unit_price.js の描画処理より）
_LEVELS = {-0.5: ForecastLevel.GOOD, 0: ForecastLevel.NORMAL, 0.5: ForecastLevel.ADVISORY, 1: ForecastLevel.WARNING}

# 日付境界の前後この時間は取得しない（サーバー側の「今日」と取り違えないため）
MIDNIGHT_GUARD = timedelta(minutes=5)

_NOTICE_LINK = re.compile(r'<a\s[^>]*href="(?P<url>[^"]+)"[^>]*>\s*(?P<title>【[^<]+)</a>')


class LooopForecastProvider:
    def __init__(self, client: httpx.Client | None = None, timeout: float = 10.0) -> None:
        self._client = client or httpx.Client(
            base_url=BASE_URL,
            timeout=timeout,
            headers={"User-Agent": "looop-smart-plug-controller (personal use)"},
        )

    def fetch(self, area: str) -> FetchResult:
        code = AREA_CODES.get(area)
        if code is None:
            raise ForecastFetchError(f"未対応のエリアです: {area}", retryable=False)

        started = datetime.now(JST)
        _ensure_outside_midnight(started)
        try:
            response = self._client.get("/api/prices", params={"select_area": code})
            response.raise_for_status()
            body = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise ForecastFetchError(f"料金予報の取得に失敗しました: {exc}") from exc
        finished = datetime.now(JST)
        if finished.date() != started.date():
            raise ForecastFetchError("取得中に日付が変わったため、取得結果を破棄しました")

        if not isinstance(body, dict):
            raise ForecastFetchError("料金予報の形式が想定と異なります")
        if "error" in body:
            raise ForecastFetchError(f"料金予報の取得元がエラーを返しました: {body['error']}")

        days = []
        for key, offset in _DAY_KEYS.items():
            day_data = body.get(key)
            if not isinstance(day_data, dict) or "disabled_flg" in day_data or "defrect_flg" in day_data:
                continue
            prices = day_data.get("price_data")
            levels = day_data.get("level")
            days.append(
                DayPrices(
                    day=started.date() + timedelta(days=offset),
                    prices=list(prices) if isinstance(prices, list) else [],
                    levels=[_to_level(v) for v in levels] if isinstance(levels, list) else [],
                )
            )
        return FetchResult(area=area, fetched_at=finished, days=days, source=SOURCE)

    def fetch_notices(self) -> list[Notice]:
        try:
            response = self._client.get("/home/news/")
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise ForecastFetchError(f"お知らせ一覧の取得に失敗しました: {exc}") from exc
        notices = []
        for match in _NOTICE_LINK.finditer(response.text):
            url = str(httpx.URL(BASE_URL).join(html.unescape(match["url"])))
            notices.append(Notice(url=url, title=html.unescape(match["title"]).strip()))
        return notices


def _to_level(value: object) -> ForecastLevel | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return _LEVELS.get(value)


def _ensure_outside_midnight(now: datetime) -> None:
    midnight = datetime.combine(now.date(), time(), tzinfo=JST)
    if now - midnight < MIDNIGHT_GUARD or midnight + timedelta(days=1) - now < MIDNIGHT_GUARD:
        raise ForecastFetchError("日付の境界付近のため、取得を見送りました")
