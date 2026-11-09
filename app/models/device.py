"""機器の登録（device_types・devices・portable_power_settings）。docs/17-multi-device.md §17.4。

制御機器（SwitchBot のプラグ）とポータブル電源を devices にまとめて登録し、種別は
device_types（マスター）で管理する。ポータブル電源だけの設定は portable_power_settings に分ける。
秘匿情報（API トークン・アカウント）は保存せず、機器 ID・名前・設定値だけを保存する（docs/17 §17.3）。

現在（Phase 9）の制御は1台のまま：制御対象（active）の制御機器のうち先頭の1台（sort_order 順）を制御し、
その給電先のポータブル電源の残量を使う（`primary_switch`）。
"""

import enum
import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Boolean, Enum, Float, ForeignKey, Integer, String, Text, UniqueConstraint, func, select, update
from sqlalchemy.orm import Mapped, Session, mapped_column, relationship

from app.config import Settings
from app.models.schedule import ControlLog, ControlSchedule, ScheduleDevice, ScheduleDeviceStatus, ScheduleRule
from app.models.settings import get_value, load_control_settings, set_value
from app.repositories.database import Base, UTCDateTime


class Vendor(enum.StrEnum):
    SWITCHBOT = "SWITCHBOT"
    JACKERY = "JACKERY"
    FOSSIBOT = "FOSSIBOT"


class DeviceCategory(enum.StrEnum):
    POWER_SWITCH = "POWER_SWITCH"  # 制御機器（ON／OFF する SwitchBot 機器）
    PORTABLE_POWER = "PORTABLE_POWER"  # ポータブル電源


class TypeSource(enum.StrEnum):
    API = "API"  # SwitchBot API の deviceType 由来
    CUSTOM = "CUSTOM"  # 独自に定義


class DeviceState(enum.StrEnum):
    ON = "ON"
    OFF = "OFF"
    UNKNOWN = "UNKNOWN"


# 実機で ON／OFF と状態の照合を確認した種別（それ以外は制御対象にするときに注意を表示する。docs/17 §17.4.2）
VERIFIED_TYPE_CODES = {(Vendor.SWITCHBOT, "Plug Mini (JP)")}
# 移行時に登録するポータブル電源の種別（docs/17 §17.4.7）
JACKERY_1000_NEW = "JACKERY_1000_NEW"
RELAY_NOTE = "実機で ON／OFF と状態の照合を確認してから対応する"

# 種別マスターの初期登録（docs/17 §17.4.2）。マイグレーションと同じ内容（テスト・起動時の補完用）
SEED_DEVICE_TYPES: list[dict[str, Any]] = [
    *(
        {
            "vendor": Vendor.SWITCHBOT,
            "code": code,
            "name": name,
            "category": DeviceCategory.POWER_SWITCH,
            "power_control": power_control,
            "charge_mode": False,
            "default_capacity_wh": None,
            "source": TypeSource.API,
            "note": note,
        }
        for code, name, power_control, note in [
            ("Plug Mini (JP)", "プラグミニ（JP）", True, "実機で確認済み"),
            ("Plug Mini (US)", "プラグミニ（US）", True, None),
            ("Plug Mini (EU)", "プラグミニ（EU）", True, None),
            ("Plug", "プラグ", True, None),
            # 電源を入り切りする機器だが、実機で確認するまでは非対応（docs/17 §17.4.2）
            ("Relay Switch 1", "リレースイッチ1", False, RELAY_NOTE),
            ("Relay Switch 1PM", "リレースイッチ1PM", False, RELAY_NOTE),
        ]
    ),
    {
        "vendor": Vendor.JACKERY,
        "code": JACKERY_1000_NEW,
        "name": "Jackery 1000 New",
        "category": DeviceCategory.PORTABLE_POWER,
        "power_control": False,
        "charge_mode": True,
        "default_capacity_wh": 1070.0,
        "source": TypeSource.CUSTOM,
        "note": None,
    },
]


class DeviceType(Base):
    __tablename__ = "device_types"
    __table_args__ = (UniqueConstraint("vendor", "code"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    vendor: Mapped[Vendor] = mapped_column(Enum(Vendor, native_enum=False, length=16))
    code: Mapped[str] = mapped_column(String(64))  # SwitchBot は deviceType の値そのまま
    name: Mapped[str] = mapped_column(String(64))
    category: Mapped[DeviceCategory] = mapped_column(Enum(DeviceCategory, native_enum=False, length=16))
    power_control: Mapped[bool] = mapped_column(Boolean, default=False)  # 制御対象にできるか
    charge_mode: Mapped[bool] = mapped_column(Boolean, default=False)  # 充電モードの変更に対応するか
    default_capacity_wh: Mapped[float | None] = mapped_column(Float, nullable=True)
    source: Mapped[TypeSource] = mapped_column(Enum(TypeSource, native_enum=False, length=8))
    note: Mapped[str | None] = mapped_column(Text, nullable=True)

    @property
    def verified(self) -> bool:
        return (self.vendor, self.code) in VERIFIED_TYPE_CODES


class Device(Base):
    __tablename__ = "devices"
    __table_args__ = (UniqueConstraint("device_type_id", "external_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    device_type_id: Mapped[int] = mapped_column(ForeignKey("device_types.id"))
    external_id: Mapped[str] = mapped_column(String(128))  # メーカー側の機器 ID
    name: Mapped[str] = mapped_column(String(50))
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 給電先のポータブル電源（制御機器のみ。未設定可）
    power_source_id: Mapped[int | None] = mapped_column(ForeignKey("devices.id"), nullable=True)
    # 想定平均負荷（W。制御機器のみ。未設定なら全体の ESTIMATED_AVERAGE_POWER_W）
    estimated_power_w: Mapped[float | None] = mapped_column(Float, nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)  # 制御対象か（外しても行は残す）
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    last_state: Mapped[DeviceState | None] = mapped_column(Enum(DeviceState, native_enum=False, length=8), nullable=True)
    last_state_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime)

    device_type: Mapped[DeviceType] = relationship(lazy="joined")
    power_source: Mapped["Device | None"] = relationship(remote_side=[id], lazy="joined", join_depth=1)
    portable_power: Mapped["PortablePowerSettings | None"] = relationship(back_populates="device", lazy="joined")


class PortablePowerSettings(Base):
    """ポータブル電源ごとの設定（docs/17 §17.4.4）。"""

    __tablename__ = "portable_power_settings"

    device_id: Mapped[int] = mapped_column(ForeignKey("devices.id"), primary_key=True)
    capacity_wh: Mapped[float] = mapped_column(Float)
    min_percent: Mapped[int] = mapped_column(Integer)
    efficiency: Mapped[float] = mapped_column(Float)
    charge_mode_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    last_percent: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_output_w: Mapped[float | None] = mapped_column(Float, nullable=True)
    last_input_w: Mapped[float | None] = mapped_column(Float, nullable=True)
    last_charge_mode: Mapped[str | None] = mapped_column(String(8), nullable=True)
    last_read_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)

    device: Mapped[Device] = relationship(back_populates="portable_power")


# でんき予報のスケジュールの対象機器（devices.id の JSON 配列。docs/17 §17.6.1）
FORECAST_SCHEDULE_DEVICES_KEY = "forecast_schedule_devices"
# 既存の1台の移行（docs/17 §17.4.7）を終えた印
DEVICES_MIGRATED_KEY = "devices_migrated"
DEFAULT_SWITCH_NAME = "スマートプラグ"
DEFAULT_PORTABLE_POWER_NAME = "ポータブル電源"


def seed_device_types(session: Session) -> None:
    """初期登録の種別のうち、未登録のものを追加する（登録済みの行は変更しない）。"""
    existing = {(t.vendor, t.code) for t in session.scalars(select(DeviceType))}
    for values in SEED_DEVICE_TYPES:
        if (values["vendor"], values["code"]) not in existing:
            session.add(DeviceType(**values))
    session.commit()


# --- 参照 ---


def active_switches(session: Session) -> list[Device]:
    """制御対象の制御機器（表示順）。"""
    return list(
        session.scalars(
            select(Device)
            .join(Device.device_type)
            .where(Device.active.is_(True), DeviceType.category == DeviceCategory.POWER_SWITCH)
            .order_by(Device.sort_order, Device.id)
        ).unique()
    )


def primary_switch(session: Session) -> Device | None:
    """現在制御する1台（制御対象の先頭）。複数機器の制御（docs/17 §17.7、Phase 10）までは1台だけ制御する。"""
    switches = active_switches(session)
    return switches[0] if switches else None


def portable_powers(session: Session) -> list[Device]:
    """登録済み（active）のポータブル電源。"""
    return list(
        session.scalars(
            select(Device)
            .join(Device.device_type)
            .where(Device.active.is_(True), DeviceType.category == DeviceCategory.PORTABLE_POWER)
            .order_by(Device.sort_order, Device.id)
        ).unique()
    )


def find_device(session: Session, vendor: Vendor, external_id: str) -> Device | None:
    return session.scalars(
        select(Device).join(Device.device_type).where(DeviceType.vendor == vendor, Device.external_id == external_id)
    ).first()


def find_type(session: Session, vendor: Vendor, code: str) -> DeviceType | None:
    return session.scalars(select(DeviceType).where(DeviceType.vendor == vendor, DeviceType.code == code)).first()


def forecast_schedule_device_ids(session: Session) -> list[int]:
    raw = get_value(session, FORECAST_SCHEDULE_DEVICES_KEY)
    try:
        values = json.loads(raw) if raw else []
    except ValueError:
        return []
    return [int(v) for v in values if isinstance(v, int)]


def set_forecast_schedule_device_ids(session: Session, ids: list[int]) -> None:
    set_value(session, FORECAST_SCHEDULE_DEVICES_KEY, json.dumps(ids))


def next_sort_order(session: Session) -> int:
    return (session.scalar(select(func.max(Device.sort_order))) or 0) + 1


# --- SwitchBot の機器一覧（docs/17 §17.5.2） ---


@dataclass(frozen=True)
class SwitchBotDevice:
    """SwitchBot API の deviceList の1件（画面の左ペイン用）。"""

    external_id: str
    name: str
    type_code: str
    cloud_enabled: bool
    device_type: DeviceType


def ensure_switchbot_types(session: Session, device_list: list[dict[str, Any]]) -> list[SwitchBotDevice]:
    """一覧の deviceType を種別マスターと照合する。未知の種別は非対応（power_control=false）として追加する。"""
    result: list[SwitchBotDevice] = []
    added = False
    for item in device_list:
        external_id = str(item.get("deviceId") or "")
        code = str(item.get("deviceType") or "")
        if not external_id or not code:
            continue
        device_type = find_type(session, Vendor.SWITCHBOT, code)
        if device_type is None:
            device_type = DeviceType(
                vendor=Vendor.SWITCHBOT,
                code=code[:64],
                name=code[:64],
                category=DeviceCategory.POWER_SWITCH,
                power_control=False,
                charge_mode=False,
                source=TypeSource.API,
                note="SwitchBot の機器一覧から自動で追加（非対応）",
            )
            session.add(device_type)
            session.flush()
            added = True
        result.append(
            SwitchBotDevice(
                external_id=external_id,
                name=str(item.get("deviceName") or external_id),
                type_code=code,
                # 項目がない場合は有効とみなす（無効のときだけ false が返る）
                cloud_enabled=item.get("enableCloudService") is not False,
                device_type=device_type,
            )
        )
    if added:
        session.commit()
    return result


# --- 既存の1台の移行（docs/17 §17.4.7） ---


def _now() -> datetime:
    return datetime.now(UTC)


def migrate_legacy_devices(
    session: Session,
    settings: Settings,
    *,
    switchbot_name: Callable[[str], str | None] | None = None,
    jackery_device: Callable[[], tuple[str, str | None] | None] | None = None,
) -> bool:
    """SWITCHBOT_DEVICE_ID・JACKERY_DEVICE_ID と現在の設定値から機器を登録する（初回起動時に1回だけ）。

    - switchbot_name：SwitchBot アプリでの名前を返す（取得できなければ None。名前は「スマートプラグ」にする）
    - jackery_device：JACKERY_DEVICE_ID が空欄のときに、共有されている1台の (機器 ID, 名前) を返す
      （共有されていなければ None、取得に失敗したら例外。失敗した場合は次の起動時にやり直す）
    すでに登録済みの機器は変更しない（何度呼んでもよい）。移行を終えたら True。
    """
    if get_value(session, DEVICES_MIGRATED_KEY) == "true":
        return True
    now = _now()

    plug: Device | None = None
    if settings.switchbot_device_id:
        plug = find_device(session, Vendor.SWITCHBOT, settings.switchbot_device_id)
        if plug is None:
            name = None
            if switchbot_name is not None:
                try:
                    name = switchbot_name(settings.switchbot_device_id)
                except Exception:
                    name = None
            # deviceType は分からない場合もあるため、移行は実機で確認済みのプラグミニ（JP）として登録する
            device_type = find_type(session, Vendor.SWITCHBOT, "Plug Mini (JP)")
            assert device_type is not None, "種別マスターが初期登録されていません（alembic upgrade head）"
            plug = Device(
                device_type_id=device_type.id,
                external_id=settings.switchbot_device_id,
                name=(name or DEFAULT_SWITCH_NAME)[:50],
                active=True,
                sort_order=next_sort_order(session),
                created_at=now,
                updated_at=now,
            )
            session.add(plug)
            session.flush()
            # 機器の登録前の制御履歴・スケジュール・ルールは、この1台のものとして関連付ける
            session.execute(update(ControlLog).where(ControlLog.device_id.is_(None)).values(device_id=plug.id))
            for schedule in session.scalars(select(ControlSchedule).where(~ControlSchedule.devices.any())):
                status = (
                    ScheduleDeviceStatus(schedule.status.value)
                    if schedule.status.value in ScheduleDeviceStatus.__members__
                    else ScheduleDeviceStatus.PENDING
                )
                schedule.devices = [ScheduleDevice(device_id=plug.id, status=status)]
            for rule in session.scalars(select(ScheduleRule).where(~ScheduleRule.devices.any())):
                rule.set_devices([plug.id])
            if get_value(session, FORECAST_SCHEDULE_DEVICES_KEY) is None:
                set_forecast_schedule_device_ids(session, [plug.id])

    completed = True
    if settings.jackery_device_id or settings.jackery_account:
        battery_id, battery_name = settings.jackery_device_id, None
        if not battery_id and jackery_device is not None:
            try:
                found = jackery_device()
            except Exception:
                found, completed = None, False
            if found is not None:
                battery_id, battery_name = found
        if battery_id:
            battery = find_device(session, Vendor.JACKERY, battery_id)
            if battery is None:
                battery = register_portable_power(session, settings, battery_id, battery_name or "Jackery 1000 New", now=now)
            if plug is not None and plug.power_source_id is None:
                plug.power_source_id = battery.id

    session.commit()
    if completed:
        set_value(session, DEVICES_MIGRATED_KEY, "true")
    return completed


def register_portable_power(
    session: Session, settings: Settings, external_id: str, name: str, *, now: datetime | None = None
) -> Device:
    """Jackery のポータブル電源を登録し、現在の設定値で portable_power_settings を作る。"""
    now = now or _now()
    device_type = find_type(session, Vendor.JACKERY, JACKERY_1000_NEW)
    assert device_type is not None, "種別マスターが初期登録されていません（alembic upgrade head）"
    control = load_control_settings(session, settings)
    device = Device(
        device_type_id=device_type.id,
        external_id=external_id,
        name=name[:50] or DEFAULT_PORTABLE_POWER_NAME,
        active=True,
        sort_order=next_sort_order(session),
        created_at=now,
        updated_at=now,
    )
    session.add(device)
    session.flush()
    session.add(
        PortablePowerSettings(
            device_id=device.id,
            capacity_wh=control.battery_capacity_wh,
            min_percent=control.battery_min_percent,
            efficiency=control.round_trip_efficiency,
            charge_mode_enabled=True,
        )
    )
    session.flush()
    return device
