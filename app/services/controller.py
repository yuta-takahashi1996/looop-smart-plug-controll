"""Control Service（docs/03-architecture.md）。

自動・手動・安全制御による状態変更はすべて ControlService を経由させ、制御履歴（control_logs）に記録する。
Dry Run では SwitchBot へのコマンドを送信せず「実行するはずだった操作」を記録する。実制御は DRY_RUN=false
かつ画面で有効にした場合のみ行う（docs/14 §14.3）。PlugMonitor はプラグ状態の読み取りのみを行い、画面は
SwitchBot API を直接呼ばずに最後の確認結果を参照する（docs/05 §5.9）。BatteryMonitor はポータブル電源の
残量を取得する（docs/07 §7.7）。
"""

import logging
import threading
import time as time_module
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from app.clients.jackery import JackeryClient, JackeryError
from app.clients.switchbot import PlugStatus, SwitchBotClient, SwitchBotError
from app.config import Settings
from app.models.schedule import ControlLog, ControlResult, ControlSchedule, ControlSource, PlugState, ScheduleStatus
from app.models.settings import (
    EventCategory,
    EventLevel,
    get_value,
    load_control_settings,
    record_event,
    resolve_events,
    set_value,
)
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


class BatteryMonitor:
    """ポータブル電源（Jackery）の残量・消費電力の取得（docs/07 §7.7）。

    取得はスケジュールの開始時と OFF 中の5分ごと（ControlService が呼び出す）。OFF の間の消費電力の
    平均を予測に使うため、OFF を始めたときに samples をリセットする。
    """

    def __init__(self, client: JackeryClient | None, device_id: str, session_factory: Callable[[], Session]) -> None:
        self._client = client
        self._device_id = device_id
        self._session_factory = session_factory
        self._lock = threading.Lock()
        self.last: safety.BatteryReading | None = None
        self.last_error: str | None = None
        self.samples: list[float] = []

    @property
    def configured(self) -> bool:
        return self._client is not None and self._client.configured

    def reset_samples(self) -> None:
        """OFF を始めたときに呼ぶ。直近の取得値（OFF 開始時に取得したもの）は残す。"""
        with self._lock:
            self.samples = [self.last.output_w] if self.last is not None else []

    def average_output_w(self) -> float | None:
        with self._lock:
            return sum(self.samples) / len(self.samples) if self.samples else None

    def read(self, now: datetime) -> safety.BatteryReading | None:
        """残量と消費電力を取得する。取得できなければ None（エラーはイベントとして記録する）。"""
        if not self.configured:
            self.last_error = "Jackery のアカウントが未設定です"
            return None
        assert self._client is not None
        try:
            device_id = self._device_id or str(self._client.list_devices()[0].get("devId"))
            status = self._client.get_status(device_id)
            if status.remaining_percent is None or status.output_power_w is None:
                raise JackeryError("残量または消費電力を取得できませんでした")
        except (JackeryError, IndexError) as exc:
            message = f"ポータブル電源の状態を取得できませんでした：{exc}"
            previous, self.last_error = self.last_error, message
            logger.warning(message)
            if message != previous:
                with self._session_factory() as session:
                    record_event(session, EventLevel.ERROR, EventCategory.BATTERY, message)
            return None
        reading = safety.BatteryReading(status.remaining_percent, status.output_power_w, now)
        had_error = self.last_error is not None
        with self._lock:
            self.last, self.last_error = reading, None
            self.samples.append(status.output_power_w)
        if had_error:
            with self._session_factory() as session:
                resolve_events(session, EventCategory.BATTERY)
        return reading


class ControlMode(StrEnum):
    AUTO = "AUTO"  # スケジュールによる自動制御
    MANUAL = "MANUAL"  # 手動操作を優先（自動制御は停止。安全制御は継続）


CONTROL_MODE_KEY = "control_mode"
REAL_CONTROL_KEY = "real_control_enabled"

# コマンド送信後の状態確認（docs/08 §8.3）
VERIFY_WAIT_SECONDS = 3
VERIFY_ATTEMPTS = 5
# 送信・確認に失敗した操作は、この間は同じ要求を繰り返さない（SwitchBot API の呼び出し回数の上限のため）
RETRY_AFTER = timedelta(minutes=5)


class ControlService:
    """状態変更の唯一の経路。スケジュールの実行・手動操作・安全制御をすべてここで行い、制御履歴に記録する。

    - Dry Run：コマンドを送らず、送っていればこうなっているはずの状態（state）を想定して記録する。
    - 実制御：DRY_RUN=false かつ画面で実制御を有効にした場合のみ。送信前後に状態を取得して照合する。
    - モード：AUTO（スケジュールを実行）／MANUAL（手動操作を優先。安全制御は継続）。DB に保持し再起動後も引き継ぐ。
    state は確認できているプラグの状態。未初期化・確認できない間は None（SAFE）。
    """

    def __init__(
        self,
        settings: Settings,
        session_factory: Callable[[], Session],
        *,
        switchbot: SwitchBotClient | None = None,
        battery: BatteryMonitor | None = None,
        sleep: Callable[[float], None] = time_module.sleep,
    ) -> None:
        self._settings = settings
        self._session_factory = session_factory
        self._tz = ZoneInfo(settings.tz)
        self._lock = threading.RLock()
        self._state: PlugState | None = None
        self._switchbot = switchbot
        self.battery = battery or BatteryMonitor(None, "", session_factory)
        self._sleep = sleep
        self._control_mode = ControlMode.AUTO
        self._real_control_enabled = False
        self._manual_off_since: datetime | None = None
        self._failed: tuple[PlugState, datetime] | None = None  # 最後に失敗した要求と時刻
        self._sample_start: dict[int, int] = {}  # スケジュールごとの消費電力サンプルの開始位置（実測値の記録用）
        self._load_persisted()

    # --- 状態 ---

    def _load_persisted(self) -> None:
        try:
            with self._session_factory() as session:
                self._control_mode = ControlMode(get_value(session, CONTROL_MODE_KEY) or ControlMode.AUTO)
                self._real_control_enabled = get_value(session, REAL_CONTROL_KEY) == "true"
        except Exception:
            logger.exception("failed to load control mode; using AUTO / Dry Run")

    @property
    def dry_run(self) -> bool:
        """DRY_RUN=false かつ画面で実制御を有効にした場合のみ実制御（docs/14 §14.3）。"""
        return self._settings.dry_run or not self._real_control_enabled or self._switchbot is None

    @property
    def can_enable_real_control(self) -> bool:
        return not self._settings.dry_run and self._switchbot is not None and self._switchbot.configured

    @property
    def control_mode(self) -> ControlMode:
        return self._control_mode

    @property
    def state(self) -> PlugState | None:
        return self._state

    @property
    def mode(self) -> str:
        """docs/07 §7.3 の制御状態（SAFE / GRID / BATTERY / MANUAL）。"""
        if self._control_mode == ControlMode.MANUAL:
            return "MANUAL"
        if self._state is None:
            return "SAFE"
        return "BATTERY" if self._state == PlugState.OFF else "GRID"

    def set_real_control(self, enabled: bool) -> None:
        """実制御の有効・無効（画面での確認操作から呼ぶ）。有効にできない環境では何もしない。"""
        with self._lock, self._session_factory() as session:
            if enabled and not self.can_enable_real_control:
                raise ValueError("DRY_RUN=false かつ SwitchBot の認証情報が設定されている場合のみ実制御を有効にできます")
            set_value(session, REAL_CONTROL_KEY, "true" if enabled else "false")
            self._real_control_enabled = enabled
            # Dry Run の想定状態と実機の状態は異なるため、次のサイクルで実機の状態から初期化し直す
            self._state = None
            logger.warning("real control %s", "enabled" if enabled else "disabled")

    # --- 状態変更 ---

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
        """プラグの状態変更を要求し、制御履歴に記録する。実制御では送信後に状態を確認して照合する。"""
        with self._lock:
            executed_at = now or datetime.now(UTC)
            log = ControlLog(
                executed_at=executed_at,
                source=source,
                previous_state=self._state,
                requested_state=target,
                reason=reason,
                dry_run=self.dry_run,
                schedule_id=schedule_id,
            )
            if self.dry_run:
                log.actual_state, log.result = None, ControlResult.DRY_RUN
                self._state = target
            else:
                self._execute(log, target)
            session.add(log)
            session.commit()
            if log.result in (ControlResult.FAILED, ControlResult.UNCONFIRMED):
                self._failed = (target, executed_at)
                record_event(session, EventLevel.ERROR, EventCategory.SWITCHBOT, f"プラグを {target} にできませんでした：{log.error_message}")
            elif log.result == ControlResult.SUCCESS:
                if self._failed is not None:
                    resolve_events(session, EventCategory.SWITCHBOT)
                self._failed = None
            if target == PlugState.OFF and log.previous_state != PlugState.OFF:
                self.battery.reset_samples()
            logger.info("%s %s -> %s (%s: %s) %s", "[dry-run]" if log.dry_run else "[real]", log.previous_state, target, source, reason, log.result)
            return log

    def _execute(self, log: ControlLog, target: PlugState) -> None:
        """docs/08 §8.3：操作前の状態取得 → 必要な場合のみ送信 → 状態の再取得 → 期待状態との照合。"""
        assert self._switchbot is not None
        try:
            current = self._switchbot.get_status().power
            log.previous_state = PlugState(current)
        except SwitchBotError as exc:
            current = None
            logger.warning("failed to get plug status before command: %s", exc)
        if current == target:
            log.actual_state, log.result = target, ControlResult.SUCCESS
            self._state = target
            return

        actual: PlugState | None = None
        error: str | None = None
        for _ in range(2):  # 期待状態にならなければ1回だけ送り直す
            try:
                self._switchbot.send_power_command(target.value)  # type: ignore[arg-type]
            except SwitchBotError as exc:
                error = str(exc)
                continue
            for _ in range(VERIFY_ATTEMPTS):
                self._sleep(VERIFY_WAIT_SECONDS)
                try:
                    actual = PlugState(self._switchbot.get_status().power)
                except SwitchBotError as exc:
                    error, actual = str(exc), None
                    continue
                if actual == target:
                    log.actual_state, log.result = actual, ControlResult.SUCCESS
                    self._state = target
                    return
            error = error or f"送信後の状態が {actual} のままです"
        log.actual_state = actual
        log.error_message = error
        # 照合できなければ成功扱いにしない（docs/14 §14.2）
        log.result = ControlResult.FAILED if actual is not None else ControlResult.UNCONFIRMED
        self._state = actual

    def _should_retry(self, target: PlugState, now: datetime) -> bool:
        return self._failed is None or self._failed[0] != target or now - self._failed[1] >= RETRY_AFTER

    def initialize(self, now: datetime | None = None) -> None:
        """起動時に安全側（ON）へ初期化する（docs/09 §9.2）。失敗しても Web UI は起動させる。"""
        try:
            with self._session_factory() as session:
                self.request(session, PlugState.ON, ControlSource.SAFETY, "起動時の安全側への初期化", now=now)
        except Exception:
            logger.exception("failed to initialize control state; will retry in the next control cycle")

    def shutdown(self, now: datetime | None = None) -> None:
        """終了時に ON へ戻す（docs/09 §9.3）。"""
        with self._lock, self._session_factory() as session:
            if self._state != PlugState.ON:
                self.request(session, PlugState.ON, ControlSource.SAFETY, "終了時の ON 復帰", now=now)
            local_now = self._local(now)
            self._abort_running(session, local_now, "システムの終了")

    # --- 手動操作（docs/05 §5.8） ---

    def manual_power(self, target: PlugState, now: datetime | None = None) -> ControlLog:
        """手動でプラグを ON/OFF する。MANUAL モードへ移行し、自動制御で上書きしない。"""
        local_now = self._local(now)
        with self._lock, self._session_factory() as session:
            self._set_mode(session, ControlMode.MANUAL)
            self._abort_running(session, local_now, "手動操作")
            if target == PlugState.OFF:
                decision = self._check_manual_off(session, local_now, starting=True)
                if not decision.allowed:
                    raise ValueError(decision.reason)
            log = self.request(session, target, ControlSource.MANUAL, f"手動操作（{target}）", now=local_now)
            self._manual_off_since = local_now if self._state == PlugState.OFF else None
            return log

    def stop_auto(self, now: datetime | None = None) -> None:
        """自動制御を停止する（プラグの状態は変えない）。"""
        local_now = self._local(now)
        with self._lock, self._session_factory() as session:
            self._set_mode(session, ControlMode.MANUAL)
            self._abort_running(session, local_now, "自動制御の停止")
            self._manual_off_since = local_now if self._state == PlugState.OFF else None

    def start_auto(self) -> None:
        """自動制御を再開する（MANUAL → SAFE。次の制御サイクルでスケジュールに従う）。"""
        with self._lock, self._session_factory() as session:
            self._set_mode(session, ControlMode.AUTO)
            self._manual_off_since = None

    def _set_mode(self, session: Session, mode: ControlMode) -> None:
        set_value(session, CONTROL_MODE_KEY, mode.value)
        self._control_mode = mode

    def _abort_running(self, session: Session, now: datetime, reason: str) -> None:
        for day in (now.date() - timedelta(days=1), now.date()):
            for s in optimizer.schedules_for(session, day):
                if s.status == ScheduleStatus.RUNNING:
                    self._finish(session, s, min(now, s.end_at), ScheduleStatus.ABORTED, reason)

    def _check_manual_off(self, session: Session, now: datetime, *, starting: bool) -> safety.OffDecision:
        """手動 OFF の安全判定：残量で判定し、取得できない間は最大連続OFF時間で ON に戻す。"""
        control = load_control_settings(session, self._settings)
        reading = self._battery_reading(now, force=starting)
        if safety.battery_known(reading, now):
            assert reading is not None
            return safety.check_battery(reading, now, control, average_output_w=self.battery.average_output_w(), end_at=None)
        if self._manual_off_since is not None and not starting:
            minutes = (now - self._manual_off_since).total_seconds() // 60
            if minutes >= control.max_continuous_off_minutes:
                return safety.OffDecision(
                    False, f"残量を取得できないため、最大連続OFF時間（{control.max_continuous_off_minutes}分）で ON に戻します"
                )
        return safety.OffDecision(True)

    def _battery_reading(self, now: datetime, *, force: bool) -> safety.BatteryReading | None:
        """残量を取得する（OFF 開始時は必ず、OFF 中は5分ごと）。取得しない場合は直近の値を返す。"""
        last = self.battery.last
        if force or last is None or now - last.at >= safety.BATTERY_CHECK_INTERVAL:
            return self.battery.read(now) or last
        return last

    # --- 制御サイクル（docs/07 §7.8） ---

    def _local(self, now: datetime | None) -> datetime:
        return (now or datetime.now(UTC)).astimezone(self._tz)

    def run_cycle(self, now: datetime | None = None) -> None:
        """毎分実行する。AUTO では有効なスケジュールの間は OFF、それ以外は ON。MANUAL では安全制御のみ。"""
        now = self._local(now)
        with self._lock, self._session_factory() as session:
            if self._state is None and self._should_retry(PlugState.ON, now):
                # 未初期化・状態不明は安全側へ（MANUAL でも ON に戻す。実制御では実機の状態を取得して照合する）
                self.request(session, PlugState.ON, ControlSource.SAFETY, "安全側への初期化", now=now)
            if self._state is None:
                # ON を確認できない間は OFF を要求しない（終了したスケジュールの後処理のみ行う）
                self._close_ended(session, now, optimizer.schedules_for(session, now.date()))
                return

            schedules = optimizer.schedules_for(session, now.date())
            self._close_ended(session, now, schedules)

            if self._control_mode == ControlMode.MANUAL:
                self._run_manual_safety(session, now)
                return

            active = next(
                (
                    s
                    for s in schedules
                    if s.enabled and s.start_at <= now < s.end_at and s.status in (ScheduleStatus.PENDING, ScheduleStatus.RUNNING)
                ),
                None,
            )
            if active is None:
                if self._state == PlugState.OFF and self._should_retry(PlugState.ON, now):
                    self.request(session, PlugState.ON, ControlSource.AUTO, "スケジュール終了", now=now)
                return
            if active.target_state == PlugState.ON:
                self._run_on_schedule(session, active, now)
                return

            decision = self._check_schedule(session, active, schedules, now)
            if decision.allowed:
                if active.status == ScheduleStatus.PENDING:
                    active.status = ScheduleStatus.RUNNING
                    active.executed_start_at = now
                    reading = self.battery.last
                    active.battery_start_percent = reading.percent if safety.battery_known(reading, now) else None  # type: ignore[union-attr]
                    session.commit()
                if self._state != PlugState.OFF and self._should_retry(PlugState.OFF, now):
                    self.request(session, PlugState.OFF, ControlSource.AUTO, active.reason, schedule_id=active.id, now=now)
                self._sample_start.setdefault(active.id, max(0, len(self.battery.samples) - 1))
                return
            # 安全制御で開始・継続できない
            if self._state != PlugState.ON and self._should_retry(PlugState.ON, now):
                self.request(session, PlugState.ON, ControlSource.SAFETY, decision.reason or "", schedule_id=active.id, now=now)
            if active.status == ScheduleStatus.RUNNING:
                self._finish(session, active, now, ScheduleStatus.ABORTED, decision.reason)
            else:
                active.status = ScheduleStatus.ABORTED
                active.status_reason = decision.reason
                session.commit()

    def _run_on_schedule(self, session: Session, schedule: ControlSchedule, now: datetime) -> None:
        """任意 ON のスケジュール（docs/07 §7.9）：OFF を要求せず、OFF 中なら ON に戻す。安全側のため判定は不要。"""
        if schedule.status == ScheduleStatus.PENDING:
            schedule.status = ScheduleStatus.RUNNING
            session.commit()
        if self._state != PlugState.ON and self._should_retry(PlugState.ON, now):
            self.request(session, PlugState.ON, ControlSource.AUTO, schedule.reason, schedule_id=schedule.id, now=now)

    def _check_schedule(
        self, session: Session, schedule: ControlSchedule, schedules: list[ControlSchedule], now: datetime
    ) -> safety.OffDecision:
        control = load_control_settings(session, self._settings)
        starting = self._state != PlugState.OFF
        reading = self._battery_reading(now, force=starting)
        known = safety.battery_known(reading, now)
        decision = safety.check_off(
            schedule,
            now,
            control,
            has_forecast=forecast_service.has_forecast(session, self._settings.looop_area, now.date(), self._tz),
            day_off_minutes=safety.off_minutes(schedules, now),
            battery_known=known,
        )
        if decision.allowed and known:
            assert reading is not None
            decision = safety.check_battery(
                reading, now, control, average_output_w=self.battery.average_output_w(), end_at=schedule.end_at
            )
        return decision

    def _run_manual_safety(self, session: Session, now: datetime) -> None:
        """MANUAL モードでも安全制御は継続する（docs/05 §5.8）。"""
        if self._state != PlugState.OFF:
            return
        decision = self._check_manual_off(session, now, starting=False)
        if not decision.allowed and self._should_retry(PlugState.ON, now):
            self.request(session, PlugState.ON, ControlSource.SAFETY, decision.reason or "", now=now)
            self._manual_off_since = None

    def _close_ended(self, session: Session, now: datetime, schedules: list[ControlSchedule]) -> None:
        """終了時刻を過ぎたスケジュールの後処理（前日の 24:00 まで実行したものを含む）。"""
        for s in optimizer.schedules_for(session, now.date() - timedelta(days=1)) + schedules:
            if s.end_at <= now and s.status in (ScheduleStatus.PENDING, ScheduleStatus.RUNNING):
                if s.status == ScheduleStatus.RUNNING:
                    self._finish(session, s, s.end_at, ScheduleStatus.DONE, None)
                else:
                    s.status = ScheduleStatus.SKIPPED
                    if not s.enabled:
                        s.status_reason = "無効のため実行しませんでした"
                    elif self._control_mode == ControlMode.MANUAL:
                        s.status_reason = "自動制御を停止していました"
                    else:
                        s.status_reason = "開始時刻にシステムが停止していました"
        session.commit()

    def _finish(
        self, session: Session, schedule: ControlSchedule, end: datetime, status: ScheduleStatus, reason: str | None
    ) -> None:
        """実行中のスケジュールを終了し、実行した時間帯の推定節約額と実測値を記録する。"""
        schedule.status = status
        schedule.status_reason = reason
        if schedule.target_state == PlugState.ON:
            session.commit()  # 任意 ON は OFF 時間・節約額・実測値の記録の対象外
            return
        schedule.executed_end_at = end
        self._record_measurements(schedule)
        forecast = forecast_service.get_day_forecast(session, self._settings.looop_area, schedule.schedule_date, self._tz)
        if forecast.slots and schedule.executed_start_at is not None:
            params = optimizer.PlanParams.from_settings(load_control_settings(session, self._settings))
            schedule.executed_saving = optimizer.saving_between(forecast.slots, schedule.executed_start_at, end, params)
        session.commit()


    def _record_measurements(self, schedule: ControlSchedule) -> None:
        """終了時の残量と、そのスケジュールの間の平均消費電力を記録する（取得できた場合のみ）。"""
        start = self._sample_start.pop(schedule.id, None)
        if not self.battery.configured or schedule.executed_start_at is None:
            return
        reading = self.battery.read(datetime.now(UTC))
        if reading is not None:
            schedule.battery_end_percent = reading.percent
        if start is not None:
            samples = self.battery.samples[start:]
            if samples:
                schedule.measured_power_w = round(sum(samples) / len(samples), 1)
