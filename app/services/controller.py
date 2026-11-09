"""Control Service（docs/03-architecture.md）。

自動・手動・安全制御による状態変更はすべて ControlService を経由させ、制御履歴（control_logs）に記録する。
Dry Run では SwitchBot へのコマンドを送信せず「実行するはずだった操作」を記録する。実制御は DRY_RUN=false
かつ画面で有効にした場合のみ行う（docs/14 §14.3）。PlugMonitor は制御機器の状態の読み取りのみを行い、画面は
SwitchBot API を直接呼ばずに最後の確認結果を参照する（docs/05 §5.9）。BatteryMonitor はポータブル電源の
残量の取得と充電モードの変更を行う（docs/07 §7.7・§7.10）。

複数機器の制御（docs/17 §17.7）：制御対象（devices の active=true の制御機器）ごとに状態（GRID / BATTERY / SAFE）を持ち、
制御サイクルの中で機器ごとに要求する状態を決める。安全判定は機器ごとに給電先のポータブル電源の残量で行い、送信と照合は
機器ごとに並行して行う（同時に最大5台）。ある機器の失敗で他の機器の制御は止めない。自動制御の停止・再開（AUTO／MANUAL）は
システム全体で1つ。
"""

import logging
import threading
import time as time_module
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from app.clients.jackery import ChargeMode, JackeryClient, JackeryCommandUnconfirmed, JackeryError
from app.clients.switchbot import PlugStatus, SwitchBotClient, SwitchBotError
from app.config import Settings
from app.models import device as device_models
from app.models.device import Device, DeviceState
from app.models.schedule import (
    ChargeModeOption,
    ControlLog,
    ControlResult,
    ControlSchedule,
    ControlSource,
    PlugState,
    ScheduleDevice,
    ScheduleDeviceStatus,
    ScheduleStatus,
)
from app.models.settings import (
    ControlSettings,
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


def _credentials_configured(client: object | None) -> bool:
    if client is None:
        return False
    return bool(getattr(client, "credentials_configured", getattr(client, "configured", False)))


# --- 制御機器の状態の確認 ---


@dataclass(frozen=True)
class PlugSnapshot:
    """最後に確認した制御機器の状態。status が None なら未確認（取得失敗または未設定）。"""

    status: PlugStatus | None
    checked_at: datetime | None
    error: str | None


EMPTY_SNAPSHOT = PlugSnapshot(status=None, checked_at=None, error=None)


class PlugMonitor:
    """制御対象の機器ごとの状態の確認（5分ごと・画面からの手動取得。docs/09 §9.4）。読み取りのみ。"""

    def __init__(self, client: SwitchBotClient, session_factory: Callable[[], Session]) -> None:
        self._client = client
        self._session_factory = session_factory
        self._lock = threading.Lock()
        self._snapshots: dict[int, PlugSnapshot] = {}

    @property
    def configured(self) -> bool:
        return _credentials_configured(self._client)

    def snapshot(self, device_id: int) -> PlugSnapshot:
        with self._lock:
            return self._snapshots.get(device_id, EMPTY_SNAPSHOT)

    def reset(self, device_id: int | None = None) -> None:
        """機器を制御対象から外したときなどに、前の状態を表示しないよう未確認に戻す。"""
        with self._lock:
            if device_id is None:
                self._snapshots.clear()
            else:
                self._snapshots.pop(device_id, None)

    def refresh(self) -> dict[int, PlugSnapshot]:
        """制御対象のすべての機器の状態を取得する。"""
        with self._session_factory() as session:
            devices = [(d.id, d.name, d.external_id) for d in device_models.active_switches(session)]
        return {device_id: self.refresh_device(device_id, name, external_id) for device_id, name, external_id in devices}

    def refresh_device(self, device_id: int, name: str, external_id: str) -> PlugSnapshot:
        """1台の状態を取得して保持する。失敗時はイベントを記録する（同じエラーの連続記録はしない）。"""
        if not self.configured:
            snapshot = PlugSnapshot(status=None, checked_at=None, error="SwitchBot の認証情報が未設定です")
            with self._lock:
                self._snapshots[device_id] = snapshot
            return snapshot

        now = datetime.now(UTC)
        try:
            status = self._client.get_status(external_id)
        except SwitchBotError as exc:
            message = str(exc)
            with self._lock:
                previous_error = self._snapshots.get(device_id, EMPTY_SNAPSHOT).error
                # 直前に確認できていた状態は保持しない（古い状態を現在の状態として表示しないため）
                self._snapshots[device_id] = PlugSnapshot(status=None, checked_at=now, error=message)
            logger.warning("status check failed (%s): %s", name, message)
            self._save_device_state(device_id, DeviceState.UNKNOWN, now)
            if message != previous_error:
                with self._session_factory() as session:
                    record_event(session, EventLevel.ERROR, EventCategory.SWITCHBOT, f"{name}：{message}", device_id=device_id)
            return self.snapshot(device_id)

        with self._lock:
            previous = self._snapshots.get(device_id, EMPTY_SNAPSHOT)
            # 初回成功時も解決する（再起動前に記録された未解決エラーを残さないため）
            needs_resolve = previous.error is not None or previous.checked_at is None
            self._snapshots[device_id] = PlugSnapshot(status=status, checked_at=now, error=None)
        self._save_device_state(device_id, DeviceState(status.power), now)
        if needs_resolve:
            with self._session_factory() as session:
                resolve_events(session, EventCategory.SWITCHBOT, device_id=device_id)
        return self.snapshot(device_id)

    def _save_device_state(self, device_id: int, state: DeviceState, at: datetime) -> None:
        """devices.last_state に最後に取得した状態を残す。"""
        try:
            with self._session_factory() as session:
                device = session.get(Device, device_id)
                if device is not None:
                    device.last_state, device.last_state_at = state, at
                    session.commit()
        except Exception:
            logger.exception("failed to save device state")


# --- ポータブル電源 ---


class BatteryMonitor:
    """ポータブル電源（Jackery）1台の残量・消費電力の取得と充電モードの変更（docs/07 §7.7・§7.10）。

    取得はスケジュールの開始時と OFF 中の5分ごと（ControlService が呼び出す）。OFF の間の消費電力の
    平均を予測に使うため、紐づく機器が OFF を始めたときに samples をリセットする。
    """

    def __init__(
        self,
        client: JackeryClient | None,
        device_id: str | None,
        session_factory: Callable[[], Session],
        *,
        owner_id: int | None = None,
        name: str | None = None,
    ) -> None:
        self._client = client
        # 取得するポータブル電源の機器 ID。空文字は共有されている1台（機器の登録前の動作）、None は未設定
        self._device_id = device_id
        self._session_factory = session_factory
        self.owner_id = owner_id  # devices.id（イベントの関連付け用）
        self.name = name
        self._lock = threading.Lock()
        self.last: safety.BatteryReading | None = None
        self.last_error: str | None = None
        self.samples: list[float] = []
        # 最後に確認した充電モード（cs が高速充電・静かな充電以外なら None）と確認時刻
        self.charge_mode: ChargeMode | None = None
        self.charge_mode_at: datetime | None = None

    @property
    def configured(self) -> bool:
        return self._client is not None and self._client.configured and self._device_id is not None

    @property
    def device_id(self) -> str | None:
        return self._device_id

    def reset_samples(self) -> None:
        """紐づく機器が OFF を始めたときに呼ぶ。直近の取得値（OFF 開始時に取得したもの）は残す。"""
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
            status = self._client.get_status(self._resolve_device_id())
            if status.remaining_percent is None or status.output_power_w is None:
                raise JackeryError("残量または消費電力を取得できませんでした")
        except (JackeryError, IndexError) as exc:
            prefix = f"{self.name}：" if self.name else ""
            message = f"{prefix}ポータブル電源の状態を取得できませんでした：{exc}"
            previous, self.last_error = self.last_error, message
            logger.warning(message)
            if message != previous:
                with self._session_factory() as session:
                    record_event(session, EventLevel.ERROR, EventCategory.BATTERY, message, device_id=self.owner_id)
            return None
        reading = safety.BatteryReading(status.remaining_percent, status.output_power_w, now)
        had_error = self.last_error is not None
        with self._lock:
            self.last, self.last_error = reading, None
            self.samples.append(status.output_power_w)
            self.charge_mode, self.charge_mode_at = status.charge_mode, now
        if had_error:
            with self._session_factory() as session:
                resolve_events(session, EventCategory.BATTERY, device_id=self.owner_id)
        return reading

    def _resolve_device_id(self) -> str:
        assert self._client is not None and self._device_id is not None
        if self._device_id:
            return self._device_id
        devices = self._client.list_devices()
        if not devices:
            raise JackeryError("共有されたポータブル電源が見つかりません")
        return str(devices[0].get("devId"))

    def set_charge_mode(self, mode: ChargeMode, now: datetime) -> None:
        """充電モードを変更する（照合できなければ JackeryError）。呼び出しは ControlService から。"""
        if not self.configured:
            raise JackeryError("Jackery のアカウントが未設定です")
        assert self._client is not None
        status = self._client.set_charge_mode(self._resolve_device_id(), mode)
        with self._lock:
            self.charge_mode, self.charge_mode_at = status.charge_mode, now


@dataclass(frozen=True)
class ChargeModeChange:
    """充電モードの変更の結果（画面表示用。履歴は system_events に残す）。"""

    mode: ChargeMode
    source: ControlSource
    reason: str
    result: ControlResult
    at: datetime
    error: str | None = None


@dataclass
class PowerSource:
    """給電先のポータブル電源（devices.id ごと）。"""

    id: int
    name: str
    monitor: BatteryMonitor
    charge_mode_enabled: bool = True


# --- 制御 ---


class ControlMode(StrEnum):
    AUTO = "AUTO"  # スケジュールによる自動制御
    MANUAL = "MANUAL"  # 手動操作を優先（自動制御は停止。安全制御は継続）


@dataclass
class DeviceRuntime:
    """制御機器ごとの制御状態。state は確認できている状態（未初期化・確認できない間は None = SAFE）。"""

    id: int
    name: str
    external_id: str
    power_source_id: int | None
    state: PlugState | None = None
    failed: tuple[PlugState, datetime] | None = None  # 最後に失敗した要求と時刻
    startup_kept: bool = False  # 起動時に取得した状態を引き継いだ（最初のサイクルの記録の理由に使う）
    manual_off_since: datetime | None = None

    @property
    def mode(self) -> str:
        if self.state is None:
            return "SAFE"
        return "BATTERY" if self.state == PlugState.OFF else "GRID"


@dataclass
class _Request:
    device: DeviceRuntime
    target: PlugState
    source: ControlSource
    reason: str
    schedule_id: int | None = None


CONTROL_MODE_KEY = "control_mode"
REAL_CONTROL_KEY = "real_control_enabled"
# 充電モードの切り替えの対象として扱っている実行中のスケジュールの ID（カンマ区切り）。再起動をまたいで
# 終了時のモードを設定するために保持する
CHARGE_MODE_RUNNING_KEY = "charge_mode_running_schedules"
# 旧形式（実行中かどうかのみ）。移行時に、実行中だった場合は設定の既定値の終了時のモードに戻すために読む
LEGACY_CHARGE_MODE_SCHEDULE_KEY = "charge_mode_schedule_active"

# コマンド送信後の状態確認（docs/08 §8.3）
VERIFY_WAIT_SECONDS = 3
VERIFY_ATTEMPTS = 5
# 送信・確認に失敗した操作は、この間は同じ要求を繰り返さない（SwitchBot API の呼び出し回数の上限のため）
RETRY_AFTER = timedelta(minutes=5)
# 送信と照合を並行して行う機器の数の上限（docs/17 §17.7.2）
MAX_PARALLEL_DEVICES = 5

_ACTIVE_DEVICE_STATUSES = (ScheduleDeviceStatus.PENDING, ScheduleDeviceStatus.RUNNING)


class ControlService:
    """状態変更の唯一の経路。スケジュールの実行・手動操作・安全制御をすべてここで行い、制御履歴に記録する。

    - Dry Run：コマンドを送らず、送っていればこうなっているはずの状態（state）を想定して記録する。
    - 実制御：DRY_RUN=false かつ画面で実制御を有効にした場合のみ。送信前後に状態を取得して照合する。
    - モード：AUTO（スケジュールを実行）／MANUAL（手動操作を優先。安全制御は継続）。DB に保持し再起動後も引き継ぐ。
    制御対象の機器は操作のたびに DB（devices）から読み直す。

    battery：すべての給電先で共用する BatteryMonitor（テスト用）。battery_factory：給電先ごとに BatteryMonitor を作る
    （ポータブル電源の機器 ID・devices.id・名前を受け取る）。どちらもなければ残量は常に取得できない扱い。
    """

    def __init__(
        self,
        settings: Settings,
        session_factory: Callable[[], Session],
        *,
        switchbot: SwitchBotClient | None = None,
        battery: BatteryMonitor | None = None,
        battery_factory: Callable[[str, int, str], BatteryMonitor] | None = None,
        sleep: Callable[[float], None] = time_module.sleep,
    ) -> None:
        self._settings = settings
        self._session_factory = session_factory
        self._tz = ZoneInfo(settings.tz)
        self._lock = threading.RLock()
        self._switchbot = switchbot
        self._shared_battery = battery
        self._battery_factory = battery_factory
        self._sleep = sleep
        self._control_mode = ControlMode.AUTO
        self._real_control_enabled = False
        self._devices: dict[int, DeviceRuntime] = {}
        self._sources: dict[int, PowerSource] = {}
        self._loaded = False
        self._read_in_cycle: set[int] = set()  # この操作の中で残量を取得したポータブル電源（同じものは1回だけ取得する）
        # 機器ごとの消費電力サンプルの開始位置（(schedule_id, device_id) → samples の位置。実測値の記録用）
        self._sample_start: dict[tuple[int, int], int] = {}
        # 充電モード（docs/07 §7.10）：切り替えの対象とした実行中のスケジュール、Dry Run の想定のモード、最後の変更、
        # 失敗した自動変更（給電先の devices.id ごと）
        self._charge_running: set[int] = set()
        self._charge_legacy_active = False
        self._assumed_charge_mode: dict[int, ChargeMode] = {}
        self.charge_changes: dict[int, ChargeModeChange] = {}
        self.last_charge_change: ChargeModeChange | None = None
        self._charge_failed: dict[int, tuple[ChargeMode, datetime]] = {}
        self._load_persisted()

    # --- 状態 ---

    def _load_persisted(self) -> None:
        try:
            with self._session_factory() as session:
                self._control_mode = ControlMode(get_value(session, CONTROL_MODE_KEY) or ControlMode.AUTO)
                self._real_control_enabled = get_value(session, REAL_CONTROL_KEY) == "true"
                running = get_value(session, CHARGE_MODE_RUNNING_KEY)
                if running is None:
                    self._charge_legacy_active = get_value(session, LEGACY_CHARGE_MODE_SCHEDULE_KEY) == "true"
                self._charge_running = {int(i) for i in (running or "").split(",") if i}
        except Exception:
            logger.exception("failed to load control mode; using AUTO / Dry Run")

    def bind_devices(self, session: Session) -> None:
        """制御対象の機器と給電先のポータブル電源を DB から読み直す（起動時・制御機器の保存時・各操作の開始時）。

        外した機器の状態は捨て、新しく加えた機器は状態不明（次の制御サイクルで状態を取得して初期化）から始める。
        """
        with self._lock:
            devices: dict[int, DeviceRuntime] = {}
            for d in device_models.active_switches(session):
                runtime = self._devices.get(d.id)
                if runtime is None or runtime.external_id != d.external_id:
                    runtime = DeviceRuntime(d.id, d.name, d.external_id, d.power_source_id)
                runtime.name, runtime.power_source_id = d.name, d.power_source_id
                devices[d.id] = runtime
            if devices.keys() != self._devices.keys():
                logger.info("controlling devices: %s", ", ".join(d.name for d in devices.values()) or "(none)")
            self._devices = devices

            sources: dict[int, PowerSource] = {}
            for source_id in {d.power_source_id for d in devices.values() if d.power_source_id is not None}:
                source = session.get(Device, source_id)
                if source is None or not source.active:
                    continue
                current = self._sources.get(source_id)
                monitor: BatteryMonitor | None
                if self._shared_battery is not None:
                    monitor = self._shared_battery
                elif current is not None and current.monitor.device_id == source.external_id:
                    monitor = current.monitor
                    monitor.name = source.name
                elif self._battery_factory is not None:
                    monitor = self._battery_factory(source.external_id, source.id, source.name)
                else:
                    monitor = None
                if monitor is None:
                    continue
                enabled = source.portable_power.charge_mode_enabled if source.portable_power is not None else True
                sources[source_id] = PowerSource(source_id, source.name, monitor, enabled)
            self._sources = sources
            self._loaded = True

    @property
    def devices(self) -> list[DeviceRuntime]:
        """制御対象の機器（表示順）。"""
        return list(self._devices.values())

    def device(self, device_id: int) -> DeviceRuntime | None:
        return self._devices.get(device_id)

    @property
    def power_sources(self) -> list[PowerSource]:
        return list(self._sources.values())

    def power_source(self, source_id: int | None) -> PowerSource | None:
        return self._sources.get(source_id) if source_id is not None else None

    def _monitor_for(self, device: DeviceRuntime) -> BatteryMonitor | None:
        source = self.power_source(device.power_source_id)
        return source.monitor if source is not None else None

    @property
    def battery(self) -> BatteryMonitor:
        """最初の給電先の BatteryMonitor（給電先がなければ未設定の BatteryMonitor）。"""
        sources = self.power_sources
        if sources:
            return sources[0].monitor
        return self._shared_battery or BatteryMonitor(None, None, self._session_factory)

    @property
    def dry_run(self) -> bool:
        """DRY_RUN=false かつ画面で実制御を有効にした場合のみ実制御（docs/14 §14.3）。"""
        return self._settings.dry_run or not self._real_control_enabled or not _credentials_configured(self._switchbot)

    @property
    def can_enable_real_control(self) -> bool:
        return not self._settings.dry_run and _credentials_configured(self._switchbot)

    @property
    def control_mode(self) -> ControlMode:
        return self._control_mode

    @property
    def mode(self) -> str:
        """docs/07 §7.3 の制御状態の全体の表示（MANUAL、いずれかが SAFE なら SAFE、OFF があれば BATTERY、それ以外は GRID）。"""
        if self._control_mode == ControlMode.MANUAL:
            return "MANUAL"
        modes = {d.mode for d in self._devices.values()}
        if not modes or "SAFE" in modes:
            return "SAFE"
        return "BATTERY" if "BATTERY" in modes else "GRID"

    def device_state(self, device_id: int) -> PlugState | None:
        device = self._devices.get(device_id)
        return device.state if device is not None else None

    @property
    def state(self) -> PlugState | None:
        """制御機器が1台のときの状態（複数台のときは None。表示・テスト用）。"""
        devices = list(self._devices.values())
        return devices[0].state if len(devices) == 1 else None

    def charge_mode_for(self, source_id: int) -> ChargeMode | None:
        """表示用の充電モード。Dry Run では変更した想定のモード（未変更なら最後に確認したモード）。"""
        if self.dry_run and source_id in self._assumed_charge_mode:
            return self._assumed_charge_mode[source_id]
        source = self._sources.get(source_id)
        return source.monitor.charge_mode if source is not None else None

    @property
    def charge_mode(self) -> ChargeMode | None:
        """最初の給電先の充電モード（表示・テスト用）。"""
        sources = self.power_sources
        return self.charge_mode_for(sources[0].id) if sources else None

    def set_real_control(self, enabled: bool) -> None:
        """実制御の有効・無効（画面での確認操作から呼ぶ）。有効にできない環境では何もしない。"""
        with self._lock, self._session_factory() as session:
            if enabled and not self.can_enable_real_control:
                raise ValueError("DRY_RUN=false かつ SwitchBot の認証情報が設定されている場合のみ実制御を有効にできます")
            set_value(session, REAL_CONTROL_KEY, "true" if enabled else "false")
            self._real_control_enabled = enabled
            # Dry Run の想定状態と実機の状態は異なるため、次のサイクルで実機の状態から初期化し直す
            for device in self._devices.values():
                device.state = None
            logger.warning("real control %s", "enabled" if enabled else "disabled")

    # --- 状態変更 ---

    def request(
        self,
        session: Session,
        target: PlugState,
        source: ControlSource,
        reason: str,
        *,
        device_id: int | None = None,
        schedule_id: int | None = None,
        now: datetime | None = None,
    ) -> ControlLog:
        """機器1台の状態変更を要求し、制御履歴に記録する。device_id を省略できるのは制御機器が1台のときだけ。"""
        with self._lock:
            if not self._loaded:
                self.bind_devices(session)
            device = self._resolve_device(device_id)
            return self._send(session, [_Request(device, target, source, reason, schedule_id)], now)[0]

    def _resolve_device(self, device_id: int | None) -> DeviceRuntime:
        if device_id is None:
            if len(self._devices) != 1:
                raise ValueError("機器を指定してください")
            return next(iter(self._devices.values()))
        device = self._devices.get(device_id)
        if device is None:
            raise ValueError("制御対象の機器ではありません")
        return device

    def _send(self, session: Session, requests: list[_Request], now: datetime | None) -> list[ControlLog]:
        """要求をまとめて実行する。実制御では送信と照合を機器ごとに並行して行い、記録は順に行う。"""
        if not requests:
            return []
        executed_at = now or datetime.now(UTC)
        dry_run = self.dry_run
        logs = [
            ControlLog(
                executed_at=executed_at,
                source=r.source,
                previous_state=r.device.state,
                requested_state=r.target,
                reason=r.reason,
                dry_run=dry_run,
                schedule_id=r.schedule_id,
                device_id=r.device.id,
            )
            for r in requests
        ]
        previous_off = {r.device.id: r.device.state == PlugState.OFF for r in requests}
        if dry_run:
            for r, log in zip(requests, logs, strict=True):
                log.actual_state, log.result = None, ControlResult.DRY_RUN
                r.device.state = r.target
        elif len(requests) == 1:
            self._execute_safely(requests[0].device, logs[0], requests[0].target)
        else:
            with ThreadPoolExecutor(max_workers=min(MAX_PARALLEL_DEVICES, len(requests))) as pool:
                list(pool.map(lambda pair: self._execute_safely(pair[0].device, pair[1], pair[0].target), zip(requests, logs, strict=True)))

        for log in logs:
            session.add(log)
        session.commit()
        for r, log in zip(requests, logs, strict=True):
            device = r.device
            if log.result in (ControlResult.FAILED, ControlResult.UNCONFIRMED):
                device.failed = (r.target, executed_at)
                record_event(
                    session,
                    EventLevel.ERROR,
                    EventCategory.SWITCHBOT,
                    f"{device.name} を {r.target} にできませんでした：{log.error_message}",
                    device_id=device.id,
                )
            elif log.result == ControlResult.SUCCESS:
                if device.failed is not None:
                    resolve_events(session, EventCategory.SWITCHBOT, device_id=device.id)
                device.failed = None
            if r.target == PlugState.OFF and not previous_off[device.id] and device.state == PlugState.OFF:
                self._reset_samples_for(device)
            logger.info(
                "%s %s: %s -> %s (%s: %s) %s",
                "[dry-run]" if log.dry_run else "[real]",
                device.name,
                log.previous_state,
                r.target,
                r.source,
                r.reason,
                log.result,
            )
        return logs

    def _reset_samples_for(self, device: DeviceRuntime) -> None:
        """給電先の消費電力の平均を、OFF の間の値だけで取り直す（同じ給電先の他の機器が OFF 中ならそのまま）。"""
        monitor = self._monitor_for(device)
        if monitor is None:
            return
        others_off = any(
            d.id != device.id and d.state == PlugState.OFF and self._monitor_for(d) is monitor for d in self._devices.values()
        )
        if not others_off:
            monitor.reset_samples()

    def _execute_safely(self, device: DeviceRuntime, log: ControlLog, target: PlugState) -> None:
        """1台の想定外の例外で他の機器の処理・記録を止めない。"""
        try:
            self._execute(device, log, target)
        except Exception as exc:
            logger.exception("unexpected error while controlling %s", device.name)
            log.actual_state, log.result = None, ControlResult.UNCONFIRMED
            log.error_message = f"送信中にエラーが発生しました：{exc.__class__.__name__}"
            device.state = None

    def _execute(self, device: DeviceRuntime, log: ControlLog, target: PlugState) -> None:
        """docs/08 §8.3：操作前の状態取得 → 必要な場合のみ送信 → 状態の再取得 → 期待状態との照合。"""
        assert self._switchbot is not None
        try:
            current = self._switchbot.get_status(device.external_id).power
            log.previous_state = PlugState(current)
        except SwitchBotError as exc:
            current = None
            logger.warning("failed to get status of %s before command: %s", device.name, exc)
        if current == target:
            log.actual_state, log.result = target, ControlResult.SUCCESS
            device.state = target
            return

        actual: PlugState | None = None
        error: str | None = None
        for _ in range(2):  # 期待状態にならなければ1回だけ送り直す
            try:
                self._switchbot.send_power_command(target.value, device.external_id)  # type: ignore[arg-type]
            except SwitchBotError as exc:
                error = str(exc)
                continue
            for _ in range(VERIFY_ATTEMPTS):
                self._sleep(VERIFY_WAIT_SECONDS)
                try:
                    actual = PlugState(self._switchbot.get_status(device.external_id).power)
                except SwitchBotError as exc:
                    error, actual = str(exc), None
                    continue
                if actual == target:
                    log.actual_state, log.result = actual, ControlResult.SUCCESS
                    device.state = target
                    return
            error = error or f"送信後の状態が {actual} のままです"
        log.actual_state = actual
        log.error_message = error
        # 照合できなければ成功扱いにしない（docs/14 §14.2）
        log.result = ControlResult.FAILED if actual is not None else ControlResult.UNCONFIRMED
        device.state = actual

    @staticmethod
    def _should_retry(device: DeviceRuntime, target: PlugState, now: datetime) -> bool:
        return device.failed is None or device.failed[0] != target or now - device.failed[1] >= RETRY_AFTER

    def initialize(self, now: datetime | None = None) -> None:
        """起動時は機器ごとに状態を取得して引き継ぐ（docs/09 §9.2、docs/17 §17.7.4）。失敗しても Web UI は起動させる。

        状態を取得できた機器は操作しない（実行中の OFF のスケジュールの途中で再起動しても ON に戻さない。ON・OFF は
        次の制御サイクルがスケジュールに従って判断する）。取得できない機器だけ安全側（ON）へ初期化する。
        """
        try:
            with self._lock, self._session_factory() as session:
                self.bind_devices(session)
                unknown: list[DeviceRuntime] = []
                for device in self._devices.values():
                    if self._switchbot is None:
                        unknown.append(device)
                        continue
                    try:
                        power = PlugState(self._switchbot.get_status(device.external_id).power)
                    except (SwitchBotError, ValueError) as exc:
                        logger.warning("failed to get status of %s at startup; initializing to ON: %s", device.name, exc)
                        unknown.append(device)
                        continue
                    device.state, device.startup_kept = power, True
                    if power == PlugState.OFF and self._control_mode == ControlMode.MANUAL:
                        # 手動 OFF の継続時間は起動時から数える（残量を取得できない間の制限のため）
                        device.manual_off_since = self._local(now)
                    logger.info("%s is %s at startup; keeping the current state", device.name, power)
                self._send(
                    session,
                    [_Request(d, PlugState.ON, ControlSource.SAFETY, "起動時の安全側への初期化") for d in unknown],
                    now,
                )
        except Exception:
            logger.exception("failed to initialize control state; will retry in the next control cycle")

    def shutdown(self, now: datetime | None = None) -> None:
        """終了時は機器・充電モードを操作せず、実行中のスケジュールも実行中のまま残す（docs/09 §9.3）。"""
        with self._lock:
            logger.info("shutting down without changing devices (%s)", {d.name: d.state for d in self._devices.values()})

    # --- 手動操作（docs/05 §5.8） ---

    def manual_power(self, target: PlugState, now: datetime | None = None, *, device_id: int | None = None) -> ControlLog:
        """機器を手動で ON/OFF する。MANUAL モード（全体）へ移行し、実行中のスケジュールは全機器で中断する。"""
        local_now = self._local(now)
        with self._lock, self._session_factory() as session:
            self._begin(session)
            device = self._resolve_device(device_id)
            self._set_mode(session, ControlMode.MANUAL)
            self._abort_running(session, local_now, "手動操作")
            if target == PlugState.OFF:
                decision = self._check_manual_off(session, device, local_now, starting=True)
                if not decision.allowed:
                    raise ValueError(decision.reason)
            log = self._send(session, [_Request(device, target, ControlSource.MANUAL, f"手動操作（{target}）")], local_now)[0]
            for d in self._devices.values():
                if d is device:
                    d.manual_off_since = local_now if d.state == PlugState.OFF else None
                elif d.state == PlugState.OFF and d.manual_off_since is None:
                    d.manual_off_since = local_now  # 中断したスケジュールで OFF のままの機器も手動 OFF として保護する
            self._sync_charge_mode(session, local_now)
            return log

    def stop_auto(self, now: datetime | None = None) -> None:
        """自動制御を停止する（機器の状態は変えない）。"""
        local_now = self._local(now)
        with self._lock, self._session_factory() as session:
            self._begin(session)
            self._set_mode(session, ControlMode.MANUAL)
            self._abort_running(session, local_now, "自動制御の停止")
            for d in self._devices.values():
                d.manual_off_since = local_now if d.state == PlugState.OFF else None
            self._sync_charge_mode(session, local_now)

    def start_auto(self) -> None:
        """自動制御を再開する（MANUAL → SAFE。次の制御サイクルでスケジュールに従う）。"""
        with self._lock, self._session_factory() as session:
            self._set_mode(session, ControlMode.AUTO)
            for d in self._devices.values():
                d.manual_off_since = None

    def _set_mode(self, session: Session, mode: ControlMode) -> None:
        set_value(session, CONTROL_MODE_KEY, mode.value)
        self._control_mode = mode

    def _abort_running(self, session: Session, now: datetime, reason: str) -> None:
        for day in (now.date() - timedelta(days=1), now.date()):
            for s in optimizer.schedules_for(session, day):
                if s.status != ScheduleStatus.RUNNING:
                    continue
                for sd in s.devices:
                    if sd.status == ScheduleDeviceStatus.RUNNING:
                        self._finish_device(session, s, sd, min(now, s.end_at), ScheduleDeviceStatus.ABORTED, reason)
                    elif sd.status == ScheduleDeviceStatus.RETURNED:
                        self._close_returned(sd)
                    elif sd.status == ScheduleDeviceStatus.PENDING:
                        sd.status, sd.status_reason = ScheduleDeviceStatus.SKIPPED, reason
                self._aggregate(s)
                if s.status != ScheduleStatus.DONE:
                    s.status, s.status_reason = ScheduleStatus.ABORTED, reason
        session.commit()

    def _check_manual_off(self, session: Session, device: DeviceRuntime, now: datetime, *, starting: bool) -> safety.OffDecision:
        """手動 OFF の安全判定：給電先の残量で判定し、取得できない間・給電先がない機器は最大連続OFF時間で ON に戻す。"""
        control = load_control_settings(session, self._settings)
        monitor = self._monitor_for(device)
        reading = self._battery_reading(monitor, now, force=starting)
        if monitor is not None and safety.battery_known(reading, now):
            assert reading is not None
            decision = safety.check_battery(reading, now, control, average_output_w=monitor.average_output_w(), end_at=None)
            if not decision.allowed and len(self._sources) > 1:
                return safety.OffDecision(False, f"{self._source_name(device)}：{decision.reason}")
            return decision
        if device.manual_off_since is not None and not starting:
            minutes = (now - device.manual_off_since).total_seconds() // 60
            if minutes >= control.max_continuous_off_minutes:
                return safety.OffDecision(
                    False, f"残量を取得できないため、最大連続OFF時間（{control.max_continuous_off_minutes}分）で ON に戻します"
                )
        return safety.OffDecision(True)

    def _battery_reading(self, monitor: BatteryMonitor | None, now: datetime, *, force: bool) -> safety.BatteryReading | None:
        """残量を取得する（OFF 開始時は必ず、OFF 中は5分ごと。同じ操作の中では同じポータブル電源を1回だけ取得する）。"""
        if monitor is None:
            return None
        last = monitor.last
        if id(monitor) in self._read_in_cycle:
            return last
        if force or last is None or now - last.at >= safety.BATTERY_CHECK_INTERVAL:
            self._read_in_cycle.add(id(monitor))
            return monitor.read(now) or last
        return last

    def _source_name(self, device: DeviceRuntime) -> str:
        source = self.power_source(device.power_source_id)
        return source.name if source is not None else "ポータブル電源"

    # --- 充電モード（docs/07 §7.10） ---

    def manual_charge_mode(self, mode: ChargeMode, now: datetime | None = None, *, source_id: int | None = None) -> ChargeModeChange:
        """画面から充電モードを変更する。プラグの制御モード（AUTO/MANUAL）は変えない。"""
        local_now = self._local(now)
        with self._lock, self._session_factory() as session:
            self._begin(session)
            sources = self.power_sources
            source = self._sources.get(source_id) if source_id is not None else (sources[0] if len(sources) == 1 else None)
            if source is None or not source.monitor.configured:
                raise ValueError("Jackery のアカウントまたは給電先のポータブル電源が未設定のため、充電モードを変更できません")
            self._charge_failed.pop(source.id, None)  # 手動の変更を優先し、失敗した自動変更は再試行しない
            return self._change_charge_mode(session, source, mode, ControlSource.MANUAL, f"手動操作（{mode.label}）", local_now)

    def _charge_targets(self, session: Session, schedule_ids: set[int]) -> list[PowerSource]:
        """スケジュールの対象機器の給電先（充電モードの切り替えを有効にしたもの）。"""
        ids: set[int] = set()
        for schedule_id in schedule_ids:
            schedule = session.get(ControlSchedule, schedule_id)
            if schedule is None:
                continue
            for device_id in schedule.device_ids:
                device = self._devices.get(device_id)
                if device is not None and device.power_source_id is not None:
                    ids.add(device.power_source_id)
        return [s for s in self.power_sources if s.id in ids and s.charge_mode_enabled]

    def _sync_charge_mode(self, session: Session, now: datetime) -> None:
        """スケジュールの開始・終了時に、スケジュールごとの設定に従って充電モードを切り替える。

        実行中（RUNNING）になったスケジュールは開始時、実行中でなくなった（終了・中断）スケジュールは終了時の
        モードを設定する。同じサイクルで終了と開始が重なる場合（連続するスケジュール）は、開始するスケジュールの
        設定を優先し、「変更しない」の設定は無視する（送信は1回のみ。すでにそのモードなら送らない）。
        送り先は、そのスケジュールの対象機器の給電先。充電モードは電源の供給に影響しないため、失敗してもプラグの
        制御は止めない。
        """
        try:
            running = {
                s.id: s
                for day in (now.date() - timedelta(days=1), now.date())
                for s in optimizer.schedules_for(session, day)
                if s.status == ScheduleStatus.RUNNING
            }
            ended = self._charge_running - running.keys()
            started = running.keys() - self._charge_running
            if ended or started or self._charge_legacy_active:
                modes: list[tuple[ChargeModeOption, str]] = []
                targets = self._charge_targets(session, ended | started)
                if self._charge_legacy_active and not running:
                    # 旧形式で実行中として記録していた（このサイクルまでに終了した）スケジュール
                    end_mode = load_control_settings(session, self._settings).charge_mode_end
                    modes.append((end_mode, "スケジュール終了"))
                    targets = [s for s in self.power_sources if s.charge_mode_enabled]
                for schedule_id in sorted(ended):
                    schedule = session.get(ControlSchedule, schedule_id)
                    if schedule is not None:
                        modes.append((schedule.charge_mode_end, "スケジュール終了"))
                for schedule in sorted((running[i] for i in started), key=lambda s: s.start_at):
                    modes.append((schedule.charge_mode_start, "スケジュール開始"))
                self._charge_running = set(running)
                self._charge_legacy_active = False
                set_value(session, CHARGE_MODE_RUNNING_KEY, ",".join(map(str, sorted(running))))
                target = next(((m, r) for m, r in reversed(modes) if m != ChargeModeOption.KEEP), None)
                if target is None:
                    return
                mode = ChargeMode(target[0].value)
                for source in targets:
                    if ended and started and mode == self.charge_mode_for(source.id):
                        continue  # 連続するスケジュールの間で、すでに同じモード
                    self._charge_failed.pop(source.id, None)
                    if source.monitor.configured:
                        self._change_charge_mode(session, source, mode, ControlSource.AUTO, target[1], now)
                return
            for source_id, (mode, at) in list(self._charge_failed.items()):
                source = self._sources.get(source_id)
                if source is not None and now - at >= RETRY_AFTER:
                    self._change_charge_mode(session, source, mode, ControlSource.AUTO, f"充電モード（{mode.label}）の再試行", now)
        except Exception:
            logger.exception("failed to sync charge mode")

    def _change_charge_mode(
        self, session: Session, source: PowerSource, mode: ChargeMode, origin: ControlSource, reason: str, now: datetime
    ) -> ChargeModeChange:
        """充電モードを変更し、結果を system_events に記録する。Dry Run では送信しない。"""
        error: str | None = None
        if self.dry_run:
            result = ControlResult.DRY_RUN
            self._assumed_charge_mode[source.id] = mode
        else:
            self._assumed_charge_mode.pop(source.id, None)
            try:
                source.monitor.set_charge_mode(mode, now)
                result = ControlResult.SUCCESS
            except JackeryCommandUnconfirmed as exc:
                result, error = ControlResult.UNCONFIRMED, str(exc)
            except JackeryError as exc:
                result, error = ControlResult.FAILED, str(exc)

        change = ChargeModeChange(mode, origin, reason, result, now, error)
        self.charge_changes[source.id] = change
        self.last_charge_change = change
        label = f"{source.name}の" if len(self._sources) > 1 else ""
        if error is not None:
            logger.warning("charge mode of %s -> %s failed (%s: %s): %s", source.name, mode, origin, reason, error)
            # 同じ要求の再試行の失敗は記録し直さない
            previous = self._charge_failed.get(source.id)
            first_failure = previous is None or previous[0] != mode
            if origin == ControlSource.AUTO:
                self._charge_failed[source.id] = (mode, now)
            if first_failure or origin != ControlSource.AUTO:
                record_event(
                    session,
                    EventLevel.ERROR,
                    EventCategory.CHARGE_MODE,
                    f"{label}充電モードを{mode.label}にできませんでした：{error}",
                    device_id=source.id,
                )
            return change

        self._charge_failed.pop(source.id, None)
        resolve_events(session, EventCategory.CHARGE_MODE, device_id=source.id)
        prefix = "（Dry Run）" if result == ControlResult.DRY_RUN else ""
        # 変更の記録として残す（エラーではないため解決済みにして、ダッシュボードのお知らせには出さない）
        event = record_event(
            session,
            EventLevel.INFO,
            EventCategory.CHARGE_MODE,
            f"{prefix}{label}充電モードを{mode.label}にしました（{origin}：{reason}）",
            device_id=source.id,
        )
        event.resolved_at = event.occurred_at
        session.commit()
        logger.info(
            "%s charge mode of %s -> %s (%s: %s)",
            "[dry-run]" if result == ControlResult.DRY_RUN else "[real]",
            source.name,
            mode,
            origin,
            reason,
        )
        return change

    # --- 制御サイクル（docs/07 §7.8、docs/17 §17.7.2） ---

    def _local(self, now: datetime | None) -> datetime:
        return (now or datetime.now(UTC)).astimezone(self._tz)

    def _begin(self, session: Session) -> None:
        """操作の開始：制御対象の機器を読み直し、残量の取得の記録をリセットする。"""
        self.bind_devices(session)
        self._read_in_cycle = set()

    def run_cycle(self, now: datetime | None = None) -> None:
        """毎分実行する。AUTO では機器ごとに、その機器を対象に含む有効なスケジュールの間は OFF、それ以外は ON。
        MANUAL では安全制御のみ。プラグの制御のあとに、スケジュールの開始・終了に合わせて充電モードを切り替える。
        """
        now = self._local(now)
        with self._lock, self._session_factory() as session:
            try:
                self._begin(session)
                self._run_plug_cycle(session, now)
            finally:
                self._sync_charge_mode(session, now)

    def _run_plug_cycle(self, session: Session, now: datetime) -> None:
        startup = {d.id for d in self._devices.values() if d.startup_kept}
        for d in self._devices.values():
            d.startup_kept = False
        # 未初期化・状態不明の機器は安全側へ（MANUAL でも ON に戻す。実制御では実機の状態を取得して照合する）
        self._send(
            session,
            [
                _Request(d, PlugState.ON, ControlSource.SAFETY, "安全側への初期化")
                for d in self._devices.values()
                if d.state is None and self._should_retry(d, PlugState.ON, now)
            ],
            now,
        )

        schedules = optimizer.schedules_for(session, now.date())
        self._close_ended(session, now, schedules)

        if self._control_mode == ControlMode.MANUAL:
            self._run_manual_safety(session, now)
            return

        active = [
            s
            for s in schedules
            if s.enabled and s.start_at <= now < s.end_at and s.status in (ScheduleStatus.PENDING, ScheduleStatus.RUNNING)
        ]
        for s in active:
            if not s.devices:
                s.status, s.status_reason = ScheduleStatus.SKIPPED, "未実行（対象機器がありません）"
        session.commit()
        by_device: dict[int, ControlSchedule] = {}
        for s in active:
            for device_id in s.device_ids:
                by_device.setdefault(device_id, s)  # 時間が重ならないため、多くても1つ（docs/17 §17.6.3）

        control = load_control_settings(session, self._settings)
        has_forecast = forecast_service.has_forecast(session, self._settings.looop_area, now.date(), self._tz)
        day_off = safety.off_minutes(schedules, now)
        requests: list[_Request] = []
        started: list[tuple[ControlSchedule, DeviceRuntime]] = []
        for device in self._devices.values():
            if device.state is None:
                continue  # ON を確認できない間は OFF を要求しない
            schedule = by_device.get(device.id)
            sd = schedule.device(device.id) if schedule is not None else None
            if schedule is None or sd is None:
                if device.state == PlugState.OFF and self._should_retry(device, PlugState.ON, now):
                    reason = "起動時に OFF だったため ON に戻す" if device.id in startup else "スケジュール終了"
                    requests.append(_Request(device, PlugState.ON, ControlSource.AUTO, reason))
                continue
            if sd.status not in _ACTIVE_DEVICE_STATUSES:
                # 残量低下で ON に戻した（RETURNED）・中断した機器は、このスケジュールの終了まで OFF に戻さない
                if device.state == PlugState.OFF and self._should_retry(device, PlugState.ON, now):
                    requests.append(
                        _Request(device, PlugState.ON, ControlSource.SAFETY, sd.status_reason or "スケジュールの対象外", schedule.id)
                    )
                continue
            if schedule.target_state == PlugState.ON:
                self._run_on_schedule(schedule, sd, device, now, requests)
                continue
            if self._run_off_schedule(session, schedule, sd, device, now, control, has_forecast, day_off, requests):
                started.append((schedule, device))
        session.commit()
        self._send(session, requests, now)
        for schedule, device in started:
            monitor = self._monitor_for(device)
            if monitor is not None:
                self._sample_start.setdefault((schedule.id, device.id), max(0, len(monitor.samples) - 1))
        for s in active:
            self._aggregate(s)
        session.commit()

    def _run_on_schedule(
        self, schedule: ControlSchedule, sd: ScheduleDevice, device: DeviceRuntime, now: datetime, requests: list[_Request]
    ) -> None:
        """任意 ON のスケジュール（docs/07 §7.9）：OFF を要求せず、OFF 中なら ON に戻す。安全側のため判定は不要。"""
        if sd.status == ScheduleDeviceStatus.PENDING:
            sd.status = ScheduleDeviceStatus.RUNNING
            schedule.status = ScheduleStatus.RUNNING
        if device.state != PlugState.ON and self._should_retry(device, PlugState.ON, now):
            requests.append(_Request(device, PlugState.ON, ControlSource.AUTO, schedule.reason, schedule.id))

    def _run_off_schedule(
        self,
        session: Session,
        schedule: ControlSchedule,
        sd: ScheduleDevice,
        device: DeviceRuntime,
        now: datetime,
        control: ControlSettings,
        has_forecast: bool,
        day_off: int,
        requests: list[_Request],
    ) -> bool:
        """OFF のスケジュールの機器1台分の判定（docs/17 §17.7.3）。OFF を続ける（始める）なら True。"""
        monitor = self._monitor_for(device)
        starting = sd.status == ScheduleDeviceStatus.PENDING or device.state != PlugState.OFF
        reading = self._battery_reading(monitor, now, force=starting)
        known = monitor is not None and safety.battery_known(reading, now)
        decision = safety.check_off(
            schedule,
            now,
            control,
            has_forecast=has_forecast,
            day_off_minutes=day_off,
            battery_known=known,
            started_at=sd.executed_start_at,
        )
        battery_decision = None
        if decision.allowed and known:
            assert reading is not None and monitor is not None
            battery_decision = safety.check_battery(
                reading, now, control, average_output_w=monitor.average_output_w(), end_at=schedule.end_at
            )
        if decision.allowed and (battery_decision is None or battery_decision.allowed):
            if sd.status == ScheduleDeviceStatus.PENDING:
                sd.status = ScheduleDeviceStatus.RUNNING
                sd.executed_start_at = now
                sd.power_source_id = device.power_source_id
                sd.battery_start_percent = reading.percent if known else None  # type: ignore[union-attr]
                schedule.status = ScheduleStatus.RUNNING
            if device.state != PlugState.OFF and self._should_retry(device, PlugState.OFF, now):
                requests.append(_Request(device, PlugState.OFF, ControlSource.AUTO, schedule.reason, schedule.id))
            return True

        if battery_decision is not None:
            # 残量が下限以下（予測を含む）：この機器だけ ON に戻し、スケジュールは続ける（docs/17 §17.7.3）
            reason = f"{self._source_name(device)}の残量が下限以下のため ON に戻す（{battery_decision.reason}）"
            status = ScheduleDeviceStatus.RETURNED
        else:
            # 残量以外の判定（予報なし・許可時間・OFF 時間の制限）：この機器の実行を中断する
            reason = decision.reason or ""
            status = ScheduleDeviceStatus.ABORTED
        if sd.executed_start_at is not None:
            self._finish_device(session, schedule, sd, now, status, reason)
        else:
            sd.status, sd.status_reason = status, reason
        if device.state != PlugState.ON and self._should_retry(device, PlugState.ON, now):
            requests.append(_Request(device, PlugState.ON, ControlSource.SAFETY, reason, schedule.id))
        return False

    def _run_manual_safety(self, session: Session, now: datetime) -> None:
        """MANUAL モードでも安全制御は継続する（docs/05 §5.8）。機器ごとに給電先の残量で判定する。"""
        requests = []
        for device in self._devices.values():
            if device.state != PlugState.OFF:
                continue
            decision = self._check_manual_off(session, device, now, starting=False)
            if not decision.allowed and self._should_retry(device, PlugState.ON, now):
                requests.append(_Request(device, PlugState.ON, ControlSource.SAFETY, decision.reason or ""))
        self._send(session, requests, now)
        for r in requests:
            if r.device.state == PlugState.ON:
                r.device.manual_off_since = None

    def _close_ended(self, session: Session, now: datetime, schedules: list[ControlSchedule]) -> None:
        """終了時刻を過ぎたスケジュールの後処理（前日の 24:00 まで実行したものを含む）。"""
        for s in optimizer.schedules_for(session, now.date() - timedelta(days=1)) + schedules:
            if s.end_at > now or s.status not in (ScheduleStatus.PENDING, ScheduleStatus.RUNNING):
                continue
            if not s.devices:
                s.status, s.status_reason = ScheduleStatus.SKIPPED, "未実行（対象機器がありません）"
                continue
            for sd in s.devices:
                if sd.status == ScheduleDeviceStatus.RUNNING:
                    self._finish_device(session, s, sd, s.end_at, ScheduleDeviceStatus.DONE, None)
                elif sd.status == ScheduleDeviceStatus.RETURNED:
                    self._close_returned(sd)
                elif sd.status == ScheduleDeviceStatus.PENDING:
                    sd.status = ScheduleDeviceStatus.SKIPPED
                    if not s.enabled:
                        sd.status_reason = "無効のため実行しませんでした"
                    elif self._control_mode == ControlMode.MANUAL:
                        sd.status_reason = "自動制御を停止していました"
                    elif sd.device_id not in self._devices:
                        sd.status_reason = "制御対象ではありませんでした"
                    else:
                        sd.status_reason = "開始時刻にシステムが停止していました"
            self._aggregate(s)
        session.commit()

    def _close_returned(self, sd: ScheduleDevice) -> None:
        """残量低下で ON に戻した機器は、スケジュールの終了時に完了とする（中断扱いにしない。docs/17 §17.4.5）。"""
        sd.status = ScheduleDeviceStatus.DONE
        if sd.executed_end_at is not None:
            sd.status_reason = f"残量低下のため {sd.executed_end_at.astimezone(self._tz):%H:%M} に ON に戻した"
        else:
            sd.status_reason = "残量低下のため開始しませんでした"

    @staticmethod
    def _aggregate(schedule: ControlSchedule) -> None:
        """機器ごとの状態をスケジュールの状態にまとめる（docs/17 §17.4.5）。"""
        statuses = [sd.status for sd in schedule.devices]
        if not statuses:
            return
        if any(s in (ScheduleDeviceStatus.RUNNING, ScheduleDeviceStatus.RETURNED) for s in statuses):
            schedule.status = ScheduleStatus.RUNNING
        elif all(s == ScheduleDeviceStatus.PENDING for s in statuses):
            pass
        elif any(s == ScheduleDeviceStatus.PENDING for s in statuses):
            schedule.status = ScheduleStatus.RUNNING
        else:
            if ScheduleDeviceStatus.DONE in statuses:
                schedule.status = ScheduleStatus.DONE
            elif all(s == ScheduleDeviceStatus.SKIPPED for s in statuses):
                schedule.status = ScheduleStatus.SKIPPED
            else:
                schedule.status = ScheduleStatus.ABORTED
            if schedule.status != ScheduleStatus.DONE:
                schedule.status_reason = next((sd.status_reason for sd in schedule.devices if sd.status_reason), None)
        savings = [sd.executed_saving for sd in schedule.devices if sd.executed_saving is not None]
        schedule.executed_saving = round(sum(savings), 1) if savings else None

    def _finish_device(
        self,
        session: Session,
        schedule: ControlSchedule,
        sd: ScheduleDevice,
        end: datetime,
        status: ScheduleDeviceStatus,
        reason: str | None,
    ) -> None:
        """機器の実行を終える。OFF のスケジュールは実行した時間帯の推定節約額と実測値を記録する。"""
        sd.status = status
        sd.status_reason = reason
        if schedule.target_state == PlugState.ON or sd.executed_start_at is None:
            return  # 任意 ON は OFF 時間・節約額・実測値の記録の対象外
        sd.executed_end_at = end
        self._record_measurements(schedule, sd, end)
        forecast = forecast_service.get_day_forecast(session, self._settings.looop_area, schedule.schedule_date, self._tz)
        if forecast.slots:
            params = optimizer.PlanParams.from_settings(load_control_settings(session, self._settings))
            device = session.get(Device, sd.device_id)
            sd.executed_saving = optimizer.saving_between(
                forecast.slots, sd.executed_start_at, end, optimizer.device_params(params, device)
            )
        self._aggregate(schedule)

    def _record_measurements(self, schedule: ControlSchedule, sd: ScheduleDevice, end: datetime) -> None:
        """終了時の給電先の残量と、その間の平均消費電力を記録する（取得できた場合のみ）。"""
        start = self._sample_start.pop((schedule.id, sd.device_id), None)
        device = self._devices.get(sd.device_id)
        monitor = self._monitor_for(device) if device is not None else None
        if monitor is None or not monitor.configured:
            return
        now = self._local(end)
        reading = self._battery_reading(monitor, now, force=True)
        if reading is not None and safety.battery_known(reading, now):
            sd.battery_end_percent = reading.percent
        if start is not None:
            samples = monitor.samples[start:]
            if samples:
                sd.measured_power_w = round(sum(samples) / len(samples), 1)
