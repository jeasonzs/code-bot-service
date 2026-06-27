"""USB transport layer - pyusb-based communication with the device.

Handles:
- USB device discovery (vendor 0x1A86, product 0xCB0B)
- Vendor bulk OUT (EP1) - send commands to device
- Vendor bulk IN (EP2) - receive touch events from device
- HID Keyboard IN (EP3) - send keystroke reports
"""

from __future__ import annotations

import threading
import time
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


# WCH USB VID + our PID
DEVICE_VID = 0x1A86
DEVICE_PID = 0xCB0B


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
        self._ep_out = None  # EP1 OUT: Vendor bulk OUT
        self._ep_in = None   # EP2 IN:  Vendor bulk IN
        self._ep_hid = None  # EP3 IN:  HID Keyboard report

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
        return DeviceInfo(
            vendor_id=dev.idVendor,
            product_id=dev.idProduct,
            bus=dev.bus,
            address=dev.address,
            port=dev.port_number if hasattr(dev, "port_number") else 0,
            serial=usb.util.get_string(dev, dev.iSerialNumber) if dev.iSerialNumber else None,
            product_name=usb.util.get_string(dev, dev.iProduct) if dev.iProduct else None,
        )

    def list_all(self) -> list[DeviceInfo]:
        """List all Code Bot devices on USB bus."""
        results: list[DeviceInfo] = []
        for dev in usb.core.find(
            find_all=True,
            idVendor=self.vid,
            idProduct=self.pid,
            backend=self._backend,
        ):
            results.append(DeviceInfo(
                vendor_id=dev.idVendor,
                product_id=dev.idProduct,
                bus=dev.bus,
                address=dev.address,
                port=dev.port_number if hasattr(dev, "port_number") else 0,
                serial=usb.util.get_string(dev, dev.iSerialNumber) if dev.iSerialNumber else None,
                product_name=usb.util.get_string(dev, dev.iProduct) if dev.iProduct else None,
            ))
        return results

    # ----- Open/close -----
    def open(self) -> bool:
        """Open device, claim interface, prepare endpoints.

        Returns True on success.
        """
        self._dev = usb.core.find(
            idVendor=self.vid,
            idProduct=self.pid,
            backend=self._backend,
        )
        if self._dev is None:
            return False

        try:
            # Detach kernel driver if needed (Linux)
            if self._dev.is_kernel_driver_active(0):
                self._dev.detach_kernel_driver(0)
        except (NotImplementedError, usb.core.USBError):
            pass  # not supported on this platform

        # Set configuration (idempotent)
        try:
            self._dev.set_configuration()
        except usb.core.USBError:
            pass

        # Claim all 3 interfaces (Vendor / HID / CDC)
        # We claim them one at a time to avoid Windows issues
        for iface in (0, 1, 2):
            try:
                usb.util.claim_interface(self._dev, iface)
            except usb.core.USBError:
                # If interface is busy, try detach kernel driver first
                try:
                    if self._dev.is_kernel_driver_active(iface):
                        self._dev.detach_kernel_driver(iface)
                    usb.util.claim_interface(self._dev, iface)
                except (NotImplementedError, usb.core.USBError):
                    pass  # give up on this interface

        # Find endpoints
        cfg = self._dev.get_active_configuration()
        for ep in cfg[(0, 0)]:  # Interface 0, alt 0
            if usb.util.endpoint_direction(ep.bEndpointAddress) == usb.util.ENDPOINT_OUT:
                self._ep_out = ep
            elif usb.util.endpoint_direction(ep.bEndpointAddress) == usb.util.ENDPOINT_IN:
                self._ep_in = ep
        for ep in cfg[(1, 0)]:  # Interface 1, alt 0 (HID)
            if usb.util.endpoint_direction(ep.bEndpointAddress) == usb.util.ENDPOINT_IN:
                self._ep_hid = ep
        return True

    def close(self) -> None:
        """Release interfaces and close device."""
        if self._dev is not None:
            for iface in (0, 1, 2):
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
        """Send a frame to the device via Vendor bulk OUT."""
        if not self.is_open:
            return False
        data = frame.encode()
        with self._tx_lock:
            try:
                self._ep_out.write(data, timeout=timeout)
                return True
            except usb.core.USBError:
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
