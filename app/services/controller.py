"""Control Service（docs/03-architecture.md）。

自動・手動・安全制御による状態変更はすべてここを経由させる（Phase 6 で実装）。
現時点ではプラグ状態の監視（読み取り）のみを提供し、SwitchBot へのコマンドは送信しない。
画面は SwitchBot API を直接呼ばず、ここで保持している最新の確認結果を参照する（docs/05 §5.9）。
"""

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from app.clients.switchbot import PlugStatus, SwitchBotClient, SwitchBotError
from app.models.settings import EventCategory, EventLevel, record_event, resolve_events

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PlugSnapshot:
    """最後に確認したプラグの状態。status が None なら未確認（取得失敗または未設定）。"""

    status: PlugStatus | None
    checked_at: datetime | None
    error: str | None


class PlugMonitor:
    def __init__(self, client: SwitchBotClient, session_factory: Callable[[], Session]) -> None:
        self._client = client
        self._session_factory = session_factory
        self._lock = threading.Lock()
        self._snapshot = PlugSnapshot(status=None, checked_at=None, error=None)

    @property
    def configured(self) -> bool:
        return self._client.configured

    def snapshot(self) -> PlugSnapshot:
        with self._lock:
            return self._snapshot

    def refresh(self) -> PlugSnapshot:
        """SwitchBot から状態を取得して保持する。失敗時はイベントを記録する（同じエラーの連続記録はしない）。"""
        if not self._client.configured:
            snapshot = PlugSnapshot(
                status=None, checked_at=None, error="SwitchBot の認証情報またはデバイスIDが未設定です"
            )
            with self._lock:
                self._snapshot = snapshot
            return snapshot

        now = datetime.now(UTC)
        try:
            status = self._client.get_status()
        except SwitchBotError as exc:
            message = str(exc)
            with self._lock:
                previous_error = self._snapshot.error
                # 直前に確認できていた状態は保持しない（古い状態を現在の状態として表示しないため）
                self._snapshot = PlugSnapshot(status=None, checked_at=now, error=message)
            logger.warning("plug status check failed: %s", message)
            if message != previous_error:
                with self._session_factory() as session:
                    record_event(session, EventLevel.ERROR, EventCategory.SWITCHBOT, message)
            return self.snapshot()

        with self._lock:
            # 初回成功時も解決する（再起動前に記録された未解決エラーを残さないため）
            needs_resolve = self._snapshot.error is not None or self._snapshot.checked_at is None
            self._snapshot = PlugSnapshot(status=status, checked_at=now, error=None)
        if needs_resolve:
            with self._session_factory() as session:
                resolve_events(session, EventCategory.SWITCHBOT)
        return self.snapshot()
