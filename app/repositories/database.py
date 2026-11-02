"""SQLite への接続とセッション管理。"""

from collections.abc import Iterator
from datetime import UTC, datetime

from sqlalchemy import DateTime, Dialect, TypeDecorator, create_engine, event
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import get_settings


class Base(DeclarativeBase):
    pass


class UTCDateTime(TypeDecorator[datetime]):
    """タイムゾーン付き日時を UTC に揃えて保存し、UTC として読み出す。

    SQLite はタイムゾーンを保持しないため、比較・並び替えが崩れないよう UTC に統一する。
    """

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("タイムゾーンなしの日時は保存できません")
        return value.astimezone(UTC).replace(tzinfo=None)

    def process_result_value(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        return value.replace(tzinfo=UTC) if value is not None else None


def create_db_engine(database_url: str) -> Engine:
    engine = create_engine(
        database_url,
        # FastAPI のスレッドプールとスケジューラーから同一 DB を利用するため
        connect_args={"check_same_thread": False},
        # インメモリ DB（テスト用）は接続ごとに別 DB になるため、単一接続を共有する
        poolclass=StaticPool if make_url(database_url).database in (None, "", ":memory:") else None,
    )

    @event.listens_for(engine, "connect")
    def _set_sqlite_pragma(dbapi_connection, _connection_record) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.close()

    return engine


engine = create_db_engine(get_settings().database_url)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def get_session() -> Iterator[Session]:
    """FastAPI の依存性注入用セッション。"""
    with SessionLocal() as session:
        yield session
