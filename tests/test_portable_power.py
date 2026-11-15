"""複数のポータブル電源（docs/17-multi-device.md §17.8、Phase 11）。ポータブル電源のクライアントは模擬し、実機には送信しない。"""

from dataclasses import dataclass
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.clients.portable_power import ChargeMode, PortablePowerError, PortablePowerInfo
from app.config import get_settings
from app.main import app
from app.models import device as device_models
from app.models.device import Device, Vendor
from app.models.schedule import ControlSource, PlugState
from app.repositories.database import SessionLocal
from app.services.controller import BatteryMonitor, ControlService
from tests.conftest import csrf_token, register_plug


@dataclass
class Status:
    remaining_percent: int | None
    output_power_w: float | None
    input_power_w: float | None = None
    charge_mode: ChargeMode | None = ChargeMode.FAST


class FakePortablePower:
    """メーカーのクライアントの模擬（app/clients/portable_power.py の形）。"""

    def __init__(self, devices: list[PortablePowerInfo] | None = None) -> None:
        self.configured = True
        self.devices = devices if devices is not None else [PortablePowerInfo("JK1", "Jackery 1000 New"), PortablePowerInfo("JK2", "書斎の Jackery", "1000 New")]
        self.list_error: PortablePowerError | None = None
        self.percent: dict[str, int] = {}

    def list_devices(self) -> list[PortablePowerInfo]:
        if self.list_error is not None:
            raise self.list_error
        return list(self.devices)

    def read(self, external_id: str) -> Status:
        return Status(self.percent.get(external_id, 80), 120.0, 0.0)

    def set_charge_mode(self, external_id: str, mode: ChargeMode) -> Status:
        return Status(self.percent.get(external_id, 80), 120.0, 0.0, mode)


@pytest.fixture
def portable(logged_in_client: TestClient) -> FakePortablePower:
    fake = FakePortablePower()
    app.state.portable_power_clients = {Vendor.JACKERY: fake}
    app.state.control = ControlService(
        get_settings(),
        SessionLocal,
        battery_factory=lambda ext, device_id, name: BatteryMonitor(fake, ext, SessionLocal, owner_id=device_id, name=name),  # type: ignore[arg-type]
    )
    return fake


def _post(client: TestClient, path: str, data: dict | None = None):
    return client.post(path, data={**(data or {}), "csrf_token": csrf_token(client)})


def _form(name: str = "Jackery 1000 New", **extra: object) -> dict:
    return {"name": name, "capacity_wh": "1070", "min_percent": "5", "efficiency_percent": "85", "charge_mode_enabled": "on", **extra}


def _source(session: Session, external_id: str = "JK1") -> Device:
    session.expire_all()
    device = device_models.find_device(session, Vendor.JACKERY, external_id)
    assert device is not None
    return device


# --- 設定画面（docs/17 §17.8.2） ---


def test_settings_page_shows_portable_power_blocks(logged_in_client: TestClient, portable: FakePortablePower, session: Session) -> None:
    register_plug(session)

    text = logged_in_client.get("/settings").text

    assert 'id="portable-powers"' in text
    assert "Jackery 1000 New" in text and "ID: …JK1" in text
    assert 'name="linked_ids"' in text and "リビングのプラグ" in text
    assert "＋ ポータブル電源を追加" in text
    assert '<option value="JACKERY">Jackery</option>' in text


def test_vendor_labels() -> None:
    assert [v.label for v in Vendor] == ["SwitchBot", "Jackery", "FOSSiBOT"]


def test_add_portable_power_lists_only_unregistered_devices(logged_in_client: TestClient, portable: FakePortablePower, session: Session) -> None:
    register_plug(session)  # JK1 を登録済み

    text = logged_in_client.get("/settings/portable-powers/new?vendor=JACKERY").text

    assert 'value="JK2"' in text and 'value="JK1"' not in text
    assert 'name="capacity_wh" type="number" min="1" step="1" required value="1070"' in text  # 種別の公称容量


def test_add_portable_power(logged_in_client: TestClient, portable: FakePortablePower, session: Session) -> None:
    plug = register_plug(session, battery=False)

    response = _post(
        logged_in_client,
        "/settings/portable-powers",
        {"vendor": "JACKERY", "external_id": "JK2", **_form("書斎の Jackery", capacity_wh="2000", min_percent="20", efficiency_percent="80"), "linked_ids": str(plug.id)},
    )

    assert "書斎の Jackery を追加しました" in response.text
    source = _source(session, "JK2")
    assert (source.portable_power.capacity_wh, source.portable_power.min_percent, source.portable_power.efficiency) == (2000, 20, 0.8)
    session.refresh(plug)
    assert plug.power_source_id == source.id
    control: ControlService = app.state.control
    assert control.power_source(source.id).limits.min_percent == 20  # 判定に使う値をすぐに読み直す


@pytest.mark.parametrize(
    ("data", "message"),
    [
        ({"external_id": "JK9"}, "機器一覧に見つかりません"),
        ({"external_id": "JK1"}, "すでに登録されています"),
        ({"external_id": "JK2", "name": "Jackery 1000 New"}, "同じ名前は使えません"),
        ({"external_id": "JK2", "min_percent": "120"}, "残量の下限"),
        ({"external_id": "JK2", "name": ""}, "名前"),
    ],
)
def test_add_portable_power_validation(logged_in_client: TestClient, portable: FakePortablePower, session: Session, data: dict, message: str) -> None:
    register_plug(session)

    response = _post(logged_in_client, "/settings/portable-powers", {"vendor": "JACKERY", **_form("新しい電源"), **data})

    assert "追加できませんでした" in response.text and message in response.text
    assert device_models.find_device(session, Vendor.JACKERY, "JK2") is None


def test_add_requires_configured_vendor(logged_in_client: TestClient, portable: FakePortablePower) -> None:
    portable.configured = False

    assert "環境変数" in logged_in_client.get("/settings").text
    assert "アカウントが未設定" in logged_in_client.get("/settings/portable-powers/new?vendor=JACKERY").text
    response = _post(logged_in_client, "/settings/portable-powers", {"vendor": "JACKERY", "external_id": "JK2", **_form("x")})
    assert "アカウントが未設定" in response.text


def test_device_list_error_is_shown(logged_in_client: TestClient, portable: FakePortablePower) -> None:
    portable.list_error = PortablePowerError("通信エラー")

    assert "機器の一覧を取得できませんでした：通信エラー" in logged_in_client.get("/settings/portable-powers/new?vendor=JACKERY").text


def test_edit_portable_power_and_move_linked_plug(logged_in_client: TestClient, portable: FakePortablePower, session: Session) -> None:
    plug = register_plug(session)
    second = device_models.register_portable_power(session, get_settings(), "JK2", "書斎の Jackery")
    session.commit()

    # 別のポータブル電源に紐づいているプラグを選ぶと、紐づけ先を移す
    response = _post(logged_in_client, f"/settings/portable-powers/{second.id}", _form("書斎の Jackery", min_percent="30", linked_ids=str(plug.id)))

    assert "書斎の Jackery を保存しました" in response.text
    session.expire_all()
    assert session.get(Device, plug.id).power_source_id == second.id
    assert session.get(Device, second.id).portable_power.min_percent == 30
    control: ControlService = app.state.control
    assert control.device(plug.id).power_source_id == second.id

    # チェックを外すと紐づけを解除する
    _post(logged_in_client, f"/settings/portable-powers/{second.id}", _form("書斎の Jackery"))
    session.expire_all()
    assert session.get(Device, plug.id).power_source_id is None


def test_edit_rejects_duplicate_name(logged_in_client: TestClient, portable: FakePortablePower, session: Session) -> None:
    register_plug(session)
    second = device_models.register_portable_power(session, get_settings(), "JK2", "書斎の Jackery")
    session.commit()

    response = _post(logged_in_client, f"/settings/portable-powers/{second.id}", _form("Jackery 1000 New"))

    assert "同じ名前は使えません" in response.text
    session.expire_all()
    assert session.get(Device, second.id).name == "書斎の Jackery"


def test_charge_mode_can_be_disabled_per_portable_power(logged_in_client: TestClient, portable: FakePortablePower, session: Session) -> None:
    register_plug(session)
    source = _source(session)
    form = _form()
    form.pop("charge_mode_enabled")

    _post(logged_in_client, f"/settings/portable-powers/{source.id}", {**form, "linked_ids": str(device_models.active_switches(session)[0].id)})

    assert _source(session).portable_power.charge_mode_enabled is False
    control: ControlService = app.state.control
    assert control.power_source(source.id).charge_mode_enabled is False


def test_delete_portable_power_unlinks_plugs(logged_in_client: TestClient, portable: FakePortablePower, session: Session) -> None:
    plug = register_plug(session)
    source = _source(session)

    text = logged_in_client.get("/settings").text
    assert "紐づくプラグ 1台の給電先を未設定に戻してから削除します。" in text

    response = _post(logged_in_client, f"/settings/portable-powers/{source.id}/delete")

    assert "削除しました（紐づくプラグ 1台の給電先を未設定にしました）" in response.text
    session.expire_all()
    assert session.get(Device, plug.id).power_source_id is None
    assert session.get(Device, source.id).active is False  # 行は残す
    assert app.state.control.power_source(source.id) is None

    # 削除した機器を追加し直せる
    response = _post(logged_in_client, "/settings/portable-powers", {"vendor": "JACKERY", "external_id": "JK1", **_form("戻した Jackery")})
    assert "戻した Jackery を追加しました" in response.text
    assert _source(session).active is True


def test_power_source_of_off_device_cannot_be_deleted(logged_in_client: TestClient, portable: FakePortablePower, session: Session) -> None:
    plug = register_plug(session)
    source = _source(session)
    control: ControlService = app.state.control
    control.bind_devices(session)
    control.request(session, PlugState.OFF, ControlSource.MANUAL, "テスト", device_id=plug.id)

    response = _post(logged_in_client, f"/settings/portable-powers/{source.id}/delete")

    assert "削除できません" in response.text
    assert _source(session).active is True


# --- 残量の取得・ダッシュボード ---


def test_reading_is_saved_to_portable_power_settings(portable: FakePortablePower, session: Session) -> None:
    register_plug(session)
    source = _source(session)
    portable.percent["JK1"] = 64
    monitor = BatteryMonitor(portable, "JK1", SessionLocal, owner_id=source.id, name=source.name)  # type: ignore[arg-type]

    reading = monitor.read(datetime.now(UTC))

    assert reading is not None and reading.percent == 64
    pp = _source(session).portable_power
    assert (pp.last_percent, pp.last_output_w, pp.last_charge_mode) == (64, 120.0, "FAST")
    assert pp.last_read_at is not None


def test_dashboard_shows_portable_power_without_linked_plug(logged_in_client: TestClient, portable: FakePortablePower, session: Session) -> None:
    register_plug(session)
    device_models.register_portable_power(session, get_settings(), "JK2", "予備の Jackery")
    session.commit()

    text = logged_in_client.get("/").text

    assert "予備の Jackery" in text
    control: ControlService = app.state.control
    response = _post(logged_in_client, f"/control/charge-mode/{_source(session, 'JK2').id}/silent")
    assert response.status_code == 200
    assert control.charge_mode_for(_source(session, "JK2").id) == ChargeMode.SILENT  # Dry Run の想定
