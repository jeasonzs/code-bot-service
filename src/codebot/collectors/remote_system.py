"""Remote (SSH) implementation of the :class:`SystemCollector` ABC.

Background thread holds a single long-lived :class:`paramiko.SSHClient`
and re-uses it across ticks. Sampling rate is intentionally lower than
the local collector (0.5Hz vs 2Hz) — SSH roundtrips are expensive and
the LCD is the bottleneck anyway.

The remote script reads **cumulative** counters only — no delta math in
shell. The daemon computes rates (CPU%, disk_io_kbs, rx/tx_kbs) across
consecutive ticks in Python. Why: any sleep-based delta measurement in
the shell would have to budget the script's own awk overhead into the
timing window, and on a slow embedded target that overhead is a large
fraction of the window — the script would be measuring itself.

The remote script is plain POSIX shell + ``awk`` + standard ``/proc`` —
no python, no busybox-specific extensions. Runs unchanged on OpenWrt /
busybox, plain Linux, and any system that exposes ``/proc/stat``,
``/proc/meminfo``, ``/proc/cpuinfo``, ``/proc/diskstats``,
``/proc/net/dev`` and ``df -k``.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from typing import Optional

import paramiko

from ..ssh import SshTarget
from ..ssh_client import open_ssh, run_remote
from .system import SystemCollector, SystemSnapshot


log = logging.getLogger("codebot.collectors.remote_system")


# POSIX shell + awk. Sent over stdin to `sh` on the remote so single
# quotes inside the awk patterns don't collide with any outer quoting
# layer. Outputs one JSON line with cumulative counters — daemon does
# the delta math in Python (see ``RemoteSystemCollector._run``).
#
# Why one awk over three /proc files instead of three awk processes?
# Each awk spawn on a slow embedded target is ~30ms of process startup.
# Reading stat+diskstats+netdev in one awk keeps the script's own CPU
# footprint small, which matters when later we decide to do in-script
# sampling too.
_REMOTE_SCRIPT = r"""
# Read /proc/stat (cpu total + idle), /proc/diskstats (sectors R/W),
# /proc/net/dev (RX/TX bytes). Outputs 6 lines of cumulative counters.
# Pattern rationale:
#   /^cpu /             — matches the aggregate "cpu" line of /proc/stat
#                         only (not "cpu0", "cpu1", etc.). Spelled with
#                         trailing space so "cpuN" lines don't match.
#   $1~/^[0-9]+$/ && $3~/^[a-z]/ — diskstats rows: $1=major (numeric),
#                         $3=device-name (alpha). /proc/stat other
#                         lines ($1="intr", "ctxt", "btime", ...) fail
#                         the numeric check.
#   $1~/:$/ && $1!="lo:" — netdev interface lines end with ":" (e.g.
#                         "eth0:"). Skip loopback so LAN-facing rates
#                         aren't polluted by local socket traffic.
counters=$(awk '
    /^cpu / {
        t = $2+$3+$4+$5+$6+$7+$8+$9+$10
        i = $5+$6
    }
    NF >= 11 && $1 ~ /^[0-9]+$/ && $3 ~ /^[a-z]/ { rs += $6; ws += $10 }
    $1 ~ /:$/ && $1 != "lo:"                   { rx += $2; tx += $10 }
    END { print t+0; print i+0; print rs+0; print ws+0; print rx+0; print tx+0 }
' /proc/stat /proc/diskstats /proc/net/dev)
cpu_t=$(echo "$counters"  | sed -n 1p)
cpu_i=$(echo "$counters"  | sed -n 2p)
disk_rs=$(echo "$counters" | sed -n 3p)
disk_ws=$(echo "$counters" | sed -n 4p)
net_rx=$(echo "$counters"  | sed -n 5p)
net_tx=$(echo "$counters"  | sed -n 6p)

# Memory: MemAvailable (modern kernels) or fall back to MemFree.
mem=$(awk '
    /^MemTotal:/     { t = $2 }
    /^MemAvailable:/ { a = $2 }
    /^MemFree:/      { f = $2 }
    END {
        if (a == 0) a = f
        if (t > 0) printf "%.1f %.3f %.3f", (1 - a/t)*100, (t-a)/1024/1024, t/1024/1024
        else       printf "0.0 0.0 0.0"
    }
' /proc/meminfo)
mem_pct=$(echo "$mem" | awk '{print $1}')
mem_used_gb=$(echo "$mem" | awk '{print $2}')
mem_total_gb=$(echo "$mem" | awk '{print $3}')

# Disk on root: `df -k` columns are Filesystem 1K-blocks Used Available Use% Mounted.
# FS="[ \t]+" collapses the variable-width whitespace between fields.
disk=$(df -k / 2>/dev/null | awk '
    BEGIN { FS = "[ \t]+" }
    NR == 2 {
        pct = ($2 == 0) ? 0 : $3 * 100 / $2
        printf "%.1f %.3f %.3f %.3f", pct, $3/1024/1024, $4/1024/1024, $2/1024/1024
    }
')
disk_pct=$(echo "$disk" | awk '{print $1}')
disk_used_gb=$(echo "$disk" | awk '{print $2}')
disk_free_gb=$(echo "$disk" | awk '{print $3}')
disk_total_gb=$(echo "$disk" | awk '{print $4}')

# Logical cores: count "processor" lines in /proc/cpuinfo.
cores=$(awk 'BEGIN{n=0} /^processor[[:space:]]*:/{n++} END{print n+0}' /proc/cpuinfo)

# CPU freq MHz from sysfs; 0 if not exposed.
freq=0
if [ -r /sys/devices/system/cpu/cpu0/cpufreq/scaling_cur_freq ]; then
    freq=$(awk '{printf "%.0f", $1/1000}' /sys/devices/system/cpu/cpu0/cpufreq/scaling_cur_freq)
fi

printf '{"cpu_t":%s,"cpu_i":%s,"disk_rs":%s,"disk_ws":%s,"net_rx":%s,"net_tx":%s,"cpu_freq_mhz":%s,"cores":%s,"mem_pct":%s,"mem_used_gb":%s,"mem_total_gb":%s,"disk_pct":%s,"disk_used_gb":%s,"disk_free_gb":%s,"disk_total_gb":%s}\n' \
    "$cpu_t" "$cpu_i" "$disk_rs" "$disk_ws" "$net_rx" "$net_tx" \
    "$freq" "$cores" \
    "$mem_pct" "$mem_used_gb" "$mem_total_gb" \
    "$disk_pct" "$disk_used_gb" "$disk_free_gb" "$disk_total_gb"
"""


def _parse_remote_raw(out: str) -> dict:
    """Parse the script's single JSON line into cumulative counters.

    The remote script writes one JSON object containing every counter
    the daemon needs: cpu total/idle jiffies, disk sectors R/W,
    network bytes R/TX, plus the read-only snapshot fields (memory,
    disk usage, cores, freq).
    """
    data = json.loads(out.strip().splitlines()[-1])
    return {
        "cpu_t": int(data["cpu_t"]),
        "cpu_i": int(data["cpu_i"]),
        "disk_rs": int(data["disk_rs"]),
        "disk_ws": int(data["disk_ws"]),
        "net_rx": int(data["net_rx"]),
        "net_tx": int(data["net_tx"]),
        "cpu_freq_mhz": float(data["cpu_freq_mhz"]),
        "cores": int(data["cores"]),
        "mem_pct": float(data["mem_pct"]),
        "mem_used_gb": float(data["mem_used_gb"]),
        "mem_total_gb": float(data["mem_total_gb"]),
        "disk_pct": float(data["disk_pct"]),
        "disk_used_gb": float(data["disk_used_gb"]),
        "disk_free_gb": float(data["disk_free_gb"]),
        "disk_total_gb": float(data["disk_total_gb"]),
    }


def _compute_rates(curr: dict, prev: dict, dt: float) -> dict:
    """Compute CPU%, disk_io_kbs, rx/tx_kbs from two consecutive
    cumulative-counter snapshots.

    ``dt`` is the wall-clock interval between ``prev`` and ``curr``
    snapshots (seconds). Returns a dict with the four rate fields.

    Edge cases:
      - First tick (prev is None) — caller treats rates as 0 since
        there's no delta baseline.
      - Counter reset / negative delta — /proc counters are 64-bit on
        modern kernels but can wrap or be reset on a busybox reboot.
        A negative delta is treated as 0% rather than flashing a
        negative or >100 number on the LCD.
      - dt == 0 (shouldn't happen — collector sleeps between ticks).
        Guarded with a max(dt, 1e-3) to avoid division by zero.
    """
    dt = max(dt, 1e-3)

    d_cpu = curr["cpu_t"] - prev["cpu_t"]
    d_cpu_i = curr["cpu_i"] - prev["cpu_i"]
    # Any negative delta means the counter reset (or we lost samples).
    # Treat as 0% rather than flashing a negative or >100 number on LCD.
    if d_cpu > 0 and d_cpu_i >= 0:
        cpu_pct = (1 - d_cpu_i / d_cpu) * 100.0
        if cpu_pct < 0:
            cpu_pct = 0.0
        elif cpu_pct > 100:
            cpu_pct = 100.0
    else:
        cpu_pct = 0.0

    # Disk I/O rate: sectors * 512 / 1024 / dt = KB/s
    d_rs = max(0, curr["disk_rs"] - prev["disk_rs"])
    d_ws = max(0, curr["disk_ws"] - prev["disk_ws"])
    disk_io_kbs = (d_rs + d_ws) * 0.5 / dt

    # Network: bytes / 1024 / dt = KB/s
    d_rx = max(0, curr["net_rx"] - prev["net_rx"])
    d_tx = max(0, curr["net_tx"] - prev["net_tx"])
    rx_kbs = d_rx / 1024.0 / dt
    tx_kbs = d_tx / 1024.0 / dt

    return {
        "cpu_pct": cpu_pct,
        "disk_io_rate_kbs": disk_io_kbs,
        "rx_rate_kbs": rx_kbs,
        "tx_rate_kbs": tx_kbs,
    }


class RemoteSystemCollector(SystemCollector):
    """Sample system metrics over SSH using a long-lived paramiko client."""

    def __init__(self, target: SshTarget, hz: float = 0.5) -> None:
        self._target = target
        self.hz = hz
        self._lock = threading.Lock()
        self._latest: Optional[SystemSnapshot] = None
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._client: Optional[paramiko.SSHClient] = None
        self._connected = False
        # Previous cumulative-counter snapshot — used to compute rates
        # across consecutive ticks. None until the first successful tick.
        self._prev: Optional[dict] = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None
        self._close_client()

    def _close_client(self) -> None:
        if self._client is not None:
            try:
                self._client.close()
            except OSError as e:
                log.debug("ssh client close: %s", e)
            self._client = None
            self._connected = False

    def _run(self) -> None:
        period = 1.0 / self.hz
        backoff = 1.0
        while not self._stop.is_set():
            if not self._connected:
                try:
                    self._client = open_ssh(self._target, timeout=3.0)
                    self._connected = True
                    backoff = 1.0
                except (paramiko.SSHException, OSError) as e:
                    log.debug("remote system ssh connect failed: %s", e)
                    self._connected = False
                    self._stop.wait(min(backoff, 5.0))
                    backoff = min(backoff * 2, 5.0)
                    continue
            try:
                # Pipe the script into `sh` via stdin — avoids the
                # `sh -c '...'` single-quote escaping headache and keeps
                # the script readable. `sh` with no args reads commands
                # from stdin when it's not a TTY.
                _rc, out, _err = run_remote(self._client, "sh",
                                            stdin_data=_REMOTE_SCRIPT,
                                            timeout=2.0)
                raw = _parse_remote_raw(out)
                now = time.time()

                if self._prev is None:
                    # First tick — no delta baseline. Emit a snapshot
                    # with all rates at 0; subsequent ticks compute
                    # real rates.
                    rates = {
                        "cpu_pct": 0.0,
                        "disk_io_rate_kbs": 0.0,
                        "rx_rate_kbs": 0.0,
                        "tx_rate_kbs": 0.0,
                    }
                else:
                    dt = now - self._prev["ts"]
                    rates = _compute_rates(raw, self._prev, dt)

                self._prev = {**raw, "ts": now}

                snap = SystemSnapshot(
                    cpu_pct=rates["cpu_pct"],
                    cpu_freq_mhz=raw["cpu_freq_mhz"],
                    cores_logical=raw["cores"],
                    cpu_temp_c=None,            # remote temp parsing isn't worth the SSH bytes
                    gpu_pct=None,                # no nvidia-smi on embedded boxes
                    mem_pct=raw["mem_pct"],
                    mem_used_gb=raw["mem_used_gb"],
                    mem_total_gb=raw["mem_total_gb"],
                    disk_pct=raw["disk_pct"],
                    disk_used_gb=raw["disk_used_gb"],
                    disk_free_gb=raw["disk_free_gb"],
                    disk_total_gb=raw["disk_total_gb"],
                    disk_io_rate_kbs=rates["disk_io_rate_kbs"],
                    rx_bytes=raw["net_rx"],
                    tx_bytes=raw["net_tx"],
                    rx_rate_kbs=rates["rx_rate_kbs"],
                    tx_rate_kbs=rates["tx_rate_kbs"],
                    ts=now,
                )
                with self._lock:
                    self._latest = snap
            except (paramiko.SSHException, OSError, ValueError) as e:
                log.debug("remote system sample failed: %s", e)
                self._close_client()   # reconnect next tick
                # On reconnect, drop the baseline so we don't compute
                # a bogus rate across the disconnect window.
                self._prev = None
                self._stop.wait(min(backoff, 5.0))
                backoff = min(backoff * 2, 5.0)
                continue
            backoff = 1.0
            self._stop.wait(period)

    def snapshot(self) -> Optional[SystemSnapshot]:
        with self._lock:
            return self._latest
