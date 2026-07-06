"""Wire protocol v3 codec - host (Python) side.

Mirrors firmware/src/protocol/proto.h.

v3 重构: 无 magic/ver/len/crc, 每个命令 = 1B cmd + packed struct (≤64B 单包).
"""

from __future__ import annotations
import struct
from dataclasses import dataclass
from enum import IntEnum
from typing import Optional


# ==============================================================
# Command IDs (mirrors proto.h)
# ==============================================================
class Cmd(IntEnum):
    """Command IDs."""

    PING              = 0x01
    PONG              = 0x02   # device → host
    RESET_DISPLAY     = 0x10
    SET_BRIGHTNESS    = 0x11
    CLEAR             = 0x12
    DRAW_RECT_BEGIN   = 0x20   # 打开 EP5 OUT 数据通道
    DRAW_RECT_END     = 0x21   # 礼貌结束
    DRAW_RECT_ABORT   = 0x22   # 强制结束
    TOUCH_EVENT       = 0x30   # device → host
    LOG               = 0xF0   # device → host


class TouchEvent(IntEnum):
    """Touch event types (mirrors proto.h)."""

    DOWN              = 0
    MOVE              = 1
    UP                = 2
    SWIPE_LEFT        = 3
    SWIPE_RIGHT       = 4
    LONG_PRESS        = 5
    LONG_PRESS_RELEASE = 6


# ==============================================================
# Packed struct formats (LE)
# ==============================================================
# EP1 OUT (host -> device)
FMT_SET_BRIGHTNESS    = struct.Struct("<BB")         # cmd, brightness
FMT_CLEAR             = struct.Struct("<BH")         # cmd, color RGB565 LE
FMT_DRAW_RECT_BEGIN   = struct.Struct("<BHHHH")      # cmd, x, y, w, h

# EP2 IN (device -> host)
FMT_PONG              = struct.Struct("<BI")         # cmd, status (LE)
FMT_TOUCH_EVENT       = struct.Struct("<BBHH")       # cmd, event_type, x, y


# ==============================================================
# Frame encode/decode (v3 simple codec)
# ==============================================================
@dataclass
class Frame:
    """A wire protocol v3 frame: 1B cmd + payload."""

    cmd: Cmd
    payload: bytes = b""

    def encode(self) -> bytes:
        """Serialize to bytes: [cmd (1B)] [payload (N B)].

        No header, no CRC, no length field. USB bulk SIE has hardware CRC.
        Total size is 1 + len(payload), max 64B.
        """
        return bytes([int(self.cmd)]) + self.payload

    @classmethod
    def decode(cls, data: bytes) -> Optional["Frame"]:
        """Parse a single frame from bytes. Returns None if too short.

        The caller is responsible for tracking the 1-frame-per-USB-packet
        invariant: every USB OUT/IN packet is exactly one frame.
        """
        if len(data) < 1:
            return None
        cmd_byte = data[0]
        try:
            cmd_enum = Cmd(cmd_byte)
        except ValueError:
            cmd_enum = Cmd(0)  # unknown; caller may ignore
        return cls(cmd=cmd_enum, payload=bytes(data[1:]))

    # ----- Convenience builders (host -> device) -----
    @classmethod
    def ping(cls) -> "Frame":
        return cls(cmd=Cmd.PING)

    @classmethod
    def reset_display(cls) -> "Frame":
        return cls(cmd=Cmd.RESET_DISPLAY)

    @classmethod
    def set_brightness(cls, pct: int) -> "Frame":
        if not 0 <= pct <= 100:
            raise ValueError(f"pct must be 0-100, got {pct}")
        return cls(cmd=Cmd.SET_BRIGHTNESS, payload=FMT_SET_BRIGHTNESS.pack(Cmd.SET_BRIGHTNESS, pct)[1:])

    @classmethod
    def clear(cls, rgb565: int) -> "Frame":
        return cls(cmd=Cmd.CLEAR, payload=FMT_CLEAR.pack(Cmd.CLEAR, rgb565 & 0xFFFF)[1:])

    @classmethod
    def draw_rect_begin(cls, x: int, y: int, w: int, h: int) -> "Frame":
        return cls(cmd=Cmd.DRAW_RECT_BEGIN, payload=FMT_DRAW_RECT_BEGIN.pack(Cmd.DRAW_RECT_BEGIN, x, y, w, h)[1:])

    @classmethod
    def draw_rect_end(cls) -> "Frame":
        return cls(cmd=Cmd.DRAW_RECT_END)

    @classmethod
    def draw_rect_abort(cls) -> "Frame":
        return cls(cmd=Cmd.DRAW_RECT_ABORT)

    # ----- Decoded payload accessors (device -> host) -----
    def decode_pong(self) -> int:
        """Extract status flags from PONG payload."""
        if self.cmd != Cmd.PONG:
            raise ValueError(f"not a PONG frame: {self.cmd}")
        return struct.unpack("<I", self.payload)[0]

    def decode_touch(self) -> "TouchReport":
        """Extract touch event from TOUCH_EVENT payload."""
        if self.cmd != Cmd.TOUCH_EVENT:
            raise ValueError(f"not a TOUCH_EVENT frame: {self.cmd}")
        event_type, x, y = struct.unpack("<BHH", self.payload)
        return TouchReport(event_type=TouchEvent(event_type), x=x, y=y)


@dataclass
class TouchReport:
    """Touch event payload decoded from TOUCH_EVENT frame."""

    event_type: TouchEvent
    x: int
    y: int


# ==============================================================
# Convenience builders (top-level functions, keep API compatibility)
# ==============================================================
def build_ping() -> Frame:
    return Frame.ping()


def build_set_brightness(pct: int) -> Frame:
    return Frame.set_brightness(pct)


def build_clear(rgb565: int) -> Frame:
    return Frame.clear(rgb565)


def build_draw_rect_begin(x: int, y: int, w: int, h: int) -> Frame:
    return Frame.draw_rect_begin(x, y, w, h)


def build_draw_rect_end() -> Frame:
    return Frame.draw_rect_end()


def build_draw_rect_abort() -> Frame:
    return Frame.draw_rect_abort()


__all__ = [
    "Cmd",
    "TouchEvent",
    "Frame",
    "TouchReport",
    "build_ping",
    "build_set_brightness",
    "build_clear",
    "build_draw_rect_begin",
    "build_draw_rect_end",
    "build_draw_rect_abort",
]
