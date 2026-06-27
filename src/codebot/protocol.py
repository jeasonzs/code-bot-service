"""Wire protocol codec - frame encoding/decoding (Python host side).

Mirrors firmware/src/protocol/proto.h
"""

from __future__ import annotations
import struct
from dataclasses import dataclass
from enum import IntEnum
from typing import Iterator


# ==============================================================
# Protocol constants
# ==============================================================
MAGIC = 0xCB
VERSION = 0x02
HEADER_SIZE = 8
CRC_SIZE = 2
MAX_PAYLOAD = 512
MAX_FRAMESIZE = HEADER_SIZE + MAX_PAYLOAD + CRC_SIZE  # 522

# Header layout (6 bytes + 2 bytes CRC, no padding)
# magic(1) + version(1) + cmd(1) + flags(1) + length(2 LE) = 6 bytes
_HEADER_FMT = "<4BH"  # 4 bytes + unsigned short
_CRC_OFFSET = 6
_HEADER_NO_CRC = struct.Struct(_HEADER_FMT)  # 6 bytes
_FULL_HEADER = struct.Struct("<4BHH")  # 8 bytes including CRC


class Cmd(IntEnum):
    """Command IDs (mirrors proto.h)."""

    PING = 0x01
    PONG = 0x02
    RESET_DISPLAY = 0x10
    SET_BRIGHTNESS = 0x11
    CLEAR = 0x12
    DRAW_RECTS = 0x20
    TOUCH_EVENT = 0x30
    HID_KEYSTROKES = 0x40
    LOG = 0xF0


class TouchEvent(IntEnum):
    """Touch event types (mirrors proto.h)."""

    DOWN = 0
    MOVE = 1
    UP = 2
    SWIPE_LEFT = 3
    SWIPE_RIGHT = 4
    LONG_PRESS = 5
    LONG_PRESS_RELEASE = 6


# ==============================================================
# CRC16-CCITT
# ==============================================================
def crc16_ccitt(data: bytes, init: int = 0xFFFF) -> int:
    """CRC16-CCITT: poly=0x1021, init=0xFFFF, no reflection, no xor-out."""
    crc = init
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            if crc & 0x8000:
                crc = ((crc << 1) ^ 0x1021) & 0xFFFF
            else:
                crc = (crc << 1) & 0xFFFF
    return crc


# ==============================================================
# Frame encode/decode
# ==============================================================
@dataclass
class Frame:
    """A wire protocol frame."""

    cmd: Cmd
    payload: bytes = b""

    def encode(self) -> bytes:
        """Serialize to bytes (header + payload + CRC)."""
        # Packed: magic(1) + version(1) + cmd(1) + flags(1) + length(2 LE)
        hdr = struct.pack(_HEADER_FMT, MAGIC, VERSION, int(self.cmd), 0, len(self.payload))
        body = hdr + self.payload
        crc = crc16_ccitt(body)
        return body + struct.pack("<H", crc)

    @classmethod
    def try_parse(cls, buf: bytes) -> tuple["Frame | None", int]:
        """Try to parse a frame from a buffer.

        Returns (frame_or_None, bytes_consumed).
        Returns (None, 0) if more data needed.
        Returns (None, n) where n is bytes to skip (error / invalid).
        """
        # Search for magic byte
        for start in range(len(buf)):
            if buf[start] == MAGIC:
                break
        else:
            return None, len(buf)  # no magic, discard all

        if len(buf) < start + HEADER_SIZE - CRC_SIZE:
            return None, 0  # need at least header (without CRC)

        # Read header (no CRC)
        version, cmd, flags, length = struct.unpack_from("<BBBH", buf, start + 1)
        if version != VERSION:
            return None, 1  # bad version, skip the magic byte
        if length > MAX_PAYLOAD:
            return None, 1  # bad length, skip

        # total = header(no CRC) + payload + CRC
        total = start + (HEADER_SIZE - CRC_SIZE) + length + CRC_SIZE
        if len(buf) < total:
            return None, 0  # need more data

        # Verify CRC
        body_end = start + (HEADER_SIZE - CRC_SIZE) + length
        body = buf[start : body_end]
        expected_crc = struct.unpack_from("<H", buf, body_end)[0]
        actual_crc = crc16_ccitt(body)
        if actual_crc != expected_crc:
            return None, 1  # bad CRC, skip the magic byte

        try:
            cmd_enum = Cmd(cmd)
        except ValueError:
            cmd_enum = Cmd(0)  # unknown
        payload_start = start + (HEADER_SIZE - CRC_SIZE)
        return cls(cmd=cmd_enum, payload=bytes(buf[payload_start : body_end])), total


class FrameStream:
    """Stream parser for protocol frames from a byte stream."""

    def __init__(self) -> None:
        self._buf = bytearray()

    def feed(self, data: bytes) -> Iterator[Frame]:
        """Feed bytes, yield parsed frames, buffer the rest."""
        if data:
            self._buf.extend(data)
        while self._buf:
            frame, consumed = Frame.try_parse(bytes(self._buf))
            if frame is None:
                if consumed > 0:
                    # discard invalid bytes
                    del self._buf[:consumed]
                break  # need more data
            del self._buf[:consumed]
            yield frame


# ==============================================================
# Command builders (host -> device)
# ==============================================================
def build_ping() -> Frame:
    """Build a PING frame."""
    return Frame(cmd=Cmd.PING)


def build_set_brightness(pct: int) -> Frame:
    """Build SET_BRIGHTNESS frame. pct: 0-100."""
    if not 0 <= pct <= 100:
        raise ValueError(f"pct must be 0-100, got {pct}")
    return Frame(cmd=Cmd.SET_BRIGHTNESS, payload=struct.pack("<B", pct))


def build_clear(rgb565: int) -> Frame:
    """Build CLEAR frame. rgb565: 16-bit color."""
    return Frame(cmd=Cmd.CLEAR, payload=struct.pack("<H", rgb565 & 0xFFFF))


def build_draw_rects(rects: list[tuple[int, int, int, int, bytes]]) -> Frame:
    """Build DRAW_RECTS frame.

    rects: list of (x, y, w, h, pixels_rgb565_big_endian)
    pixels must be w*h*2 bytes.
    """
    if not 0 < len(rects) <= 16:
        raise ValueError(f"rect count must be 1-16, got {len(rects)}")
    body = struct.pack("<B", len(rects))
    for x, y, w, h, pixels in rects:
        body += struct.pack("<HHHH", x, y, w, h)
        body += pixels
    if len(body) > MAX_PAYLOAD:
        raise ValueError(f"frame too large: {len(body)} > {MAX_PAYLOAD}")
    return Frame(cmd=Cmd.DRAW_RECTS, payload=body)


def build_hid_keystrokes(reports: list[bytes], delay_ms: int = 50) -> Frame:
    """Build HID_KEYSTROKES frame.

    reports: list of 8-byte HID Keyboard reports (modifier, reserved, 6 keycodes).
    Each report should be followed by a release report (all zeros) for proper keystroke.
    """
    for r in reports:
        if len(r) != 8:
            raise ValueError(f"each HID report must be 8 bytes, got {len(r)}")
    body = struct.pack("<BB", delay_ms, len(reports))
    for r in reports:
        body += r
    return Frame(cmd=Cmd.HID_KEYSTROKES, payload=body)


def build_reset_display() -> Frame:
    """Build RESET_DISPLAY frame."""
    return Frame(cmd=Cmd.RESET_DISPLAY)


# ==============================================================
# Touch event parser (device -> host)
# ==============================================================
@dataclass
class TouchReport:
    """Touch event from device."""

    event: TouchEvent
    x: int
    y: int

    @classmethod
    def from_payload(cls, payload: bytes) -> "TouchReport":
        if len(payload) < 5:
            raise ValueError(f"payload too short: {len(payload)}")
        event, x, y = struct.unpack("<BHH", payload[:5])
        return cls(event=TouchEvent(event), x=x, y=y)


# ==============================================================
# USB HID keycodes (subset, English US)
# ==============================================================
HID_KEY_A = 0x04
HID_KEY_B = 0x05
HID_KEY_C = 0x06
HID_KEY_D = 0x07
HID_KEY_E = 0x08
HID_KEY_F = 0x09
HID_KEY_G = 0x0A
HID_KEY_H = 0x0B
HID_KEY_I = 0x0C
HID_KEY_J = 0x0D
HID_KEY_K = 0x0E
HID_KEY_L = 0x0F
HID_KEY_M = 0x10
HID_KEY_N = 0x11
HID_KEY_O = 0x12
HID_KEY_P = 0x13
HID_KEY_Q = 0x14
HID_KEY_R = 0x15
HID_KEY_S = 0x16
HID_KEY_T = 0x17
HID_KEY_U = 0x18
HID_KEY_V = 0x19
HID_KEY_W = 0x1A
HID_KEY_X = 0x1B
HID_KEY_Y = 0x1C
HID_KEY_Z = 0x1D
HID_KEY_1 = 0x1E
HID_KEY_2 = 0x1F
HID_KEY_3 = 0x20
HID_KEY_4 = 0x21
HID_KEY_5 = 0x22
HID_KEY_6 = 0x23
HID_KEY_7 = 0x24
HID_KEY_8 = 0x25
HID_KEY_9 = 0x26
HID_KEY_0 = 0x27
HID_KEY_ENTER = 0x28
HID_KEY_ESCAPE = 0x29
HID_KEY_BACKSPACE = 0x2A
HID_KEY_TAB = 0x2B
HID_KEY_SPACE = 0x2C
HID_KEY_MINUS = 0x2D
HID_KEY_EQUAL = 0x2E
HID_KEY_LEFTBRACE = 0x2F
HID_KEY_RIGHTBRACE = 0x30
HID_KEY_BACKSLASH = 0x31
HID_KEY_SEMICOLON = 0x33
HID_KEY_APOSTROPHE = 0x34
HID_KEY_GRAVE = 0x35
HID_KEY_COMMA = 0x36
HID_KEY_DOT = 0x37
HID_KEY_SLASH = 0x38
HID_KEY_CAPSLOCK = 0x39
HID_KEY_F1 = 0x3A
HID_KEY_F12 = 0x45

# Modifier bits
HID_MOD_LCTRL = 0x01
HID_MOD_LSHIFT = 0x02
HID_MOD_LALT = 0x04
HID_MOD_LGUI = 0x08
HID_MOD_RCTRL = 0x10
HID_MOD_RSHIFT = 0x20
HID_MOD_RALT = 0x40
HID_MOD_RGUI = 0x80

# Map ASCII char (printable subset) -> (modifier, keycode)
_HID_ASCII_MAP: dict[str, tuple[int, int]] = {
    "a": (0, HID_KEY_A), "b": (0, HID_KEY_B), "c": (0, HID_KEY_C), "d": (0, HID_KEY_D),
    "e": (0, HID_KEY_E), "f": (0, HID_KEY_F), "g": (0, HID_KEY_G), "h": (0, HID_KEY_H),
    "i": (0, HID_KEY_I), "j": (0, HID_KEY_J), "k": (0, HID_KEY_K), "l": (0, HID_KEY_L),
    "m": (0, HID_KEY_M), "n": (0, HID_KEY_N), "o": (0, HID_KEY_O), "p": (0, HID_KEY_P),
    "q": (0, HID_KEY_Q), "r": (0, HID_KEY_R), "s": (0, HID_KEY_S), "t": (0, HID_KEY_T),
    "u": (0, HID_KEY_U), "v": (0, HID_KEY_V), "w": (0, HID_KEY_W), "x": (0, HID_KEY_X),
    "y": (0, HID_KEY_Y), "z": (0, HID_KEY_Z),
    "A": (HID_MOD_LSHIFT, HID_KEY_A), "B": (HID_MOD_LSHIFT, HID_KEY_B), "C": (HID_MOD_LSHIFT, HID_KEY_C),
    "D": (HID_MOD_LSHIFT, HID_KEY_D), "E": (HID_MOD_LSHIFT, HID_KEY_E), "F": (HID_MOD_LSHIFT, HID_KEY_F),
    "G": (HID_MOD_LSHIFT, HID_KEY_G), "H": (HID_MOD_LSHIFT, HID_KEY_H), "I": (HID_MOD_LSHIFT, HID_KEY_I),
    "J": (HID_MOD_LSHIFT, HID_KEY_J), "K": (HID_MOD_LSHIFT, HID_KEY_K), "L": (HID_MOD_LSHIFT, HID_KEY_L),
    "M": (HID_MOD_LSHIFT, HID_KEY_M), "N": (HID_MOD_LSHIFT, HID_KEY_N), "O": (HID_MOD_LSHIFT, HID_KEY_O),
    "P": (HID_MOD_LSHIFT, HID_KEY_P), "Q": (HID_MOD_LSHIFT, HID_KEY_Q), "R": (HID_MOD_LSHIFT, HID_KEY_R),
    "S": (HID_MOD_LSHIFT, HID_KEY_S), "T": (HID_MOD_LSHIFT, HID_KEY_T), "U": (HID_MOD_LSHIFT, HID_KEY_U),
    "V": (HID_MOD_LSHIFT, HID_KEY_V), "W": (HID_MOD_LSHIFT, HID_KEY_W), "X": (HID_MOD_LSHIFT, HID_KEY_X),
    "Y": (HID_MOD_LSHIFT, HID_KEY_Y), "Z": (HID_MOD_LSHIFT, HID_KEY_Z),
    "1": (0, HID_KEY_1), "2": (0, HID_KEY_2), "3": (0, HID_KEY_3), "4": (0, HID_KEY_4),
    "5": (0, HID_KEY_5), "6": (0, HID_KEY_6), "7": (0, HID_KEY_7), "8": (0, HID_KEY_8),
    "9": (0, HID_KEY_9), "0": (0, HID_KEY_0),
    "!": (HID_MOD_LSHIFT, HID_KEY_1), "@": (HID_MOD_LSHIFT, HID_KEY_2),
    "#": (HID_MOD_LSHIFT, HID_KEY_3), "$": (HID_MOD_LSHIFT, HID_KEY_4),
    "%": (HID_MOD_LSHIFT, HID_KEY_5), "^": (HID_MOD_LSHIFT, HID_KEY_6),
    "&": (HID_MOD_LSHIFT, HID_KEY_7), "*": (HID_MOD_LSHIFT, HID_KEY_8),
    "(": (HID_MOD_LSHIFT, HID_KEY_9), ")": (HID_MOD_LSHIFT, HID_KEY_0),
    " ": (0, HID_KEY_SPACE), "\n": (0, HID_KEY_ENTER), "\t": (0, HID_KEY_TAB),
    "-": (0, HID_KEY_MINUS), "_": (HID_MOD_LSHIFT, HID_KEY_MINUS),
    "=": (0, HID_KEY_EQUAL), "+": (HID_MOD_LSHIFT, HID_KEY_EQUAL),
    "[": (0, HID_KEY_LEFTBRACE), "{": (HID_MOD_LSHIFT, HID_KEY_LEFTBRACE),
    "]": (0, HID_KEY_RIGHTBRACE), "}": (HID_MOD_LSHIFT, HID_KEY_RIGHTBRACE),
    "\\": (0, HID_KEY_BACKSLASH), "|": (HID_MOD_LSHIFT, HID_KEY_BACKSLASH),
    ";": (0, HID_KEY_SEMICOLON), ":": (HID_MOD_LSHIFT, HID_KEY_SEMICOLON),
    "'": (0, HID_KEY_APOSTROPHE), '"': (HID_MOD_LSHIFT, HID_KEY_APOSTROPHE),
    "`": (0, HID_KEY_GRAVE), "~": (HID_MOD_LSHIFT, HID_KEY_GRAVE),
    ",": (0, HID_KEY_COMMA), "<": (HID_MOD_LSHIFT, HID_KEY_COMMA),
    ".": (0, HID_KEY_DOT), ">": (HID_MOD_LSHIFT, HID_KEY_DOT),
    "/": (0, HID_KEY_SLASH), "?": (HID_MOD_LSHIFT, HID_KEY_SLASH),
}


def string_to_hid_reports(text: str) -> list[bytes]:
    """Convert a string to a list of 8-byte HID Keyboard reports.

    Each character produces 2 reports: press (modifier+keycode) and release (all 0).
    User must focus target input before calling.
    """
    reports: list[bytes] = []
    for ch in text:
        if ch not in _HID_ASCII_MAP:
            # Skip unsupported chars (e.g. CJK, control codes)
            continue
        modifier, keycode = _HID_ASCII_MAP[ch]
        # Press: modifier + reserved + keycode + 5 zeros
        reports.append(bytes([modifier, 0, keycode, 0, 0, 0, 0, 0]))
        # Release: all zeros
        reports.append(bytes(8))
    return reports


__all__ = [
    "MAGIC", "VERSION", "HEADER_SIZE", "MAX_PAYLOAD", "MAX_FRAMESIZE",
    "Cmd", "TouchEvent", "Frame", "FrameStream",
    "crc16_ccitt", "TouchReport",
    "build_ping", "build_set_brightness", "build_clear",
    "build_draw_rects", "build_hid_keystrokes", "build_reset_display",
    "string_to_hid_reports",
    # HID keycodes
    "HID_KEY_A", "HID_KEY_B", "HID_KEY_C", "HID_KEY_D", "HID_KEY_E", "HID_KEY_F",
    "HID_KEY_G", "HID_KEY_H", "HID_KEY_I", "HID_KEY_J", "HID_KEY_K", "HID_KEY_L",
    "HID_KEY_M", "HID_KEY_N", "HID_KEY_O", "HID_KEY_P", "HID_KEY_Q", "HID_KEY_R",
    "HID_KEY_S", "HID_KEY_T", "HID_KEY_U", "HID_KEY_V", "HID_KEY_W", "HID_KEY_X",
    "HID_KEY_Y", "HID_KEY_Z", "HID_KEY_0", "HID_KEY_1", "HID_KEY_2", "HID_KEY_3",
    "HID_KEY_4", "HID_KEY_5", "HID_KEY_6", "HID_KEY_7", "HID_KEY_8", "HID_KEY_9",
    "HID_KEY_ENTER", "HID_KEY_ESCAPE", "HID_KEY_BACKSPACE", "HID_KEY_TAB", "HID_KEY_SPACE",
    "HID_MOD_LCTRL", "HID_MOD_LSHIFT", "HID_MOD_LALT", "HID_MOD_LGUI",
    "HID_MOD_RCTRL", "HID_MOD_RSHIFT", "HID_MOD_RALT", "HID_MOD_RGUI",
]
