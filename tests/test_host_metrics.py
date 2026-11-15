from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services import host_metrics
from app.services.host_metrics import (
    HostMetricsCollector,
    HostMetricsReader,
    RawSample,
    compute,
)

MEMINFO = "MemTotal:        8000000 kB\nMemFree:         1000000 kB\nMemAvailable:    6000000 kB\n"
NET_HEADER = "Inter-|   Receive |  Transmit\n face |bytes packets errs drop fifo frame compressed multicast|bytes packets\n"


def net_line(name: str, rx: int, tx: int) -> str:
    return f"{name}: {rx} 0 0 0 0 0 0 0 {tx} 0 0 0 0 0 0 0\n"


class FakeHost:
    """ホストの /proc・/sys を模擬する（ファイルの内容を書き換えて2回目の取得に使う）。"""

    def __init__(self, root: Path) -> None:
        self.proc = root / "proc"
        self.sys = root / "sys"
        (self.proc / "1" / "net").mkdir(parents=True)
        (self.proc / "net").mkdir()
        for name in ("nvme0n1", "loop0", "zram0", "mmcblk0"):
            (self.sys / "block" / name).mkdir(parents=True)
        (self.sys / "class" / "thermal" / "thermal_zone0").mkdir(parents=True)
        (self.proc / "meminfo").write_text(MEMINFO)
        (self.proc / "net" / "dev").write_text(NET_HEADER + net_line("eth0", 1, 1))  # コンテナ自身の値
        self.set(cpu=(0, 0), cores=[(0, 0), (0, 0)], disk=(0, 0), net=(0, 0), temp="52500")

    def set(self, *, cpu, cores, disk, net, temp) -> None:
        def cpu_line(name: str, busy: int, idle: int) -> str:
            # user nice system idle iowait irq softirq steal guest guest_nice
            return f"{name} {busy} 0 0 {idle} 0 0 0 0 7 7\n"

        lines = [cpu_line("cpu", *cpu)] + [cpu_line(f"cpu{i}", *c) for i, c in enumerate(cores)]
        (self.proc / "stat").write_text("".join(lines) + "intr 1 2 3\nctxt 4\n")
        read, write = disk
        (self.proc / "diskstats").write_text(
            f" 259 0 nvme0n1 10 0 {read} 0 20 0 {write} 0 0 0 0\n"
            f" 259 1 nvme0n1p1 10 0 {read} 0 20 0 {write} 0 0 0 0\n"  # パーティションは数えない
            f"   7 0 loop0 1 0 99999 0 1 0 99999 0 0 0 0\n"
            f" 252 0 zram0 1 0 99999 0 1 0 99999 0 0 0 0\n"
        )
        rx, tx = net
        (self.proc / "1" / "net" / "dev").write_text(
            NET_HEADER
            + net_line("lo", 999999, 999999)
            + net_line("eth0", rx, tx)
            + net_line("docker0", 999999, 999999)
            + net_line("veth1234", 999999, 999999)
            + net_line("tailscale0", 999999, 999999)
        )
        (self.sys / "class" / "thermal" / "thermal_zone0" / "temp").write_text(f"{temp}\n")

    def reader(self) -> HostMetricsReader:
        return HostMetricsReader(proc_path=str(self.proc), sys_path=str(self.sys), storage_path=str(self.proc))


@pytest.fixture
def host(tmp_path: Path) -> FakeHost:
    return FakeHost(tmp_path)


def test_reader_and_compute(host: FakeHost) -> None:
    reader = host.reader()
    before = reader.read(now=100.0)
    host.set(cpu=(300, 100), cores=[(150, 50), (0, 200)], disk=(4096, 8192), net=(2_000_000, 400_000), temp="61250")
    after = reader.read(now=102.0)

    sample = compute(before, after)

    assert sample.cpu_percent == 75.0  # (300 - 0 + guest は数えない) / (300 + 100)
    assert sample.cpu_cores == [75.0, 0.0]
    assert sample.memory_total == 8_000_000 * 1024
    assert sample.memory_used == 2_000_000 * 1024  # 合計 − 利用可能（キャッシュは使用中に含めない）
    assert sample.memory_percent == 25.0
    assert sample.disk_read_bps == 4096 * 512 / 2  # 物理ディスク全体だけ（パーティション・loop・zram は除く）
    assert sample.disk_write_bps == 8192 * 512 / 2
    assert sample.net_rx_bps == 1_000_000  # 仮想のインターフェースは除く
    assert sample.net_tx_bps == 200_000
    assert sample.temperature_c == 61.25
    assert sample.storage_total > 0 and 0 <= sample.storage_percent <= 100


def test_network_falls_back_to_own_namespace(host: FakeHost) -> None:
    (host.proc / "1" / "net" / "dev").unlink()
    assert host.reader().read().net_rx_bytes == 1


def test_temperature_is_optional(host: FakeHost) -> None:
    (host.sys / "class" / "thermal" / "thermal_zone0" / "temp").unlink()
    assert host.reader().read().temperature_c is None


def test_counter_reset_is_not_negative() -> None:
    def raw(at: float, counter: int) -> RawSample:
        return RawSample(at, [(100, 50)], 100, 50, counter, counter, counter, counter, None, 100, 10)

    sample = compute(raw(0, 1000), raw(2, 10))
    assert sample.disk_read_bps == 0 and sample.net_tx_bps == 0


def test_collector_keeps_only_recent_samples(host: FakeHost) -> None:
    now = [0.0]
    collector = HostMetricsCollector(host.reader(), interval=2, history_seconds=6, clock=lambda: now[0])

    assert collector.collect() is None  # 初回は差が取れない
    for _ in range(5):
        now[0] += 2
        assert collector.collect() is not None

    samples = collector.samples()
    assert [s.at for s in samples] == [6.0, 8.0, 10.0]  # 表示範囲（6秒）より古い値は捨てる
    assert [s.at for s in collector.samples(since=8.0)] == [10.0]
    assert collector.latest() is samples[-1]


def test_collector_survives_read_errors(host: FakeHost, caplog: pytest.LogCaptureFixture) -> None:
    collector = HostMetricsCollector(host.reader())
    (host.proc / "stat").unlink()
    assert collector.collect() is None
    assert collector.collect() is None
    assert caplog.text.count("failed to read host metrics") == 1  # 同じ失敗は繰り返し記録しない


@pytest.fixture
def metrics(host: FakeHost, logged_in_client: TestClient) -> HostMetricsCollector:
    now = [1000.0]
    collector = HostMetricsCollector(host.reader(), clock=lambda: now[0])
    collector.collect()
    now[0] += 2
    collector.collect()
    app.state.host_metrics = collector
    return collector


@pytest.mark.usefixtures("default_devices")
def test_metrics_page_and_data(logged_in_client: TestClient, metrics: HostMetricsCollector) -> None:
    page = logged_in_client.get("/metrics")
    assert page.status_code == 200
    assert "メトリクス" in page.text and "js/metrics.js" in page.text

    data = logged_in_client.get("/metrics/data").json()
    assert data["interval"] == host_metrics.SAMPLE_INTERVAL_SECONDS
    assert data["history_seconds"] == host_metrics.HISTORY_SECONDS
    assert len(data["samples"]) == 1
    assert set(data["samples"][0]) >= {"cpu_percent", "cpu_cores", "memory_percent", "net_rx_bps", "temperature_c", "storage_percent"}
    assert logged_in_client.get("/metrics/data", params={"since": 1002.0}).json()["samples"] == []

    dashboard = logged_in_client.get("/").text
    assert "ホストの状態" in dashboard and "52.5" in dashboard  # CPU 温度
    assert "bps" in dashboard and "KB/s" not in dashboard  # ネットワークはビットレート


@pytest.mark.usefixtures("default_devices")
def test_dashboard_without_metrics(logged_in_client: TestClient) -> None:
    text = logged_in_client.get("/").text
    assert "ホストの状態" in text and "メトリクスを取得中です" in text


def test_metrics_requires_login(client: TestClient) -> None:
    response = client.get("/metrics/data", follow_redirects=False)
    assert response.status_code in (302, 303, 307)


@pytest.mark.parametrize(
    ("bytes_per_second", "expected"),
    [(0, ("0", "bps")), (100, ("800", "bps")), (1250, ("10.0", "Kbps")), (125_000, ("1.0", "Mbps")), (25_000_000, ("200", "Mbps")), (1.5e8, ("1.2", "Gbps"))],
)
def test_format_bitrate(bytes_per_second: float, expected: tuple[str, str]) -> None:
    assert host_metrics.format_bitrate(bytes_per_second) == expected
