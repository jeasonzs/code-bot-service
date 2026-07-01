"""USB transport layer - pyusb-based communication with the device.

Handles:
- USB device discovery (vendor 0x1A86, product 0xCB0B)
- Vendor bulk OUT (EP1 OUT, 0x01) - send commands to device
- Vendor bulk IN  (EP2 IN,  0x82) - receive touch events / PONG / LOG from device
- HID Keyboard IN (EP3 IN,  0x83) - send keystroke reports to host

Device exposes exactly two interfaces:
  Interface 0: Vendor Specific (0xFF) — bulk OUT + bulk IN
  Interface 1: HID Keyboard (0x03/0x01/0x01) — interrupt IN

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
import usb.backend

from ..protocol import (
    Frame,
    FrameStream,
    TouchReport,
    Cmd,
)


# Code Bot interfaces exposed by the firmware
VENDOR_INTERFACE = 0   # Vendor bulk IN/OUT
HID_INTERFACE    = 1   # HID Keyboard interrupt IN

# WCH USB VID + our PID
DEVICE_VID = 0x1A86
DEVICE_PID = 0xCB0B


def _safe_get_string(dev, index: int) -> Optional[str]:
    """Best-effort USB string descriptor fetch.

    Some firmwares advertise iManufacturer/iProduct/iSerial without a valid
    langid, which makes pyusb's get_string() raise. We swallow that here so
    discovery still succeeds and returns the partial info we have.
    """
    if not index:
        return None
    try:
        return usb.util.get_string(dev, index)
    except (usb.core.USBError, ValueError, NotImplementedError):
        return None


def _build_device_info(dev) -> DeviceInfo:
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
    """Thread-safe USB transport for Code Bot device."""

    def __init__(self, vid: int = DEVICE_VID, pid: int = DEVICE_PID, backend=None) -> None:
        self.vid = vid
        self.pid = pid
        self._backend = backend

        self._dev: Optional[usb.core.Device] = None
        self._ep_out = None  # 0x01 OUT: Vendor bulk OUT  (H→D commands)
        self._ep_in = None   # 0x82 IN:  Vendor bulk IN   (D→H frames)
        self._ep_hid = None  # 0x83 IN:  HID Keyboard IN   (D→H keystrokes)

        self._rx_lock = threading.Lock()
        self._tx_lock = threading.Lock()
        self._stream = FrameStream()
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
        """Open device, claim interfaces 0 (Vendor) and 1 (HID), wire up endpoints.

        Returns True on success.
        """
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
                pass  # non-Linux / not supported

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
                # one last try: detach + claim again
                try:
                    if self._dev.is_kernel_driver_active(iface):
                        self._dev.detach_kernel_driver(iface)
                    usb.util.claim_interface(self._dev, iface)
                except (NotImplementedError, usb.core.USBError):
                    pass

        # Wire up endpoints
        cfg = self._dev.get_active_configuration()
        for ep in cfg[(VENDOR_INTERFACE, 0)]:
            if usb.util.endpoint_direction(ep.bEndpointAddress) == usb.util.ENDPOINT_OUT:
                self._ep_out = ep
            elif usb.util.endpoint_direction(ep.bEndpointAddress) == usb.util.ENDPOINT_IN:
                self._ep_in = ep
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
            self._ep_hid = None

    @property
    def is_open(self) -> bool:
        return self._dev is not None and self._ep_out is not None

    # ----- Send (Vendor bulk OUT EP1) -----
    def send_frame(self, frame: Frame, timeout: int = 1000) -> bool:
        """Send a frame to the device via Vendor bulk OUT.

        On USBError, attempt to clear any endpoint halt and retry once — the
        WCH CH32X033 USBFS firmware occasionally leaves EP1 OUT NAK'd after a
        packet and needs a CLEAR_FEATURE to re-arm.
        """
        if not self.is_open:
            return False
        data = frame.encode()
        with self._tx_lock:
            try:
                self._ep_out.write(data, timeout=timeout)
                return True
            except usb.core.USBError as e:
                # Endpoint likely stalled (NAK forever after first packet).
                # Clear the halt and retry once.
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
        return self.send_frame(Frame(cmd=Cmd.PING))

    # ----- Send HID Keyboard (EP3 IN) -----
    def send_hid_report(self, report: bytes, timeout: int = 100) -> bool:
        """Send an 8-byte HID Keyboard report."""
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

    # ----- Receive (Vendor bulk IN EP2) -----
    def poll(self, timeout_ms: int = 10) -> list[Frame]:
        """Poll for incoming frames, return parsed ones.

        Non-blocking if timeout_ms small.
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
            frames = list(self._stream.feed(bytes(data)))
            # Track last touch event
            for f in frames:
                if f.cmd == Cmd.TOUCH_EVENT:
                    try:
                        self._last_touch = TouchReport.from_payload(f.payload)
                    except ValueError:
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
