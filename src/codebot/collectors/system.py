"""System metrics collector using psutil."""

from __future__ import annotations

import os
import psutil
import re
import shutil
import subprocess
import sys
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
    gpu_pct: Optional[float]
    mem_pct: float
    mem_used_gb: float
    mem_total_gb: float
    disk_pct: float
    disk_used_gb: float
    disk_free_gb: float
    disk_total_gb: float
    disk_io_rate_kbs: float
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
        self._last_disk = psutil.disk_io_counters()
        self._last_time = 0.0
        # GPU 采样方式按平台探测一次:
        #   linux  -> nvidia-smi (仅 NVIDIA)
        #   darwin -> ioreg IOAccelerator 的 "Device Utilization %" (免 sudo,
        #             Intel/Apple Silicon 都有; powermetrics 要 root 不用)
        #   windows -> 不实现, gpu_pct 恒为 None
        self._gpu_mode: Optional[str] = None
        if sys.platform.startswith("linux") and shutil.which("nvidia-smi"):
            self._gpu_mode = "nvidia"
        elif sys.platform == "darwin":
            self._gpu_mode = "ioreg"
        self._gpu_pct: Optional[float] = None
        self._gpu_ts = 0.0

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
        # 跨平台: 硬编码 "/" 在 Windows 下 ValueError; 用 os.sep 让 psutil 自动
        # 选平台相关根 (Linux/macOS: "/", Windows: "C:\\")
        disk = psutil.disk_usage(os.path.abspath(os.sep))
        net = psutil.net_io_counters()
        now = time.time()
        elapsed = now - self._last_time if self._last_time else 1.0
        rx_rate = (net.bytes_recv - self._last_net.bytes_recv) / max(elapsed, 0.001) / 1024
        tx_rate = (net.bytes_sent - self._last_net.bytes_sent) / max(elapsed, 0.001) / 1024
        # disk_io_counters() 在无磁盘统计的环境 (某些 VM/容器) 返回 None
        disk_io = psutil.disk_io_counters()
        if disk_io is not None and self._last_disk is not None:
            io_bytes = (disk_io.read_bytes - self._last_disk.read_bytes) + (
                disk_io.write_bytes - self._last_disk.write_bytes
            )
            disk_io_rate = io_bytes / max(elapsed, 0.001) / 1024
        else:
            disk_io_rate = 0.0
        self._last_net = net
        self._last_disk = disk_io
        self._last_time = now

        try:
            cpu_freq = psutil.cpu_freq().current
        except (AttributeError, OSError):
            cpu_freq = 0

        cores_logical = psutil.cpu_count(logical=True) or 0

        # Prefer known CPU package sensors. acpitz (and similar ACPI thermal
        # zones) report a static BIOS reference temp that doesn't track CPU
        # load — often ~28°C — and shouldn't be shown as "CPU temperature".
        # If no known CPU sensor is present (e.g. macOS, some VMs), fall back
        # to the first non-empty reading rather than None.
        _CPU_TEMP_KEYS = (
            "coretemp",      # Intel
            "k10temp",       # AMD K10+
            "cpu_thermal",   # ARM / Raspberry Pi
            "cpu-thermal",   # some Linux distros
            "zenpower",      # AMD Zen (third-party driver)
            "amd_thermal",   # newer AMD
        )
        cpu_temp_c: Optional[float] = None
        try:
            temps = psutil.sensors_temperatures(fahrenheit=False)
            for key in _CPU_TEMP_KEYS:
                entries = temps.get(key)
                if entries:
                    cpu_temp_c = entries[0].current
                    break
            if cpu_temp_c is None:
                for entries in temps.values():
                    if entries:
                        cpu_temp_c = entries[0].current
                        break
        except (AttributeError, OSError):
            pass

        # GPU 走 subprocess (nvidia-smi/ioreg), 比 psutil 调用贵得多,
        # 限到 1Hz, 其余 tick 复用上次的值
        if self._gpu_mode is not None and now - self._gpu_ts >= 1.0:
            self._gpu_pct = self._read_gpu_pct()
            self._gpu_ts = now

        snap = SystemSnapshot(
            cpu_pct=cpu_pct,
            cpu_freq_mhz=cpu_freq,
            cores_logical=cores_logical,
            cpu_temp_c=cpu_temp_c,
            gpu_pct=self._gpu_pct,
            mem_pct=mem.percent,
            mem_used_gb=mem.used / (1024 ** 3),
            mem_total_gb=mem.total / (1024 ** 3),
            disk_pct=disk.percent,
            disk_used_gb=disk.used / (1024 ** 3),
            # psutil 的 free 是 f_bavail (非 root 可用), 与 df 的 Avail 一致;
            # 不能用 total - used, 那样会把 ext4 的 root 预留块算进去
            disk_free_gb=disk.free / (1024 ** 3),
            disk_total_gb=disk.total / (1024 ** 3),
            disk_io_rate_kbs=disk_io_rate,
            rx_bytes=net.bytes_recv,
            tx_bytes=net.bytes_sent,
            rx_rate_kbs=rx_rate,
            tx_rate_kbs=tx_rate,
            ts=now,
        )
        with self._lock:
            self._latest = snap

    def _read_gpu_pct(self) -> Optional[float]:
        """Read GPU utilization % via platform command; None on failure."""
        if self._gpu_mode == "nvidia":
            try:
                out = subprocess.run(
                    ["nvidia-smi", "--query-gpu=utilization.gpu",
                     "--format=csv,noheader,nounits"],
                    capture_output=True, text=True, timeout=1.0,
                )
                if out.returncode == 0:
                    # 多卡时取第一块
                    return float(out.stdout.strip().splitlines()[0])
            except (OSError, subprocess.TimeoutExpired, ValueError, IndexError):
                pass
        elif self._gpu_mode == "ioreg":
            try:
                out = subprocess.run(
                    ["ioreg", "-r", "-d", "1", "-c", "IOAccelerator"],
                    capture_output=True, text=True, timeout=1.0,
                )
                m = re.search(r'"Device Utilization %"=(\d+)', out.stdout)
                if m:
                    return float(m.group(1))
            except (OSError, subprocess.TimeoutExpired):
                pass
        return None

    def snapshot(self) -> Optional[SystemSnapshot]:
        """Get the latest snapshot (or None if not sampled yet)."""
        with self._lock:
            return self._latest
