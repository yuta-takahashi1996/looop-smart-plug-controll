"""ポータブル電源のクライアントの共通の形（docs/17-multi-device.md §17.9.4）。

メーカーごとのクライアント（現在は Jackery のみ）をこの Protocol に揃え、BatteryMonitor・設定画面は
メーカーを意識せずに使う。操作は残量・出力・入力の取得と充電モード（高速充電／静かな充電）の変更だけ（docs/17 §17.1）。
"""

import enum
from dataclasses import dataclass
from typing import Protocol


class ChargeMode(enum.StrEnum):
    """充電モード。メーカーごとの値への変換は各クライアントで行う（Jackery は cs）。"""

    FAST = "FAST"  # 高速充電
    SILENT = "SILENT"  # 静かな充電

    @property
    def label(self) -> str:
        return "高速充電" if self is ChargeMode.FAST else "静かな充電"


class PortablePowerError(Exception):
    """取得・変更に失敗した（理由はメッセージ。認証情報を含めない）。"""


class PortablePowerUnconfirmed(PortablePowerError):
    """変更を送ったが、再取得で期待した状態を確認できなかった（成功扱いにしない）。"""


@dataclass(frozen=True)
class PortablePowerInfo:
    """メーカーのアカウントで取得できる機器の1件（登録時の選択肢）。"""

    external_id: str
    name: str
    model: str | None = None


class PowerStatus(Protocol):
    """取得した状態。取得できなかった項目は None。"""

    @property
    def remaining_percent(self) -> int | None: ...

    @property
    def output_power_w(self) -> float | None: ...

    @property
    def input_power_w(self) -> float | None: ...

    @property
    def charge_mode(self) -> ChargeMode | None: ...


class PortablePowerClient(Protocol):
    """メーカーごとのクライアント。アカウントはメーカーごとに1つ（環境変数）で、複数台は共有して扱う（docs/17 §17.8.1）。"""

    @property
    def configured(self) -> bool: ...

    def list_devices(self) -> list[PortablePowerInfo]: ...

    def read(self, external_id: str) -> PowerStatus: ...

    def set_charge_mode(self, external_id: str, mode: ChargeMode) -> PowerStatus:
        """充電モードを変更し、再取得して照合する（照合できなければ PortablePowerUnconfirmed）。"""
        ...
