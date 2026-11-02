import os
from collections.abc import Iterator

import pytest

# app モジュールの import 前に設定し、テストが data/app.db に触れないようにする
os.environ["DATABASE_URL"] = "sqlite://"
os.environ["DRY_RUN"] = "true"
os.environ["APP_ENV"] = "test"  # スケジューラーを起動しない
os.environ["SWITCHBOT_TOKEN"] = ""
os.environ["SWITCHBOT_SECRET"] = ""
os.environ["SWITCHBOT_DEVICE_ID"] = ""
os.environ["WEB_USERNAME"] = "admin"
# パスワード "test-password" の scrypt ハッシュ（固定値。テストのたびに計算しない）
os.environ["WEB_PASSWORD_HASH"] = (
    "scrypt$131072$8$1$v7kLIlbz9z/jD1r9Rznt4w==$yJUO0ukxkZy8wJ+cJPn4Su70/Cds2R+VZ65gYOMElOhfuqoNLaAB6/Vk5XiIOfKRz06WT9W6eh3ZNTjKkhRgPw=="
)
os.environ["SESSION_SECRET"] = "test-session-secret"
os.environ["ALLOWED_NETWORKS"] = "127.0.0.1/32"

from datetime import date, datetime, timedelta  # noqa: E402
from zoneinfo import ZoneInfo  # noqa: E402

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.main import app  # noqa: E402
from app.providers.base import DayPrices, FetchResult, ForecastFetchError, Notice  # noqa: E402
from app.repositories.database import Base, SessionLocal, engine  # noqa: E402

TEST_PASSWORD = "test-password"
ALLOWED_CLIENT = ("127.0.0.1", 50000)
JST = ZoneInfo("Asia/Tokyo")


def sample_prices(base: float = 10.0) -> list[float]:
    """0:00 から順に 0.5 円ずつ上がる 48 コマの単価。"""
    return [round(base + index * 0.5, 2) for index in range(48)]


class FakeProvider:
    """Looop への通信を行わない ForecastProvider。"""

    def __init__(self) -> None:
        self.calls = 0
        self.errors: list[ForecastFetchError] = []  # 先頭から順に送出する
        self.days: list[DayPrices] | None = None  # None なら今日・明日の sample_prices
        self.notices: list[Notice] = []

    def fetch(self, area: str) -> FetchResult:
        self.calls += 1
        if self.errors:
            raise self.errors.pop(0)
        now = datetime.now(JST)
        days = self.days
        if days is None:
            days = [
                DayPrices(now.date(), sample_prices(10.0)),
                DayPrices(now.date() + timedelta(days=1), sample_prices(20.0)),
            ]
        return FetchResult(area=area, fetched_at=now, days=days, source="fake")

    def fetch_notices(self) -> list[Notice]:
        return list(self.notices)


def today() -> date:
    return datetime.now(JST).date()


@pytest.fixture(autouse=True)
def database() -> Iterator[None]:
    Base.metadata.create_all(engine)
    yield
    Base.metadata.drop_all(engine)


@pytest.fixture
def session() -> Iterator[Session]:
    with SessionLocal() as db_session:
        yield db_session


@pytest.fixture
def provider() -> FakeProvider:
    return FakeProvider()


@pytest.fixture
def client(provider: FakeProvider) -> Iterator[TestClient]:
    with TestClient(app, client=ALLOWED_CLIENT) as test_client:
        app.state.forecast_provider = provider
        yield test_client


def csrf_token(client: TestClient) -> str:
    response = client.get("/login")
    marker = 'name="csrf_token" value="'
    start = response.text.index(marker) + len(marker)
    return response.text[start : response.text.index('"', start)]


@pytest.fixture
def logged_in_client(client: TestClient) -> TestClient:
    response = client.post(
        "/login",
        data={"username": "admin", "password": TEST_PASSWORD, "next": "/", "csrf_token": csrf_token(client)},
        follow_redirects=False,
    )
    assert response.status_code == 303
    return client
