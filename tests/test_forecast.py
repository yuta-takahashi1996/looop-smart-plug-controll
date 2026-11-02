from datetime import UTC, date, datetime, timedelta

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.forecast import AdjustmentCategory, ForecastPrice, PriceAdjustment
from app.models.settings import EventCategory, SystemEvent, unresolved_events
from app.providers import looop
from app.providers.base import DayPrices, FetchResult, ForecastFetchError, ForecastLevel, Notice
from app.providers.looop import LooopForecastProvider
from app.services import forecast as forecast_service
from tests.conftest import JST, FakeProvider, day_prices, sample_levels, sample_prices, today

# --- Looop Provider ---


LOOOP_LEVELS = [0] * 4 + [-0.5] * 4 + [0] * 26 + [0.5] * 4 + [1] * 2 + [0] * 8  # sample_levels() と同じ並び


def _looop_response(**overrides: object) -> dict:
    body: dict = {
        "0": {"price_data": sample_prices(5.0), "level": [0] * 48},
        "1": {"price_data": sample_prices(10.0), "level": LOOOP_LEVELS},
        "2": {"price_data": sample_prices(20.0), "level": [0] * 48},
        "label": [],
        "timelist": [],
    }
    body.update(overrides)
    return body


def _provider(handler) -> LooopForecastProvider:
    return LooopForecastProvider(httpx.Client(base_url=looop.BASE_URL, transport=httpx.MockTransport(handler)))


@pytest.fixture
def daytime(monkeypatch: pytest.MonkeyPatch) -> None:
    """日付境界の取得見送りに当たらないよう、境界判定を無効化する。"""
    monkeypatch.setattr(looop, "_ensure_outside_midnight", lambda now: None)


def test_looop_maps_today_and_tomorrow(daytime: None) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=_looop_response())

    result = _provider(handler).fetch("hokkaido")

    assert requests[0].url.path == "/api/prices"
    assert requests[0].url.params["select_area"] == "01"
    assert [d.day for d in result.days] == [today(), today() + timedelta(days=1)]
    assert result.days[0].prices == sample_prices(10.0)
    assert result.days[0].levels == sample_levels()
    assert result.days[1].prices == sample_prices(20.0)
    assert result.days[1].levels == [ForecastLevel.NORMAL] * 48


def test_looop_marks_unknown_levels(daytime: None) -> None:
    levels = [0] * 46 + [2, "0.5"]
    body = _looop_response(**{"1": {"price_data": sample_prices(), "level": levels}})
    result = _provider(lambda r: httpx.Response(200, json=body)).fetch("hokkaido")

    assert result.days[0].levels[-2:] == [None, None]


def test_looop_skips_unpublished_tomorrow(daytime: None) -> None:
    body = _looop_response(**{"2": {"disabled_flg": True}})
    result = _provider(lambda r: httpx.Response(200, json=body)).fetch("hokkaido")

    assert [d.day for d in result.days] == [today()]


def test_looop_skips_defective_day(daytime: None) -> None:
    body = _looop_response(**{"1": {"defrect_flg": True, "price_data": []}})
    result = _provider(lambda r: httpx.Response(200, json=body)).fetch("hokkaido")

    assert [d.day for d in result.days] == [today() + timedelta(days=1)]


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(500),
        httpx.Response(200, json={"error": "external api error"}),
        httpx.Response(200, text="<html>maintenance</html>"),
        httpx.Response(200, json=[1, 2, 3]),
    ],
)
def test_looop_fetch_errors(daytime: None, response: httpx.Response) -> None:
    with pytest.raises(ForecastFetchError) as exc_info:
        _provider(lambda r: response).fetch("hokkaido")
    assert exc_info.value.retryable


def test_looop_rejects_unknown_area() -> None:
    with pytest.raises(ForecastFetchError) as exc_info:
        _provider(lambda r: httpx.Response(200, json=_looop_response())).fetch("atlantis")
    assert not exc_info.value.retryable


@pytest.mark.parametrize(
    ("hour", "minute", "blocked"),
    [(23, 56, True), (0, 3, True), (23, 54, False), (0, 6, False), (12, 0, False)],
)
def test_looop_skips_fetch_around_midnight(hour: int, minute: int, blocked: bool) -> None:
    now = datetime(2026, 11, 1, hour, minute, tzinfo=JST)
    if blocked:
        with pytest.raises(ForecastFetchError):
            looop._ensure_outside_midnight(now)
    else:
        looop._ensure_outside_midnight(now)


def test_looop_parses_notices() -> None:
    page = """
    <li><a href="/home/news/272220/" class="u_txt1 u_txtlink">【重要】国の「電気・ガス料金支援」に関するお知らせ（2026/12～2027/2）</a></li>
    <li><a href="https://looop-denki.com/home/news/271746/" class="u_txt1 u_txtlink">【重要】不審な電話にご注意ください</a></li>
    <li><a href="/home/about/">Looopでんきとは？</a></li>
    """
    notices = _provider(lambda r: httpx.Response(200, text=page)).fetch_notices()

    assert notices == [
        Notice("https://looop-denki.com/home/news/272220/", "【重要】国の「電気・ガス料金支援」に関するお知らせ（2026/12～2027/2）"),
        Notice("https://looop-denki.com/home/news/271746/", "【重要】不審な電話にご注意ください"),
    ]


# --- 検証 ---


@pytest.mark.parametrize(
    ("prices", "message"),
    [
        (sample_prices()[:47], "コマ数"),
        (sample_prices() + [1.0], "コマ数"),
        (sample_prices()[:47] + ["12.3"], "数値ではありません"),
        (sample_prices()[:47] + [True], "数値ではありません"),
        (sample_prices()[:47] + [float("nan")], "数値ではありません"),
        (sample_prices()[:47] + [-0.01], "範囲外"),
        (sample_prices()[:47] + [1000.01], "範囲外"),
    ],
)
def test_validate_day_rejects_invalid_data(prices: list, message: str) -> None:
    with pytest.raises(forecast_service.ForecastValidationError) as exc_info:
        forecast_service.validate_day(day_prices(today(), prices))
    assert message in str(exc_info.value)


def test_validate_day_accepts_integers() -> None:
    prices = [10] * 48
    validated = forecast_service.validate_day(day_prices(today(), prices))
    assert [price for price, _ in validated] == [10.0] * 48
    assert [level for _, level in validated] == sample_levels()


@pytest.mark.parametrize(
    ("levels", "message"),
    [
        (sample_levels()[:47], "区分が 47 件"),
        ([], "区分が 0 件"),
        (sample_levels()[:47] + [None], "48 コマ目のでんき予報の区分を解釈できません"),
    ],
)
def test_validate_day_rejects_invalid_levels(levels: list, message: str) -> None:
    with pytest.raises(forecast_service.ForecastValidationError, match=message):
        forecast_service.validate_day(day_prices(today(), sample_prices(), levels))


# --- 保存 ---


def _result(*days: DayPrices) -> FetchResult:
    return FetchResult(area="hokkaido", fetched_at=datetime.now(JST), days=list(days), source="test")


def _count(session: Session) -> int:
    return session.scalar(select(func.count()).select_from(ForecastPrice))


def test_save_result_stores_48_continuous_slots(session: Session) -> None:
    saved = forecast_service.save_result(session, _result(day_prices(today(), sample_prices())), JST)

    assert saved == [today()]
    rows = session.scalars(select(ForecastPrice).order_by(ForecastPrice.slot_start)).all()
    assert len(rows) == 48
    first = rows[0].slot_start.astimezone(JST)
    assert (first.date(), first.hour, first.minute) == (today(), 0, 0)
    for previous, current in zip(rows, rows[1:]):
        assert current.slot_start - previous.slot_start == timedelta(minutes=30)
        assert previous.slot_end == current.slot_start


def test_save_result_overwrites_existing_slots(session: Session) -> None:
    forecast_service.save_result(session, _result(day_prices(today(), sample_prices(10.0))), JST)
    forecast_service.save_result(session, _result(day_prices(today(), sample_prices(30.0))), JST)

    assert _count(session) == 48
    assert session.scalar(select(func.min(ForecastPrice.energy_price))) == 30.0


def test_save_result_skips_invalid_day_and_keeps_valid_one(session: Session) -> None:
    saved = forecast_service.save_result(
        session,
        _result(day_prices(today(), sample_prices()), day_prices(today() + timedelta(days=1), sample_prices()[:10])),
        JST,
    )

    assert saved == [today()]
    assert _count(session) == 48
    events = unresolved_events(session)
    assert len(events) == 1 and "コマ数" in events[0].message


def test_save_result_rejects_past_day(session: Session) -> None:
    saved = forecast_service.save_result(
        session, _result(day_prices(today() - timedelta(days=1), sample_prices())), JST
    )

    assert saved == []
    assert _count(session) == 0
    assert "過去日" in unresolved_events(session)[0].message


# --- 取得（再試行・イベント） ---


def test_fetch_and_store_retries_then_succeeds(session: Session) -> None:
    provider = FakeProvider()
    provider.errors = [ForecastFetchError("timeout"), ForecastFetchError("timeout")]

    saved = forecast_service.fetch_and_store(session, provider, "hokkaido", JST, retry_count=2)

    assert provider.calls == 3
    assert saved == [today(), today() + timedelta(days=1)]
    assert unresolved_events(session) == []


def test_fetch_and_store_records_failure(session: Session) -> None:
    provider = FakeProvider()
    provider.errors = [ForecastFetchError("timeout")] * 3

    with pytest.raises(ForecastFetchError):
        forecast_service.fetch_and_store(session, provider, "hokkaido", JST, retry_count=2)

    assert provider.calls == 3
    events = unresolved_events(session)
    assert [e.category for e in events] == [EventCategory.FORECAST]
    assert _count(session) == 0


def test_fetch_and_store_stops_on_non_retryable_error(session: Session) -> None:
    provider = FakeProvider()
    provider.errors = [ForecastFetchError("unknown area", retryable=False)]

    with pytest.raises(ForecastFetchError):
        forecast_service.fetch_and_store(session, provider, "hokkaido", JST, retry_count=3)
    assert provider.calls == 1


def test_fetch_and_store_resolves_previous_errors(session: Session) -> None:
    provider = FakeProvider()
    provider.errors = [ForecastFetchError("timeout")]
    with pytest.raises(ForecastFetchError):
        forecast_service.fetch_and_store(session, provider, "hokkaido", JST)

    forecast_service.fetch_and_store(session, provider, "hokkaido", JST)

    assert unresolved_events(session) == []
    assert session.scalar(select(func.count()).select_from(SystemEvent)) == 1


# --- 請求単価 ---


def _adjustment(category: AdjustmentCategory, amount: float, start: date, end: date | None = None) -> PriceAdjustment:
    return PriceAdjustment(
        category=category, amount=amount, effective_from=start, effective_to=end, note="", updated_at=datetime.now(UTC)
    )


def test_billing_price_adds_adjustments_effective_on_the_day(session: Session) -> None:
    day = today()
    session.add_all(
        [
            _adjustment(AdjustmentCategory.SERVICE, 7.0, day - timedelta(days=30)),  # 期限なし
            _adjustment(AdjustmentCategory.TRANSMISSION, 8.24, day, day),  # 当日のみ（境界を含む）
            _adjustment(AdjustmentCategory.SUBSIDY, -4.5, day - timedelta(days=10), day + timedelta(days=10)),
            _adjustment(AdjustmentCategory.RENEWABLE_LEVY, 3.98, day + timedelta(days=1)),  # 翌日から
            _adjustment(AdjustmentCategory.OTHER, 100.0, day - timedelta(days=5), day - timedelta(days=1)),  # 終了済み
        ]
    )
    session.commit()
    forecast_service.save_result(session, _result(day_prices(day, sample_prices(10.0))), JST)

    forecast = forecast_service.get_day_forecast(session, "hokkaido", day, JST)

    assert forecast.adjustment_total == pytest.approx(7.0 + 8.24 - 4.5)
    assert forecast.slots[0].energy_price == 10.0
    assert forecast.slots[0].billing_price == pytest.approx(10.0 + 10.74)
    assert forecast.slots[0].start == datetime.combine(day, datetime.min.time(), tzinfo=JST)
    assert len(forecast.slots) == 48
    assert [slot.level for slot in forecast.slots] == sample_levels()


def test_billing_price_without_adjustments_equals_energy_price(session: Session) -> None:
    forecast_service.save_result(session, _result(day_prices(today(), sample_prices(10.0))), JST)

    forecast = forecast_service.get_day_forecast(session, "hokkaido", today(), JST)

    assert forecast.adjustments == []
    assert [s.billing_price for s in forecast.slots] == [s.energy_price for s in forecast.slots]


def test_day_forecast_is_empty_when_not_fetched(session: Session) -> None:
    forecast = forecast_service.get_day_forecast(session, "hokkaido", today(), JST)

    assert forecast.slots == []
    assert forecast.fetched_at is None
    assert not forecast_service.has_forecast(session, "hokkaido", today(), JST)


def test_other_area_is_not_mixed(session: Session) -> None:
    result = FetchResult(area="tokyo", fetched_at=datetime.now(JST), days=[day_prices(today(), sample_prices())], source="t")
    forecast_service.save_result(session, result, JST)

    assert forecast_service.get_day_forecast(session, "hokkaido", today(), JST).slots == []


# --- お知らせ検知 ---


def test_check_price_notices_records_only_relevant_new_notices(session: Session) -> None:
    provider = FakeProvider()
    provider.notices = [Notice("https://example.com/0", "【重要】料金改定のお知らせ（過去分）")]
    # 初回は既存のお知らせを確認済みとして記録するだけで、通知しない
    assert forecast_service.check_price_notices(session, provider) == 0
    assert unresolved_events(session) == []

    provider.notices = [
        Notice("https://example.com/1", "【重要】国の「電気・ガス料金支援」に関するお知らせ"),
        Notice("https://example.com/2", "【重要】託送料金変更に伴う料金改定および改定後単価のお知らせ"),
        Notice("https://example.com/3", "【重要】当社を装った不審な電話にご注意ください"),
        Notice("https://example.com/0", "【重要】料金改定のお知らせ（過去分）"),
    ]
    assert forecast_service.check_price_notices(session, provider) == 2
    assert forecast_service.check_price_notices(session, provider) == 0  # 同じお知らせは再通知しない

    events = unresolved_events(session)
    assert {e.reference for e in events} == {"https://example.com/1", "https://example.com/2"}
    assert all(e.category == EventCategory.PRICE_NOTICE for e in events)
