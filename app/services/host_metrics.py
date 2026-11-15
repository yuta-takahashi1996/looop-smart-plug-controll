"""ホスト（Raspberry Pi）のメトリクス（docs/18-host-metrics.md）。

/proc と /sys を読み、CPU・メモリ・ストレージ・ネットワーク・CPU 温度を一定間隔で取得する。
値はグラフに表示する直近60秒の分だけをメモリに持ち、古い値は順に捨てる。ストレージや DB には保存しない。

コンテナの中では CPU・メモリ・ディスクの IO・温度はホストの値が読めるが、ネットワークはコンテナ自身の値になる。
ホスト全体の通信量は、ホストの /proc を読み取り専用でマウントし（HOST_PROC_PATH）、PID 1 のネットワークの値を読む。
"""

import logging
import os
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

SAMPLE_INTERVAL_SECONDS = 2.0
HISTORY_SECONDS = 60  # グラフに表示する範囲（タスクマネージャーと同じ60秒）。これより古い値は捨てる
SECTOR_BYTES = 512  # /proc/diskstats のセクタは常に 512 バイト
# 集計から除くネットワークインターフェース（仮想のもの。物理の通信と二重に数えないため）
VIRTUAL_INTERFACE_PREFIXES = ("lo", "docker", "veth", "br-", "virbr", "tailscale", "wg", "tun", "tap")
# 集計から除くブロックデバイス（仮想・メモリ上のもの、LVM などの論理ボリューム）
VIRTUAL_DISK_PREFIXES = ("loop", "ram", "zram", "dm-", "md", "sr")


@dataclass(frozen=True)
class RawSample:
    """累積のカウンタ（2回の差から使用率・速度を求める）と、その時点の値。"""

    at: float
    cpu: list[tuple[int, int]]  # 先頭が全体、以降がコアごと。(全体の時間, アイドル時間)
    memory_total: int  # バイト
    memory_available: int
    disk_read_bytes: int
    disk_write_bytes: int
    net_rx_bytes: int
    net_tx_bytes: int
    temperature_c: float | None
    storage_total: int
    storage_used: int


@dataclass(frozen=True)
class MetricsSample:
    at: float  # UNIX 時刻（秒）
    cpu_percent: float
    cpu_cores: list[float]
    memory_percent: float
    memory_used: int  # バイト
    memory_total: int
    disk_read_bps: float  # バイト／秒
    disk_write_bps: float
    net_rx_bps: float
    net_tx_bps: float
    temperature_c: float | None
    storage_percent: float
    storage_used: int
    storage_total: int

    def to_dict(self) -> dict:
        return asdict(self)


class HostMetricsReader:
    """/proc・/sys・ストレージの容量を読む。パスはテストやコンテナに合わせて差し替える。"""

    def __init__(self, proc_path: str = "/proc", sys_path: str = "/sys", storage_path: str = "/") -> None:
        self.proc = Path(proc_path)
        self.sys = Path(sys_path)
        self.storage_path = storage_path

    def read(self, now: float | None = None) -> RawSample:
        memory_total, memory_available = self._memory()
        disk_read, disk_write = self._disk_io()
        net_rx, net_tx = self._network()
        storage_total, storage_used = self._storage()
        return RawSample(
            at=time.time() if now is None else now,
            cpu=self._cpu(),
            memory_total=memory_total,
            memory_available=memory_available,
            disk_read_bytes=disk_read,
            disk_write_bytes=disk_write,
            net_rx_bytes=net_rx,
            net_tx_bytes=net_tx,
            temperature_c=self._temperature(),
            storage_total=storage_total,
            storage_used=storage_used,
        )

    def _cpu(self) -> list[tuple[int, int]]:
        result = []
        for line in (self.proc / "stat").read_text().splitlines():
            if not line.startswith("cpu"):
                continue
            values = [int(v) for v in line.split()[1:]]
            # user nice system idle iowait irq softirq steal（guest は user に含まれるため足さない）
            busy_fields = values[:8]
            idle = values[3] + (values[4] if len(values) > 4 else 0)
            result.append((sum(busy_fields), idle))
        return result

    def _memory(self) -> tuple[int, int]:
        values: dict[str, int] = {}
        for line in (self.proc / "meminfo").read_text().splitlines():
            key, _, rest = line.partition(":")
            if key in ("MemTotal", "MemAvailable"):
                values[key] = int(rest.split()[0]) * 1024
        return values["MemTotal"], values["MemAvailable"]

    def _disks(self) -> set[str]:
        """物理ディスク（パーティションではなくディスク全体）の名前。"""
        block = self.sys / "block"
        if not block.is_dir():
            return set()
        return {p.name for p in block.iterdir() if not p.name.startswith(VIRTUAL_DISK_PREFIXES)}

    def _disk_io(self) -> tuple[int, int]:
        disks = self._disks()
        read = write = 0
        for line in (self.proc / "diskstats").read_text().splitlines():
            fields = line.split()
            if len(fields) < 10 or fields[2] not in disks:
                continue
            read += int(fields[5]) * SECTOR_BYTES
            write += int(fields[9]) * SECTOR_BYTES
        return read, write

    def _network(self) -> tuple[int, int]:
        # PID 1 のネットワーク名前空間（ホストの /proc をマウントしていればホスト全体）
        path = self.proc / "1" / "net" / "dev"
        try:
            text = path.read_text()
        except OSError:
            text = (self.proc / "net" / "dev").read_text()
        rx = tx = 0
        for line in text.splitlines()[2:]:
            name, _, rest = line.partition(":")
            name = name.strip()
            if not rest or name.startswith(VIRTUAL_INTERFACE_PREFIXES):
                continue
            fields = rest.split()
            rx += int(fields[0])
            tx += int(fields[8])
        return rx, tx

    def _temperature(self) -> float | None:
        try:
            return int((self.sys / "class" / "thermal" / "thermal_zone0" / "temp").read_text().strip()) / 1000
        except (OSError, ValueError):
            return None  # 温度センサーがない環境（WSL など）

    def _storage(self) -> tuple[int, int]:
        stat = os.statvfs(self.storage_path)
        total = stat.f_blocks * stat.f_frsize
        free = stat.f_bfree * stat.f_frsize
        return total, total - free


def format_bitrate(bytes_per_second: float) -> tuple[str, str]:
    """ネットワークの速度をビットレートの数値と単位にする（1000 単位。画面の表示と同じ）。"""
    bits = bytes_per_second * 8
    units = ["bps", "Kbps", "Mbps", "Gbps"]
    index = 0
    while bits >= 1000 and index < len(units) - 1:
        bits /= 1000
        index += 1
    return (f"{bits:.0f}" if bits >= 100 or index == 0 else f"{bits:.1f}"), units[index]


def _percent(part: float, whole: float) -> float:
    return round(min(max(part / whole * 100, 0.0), 100.0), 1) if whole > 0 else 0.0


def compute(prev: RawSample, cur: RawSample) -> MetricsSample:
    """2回の取得の差から、使用率と速度を求める。"""
    elapsed = max(cur.at - prev.at, 1e-6)

    def cpu_usage(before: tuple[int, int], after: tuple[int, int]) -> float:
        total, idle = after[0] - before[0], after[1] - before[1]
        return _percent(total - idle, total)

    usages = [cpu_usage(b, a) for b, a in zip(prev.cpu, cur.cpu)]

    def rate(before: int, after: int) -> float:
        # カウンタが戻った（デバイスの増減など）ときは 0 とする
        return round(max(after - before, 0) / elapsed, 1)

    memory_used = cur.memory_total - cur.memory_available
    return MetricsSample(
        at=round(cur.at, 3),
        cpu_percent=usages[0] if usages else 0.0,
        cpu_cores=usages[1:],
        memory_percent=_percent(memory_used, cur.memory_total),
        memory_used=memory_used,
        memory_total=cur.memory_total,
        disk_read_bps=rate(prev.disk_read_bytes, cur.disk_read_bytes),
        disk_write_bps=rate(prev.disk_write_bytes, cur.disk_write_bytes),
        net_rx_bps=rate(prev.net_rx_bytes, cur.net_rx_bytes),
        net_tx_bps=rate(prev.net_tx_bytes, cur.net_tx_bytes),
        temperature_c=cur.temperature_c,
        storage_percent=_percent(cur.storage_used, cur.storage_total),
        storage_used=cur.storage_used,
        storage_total=cur.storage_total,
    )


class HostMetricsCollector:
    """一定間隔で取得し、直近 HISTORY_SECONDS の分をメモリに持つ（専用のスレッド。APScheduler のログを増やさないため）。"""

    def __init__(
        self,
        reader: HostMetricsReader,
        *,
        interval: float = SAMPLE_INTERVAL_SECONDS,
        history_seconds: float = HISTORY_SECONDS,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.reader = reader
        self.interval = interval
        self._clock = clock
        self._samples: deque[MetricsSample] = deque(maxlen=max(int(history_seconds / interval), 1))
        self._prev: RawSample | None = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._error_logged = False

    def collect(self) -> MetricsSample | None:
        """1回取得する。初回は差が取れないため None。"""
        try:
            cur = self.reader.read(self._clock())
        except Exception:
            if not self._error_logged:  # 同じ失敗を2秒ごとに記録しない
                logger.exception("failed to read host metrics")
                self._error_logged = True
            return None
        self._error_logged = False
        prev, self._prev = self._prev, cur
        if prev is None:
            return None
        sample = compute(prev, cur)
        with self._lock:
            self._samples.append(sample)
        return sample

    def samples(self, since: float | None = None) -> list[MetricsSample]:
        with self._lock:
            return [s for s in self._samples if since is None or s.at > since]

    def latest(self) -> MetricsSample | None:
        with self._lock:
            return self._samples[-1] if self._samples else None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="host-metrics", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=self.interval * 2)
            self._thread = None

    def _run(self) -> None:
        while not self._stop.is_set():
            self.collect()
            self._stop.wait(self.interval)
