"""Environment diagnostics for codebotd (codebotd doctor).

检查项:
- Python 版本 (>=3.8, <3.14)
- pip 路径与版本
- 解释器架构 (x86_64 / aarch64 / arm64)
- 关键依赖: pyusb, psutil, Pillow, PyYAML, click, platformdirs
- PyUSB backend 是否可用
- USB 设备枚举 (VID=0x1A86 PID=0xCB0B 是否能找到)

PASS / FAIL / INFO 输出格式, 每行一项, 便于 grep / CI 解析。
返回值: 0 = 全部 PASS 或仅 INFO; 1 = 有 FAIL。
"""

from __future__ import annotations

import importlib.util
import logging
import os
import platform as _platform
import struct
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

log = logging.getLogger("codebot.doctor")


# Required Python version range.
# Must match pyproject.toml:
#
# requires-python = ">=3.8,<3.14"
MIN_PY = (3, 8)
MAX_PY_EXCLUSIVE = (3, 14)


# Code Bot USB device.
USB_VENDOR_ID = 0x1A86
USB_PRODUCT_ID = 0xCB0B


@dataclass
class Check:
    """One diagnostic row."""

    name: str
    status: str  # "PASS" | "FAIL" | "INFO"
    detail: str

    def render(self) -> str:
        return f"  [{self.status}] {self.name}: {self.detail}"


def _check_python() -> Check:
    """Check whether the current Python version is supported."""

    v = sys.version_info

    if MIN_PY <= (v.major, v.minor) < MAX_PY_EXCLUSIVE:
        return Check(
            "Python version",
            "PASS",
            f"{v.major}.{v.minor}.{v.micro}",
        )

    if (v.major, v.minor) < MIN_PY:
        return Check(
            "Python version",
            "FAIL",
            f"{v.major}.{v.minor}.{v.micro} < "
            f"{MIN_PY[0]}.{MIN_PY[1]}; upgrade Python",
        )

    return Check(
        "Python version",
        "FAIL",
        f"{v.major}.{v.minor}.{v.micro} >= "
        f"{MAX_PY_EXCLUSIVE[0]}.{MAX_PY_EXCLUSIVE[1]}; "
        f"downgrade or wait for support",
    )


def _check_pip() -> Check:
    """Check pip availability.

    pip is convenient but optional. uv-managed environments may not
    contain pip, so that situation is reported as INFO.
    """

    try:
        import pip

        return Check(
            "pip",
            "PASS",
            f"{pip.__version__}",
        )

    except ImportError:
        # pip is not importable.
        if importlib.util.find_spec("pip") is None:
            # Check for managed environment markers.
            venv = os.environ.get("VIRTUAL_ENV", "")

            pyvenv_cfg = Path(sys.prefix) / "pyvenv.cfg"

            is_uv = False

            if pyvenv_cfg.exists():
                try:
                    content = pyvenv_cfg.read_text(
                        errors="ignore",
                    ).lower()

                    is_uv = "uv" in content

                except OSError:
                    pass

            if is_uv or "uv" in sys.executable or venv:
                return Check(
                    "pip",
                    "INFO",
                    "not installed; managed environment — "
                    "use `uv pip install ...` if applicable",
                )

            return Check(
                "pip",
                "FAIL",
                "pip not importable; reinstall Python with ensurepip "
                "or use a Python environment that bundles pip",
            )

        return Check(
            "pip",
            "FAIL",
            "pip found but not importable; check environment integrity",
        )


def _check_arch() -> Check:
    """Check interpreter architecture."""

    machine = _platform.machine()

    bits = struct.calcsize("P") * 8

    return Check(
        "Architecture",
        "PASS",
        f"{machine} ({bits}-bit)",
    )


def _check_module(
    name: str,
    version_attr: str = "__version__",
    import_name: str | None = None,
    friendly_name: str | None = None,
) -> Check:
    """Check whether a Python module is importable.

    Some distribution names differ from import names.

    Examples:

        pyusb -> usb
        Pillow -> PIL
        PyYAML -> yaml
    """

    target = import_name or name

    label = friendly_name or name

    try:
        mod = __import__(target)

        version = getattr(
            mod,
            version_attr,
            "?",
        )

        return Check(
            label,
            "PASS",
            str(version),
        )

    except ImportError:
        return Check(
            label,
            "FAIL",
            f"not installed; reinstall with "
            f"`pip install codebot`",
        )


def _check_usb_backend() -> Check:
    """Check whether PyUSB has a usable backend.

    This is intentionally a runtime check through PyUSB itself.

    The important question is not whether a platform theoretically needs
    libusb, but whether the exact current Python + PyUSB environment can
    actually obtain a USB backend.

    This matches the behavior used by usb.core.find().
    """

    try:
        import usb.backend.libusb1

    except ImportError as e:
        return Check(
            "USB backend",
            "FAIL",
            f"PyUSB libusb backend is unavailable: {e}",
        )

    try:
        backend = usb.backend.libusb1.get_backend()

    except Exception as e:
        return Check(
            "USB backend",
            "FAIL",
            f"failed to initialize libusb backend: {e}",
        )

    if backend is None:
        if sys.platform == "darwin":
            hint = "Install libusb with: `brew install libusb`"

        elif sys.platform.startswith("linux"):
            hint = (
                "Install libusb, for example: "
                "`sudo apt install libusb-1.0-0`"
            )

        elif sys.platform == "win32":
            hint = (
                "Install the bundled libusb backend or configure "
                "a supported Windows USB backend"
            )

        else:
            hint = "Install a supported PyUSB backend"

        return Check(
            "USB backend",
            "FAIL",
            f"PyUSB could not find a usable libusb backend. {hint}",
        )

    return Check(
        "USB backend",
        "PASS",
        "libusb backend available",
    )


def _check_usb_device() -> Check:
    """Try to enumerate the Code Bot USB device."""

    try:
        import usb.core

    except ImportError:
        return Check(
            "USB device scan",
            "FAIL",
            "pyusb is not importable; cannot enumerate USB devices",
        )

    try:
        dev = usb.core.find(
            idVendor=USB_VENDOR_ID,
            idProduct=USB_PRODUCT_ID,
        )

    except usb.core.NoBackendError as e:
        return Check(
            "USB device scan",
            "FAIL",
            f"no USB backend available: {e}",
        )

    except Exception as e:
        return Check(
            "USB device scan",
            "FAIL",
            f"USB enumeration failed: {e}",
        )

    if dev is None:
        return Check(
            "USB device scan",
            "INFO",
            (
                f"Code Bot device "
                f"(0x{USB_VENDOR_ID:04X}:0x{USB_PRODUCT_ID:04X}) "
                "not found; plug in the device or run with sim-only"
            ),
        )

    return Check(
        "USB device scan",
        "PASS",
        f"found device bus={dev.bus} addr={dev.address}",
    )


CHECKS: list[Callable[[], Check]] = [
    _check_python,
    _check_pip,
    _check_arch,

    lambda: _check_module(
        "pyusb",
        import_name="usb",
        friendly_name="pyusb (as usb)",
    ),

    lambda: _check_module("psutil"),
    lambda: _check_module("PIL"),
    lambda: _check_module("yaml"),
    lambda: _check_module("click"),
    lambda: _check_module("platformdirs"),

    _check_usb_backend,
    _check_usb_device,
]


def run_doctor(verbose: bool = True) -> int:
    """Run all environment checks.

    Returns:
        0: no FAIL checks
        1: at least one FAIL check
    """

    if verbose:
        print("codebotd doctor — environment diagnostics")

        print(
            f"  platform: {_platform.platform()} "
            f"({sys.platform})"
        )

        print(
            f"  prefix:   {sys.prefix}"
        )

        print(
            f"  python:   {sys.executable}"
        )

        print("")

    fail_count = 0

    for check_fn in CHECKS:
        try:
            check = check_fn()

        except Exception as e:
            # A diagnostic check itself should not crash the whole doctor.
            check = Check(
                "internal diagnostic error",
                "FAIL",
                f"{check_fn.__name__}: {e}",
            )

        if verbose:
            print(check.render())

        if check.status == "FAIL":
            fail_count += 1

    if verbose:
        print("")

        if fail_count == 0:
            print("  All checks PASSED (or INFO).")

            return 0

        print(
            f"  {fail_count} check(s) FAILED. "
            "See hints above."
        )

        return 1

    return 1 if fail_count else 0


if __name__ == "__main__":
    sys.exit(run_doctor())