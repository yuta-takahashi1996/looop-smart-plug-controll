"""Control Service（docs/03-architecture.md）。

自動・手動・安全制御による状態変更はすべて ControlService を経由させ、制御履歴（control_logs）に記録する。
現在は Dry Run のみ対応しており、SwitchBot へのコマンドは送信せず「実行するはずだった操作」を記録する
（実制御は Phase 6）。PlugMonitor はプラグ状態の読み取りのみを行い、画面は SwitchBot API を直接呼ばずに
最後の確認結果を参照する（docs/05 §5.9）。
"""

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from app.clients.switchbot import PlugStatus, SwitchBotClient, SwitchBotError
from app.config import Settings
from app.models.schedule import ControlLog, ControlResult, ControlSchedule, ControlSource, PlugState, ScheduleStatus
from app.models.settings import EventCategory, EventLevel, load_control_settings, record_event, resolve_events
from app.services import forecast as forecast_service
from app.services import optimizer, safety

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


class ControlService:
    """状態変更の唯一の経路。スケジュールの実行（run_cycle）も、起動・終了時の安全制御もここで行う。

    state は Control Service が想定しているプラグの状態。Dry Run ではコマンドを送らないため、
    「送っていれば今こうなっているはず」の状態を表す。未初期化（SAFE）の間は None。
    """

    def __init__(self, settings: Settings, session_factory: Callable[[], Session]) -> None:
        self._settings = settings
        self._session_factory = session_factory
        self._tz = ZoneInfo(settings.tz)
        self._lock = threading.RLock()
        self._state: PlugState | None = None

    @property
    def dry_run(self) -> bool:
        # 実制御（SwitchBot へのコマンド送信）は Phase 6 で実装する。それまでは設定によらず Dry Run とする
        return True

    @property
    def state(self) -> PlugState | None:
        return self._state

    @property
    def mode(self) -> str:
        """docs/07 §7.3 の制御状態（SAFE / GRID / BATTERY）。MANUAL は Phase 6。"""
        if self._state is None:
            return "SAFE"
        return "BATTERY" if self._state == PlugState.OFF else "GRID"

    def request(
        self,
        session: Session,
        target: PlugState,
        source: ControlSource,
        reason: str,
        *,
        schedule_id: int | None = None,
        now: datetime | None = None,
    ) -> ControlLog:
        """プラグの状態変更を要求し、制御履歴に記録する。"""
        with self._lock:
            log = ControlLog(
                executed_at=now or datetime.now(UTC),
                source=source,
                previous_state=self._state,
                requested_state=target,
                actual_state=None,
                result=ControlResult.DRY_RUN,
                reason=reason,
                dry_run=self.dry_run,
                schedule_id=schedule_id,
            )
            session.add(log)
            session.commit()
            self._state = target
            logger.info("[dry-run] %s -> %s (%s: %s)", log.previous_state, target, source, reason)
            return log

    def initialize(self, now: datetime | None = None) -> None:
        """起動時に安全側（ON）へ初期化する（docs/09 §9.2）。

        失敗しても Web UI は起動させる（状態は SAFE のまま。次の制御サイクルで初期化をやり直す）。
        """
        if not self._settings.dry_run:
            logger.warning("DRY_RUN=false ですが、実制御は未実装のため Dry Run で動作します")
        try:
            with self._session_factory() as session:
                self.request(session, PlugState.ON, ControlSource.SAFETY, "起動時の安全側への初期化", now=now)
        except Exception:
            logger.exception("failed to initialize control state; will retry in the next control cycle")

    def shutdown(self, now: datetime | None = None) -> None:
        """終了時に ON へ戻す（docs/09 §9.3）。"""
        with self._session_factory() as session:
            if self._state != PlugState.ON:
                self.request(session, PlugState.ON, ControlSource.SAFETY, "終了時の ON 復帰", now=now)
            now = self._session_now(now)
            for day in (now.date() - timedelta(days=1), now.date()):
                for s in optimizer.schedules_for(session, day):
                    if s.status == ScheduleStatus.RUNNING:
                        self._finish(session, s, min(now, s.end_at), ScheduleStatus.ABORTED, "システムの終了")

    def _session_now(self, now: datetime | None) -> datetime:
        return (now or datetime.now(UTC)).astimezone(self._tz)

    def run_cycle(self, now: datetime | None = None) -> None:
        """スケジュールを実行する（1分ごと）。有効なスケジュールの間は OFF、それ以外は ON にする。"""
        now = self._session_now(now)
        with self._lock, self._session_factory() as session:
            if self._state is None:
                self.request(session, PlugState.ON, ControlSource.SAFETY, "起動時の安全側への初期化", now=now)

            schedules = optimizer.schedules_for(session, now.date())
            # 終了時刻を過ぎたスケジュールの後処理（前日の 24:00 まで実行したものを含む。ON への切り替えは下で判断する）
            for s in optimizer.schedules_for(session, now.date() - timedelta(days=1)) + schedules:
                if s.end_at <= now and s.status in (ScheduleStatus.PENDING, ScheduleStatus.RUNNING):
                    if s.status == ScheduleStatus.RUNNING:
                        self._finish(session, s, s.end_at, ScheduleStatus.DONE, None)
                    else:
                        s.status = ScheduleStatus.SKIPPED
                        s.status_reason = "無効のため実行しませんでした" if not s.enabled else "開始時刻にシステムが停止していました"
            session.commit()

            active = next(
                (
                    s
                    for s in schedules
                    if s.enabled and s.start_at <= now < s.end_at and s.status in (ScheduleStatus.PENDING, ScheduleStatus.RUNNING)
                ),
                None,
            )
            if active is not None:
                decision = safety.check_off(
                    active,
                    now,
                    load_control_settings(session, self._settings),
                    has_forecast=forecast_service.has_forecast(session, self._settings.looop_area, now.date(), self._tz),
                    day_off_minutes=safety.off_minutes(schedules, now),
                )
                if decision.allowed:
                    if active.status == ScheduleStatus.PENDING:
                        active.status = ScheduleStatus.RUNNING
                        active.executed_start_at = now
                        session.commit()
                    if self._state != PlugState.OFF:
                        self.request(session, PlugState.OFF, ControlSource.AUTO, active.reason, schedule_id=active.id, now=now)
                    return
                # 安全制御で開始・継続できない
                if self._state == PlugState.OFF:
                    self.request(session, PlugState.ON, ControlSource.SAFETY, decision.reason or "", schedule_id=active.id, now=now)
                if active.status == ScheduleStatus.RUNNING:
                    self._finish(session, active, now, ScheduleStatus.ABORTED, decision.reason)
                else:
                    active.status = ScheduleStatus.ABORTED
                    active.status_reason = decision.reason
                    session.commit()
                return

            if self._state == PlugState.OFF:
                self.request(session, PlugState.ON, ControlSource.AUTO, "スケジュール終了", now=now)

    def _finish(
        self, session: Session, schedule: ControlSchedule, end: datetime, status: ScheduleStatus, reason: str | None
    ) -> None:
        """実行中のスケジュールを終了し、実行した時間帯の推定節約額を記録する。"""
        schedule.executed_end_at = end
        schedule.status = status
        schedule.status_reason = reason
        forecast = forecast_service.get_day_forecast(session, self._settings.looop_area, schedule.schedule_date, self._tz)
        if forecast.slots and schedule.executed_start_at is not None:
            params = optimizer.PlanParams.from_settings(load_control_settings(session, self._settings))
            schedule.executed_saving = optimizer.saving_between(forecast.slots, schedule.executed_start_at, end, params)
        session.commit()
