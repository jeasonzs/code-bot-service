"""USB transport layer - pyusb-based communication with the device.

v3 重构: 控制 / 数据物理隔离到两个 OUT 端点.
- EP1 OUT (0x01) bulk: 控制通道 (Frame: 1B cmd + struct)
- EP2 IN  (0x82) bulk: Vendor 响应 (Frame: 1B cmd + struct)
- EP3 IN  (0x83) interrupt: HID Keyboard (标准 USB HID, host 端不需要管)
- EP5 OUT (0x05) bulk: 图像数据流 (raw RGB565, 0 协议)

Device exposes exactly two interfaces:
  Interface 0: Vendor Specific (0xFF) — EP1 OUT + EP2 IN + EP5 OUT
  Interface 1: HID Keyboard (0x03/0x01/0x01) — EP3 IN (设备 → host)

There is NO CDC ACM interface. Debug logging on the device side goes to
USART3 (PC18/PC19) on the MCU, not over USB. Host-side debug info comes
via the Vendor IN pipe (CMD_LOG frames).

P3.3 libusb loader: per-platform native library resolution.

  Linux:  ctypes.util.find_library('usb-1.0') — relies on system package
          libusb-1.0-0 (Debian/Ubuntu), libusb (Fedora), libusb (Alpine).
          On startup failure, raise RuntimeError with platform-specific
          package install hint.
  macOS:  no libusb required — pyusb 1.x uses Apple's IOKit framework
          via ctypes; nothing to load.
  Windows: vendor libusb-1.0.dll from codebot._vendor.libusb (fallback
           when WinUSB backend is not available); primary path is the
           WinUSB driver bound by `codebotd setup-driver`.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import logging
import os
import struct
import sys
import threading
from dataclasses import dataclass
from typing import Optional

from ..protocol import Cmd, Frame, TouchReport

# pyusb is imported lazily so we can give platform-specific guidance when
# it's missing instead of a bare ModuleNotFoundError.
usb_core = None
usb_util = None
_IMPORT_ERROR: Exception | None = None
try:
    import usb.core as _uc
    import usb.util as _uu
    usb_core = _uc
    usb_util = _uu
except ImportError as _e:
    _IMPORT_ERROR = _e


log = logging.getLogger("codebot.transport.usb")


def _load_libusb_native() -> None:
    """Resolve and load the native libusb library, per platform.

    Linux:  system libusb-1.0.so.0 via ctypes.util.find_library.
    macOS:  no-op (pyusb uses IOKit).
    Windows: codebot._vendor.libusb/windows-{arch}/libusb-1.0.dll via
             os.add_dll_directory; failure is non-fatal.

    Raises:
        RuntimeError: on Linux when system libusb is not installed.
    """
    if sys.platform == "darwin":
        log.debug("libusb not required on macOS (pyusb uses IOKit)")
        return

    if sys.platform.startswith("linux"):
        so_name = ctypes.util.find_library("usb-1.0")
        if not so_name:
            raise RuntimeError(
                "libusb-1.0 not found on this system. Install the system package:\n"
                "  Debian/Ubuntu: sudo apt install libusb-1.0-0\n"
                "  Fedora/RHEL:   sudo dnf install libusb\n"
                "  Arch:          sudo pacman -S libusb\n"
                "  Alpine:        sudo apk add libusb\n"
                "Or run: codebotd setup-driver"
            )
        try:
            ctypes.CDLL(so_name)
            log.debug("loaded native libusb: %s", so_name)
        except OSError as e:
            raise RuntimeError(
                f"libusb-1.0 found at {so_name} but failed to load: {e}"
            ) from e
        return

    if sys.platform == "win32":
        # Vendor .dll — pyusb's libusb1 backend needs libusb-1.0.dll on
        # PATH. The matching driver (libusbK or libusb-win32) is installed
        # once via Zadig, replacing the inbox winusb.sys that the firmware
        # binds via MS OS 2.0 Descriptors on first plug.
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
            log.debug("loaded vendor libusb: %s", dll_path)
        except (ImportError, OSError, FileNotFoundError) as e:
            # Not fatal: pyusb will look on PATH next (libusb-package,
            # manual install, etc).
            log.debug("vendor libusb dll not loaded (%s); checking PATH", e)
        return

    log.debug("libusb loader: platform %s untested, skipping", sys.platform)


# Load native libusb on module import. On Linux, missing libusb raises a
# clear RuntimeError instead of letting pyusb crash with NoBackendError.
if _IMPORT_ERROR is not None:
    raise RuntimeError(
        "pyusb is not installed. Install with: pip install codebot[usb]"
    ) from _IMPORT_ERROR
_load_libusb_native()


# Code Bot interfaces exposed by the firmware
VENDOR_INTERFACE = 0   # Vendor bulk IN/OUT + EP5 data OUT
HID_INTERFACE    = 1   # HID Keyboard interrupt IN

# WCH USB VID + our PID
DEVICE_VID = 0x1A86
DEVICE_PID = 0xCB0B


def _safe_get_string(dev, index: int) -> Optional[str]:
    """Best-effort USB string descriptor fetch."""
    if not index:
        return None
    try:
        return usb_util.get_string(dev, index)
    except (usb_core.USBError, ValueError, NotImplementedError):
        return None


def _build_device_info(dev) -> "DeviceInfo":
    return DeviceInfo(
        vendor_id=dev.idVendor,
        product_id=dev.idProduct,
        bus=dev.bus,
        address=dev.address,
        port=dev.port_number if hasattr(dev, "port_number") else 0,
        serial=_safe_get_string(dev, dev.iSerialNumber),
        product_name=_safe_get_string(dev, dev.iProduct),
    )


@dataclass
class DeviceInfo:
    """Discovered device info."""

    vendor_id: int
    product_id: int
    bus: int
    address: int
    port: int
    serial: Optional[str] = None
    product_name: Optional[str] = None


class UsbTransport:
    """Thread-safe USB transport for Code Bot device (v3 protocol)."""

    def __init__(self, vid: int = DEVICE_VID, pid: int = DEVICE_PID, backend=None) -> None:
        self.vid = vid
        self.pid = pid
        self._backend = backend

        self._dev: Optional[usb_core.Device] = None
        self._ep_out = None  # 0x01 OUT: Vendor bulk OUT  (H→D control commands)
        self._ep_in = None   # 0x82 IN:  Vendor bulk IN   (D→H response frames)
        self._ep_data = None # 0x05 OUT: Vendor bulk OUT  (H→D image data stream)
        self._ep_hid = None  # 0x83 IN:  HID Keyboard IN   (D→H keystrokes)

        self._rx_lock = threading.Lock()
        self._tx_lock = threading.Lock()
        # State lifecycle lock: serializes open()/close()/mark_closed() and
        # guards the _dev/_ep_* refs. Used together with the snapshot
        # pattern in send_frame/poll to avoid races with the supervisor
        # thread that reopens the device on disconnect. RLock so a future
        # caller can hold it across nested ops without deadlocking.
        self._state_lock = threading.RLock()
        self._last_touch: Optional[TouchReport] = None

    # ----- Discovery -----
    def find(self) -> Optional[DeviceInfo]:
        """Find Code Bot device by VID/PID."""
        dev = usb_core.find(
            idVendor=self.vid,
            idProduct=self.pid,
            backend=self._backend,
        )
        if dev is None:
            return None
        return _build_device_info(dev)

    def list_all(self) -> list[DeviceInfo]:
        """List all Code Bot devices on USB bus."""
        results: list[DeviceInfo] = []
        for dev in usb_core.find(
            find_all=True,
            idVendor=self.vid,
            idProduct=self.pid,
            backend=self._backend,
        ):
            results.append(_build_device_info(dev))
        return results

    # ----- Open/close -----
    def open(self) -> bool:
        """Open device, claim interfaces 0 (Vendor) and 1 (HID), wire up endpoints.

        Idempotent: if a prior device handle exists, it is closed first.
        On any failure mid-way, all refs are nulled so the caller can retry.
        """
        with self._state_lock:
            self._close_unlocked()
            dev = usb_core.find(
                idVendor=self.vid,
                idProduct=self.pid,
                backend=self._backend,
            )
            if dev is None:
                return False

            try:
                # Detach kernel driver on each interface before claiming (Linux)
                for iface in (VENDOR_INTERFACE, HID_INTERFACE):
                    try:
                        if dev.is_kernel_driver_active(iface):
                            dev.detach_kernel_driver(iface)
                    except (NotImplementedError, usb_core.USBError):
                        pass

                # Set configuration (idempotent)
                try:
                    dev.set_configuration()
                except usb_core.USBError:
                    pass

                # Claim Vendor + HID interfaces
                for iface in (VENDOR_INTERFACE, HID_INTERFACE):
                    try:
                        usb_util.claim_interface(dev, iface)
                    except usb_core.USBError:
                        try:
                            if dev.is_kernel_driver_active(iface):
                                dev.detach_kernel_driver(iface)
                            usb_util.claim_interface(dev, iface)
                        except (NotImplementedError, usb_core.USBError):
                            pass

                # Wire up endpoints by address
                cfg = dev.get_active_configuration()
                ep_out = ep_in = ep_data = ep_hid = None
                for ep in cfg[(VENDOR_INTERFACE, 0)]:
                    addr = ep.bEndpointAddress
                    if addr == 0x01:
                        ep_out = ep
                    elif addr == 0x82:
                        ep_in = ep
                    elif addr == 0x05:
                        ep_data = ep
                for ep in cfg[(HID_INTERFACE, 0)]:
                    if usb_util.endpoint_direction(ep.bEndpointAddress) == usb_util.ENDPOINT_IN:
                        ep_hid = ep

                if ep_out is None:
                    log.warning("Device opened but EP1 OUT endpoint not found")
                    self._close_unlocked()
                    return False

                # Commit refs atomically under lock
                self._dev = dev
                self._ep_out = ep_out
                self._ep_in = ep_in
                self._ep_data = ep_data
                self._ep_hid = ep_hid
                return True
            except Exception:
                log.exception("open() failed; cleaning up")
                # Try to release the half-claimed device before bailing
                self._dev = dev
                self._ep_out = None
                self._close_unlocked()
                return False

    def close(self) -> None:
        """Release interfaces and close device. Idempotent."""
        with self._state_lock:
            self._close_unlocked()

    def mark_closed(self) -> None:
        """Lightweight close for the disconnect path.

        Called by send_frame / write_pixels / poll when they detect
        ENODEV/ENOENT — the device is already physically gone, so we skip
        release_interface / dispose_resources (which would raise on a
        dead handle) and just null the refs. Supervisor thread will see
        is_open == False and reopen with backoff.
        """
        with self._state_lock:
            self._dev = None
            self._ep_out = None
            self._ep_in = None
            self._ep_data = None
            self._ep_hid = None

    def _close_unlocked(self) -> None:
        """Inner close — caller MUST hold _state_lock."""
        if self._dev is not None:
            for iface in (VENDOR_INTERFACE, HID_INTERFACE):
                try:
                    usb_util.release_interface(self._dev, iface)
                except usb_core.USBError:
                    pass
            try:
                usb_util.dispose_resources(self._dev)
            except usb_core.USBError:
                pass
        self._dev = None
        self._ep_out = None
        self._ep_in = None
        self._ep_data = None
        self._ep_hid = None

    @property
    def is_open(self) -> bool:
        return self._dev is not None and self._ep_out is not None

    # ----- Send control frame (EP1 OUT) -----
    def send_frame(self, frame: Frame, timeout: int = 1000) -> bool:
        """Send a control frame to the device via EP1 OUT (cmd + struct)."""
        # Snapshot the endpoint under state lock so a concurrent close()
        # from the supervisor doesn't leave us with a stale reference.
        with self._state_lock:
            if self._ep_out is None:
                return False
            ep_out = self._ep_out
        data = frame.encode()
        if len(data) > 64:
            raise ValueError(f"frame too large for v3 single-packet protocol: {len(data)} > 64")
        with self._tx_lock:
            try:
                ep_out.write(data, timeout=timeout)
                return True
            except usb_core.USBError as e:
                # ENODEV (32) / ENOENT (19) → device physically gone.
                # Mark closed so supervisor reconnects; no point retrying.
                if e.errno in (19, 32) or "No such device" in str(e):
                    self.mark_closed()
                    return False
                if e.errno in (None, 5) or "Pipe" in str(e) or "timed out" in str(e).lower():
                    try:
                        ep_out.clear_halt()
                    except usb_core.USBError:
                        return False
                    try:
                        ep_out.write(data, timeout=timeout)
                        return True
                    except usb_core.USBError:
                        return False
                return False

    def send_ping(self) -> bool:
        return self.send_frame(Frame.ping())

    # ----- Send image data (EP5 OUT) -----
    def write_pixels(self, data: bytes, chunk_size: int = 64) -> int:
        """Write raw RGB565 BE pixel data to EP5 OUT, split into 64B chunks.

        Byte order: each pixel is 2 bytes [high, low] (big-endian),
        matching what the GC9307 SPI expects. The MCU does NOT byte-swap;
        whatever bytes you pass in this function are sent straight to the
        LCD's SPI. canvas.to_rgb565_bytes() already produces this format.

        Returns number of bytes written. Caller must have already sent
        DRAW_RECT_BEGIN on EP1 OUT; the device NAKs EP5 OUT until BEGIN.
        """
        with self._state_lock:
            if self._ep_data is None:
                return 0
            ep_data = self._ep_data
        written = 0
        with self._tx_lock:
            try:
                ep_data.write(data, timeout=1000)
                written += len(data)
            except usb_core.USBError as e:
                if e.errno in (19, 32) or "No such device" in str(e):
                    self.mark_closed()
                return written
        return written

    # ----- Send HID Keyboard (EP3 IN - device → host) -----
    def send_hid_report(self, report: bytes, timeout: int = 100) -> bool:
        """Send an 8-byte HID Keyboard report.

        Note: In v3, the device generates HID reports autonomously (e.g., from
        touch events). The host does NOT need to send keystroke commands. This
        method is kept for backward compatibility / debugging.
        """
        with self._state_lock:
            if self._ep_hid is None:
                return False
            ep_hid = self._ep_hid
        if len(report) != 8:
            raise ValueError(f"HID report must be 8 bytes, got {len(report)}")
        with self._tx_lock:
            try:
                ep_hid.write(report, timeout=timeout)
                return True
            except usb_core.USBError as e:
                if e.errno in (19, 32) or "No such device" in str(e):
                    self.mark_closed()
                return False

    # ----- Receive (EP2 IN) -----
    def poll(self, timeout_ms: int = 10) -> list[Frame]:
        """Poll for incoming frames on EP2 IN.

        v3: each USB packet is exactly one frame (no stream parsing needed).
        Returns list of decoded frames (may be empty on timeout).
        """
        with self._state_lock:
            if self._ep_in is None:
                return []
            ep_in = self._ep_in
        try:
            data = ep_in.read(64, timeout=timeout_ms)
        except usb_core.USBTimeoutError:
            return []
        except usb_core.USBError as e:
            if e.errno in (19, 32) or "No such device" in str(e):
                self.mark_closed()
            return []

        if not data:
            return []

        with self._rx_lock:
            frame = Frame.decode(bytes(data))
            if frame is None:
                return []
            frames = [frame]
            for f in frames:
                if f.cmd == Cmd.TOUCH_EVENT:
                    try:
                        self._last_touch = f.decode_touch()
                    except (ValueError, struct.error):
                        pass
        return frames

    def get_last_touch(self) -> Optional[TouchReport]:
        return self._last_touch


__all__ = [
    "UsbTransport",
    "DeviceInfo",
    "DEVICE_VID",
    "DEVICE_PID",
]
