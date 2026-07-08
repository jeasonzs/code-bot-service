"""System metrics collector using psutil."""

from __future__ import annotations

import psutil
import time
import threading
from dataclasses import dataclass, asdict
from typing import Optional


@dataclass
class SystemSnapshot:
    cpu_pct: float
    cpu_freq_mhz: float
    cores_logical: int
    cpu_temp_c: Optional[float]
    fan_rpm: Optional[int]
    mem_pct: float
    mem_used_gb: float
    mem_total_gb: float
    disk_pct: float
    disk_used_gb: float
    disk_total_gb: float
    rx_bytes: int
    tx_bytes: int
    rx_rate_kbs: float
    tx_rate_kbs: float
    ts: float


class SystemCollector:
    """Background thread that samples system metrics at a fixed rate."""

    def __init__(self, hz: float = 2.0) -> None:
        self.hz = hz
        self._lock = threading.Lock()
        self._latest: Optional[SystemSnapshot] = None
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._last_net = psutil.net_io_counters()
        self._last_time = 0.0

    def start(self) -> None:
        """Start the background sampling thread."""
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Stop the background thread."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None

    def _run(self) -> None:
        period = 1.0 / self.hz
        while not self._stop.is_set():
            try:
                self._sample()
            except Exception as e:
                # Don't crash the thread
                pass
            self._stop.wait(period)

    def _sample(self) -> None:
        cpu_pct = psutil.cpu_percent(interval=None)
        mem = psutil.virtual_memory()
        disk = psutil.disk_usage("/")
        net = psutil.net_io_counters()
        now = time.time()
        elapsed = now - self._last_time if self._last_time else 1.0
        rx_rate = (net.bytes_recv - self._last_net.bytes_recv) / max(elapsed, 0.001) / 1024
        tx_rate = (net.bytes_sent - self._last_net.bytes_sent) / max(elapsed, 0.001) / 1024
        self._last_net = net
        self._last_time = now

        try:
            cpu_freq = psutil.cpu_freq().current
        except (AttributeError, OSError):
            cpu_freq = 0

        cores_logical = psutil.cpu_count(logical=True) or 0

        # First available sensor reading; psutil returns {} on systems without
        # sensors (e.g. macOS, some VMs), so this degrades to None gracefully.
        cpu_temp_c: Optional[float] = None
        try:
            temps = psutil.sensors_temperatures(fahrenheit=False)
            for entries in temps.values():
                if entries:
                    cpu_temp_c = entries[0].current
                    break
        except (AttributeError, OSError):
            pass

        fan_rpm: Optional[int] = None
        try:
            fans = psutil.sensors_fans()
            for entries in fans.values():
                if entries:
                    fan_rpm = int(entries[0].current)
                    break
        except (AttributeError, OSError):
            pass

        snap = SystemSnapshot(
            cpu_pct=cpu_pct,
            cpu_freq_mhz=cpu_freq,
            cores_logical=cores_logical,
            cpu_temp_c=cpu_temp_c,
            fan_rpm=fan_rpm,
            mem_pct=mem.percent,
            mem_used_gb=mem.used / (1024 ** 3),
            mem_total_gb=mem.total / (1024 ** 3),
            disk_pct=disk.percent,
            disk_used_gb=disk.used / (1024 ** 3),
            disk_total_gb=disk.total / (1024 ** 3),
            rx_bytes=net.bytes_recv,
            tx_bytes=net.bytes_sent,
            rx_rate_kbs=rx_rate,
            tx_rate_kbs=tx_rate,
            ts=now,
        )
        with self._lock:
            self._latest = snap

    def snapshot(self) -> Optional[SystemSnapshot]:
        """Get the latest snapshot (or None if not sampled yet)."""
        with self._lock:
            return self._latest
