"""Cross-platform daemon control: PID file + loopback TCP control port.

Why TCP loopback instead of named pipe / Unix socket / Windows mailslot?
  - **POSIX**: TCP loopback works on Linux + macOS without filesystem perms
    (named pipes need the same user; Unix sockets don't exist on Windows).
  - **Windows**: TCP loopback is fully supported; named pipes work but
    require `pywin32` for `CreateNamedPipe` semantics.
  - **Security**: bind to 127.0.0.1 only; not reachable from LAN.

Protocol (text, newline-delimited, max ~16 bytes):
  Request:  "STOP\\n"
  Response: "BYE\\n"

PID file lives under platformdirs.user_runtime_dir("codebot"):
  Linux:   /run/user/<uid>/codebot/codebotd.pid
  macOS:   ~/Library/Caches/codebot/codebotd.pid (or fallback)
  Windows: %LOCALAPPDATA%\\codebot\\codebotd.pid

PID file JSON content (so future fields like control_port are extensible):
  {"pid": 12345, "control_port": 39871, "sim_port": 8080}
"""

from __future__ import annotations

import json
import logging
import os
import socket
import sys
import threading
from pathlib import Path
from typing import Optional

log = logging.getLogger("codebot.ipc")


PROTOCOL_VERSION = 1


def _runtime_dir() -> Path:
    """Resolve platform-specific runtime directory.

    Falls back to ~/.codebot/ if platformdirs returns something not writable.
    """
    try:
        from platformdirs import user_runtime_dir
        # platformdirs 4.x: signature has `ensure_exists=` (not `ensure=`).
        d = Path(user_runtime_dir("codebot", ensure_exists=True))
        return d
    except (ImportError, OSError) as e:
        log.warning("platformdirs.user_runtime_dir failed (%s); using ~/.codebot", e)
        d = Path.home() / ".codebot"
        d.mkdir(parents=True, exist_ok=True)
        return d


def pid_file_path() -> Path:
    return _runtime_dir() / "codebotd.pid"


def _read_pid_file() -> Optional[dict]:
    p = pid_file_path()
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        log.debug("pid file read failed: %s", e)
        return None


def write_pid_file(pid: int, control_port: int, sim_port: int) -> Path:
    """Write PID file atomically. Returns the path written."""
    p = pid_file_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": PROTOCOL_VERSION,
        "pid": pid,
        "control_port": control_port,
        "sim_port": sim_port,
    }
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(payload), encoding="utf-8")
    os.replace(tmp, p)
    return p


def remove_pid_file() -> None:
    """Remove PID file if present. Best-effort, no error."""
    p = pid_file_path()
    try:
        p.unlink()
    except FileNotFoundError:
        pass
    except OSError as e:
        log.warning("failed to remove pid file %s: %s", p, e)


def _is_pid_alive(pid: int) -> bool:
    """Cross-platform liveness check."""
    if pid <= 0:
        return False
    if sys.platform == "win32":
        # OpenProcess is the right way but requires pywin32; this is good enough:
        # check if pid is in the process snapshot via ctypes.
        try:
            import ctypes
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            STILL_ACTIVE = 259
            h = ctypes.windll.kernel32.OpenProcess(
                PROCESS_QUERY_LIMITED_INFORMATION, False, pid
            )
            if not h:
                return False
            try:
                code = ctypes.c_ulong()
                ctypes.windll.kernel32.GetExitCodeProcess(h, ctypes.byref(code))
                return code.value == STILL_ACTIVE
            finally:
                ctypes.windll.kernel32.CloseHandle(h)
        except OSError:
            return False
    else:
        try:
            os.kill(pid, 0)
            return True
        except (ProcessLookupError, PermissionError):
            # PermissionError means it exists but we can't signal it — still alive
            return True
        except OSError:
            return False


def is_daemon_running() -> tuple[bool, Optional[dict]]:
    """Check if a codebotd daemon is currently running.

    Returns (is_running, info_dict_or_None).
    Stale PID files (where PID is no longer alive) are detected.
    """
    info = _read_pid_file()
    if info is None:
        return False, None
    pid = info.get("pid")
    if not isinstance(pid, int) or not _is_pid_alive(pid):
        log.info("stale pid file (pid=%s not alive); removing", pid)
        remove_pid_file()
        return False, None
    return True, info


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class ControlServer:
    """Tiny TCP loopback server that listens for ``STOP\\n`` and triggers shutdown.

    Lives on a daemon thread; bind fails are non-fatal (control channel just
    unavailable; user can SIGTERM the daemon on POSIX / kill on Windows).
    """

    def __init__(self, on_stop) -> None:
        self._on_stop = on_stop
        self._sock: Optional[socket.socket] = None
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self.port: int = 0

    def start(self) -> bool:
        """Bind and start accepting connections. Returns True on success."""
        try:
            self.port = _find_free_port()
            self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self._sock.bind(("127.0.0.1", self.port))
            self._sock.listen(8)
            self._sock.settimeout(0.5)  # for periodic stop-event checks
        except OSError as e:
            log.warning("control server bind failed: %s; STOP via TCP unavailable", e)
            return False

        self._thread = threading.Thread(target=self._serve, daemon=True,
                                        name="codebot-ctl")
        self._thread.start()
        return True

    def stop(self) -> None:
        self._stop.set()
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
        if self._thread is not None:
            self._thread.join(timeout=1.5)

    def _serve(self) -> None:
        while not self._stop.is_set():
            if self._sock is None:
                return
            try:
                conn, _addr = self._sock.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            try:
                with conn:
                    data = conn.recv(64)
                    if data.strip().upper() == b"STOP":
                        log.info("control: STOP received via loopback")
                        self._on_stop()
                        try:
                            conn.sendall(b"BYE\n")
                        except OSError:
                            pass
                    else:
                        try:
                            conn.sendall(b"ERR\n")
                        except OSError:
                            pass
            except OSError:
                continue


# ==============================================================
# Client side (used by `codebotd stop` / `codebotd status`)
# ==============================================================
def send_stop(timeout: float = 2.0) -> tuple[bool, str]:
    """Send STOP to a running daemon via loopback TCP. Returns (ok, message)."""
    running, info = is_daemon_running()
    if not running or info is None:
        return False, "daemon not running (no live PID file)"
    port = info.get("control_port")
    if not isinstance(port, int):
        return False, f"pid file has no control_port: {info}"
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout) as s:
            s.sendall(b"STOP\n")
            s.settimeout(timeout)
            data = s.recv(64)
        if data.strip().upper() == b"BYE":
            return True, "daemon acknowledged STOP"
        return False, f"unexpected response: {data!r}"
    except (ConnectionRefusedError, socket.timeout, OSError) as e:
        return False, f"connection failed (port={port}): {e}"
