"""Environment diagnostics for codebotd (codebotd doctor).

检查项:
- Python 版本 (>=3.10, <3.14)
- pip 路径与版本
- 解释器架构 (x86_64 / aarch64 / arm64)
- 关键依赖: pyusb, psutil, Pillow, PyYAML, click, platformdirs
- libusb 加载状态 (按平台差异化: Linux 系统包 / macOS IOKit / Windows vendor dll)
- USB 设备枚举 (VID=0x1A86 PID=0xCB0B 是否能找到)

PASS / FAIL / INFO 输出格式, 每行一项, 便于 grep / CI 解析。
返回值: 0 = 全部 PASS 或仅 INFO; 1 = 有 FAIL。
"""

from __future__ import annotations

import ctypes
import ctypes.util
import logging
import os
import platform as _platform
import struct
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

log = logging.getLogger("codebot.doctor")


# Required Python version range (must match pyproject.toml).
MIN_PY = (3, 8)
MAX_PY_EXCLUSIVE = (3, 14)


@dataclass
class Check:
    """One diagnostic row."""

    name: str
    status: str  # "PASS" | "FAIL" | "INFO"
    detail: str

    def render(self) -> str:
        return f"  [{self.status}] {self.name}: {self.detail}"


def _check_python() -> Check:
    v = sys.version_info
    if MIN_PY <= (v.major, v.minor) < MAX_PY_EXCLUSIVE:
        return Check("Python version", "PASS", f"{v.major}.{v.minor}.{v.micro}")
    if (v.major, v.minor) < MIN_PY:
        return Check(
            "Python version", "FAIL",
            f"{v.major}.{v.minor}.{v.micro} < {MIN_PY[0]}.{MIN_PY[1]}; upgrade Python",
        )
    return Check(
        "Python version", "FAIL",
        f"{v.major}.{v.minor}.{v.micro} >= {MAX_PY_EXCLUSIVE[0]}.{MAX_PY_EXCLUSIVE[1]}; "
        f"downgrade or wait for support",
    )


def _check_pip() -> Check:
    """pip is convenient but optional: uv/poetry-managed venvs don't ship it.

    We downgrade to INFO when pip isn't importable but the environment looks
    like a managed venv (uv/poetry leave a recognizable marker).
    """
    try:
        import pip  # noqa: F401
        return Check("pip", "PASS", f"{pip.__version__}")
    except ImportError:
        import importlib.util
        if importlib.util.find_spec("pip") is None:
            # Check for managed-venv markers.
            venv = os.environ.get("VIRTUAL_ENV", "")
            # uv leaves a pyvenv.cfg with `uv =` or pip is missing entirely.
            pyvenv_cfg = Path(sys.prefix) / "pyvenv.cfg"
            is_uv = False
            if pyvenv_cfg.exists():
                try:
                    is_uv = "uv" in pyvenv_cfg.read_text(errors="ignore").lower()
                except OSError:
                    pass
            if is_uv or "uv" in sys.executable:
                return Check(
                    "pip", "INFO",
                    "not installed; managed by uv — use `uv pip install ...`",
                )
            return Check(
                "pip", "FAIL",
                "pip not importable; reinstall Python with ensurepip "
                "or use a system Python that bundles pip",
            )
        return Check(
            "pip", "FAIL",
            "pip found but not importable; check venv integrity",
        )


def _check_arch() -> Check:
    machine = _platform.machine()  # e.g. x86_64, aarch64, arm64
    bits = struct.calcsize("P") * 8
    return Check("Architecture", "PASS", f"{machine} ({bits}-bit)")


def _check_module(name: str, version_attr: str = "__version__",
                   import_name: str | None = None,
                   friendly_name: str | None = None) -> Check:
    """Check that a module is importable.

    Some packages (notably pyusb) ship under a different import name than
    the distribution name; ``import_name`` overrides the import target.
    """
    target = import_name or name
    label = friendly_name or name
    try:
        mod = __import__(target)
        ver = getattr(mod, version_attr, "?")
        return Check(label, "PASS", str(ver))
    except ImportError:
        return Check(
            label, "FAIL",
            f"not installed; reinstall with `pip install codebot`",
        )


def _check_libusb() -> Check:
    """Per-platform libusb load check.

    Linux:  ctypes.util.find_library('usb-1.0') + CDLL probe
    macOS:  pyusb 自动用 IOKit, 不需要 libusb
    Windows: 尝试 vendor .dll, 失败回退到 WinUSB
    """
    if sys.platform == "darwin":
        # IOKit backend, no libusb required
        return Check(
            "libusb", "PASS",
            "not required on macOS (pyusb uses IOKit backend)",
        )

    if sys.platform.startswith("linux"):
        so_name = ctypes.util.find_library("usb-1.0")
        if not so_name:
            return Check(
                "libusb", "FAIL",
                "libusb-1.0.so.0 not found. Install: "
                "`sudo apt install libusb-1.0-0` (Debian/Ubuntu) | "
                "`sudo dnf install libusb` (Fedora) | "
                "`sudo apk add libusb` (Alpine). "
                "Or run: `codebotd setup-driver`",
            )
        try:
            ctypes.CDLL(so_name)
            return Check("libusb", "PASS", f"loaded {so_name}")
        except OSError as e:
            return Check(
                "libusb", "FAIL",
                f"found {so_name} but CDLL load failed: {e}",
            )

    if sys.platform == "win32":
        # Try vendor .dll first (Windows-x86_64 / Windows-x86)
        try:
            # Python 3.8 lacks importlib.resources.files — use the backport
            # there (declared as a conditional dep in pyproject.toml).
            try:
                from importlib.resources import files
            except ImportError:  # pragma: no cover — only on Python < 3.9
                from importlib_resources import files  # type: ignore[no-redef]
            vendor = files("codebot._vendor.libusb")
            arch = "windows-x86_64" if struct.calcsize("P") == 8 else "windows-x86"
            dll_path = str(vendor / arch / "libusb-1.0.dll")
            os.add_dll_directory(os.path.dirname(dll_path))
            ctypes.CDLL(dll_path)
            return Check("libusb", "PASS", f"loaded vendor {dll_path}")
        except (ImportError, OSError, FileNotFoundError) as e:
            # WinUSB backend works without libusb.dll
            return Check(
                "libusb", "INFO",
                f"vendor dll not loaded ({e}); WinUSB backend will be used",
            )

    return Check("libusb", "INFO", f"platform {sys.platform} untested")


def _check_usb_device() -> Check:
    """Try to enumerate Code Bot device VID=0x1A86 PID=0xCB0B."""
    try:
        import usb.core  # noqa: F401
    except ImportError:
        return Check(
            "USB device scan", "INFO",
            "pyusb not importable; skipping enumeration",
        )
    try:
        import usb.core as _uc
        dev = _uc.find(idVendor=0x1A86, idProduct=0xCB0B)
        if dev is None:
            return Check(
                "USB device scan", "INFO",
                "Code Bot device (0x1A86:0xCB0B) not found; "
                "plug in device or run with sim-only",
            )
        return Check(
            "USB device scan", "PASS",
            f"found device bus={dev.bus} addr={dev.address}",
        )
    except Exception as e:
        return Check(
            "USB device scan", "INFO",
            f"enumeration failed: {e} (sim-only mode will still work)",
        )


CHECKS: list[Callable[[], Check]] = [
    _check_python,
    _check_pip,
    _check_arch,
    # pyusb is the PyPI package name; the import target is `usb`.
    lambda: _check_module("pyusb", import_name="usb", friendly_name="pyusb (as usb)"),
    lambda: _check_module("psutil"),
    lambda: _check_module("PIL"),
    lambda: _check_module("yaml"),
    lambda: _check_module("click"),
    lambda: _check_module("platformdirs"),
    _check_libusb,
    _check_usb_device,
]


def run_doctor(verbose: bool = True) -> int:
    """Run all checks; return 0 if no FAIL, 1 otherwise."""
    if verbose:
        print(f"codebotd doctor — environment diagnostics")
        print(f"  platform: {sys.platform} ({_platform.platform()})")
        print(f"  prefix:   {sys.prefix}")
        print("")

    fail_count = 0
    for check_fn in CHECKS:
        c = check_fn()
        if verbose:
            print(c.render())
        if c.status == "FAIL":
            fail_count += 1

    if verbose:
        print("")
        if fail_count == 0:
            print("  All checks PASSED (or INFO).")
            return 0
        print(f"  {fail_count} check(s) FAILED. See hints above.")
        return 1

    return 1 if fail_count else 0


if __name__ == "__main__":
    sys.exit(run_doctor())
