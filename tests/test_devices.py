"""機器の登録（docs/17-multi-device.md §17.4・§17.5、Phase 9）。SwitchBot・Jackery は模擬し、実機には送信しない。"""

from datetime import UTC, datetime, time, timedelta

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.clients import switchbot
from app.clients.switchbot import SwitchBotClient
from app.config import get_settings
from app.main import app
from app.models import device as device_models
from app.models.device import Device, DeviceCategory, DeviceType, PortablePowerSettings, Vendor
from app.models.schedule import (
    ControlLog,
    ControlResult,
    ControlSchedule,
    ControlSource,
    EnabledBy,
    PlugState,
    RepeatType,
    ScheduleDevice,
    ScheduleDeviceStatus,
    ScheduleRule,
    ScheduleSource,
    ScheduleStatus,
)
from app.models.settings import get_value
from app.repositories.database import SessionLocal
from app.services.controller import BatteryMonitor, ControlService, PlugMonitor
from tests.conftest import csrf_token, today

DEVICE_LIST = [
    {"deviceId": "PLUG1", "deviceName": "リビングのプラグ", "deviceType": "Plug Mini (JP)", "enableCloudService": True},
    {"deviceId": "PLUG2", "deviceName": "書斎のプラグ", "deviceType": "Plug Mini (US)", "enableCloudService": True},
    {"deviceId": "RELAY", "deviceName": "給湯器", "deviceType": "Relay Switch 1", "enableCloudService": True},
    {"deviceId": "PLUG3", "deviceName": "寝室のプラグ", "deviceType": "Plug Mini (EU)", "enableCloudService": False},
    {"deviceId": "METER", "deviceName": "温湿度計", "deviceType": "Meter", "enableCloudService": True},
]


def _settings(**update: object):
    return get_settings().model_copy(update=update)


def _log(session: Session) -> ControlLog:
    log = ControlLog(
        executed_at=datetime.now(UTC),
        source=ControlSource.AUTO,
        requested_state=PlugState.OFF,
        result=ControlResult.DRY_RUN,
        reason="移行前",
        dry_run=True,
    )
    session.add(log)
    session.commit()
    return log


def _add_switch(session: Session, external_id: str, name: str, *, source: Device | None = None) -> Device:
    device_type = device_models.find_type(session, Vendor.SWITCHBOT, "Plug Mini (JP)")
    now = datetime.now(UTC)
    device = Device(
        device_type_id=device_type.id,
        external_id=external_id,
        name=name,
        active=True,
        sort_order=device_models.next_sort_order(session),
        power_source_id=source.id if source else None,
        created_at=now,
        updated_at=now,
    )
    session.add(device)
    session.commit()
    return device


# --- 移行（docs/17 §17.4.7） ---


def test_migration_registers_legacy_plug_and_battery(session: Session) -> None:
    old_log = _log(session)
    settings = _settings(switchbot_device_id="PLUG1", jackery_device_id="JK1", jackery_account="reader@example.com")

    assert device_models.migrate_legacy_devices(session, settings, switchbot_name=lambda _: "リビングのプラグ")

    plug = device_models.find_device(session, Vendor.SWITCHBOT, "PLUG1")
    battery = device_models.find_device(session, Vendor.JACKERY, "JK1")
    assert plug.name == "リビングのプラグ"
    assert plug.active and plug.device_type.code == "Plug Mini (JP)"
    assert battery.device_type.category == DeviceCategory.PORTABLE_POWER
    assert plug.power_source_id == battery.id
    # 現在の設定値でポータブル電源の設定を作る
    power = session.get(PortablePowerSettings, battery.id)
    assert (power.capacity_wh, power.min_percent, power.efficiency) == (1070, 5, 0.85)
    assert device_models.forecast_schedule_device_ids(session) == [plug.id]
    session.refresh(old_log)
    assert old_log.device_id == plug.id  # 既存の制御履歴を移行した機器に関連付ける

    # 2回目以降は何もしない
    assert device_models.migrate_legacy_devices(session, settings)
    assert len(list(session.scalars(select(Device)))) == 2


def test_migration_associates_existing_schedules_and_rules(session: Session) -> None:
    """機器の登録前に作られたスケジュール・ルールは、移行した1台のものにする（docs/17 §17.4.7 の 4）。"""
    now = datetime.now(UTC)
    done = ControlSchedule(
        schedule_date=today(), schedule_version=1, start_at=now - timedelta(hours=2), end_at=now - timedelta(hours=1),
        source_level=ScheduleSource.WARNING, enabled=True, enabled_by=EnabledBy.AUTO, reason="警報", estimated_saving=1.0,
        status=ScheduleStatus.DONE, updated_at=now,
    )  # fmt: skip
    rule = ScheduleRule(
        name="昼", target_state="OFF", repeat_type=RepeatType.DAILY, start_time=time(12), end_time=time(13), enabled=True,
        created_at=now, updated_at=now,
    )  # fmt: skip
    session.add_all([done, rule])
    session.commit()

    device_models.migrate_legacy_devices(session, _settings(switchbot_device_id="PLUG1"))

    plug = device_models.find_device(session, Vendor.SWITCHBOT, "PLUG1")
    session.expire_all()
    done = session.get(ControlSchedule, done.id)
    assert done.device_ids == [plug.id] and done.devices[0].status == ScheduleDeviceStatus.DONE
    assert session.get(ScheduleRule, rule.id).device_ids == [plug.id]


def test_migration_uses_default_name_and_skips_without_env(session: Session) -> None:
    def fail(_: str) -> str:
        raise switchbot.SwitchBotError("offline")

    device_models.migrate_legacy_devices(session, _settings(switchbot_device_id="PLUG1"), switchbot_name=fail)
    assert device_models.find_device(session, Vendor.SWITCHBOT, "PLUG1").name == "スマートプラグ"


def test_migration_without_env_registers_nothing(session: Session) -> None:
    assert device_models.migrate_legacy_devices(session, _settings())
    assert list(session.scalars(select(Device))) == []
    assert get_value(session, device_models.DEVICES_MIGRATED_KEY) == "true"


def test_migration_retries_battery_when_listing_fails(session: Session) -> None:
    settings = _settings(switchbot_device_id="PLUG1", jackery_account="reader@example.com")

    def broken() -> tuple[str, str | None]:
        raise RuntimeError("Jackery API に接続できませんでした")

    assert not device_models.migrate_legacy_devices(session, settings, jackery_device=broken)
    assert device_models.find_device(session, Vendor.SWITCHBOT, "PLUG1") is not None

    # 次の起動時にやり直す（共有されている1台を登録する）。プラグは重複して登録しない
    assert device_models.migrate_legacy_devices(session, settings, jackery_device=lambda: ("JK9", "Jackery"))
    plug = device_models.find_device(session, Vendor.SWITCHBOT, "PLUG1")
    assert plug.power_source_id == device_models.find_device(session, Vendor.JACKERY, "JK9").id
    assert len(device_models.active_switches(session)) == 1


def test_unknown_switchbot_type_is_added_as_unsupported(session: Session) -> None:
    listed = device_models.ensure_switchbot_types(session, DEVICE_LIST)

    meter = next(d for d in listed if d.external_id == "METER")
    assert not meter.device_type.power_control
    assert meter.device_type.source == "API"
    relay = next(d for d in listed if d.external_id == "RELAY")
    assert not relay.device_type.power_control  # リレースイッチは実機で確認するまで非対応（docs/17 §17.4.2）
    assert not next(d for d in listed if d.external_id == "PLUG3").cloud_enabled
    plug = next(d for d in listed if d.external_id == "PLUG2")
    assert plug.device_type.power_control and not plug.device_type.verified
    # 2回目は追加しない
    device_models.ensure_switchbot_types(session, DEVICE_LIST)
    assert len(list(session.scalars(select(DeviceType).where(DeviceType.code == "Meter")))) == 1


# --- 制御する機器の読み込み（docs/17 §17.7） ---


class FakeClient:
    """機器ごとの状態を持つ SwitchBot の模擬。"""

    credentials_configured = True

    def __init__(self) -> None:
        self.power: dict[str, str] = {}
        self.commands: list[tuple[str, str]] = []

    def get_status(self, device_id: str | None = None) -> switchbot.PlugStatus:
        return switchbot.PlugStatus(power=self.power.get(device_id or "", "ON"), device_type="Plug Mini (JP)", raw={})  # type: ignore[arg-type]

    def send_power_command(self, state: str, device_id: str | None = None) -> None:
        self.commands.append((device_id or "", state))
        self.power[device_id or ""] = state


class FakeJackery:
    configured = True


def test_bind_devices_loads_all_active_switches_and_their_batteries(session: Session) -> None:
    battery = device_models.register_portable_power(session, get_settings(), "JK1", "Jackery")
    first = _add_switch(session, "PLUG1", "リビング", source=battery)
    second = _add_switch(session, "PLUG2", "書斎")
    created: list[tuple[str, int, str]] = []

    def factory(external_id: str, device_id: int, name: str) -> BatteryMonitor:
        created.append((external_id, device_id, name))
        return BatteryMonitor(FakeJackery(), external_id, SessionLocal, owner_id=device_id, name=name)  # type: ignore[arg-type]

    control = ControlService(get_settings(), SessionLocal, switchbot=FakeClient(), battery_factory=factory)  # type: ignore[arg-type]
    control.bind_devices(session)

    assert [d.name for d in control.devices] == ["リビング", "書斎"]
    assert created == [("JK1", battery.id, "Jackery")]
    assert control.power_source(first.power_source_id).monitor.device_id == "JK1"
    assert control.device(second.id).power_source_id is None  # 給電先が未設定なら残量は取得できない扱い
    control.bind_devices(session)
    assert len(created) == 1  # 同じポータブル電源の BatteryMonitor は作り直さない
    log = control.request(session, PlugState.ON, ControlSource.SAFETY, "テスト", device_id=second.id)
    assert log.device_id == second.id


def test_removed_device_state_is_dropped(session: Session) -> None:
    plug = _add_switch(session, "PLUG1", "リビング")
    control = ControlService(get_settings(), SessionLocal, switchbot=FakeClient())  # type: ignore[arg-type]
    control.bind_devices(session)
    control.request(session, PlugState.OFF, ControlSource.MANUAL, "テスト", device_id=plug.id)
    assert control.device_state(plug.id) == PlugState.OFF

    plug.active = False
    session.commit()
    control.bind_devices(session)

    assert control.devices == []
    with pytest.raises(ValueError):
        control.request(session, PlugState.ON, ControlSource.SAFETY, "テスト", device_id=plug.id)


def test_plug_monitor_saves_last_state(session: Session) -> None:
    plug = _add_switch(session, "PLUG1", "リビング")
    PlugMonitor(FakeClient(), SessionLocal).refresh()  # type: ignore[arg-type]

    session.refresh(plug)
    assert plug.last_state == "ON"
    assert plug.last_state_at is not None


# --- 画面（docs/17 §17.5） ---


@pytest.fixture
def switchbot_api(logged_in_client: TestClient):
    """SwitchBot API を模擬した Control Service に差し替える。送信したコマンドを記録する。"""
    state = {"power": "ON", "list_calls": 0, "commands": []}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/devices"):
            state["list_calls"] += 1
            body = {"deviceList": DEVICE_LIST, "infraredRemoteList": []}
        elif request.url.path.endswith("/status"):
            body = {"power": state["power"].lower(), "deviceType": "Plug Mini (JP)"}
        else:
            state["commands"].append(request.url.path)
            body = {}
        return httpx.Response(200, json={"statusCode": 100, "body": body, "message": "success"})

    http = httpx.Client(base_url=switchbot.BASE_URL, transport=httpx.MockTransport(handler))
    client = SwitchBotClient("token", "secret", "", client=http)
    app.state.switchbot = client
    app.state.plug_monitor = PlugMonitor(client, SessionLocal)
    app.state.control = ControlService(
        get_settings(),
        SessionLocal,
        switchbot=client,
        battery_factory=lambda ext, device_id, name: BatteryMonitor(FakeJackery(), ext, SessionLocal, owner_id=device_id, name=name),  # type: ignore[arg-type]
        sleep=lambda _: None,
    )
    return state


def _token(client: TestClient) -> str:
    return csrf_token(client)


def test_devices_page_lists_switchbot_devices(logged_in_client: TestClient, switchbot_api: dict) -> None:
    response = logged_in_client.get("/devices")

    assert response.status_code == 200
    assert "リビングのプラグ" in response.text
    # 非対応の種別（温湿度計・リレースイッチ）は一覧に出さず、件数だけ表示する
    assert "非対応の機器 2台" in response.text
    assert "クラウドサービスが無効" in response.text  # プラグミニ（EU）
    assert "実機での確認が済んでいない種別です" in response.text  # プラグミニ（US）
    # 一覧は24時間以内なら再取得しない
    logged_in_client.get("/devices")
    assert switchbot_api["list_calls"] == 1
    logged_in_client.post("/devices/refresh", data={"csrf_token": _token(logged_in_client)})
    assert switchbot_api["list_calls"] == 2


def test_devices_page_shows_error_when_not_configured(logged_in_client: TestClient) -> None:
    response = logged_in_client.get("/devices")

    assert response.status_code == 200
    assert "SWITCHBOT_TOKEN" in response.text


def test_save_adds_devices_to_control(logged_in_client: TestClient, switchbot_api: dict, session: Session) -> None:
    response = logged_in_client.post(
        "/devices", data={"csrf_token": _token(logged_in_client), "active": ["PLUG1", "PLUG2"]}, follow_redirects=False
    )

    assert response.status_code == 303
    switches = device_models.active_switches(session)
    assert [(d.external_id, d.name) for d in switches] == [("PLUG1", "リビングのプラグ"), ("PLUG2", "書斎のプラグ")]
    assert switches[0].power_source_id is None and switches[0].estimated_power_w is None
    assert [d.id for d in app.state.control.devices] == [d.id for d in switches]  # 制御対象はすべて制御する


@pytest.mark.parametrize(("external_id", "message"), [("METER", "対応していない"), ("RELAY", "対応していない"), ("PLUG3", "クラウドサービスが無効")])
def test_save_rejects_unsupported_devices(
    logged_in_client: TestClient, switchbot_api: dict, session: Session, external_id: str, message: str
) -> None:
    response = logged_in_client.post("/devices", data={"csrf_token": _token(logged_in_client), "active": [external_id]})

    assert response.status_code == 422
    assert message in response.text
    assert device_models.active_switches(session) == []


def test_remove_keeps_row_and_reactivates_same_row(
    logged_in_client: TestClient, switchbot_api: dict, session: Session
) -> None:
    token = _token(logged_in_client)
    logged_in_client.post("/devices", data={"csrf_token": token, "active": ["PLUG1"]})
    plug = device_models.find_device(session, Vendor.SWITCHBOT, "PLUG1")
    device_models.set_forecast_schedule_device_ids(session, [plug.id])

    logged_in_client.post("/devices", data={"csrf_token": token})

    session.expire_all()
    assert not session.get(Device, plug.id).active
    assert device_models.forecast_schedule_device_ids(session) == []
    assert app.state.control.device(plug.id) is None
    logged_in_client.post("/devices", data={"csrf_token": token, "active": ["PLUG1"]})
    session.expire_all()
    assert session.get(Device, plug.id).active
    assert len(list(session.scalars(select(Device)))) == 1


def _running_schedule(session: Session, device_id: int) -> ControlSchedule:
    now = datetime.now(UTC)
    schedule = ControlSchedule(
        devices=[ScheduleDevice(device_id=device_id, status=ScheduleDeviceStatus.RUNNING, executed_start_at=now)],
        schedule_date=today(),
        schedule_version=1,
        start_at=now - timedelta(minutes=10),
        end_at=now + timedelta(minutes=50),
        source_level=ScheduleSource.WARNING,
        enabled=True,
        enabled_by=EnabledBy.AUTO,
        reason="でんき警報",
        estimated_saving=1.0,
        status=ScheduleStatus.RUNNING,
        updated_at=now,
    )
    session.add(schedule)
    session.commit()
    return schedule


def test_cannot_remove_controlling_device_while_schedule_running(
    logged_in_client: TestClient, switchbot_api: dict, session: Session
) -> None:
    token = _token(logged_in_client)
    logged_in_client.post("/devices", data={"csrf_token": token, "active": ["PLUG1", "PLUG2"]})
    plug = device_models.find_device(session, Vendor.SWITCHBOT, "PLUG1")
    _running_schedule(session, plug.id)

    # 実行中のスケジュールの対象ではない機器は外せる
    response = logged_in_client.post("/devices", data={"csrf_token": token, "active": ["PLUG1"]}, follow_redirects=False)
    assert response.status_code == 303

    response = logged_in_client.post("/devices", data={"csrf_token": token})

    assert response.status_code == 422
    assert "実行中のスケジュールが終わってから" in response.text
    assert len(device_models.active_switches(session)) == 1


def test_removing_device_updates_pending_schedules_and_rules(
    logged_in_client: TestClient, switchbot_api: dict, session: Session
) -> None:
    """外した機器は開始前のスケジュール・ルールから外し、0台になったら未実行・無効にする（docs/17 §17.5.3）。"""
    token = _token(logged_in_client)
    logged_in_client.post("/devices", data={"csrf_token": token, "active": ["PLUG1", "PLUG2"]})
    plug1 = device_models.find_device(session, Vendor.SWITCHBOT, "PLUG1")
    plug2 = device_models.find_device(session, Vendor.SWITCHBOT, "PLUG2")
    now = datetime.now(UTC)
    both = ControlSchedule(
        schedule_date=today() + timedelta(days=1), schedule_version=1, start_at=now + timedelta(days=1), end_at=now + timedelta(days=1, hours=1),
        source_level=ScheduleSource.WARNING, enabled=True, enabled_by=EnabledBy.MANUAL, manually_modified=True, reason="警報",
        estimated_saving=1.0, updated_at=now,
        devices=[ScheduleDevice(device_id=plug1.id), ScheduleDevice(device_id=plug2.id)],
    )  # fmt: skip
    only1 = ControlSchedule(
        schedule_date=today() + timedelta(days=1), schedule_version=1, start_at=now + timedelta(days=1, hours=2), end_at=now + timedelta(days=1, hours=3),
        source_level=ScheduleSource.WARNING, enabled=True, enabled_by=EnabledBy.MANUAL, manually_modified=True, reason="警報",
        estimated_saving=1.0, updated_at=now, devices=[ScheduleDevice(device_id=plug1.id)],
    )  # fmt: skip
    rule = ScheduleRule(
        name="昼", target_state="OFF", repeat_type=RepeatType.DAILY, start_time=time(12), end_time=time(13), enabled=True,
        created_at=now, updated_at=now,
    )  # fmt: skip
    rule.set_devices([plug1.id])
    session.add_all([both, only1, rule])
    session.commit()

    page = logged_in_client.get("/devices").text
    assert "開始前のスケジュール 2件から外します" in page and "任意スケジュールのルール 1件から外します" in page
    logged_in_client.post("/devices", data={"csrf_token": token, "active": ["PLUG2"]})

    session.expire_all()
    assert session.get(ControlSchedule, both.id).device_ids == [plug2.id]
    assert session.get(ControlSchedule, only1.id).status == ScheduleStatus.SKIPPED
    rule = session.get(ScheduleRule, rule.id)
    assert rule.device_ids == [] and not rule.enabled


def test_removing_off_device_turns_it_on_first(logged_in_client: TestClient, switchbot_api: dict, session: Session) -> None:
    token = _token(logged_in_client)
    logged_in_client.post("/devices", data={"csrf_token": token, "active": ["PLUG1"]})
    control = app.state.control
    control.bind_devices(session)
    control.request(session, PlugState.OFF, ControlSource.MANUAL, "手動 OFF")  # Dry Run の想定状態で OFF

    response = logged_in_client.post("/devices", data={"csrf_token": token}, follow_redirects=False)

    assert response.status_code == 303
    session.expire_all()
    last = session.scalars(select(ControlLog).order_by(ControlLog.id.desc())).first()
    assert last.requested_state == PlugState.ON
    assert "制御対象から外すため" in last.reason
    assert device_models.active_switches(session) == []


def test_edit_device_sets_power_source(logged_in_client: TestClient, switchbot_api: dict, session: Session) -> None:
    token = _token(logged_in_client)
    logged_in_client.post("/devices", data={"csrf_token": token, "active": ["PLUG1"]})
    plug = device_models.find_device(session, Vendor.SWITCHBOT, "PLUG1")
    battery = device_models.register_portable_power(session, get_settings(), "JK1", "Jackery 1000 New")
    session.commit()

    response = logged_in_client.post(
        f"/devices/{plug.id}",
        data={"csrf_token": token, "name": "リビング", "note": "テレビ", "power_source_id": str(battery.id), "estimated_power_w": "120"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    session.expire_all()
    plug = session.get(Device, plug.id)
    assert (plug.name, plug.note, plug.power_source_id, plug.estimated_power_w) == ("リビング", "テレビ", battery.id, 120)
    assert app.state.control.power_source(battery.id).monitor.device_id == "JK1"
    assert "給電先：Jackery 1000 New" in logged_in_client.get("/devices").text


def test_edit_device_validation(logged_in_client: TestClient, switchbot_api: dict, session: Session) -> None:
    token = _token(logged_in_client)
    logged_in_client.post("/devices", data={"csrf_token": token, "active": ["PLUG1"]})
    plug = device_models.find_device(session, Vendor.SWITCHBOT, "PLUG1")
    battery = device_models.register_portable_power(session, get_settings(), "JK1", "Jackery 1000 New")
    session.commit()

    response = logged_in_client.post(f"/devices/{plug.id}", data={"csrf_token": token, "name": "", "power_source_id": "999"})
    assert response.status_code == 422
    assert "名前" in response.text

    # 実行中のスケジュールがある間は給電先を変更できない
    _running_schedule(session, plug.id)
    response = logged_in_client.post(
        f"/devices/{plug.id}", data={"csrf_token": token, "name": "リビング", "power_source_id": str(battery.id)}
    )
    assert response.status_code == 422
    assert "実行中のスケジュールが終わってから変更" in response.text


def test_devices_page_requires_login(client: TestClient) -> None:
    response = client.get("/devices", follow_redirects=False)
    assert response.status_code in (302, 303)
