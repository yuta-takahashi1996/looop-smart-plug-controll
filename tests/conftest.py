import os
from collections.abc import Iterator

import pytest

# app モジュールの import 前に設定し、テストが data/app.db に触れないようにする
os.environ["DATABASE_URL"] = "sqlite://"
os.environ["DRY_RUN"] = "true"
os.environ["WEB_USERNAME"] = "admin"
# パスワード "test-password" の scrypt ハッシュ（固定値。テストのたびに計算しない）
os.environ["WEB_PASSWORD_HASH"] = (
    "scrypt$131072$8$1$v7kLIlbz9z/jD1r9Rznt4w==$yJUO0ukxkZy8wJ+cJPn4Su70/Cds2R+VZ65gYOMElOhfuqoNLaAB6/Vk5XiIOfKRz06WT9W6eh3ZNTjKkhRgPw=="
)
os.environ["SESSION_SECRET"] = "test-session-secret"
os.environ["ALLOWED_NETWORKS"] = "127.0.0.1/32"

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402
from app.repositories.database import Base, engine  # noqa: E402

TEST_PASSWORD = "test-password"
ALLOWED_CLIENT = ("127.0.0.1", 50000)


@pytest.fixture(autouse=True)
def database() -> Iterator[None]:
    Base.metadata.create_all(engine)
    yield
    Base.metadata.drop_all(engine)


@pytest.fixture
def client() -> Iterator[TestClient]:
    with TestClient(app, client=ALLOWED_CLIENT) as test_client:
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
