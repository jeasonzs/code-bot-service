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
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Optional

import usb.core
import usb.util

from ..protocol import Cmd, Frame, TouchReport


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
        return usb.util.get_string(dev, index)
    except (usb.core.USBError, ValueError, NotImplementedError):
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

        self._dev: Optional[usb.core.Device] = None
        self._ep_out = None  # 0x01 OUT: Vendor bulk OUT  (H→D control commands)
        self._ep_in = None   # 0x82 IN:  Vendor bulk IN   (D→H response frames)
        self._ep_data = None # 0x05 OUT: Vendor bulk OUT  (H→D image data stream)
        self._ep_hid = None  # 0x83 IN:  HID Keyboard IN   (D→H keystrokes)

        self._rx_lock = threading.Lock()
        self._tx_lock = threading.Lock()
        self._last_touch: Optional[TouchReport] = None

    # ----- Discovery -----
    def find(self) -> Optional[DeviceInfo]:
        """Find Code Bot device by VID/PID."""
        dev = usb.core.find(
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
        for dev in usb.core.find(
            find_all=True,
            idVendor=self.vid,
            idProduct=self.pid,
            backend=self._backend,
        ):
            results.append(_build_device_info(dev))
        return results

    # ----- Open/close -----
    def open(self) -> bool:
        """Open device, claim interfaces 0 (Vendor) and 1 (HID), wire up endpoints."""
        self._dev = usb.core.find(
            idVendor=self.vid,
            idProduct=self.pid,
            backend=self._backend,
        )
        if self._dev is None:
            return False

        # Detach kernel driver on each interface before claiming (Linux)
        for iface in (VENDOR_INTERFACE, HID_INTERFACE):
            try:
                if self._dev.is_kernel_driver_active(iface):
                    self._dev.detach_kernel_driver(iface)
            except (NotImplementedError, usb.core.USBError):
                pass

        # Set configuration (idempotent)
        try:
            self._dev.set_configuration()
        except usb.core.USBError:
            pass

        # Claim Vendor + HID interfaces
        for iface in (VENDOR_INTERFACE, HID_INTERFACE):
            try:
                usb.util.claim_interface(self._dev, iface)
            except usb.core.USBError:
                try:
                    if self._dev.is_kernel_driver_active(iface):
                        self._dev.detach_kernel_driver(iface)
                    usb.util.claim_interface(self._dev, iface)
                except (NotImplementedError, usb.core.USBError):
                    pass

        # Wire up endpoints by address
        cfg = self._dev.get_active_configuration()
        for ep in cfg[(VENDOR_INTERFACE, 0)]:
            addr = ep.bEndpointAddress
            if addr == 0x01:
                self._ep_out = ep
            elif addr == 0x82:
                self._ep_in = ep
            elif addr == 0x05:
                self._ep_data = ep
        for ep in cfg[(HID_INTERFACE, 0)]:
            if usb.util.endpoint_direction(ep.bEndpointAddress) == usb.util.ENDPOINT_IN:
                self._ep_hid = ep
        return True

    def close(self) -> None:
        """Release interfaces and close device."""
        if self._dev is not None:
            for iface in (VENDOR_INTERFACE, HID_INTERFACE):
                try:
                    usb.util.release_interface(self._dev, iface)
                except usb.core.USBError:
                    pass
            try:
                usb.util.dispose_resources(self._dev)
            except usb.core.USBError:
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
        if not self.is_open:
            return False
        data = frame.encode()
        if len(data) > 64:
            raise ValueError(f"frame too large for v3 single-packet protocol: {len(data)} > 64")
        with self._tx_lock:
            try:
                self._ep_out.write(data, timeout=timeout)
                return True
            except usb.core.USBError as e:
                if e.errno in (None, 5, 19, 32) or "Pipe" in str(e) or "timed out" in str(e).lower():
                    try:
                        self._ep_out.clear_halt()
                    except usb.core.USBError:
                        return False
                    try:
                        self._ep_out.write(data, timeout=timeout)
                        return True
                    except usb.core.USBError:
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
        if not self.is_open or self._ep_data is None:
            return 0
        written = 0
        with self._tx_lock:
            for off in range(0, len(data), chunk_size):
                chunk = data[off:off + chunk_size]
                try:
                    self._ep_data.write(chunk, timeout=1000)
                    written += len(chunk)
                except usb.core.USBError:
                    return written
        return written

    # ----- Send HID Keyboard (EP3 IN - device → host) -----
    def send_hid_report(self, report: bytes, timeout: int = 100) -> bool:
        """Send an 8-byte HID Keyboard report.

        Note: In v3, the device generates HID reports autonomously (e.g., from
        touch events). The host does NOT need to send keystroke commands. This
        method is kept for backward compatibility / debugging.
        """
        if not self.is_open or self._ep_hid is None:
            return False
        if len(report) != 8:
            raise ValueError(f"HID report must be 8 bytes, got {len(report)}")
        with self._tx_lock:
            try:
                self._ep_hid.write(report, timeout=timeout)
                return True
            except usb.core.USBError:
                return False

    # ----- Receive (EP2 IN) -----
    def poll(self, timeout_ms: int = 10) -> list[Frame]:
        """Poll for incoming frames on EP2 IN.

        v3: each USB packet is exactly one frame (no stream parsing needed).
        Returns list of decoded frames (may be empty on timeout).
        """
        if not self.is_open:
            return []
        try:
            data = self._ep_in.read(64, timeout=timeout_ms)
        except usb.core.USBTimeoutError:
            return []
        except usb.core.USBError:
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
