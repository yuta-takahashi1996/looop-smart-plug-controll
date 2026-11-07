"""ORM モデル。Alembic の autogenerate が Base.metadata を参照できるよう、ここで全モデルを import する。"""

from app.models.device import Device, DeviceType, PortablePowerSettings
from app.models.forecast import ForecastPrice, PriceAdjustment
from app.models.schedule import ControlLog, ControlSchedule, DailySummary
from app.models.settings import SystemEvent, SystemSetting

__all__ = [
    "ControlLog",
    "ControlSchedule",
    "DailySummary",
    "Device",
    "DeviceType",
    "ForecastPrice",
    "PortablePowerSettings",
    "PriceAdjustment",
    "SystemEvent",
    "SystemSetting",
]
