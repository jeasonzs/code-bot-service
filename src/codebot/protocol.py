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
        """Serialize to bytes (8B header incl. CRC + payload).

        Wire layout per protocol.md §2:
            [header 8B: magic ver cmd flags length crc16] [payload N B]

        CRC is computed over the 6-byte header-without-CRC + payload, then
        stored in the header's crc16 field (offset 6-7, LE).
        """
        hdr_no_crc = struct.pack(_HEADER_FMT, MAGIC, VERSION, int(self.cmd), 0, len(self.payload))
        crc = crc16_ccitt(hdr_no_crc + self.payload)
        full_hdr = _FULL_HEADER.pack(MAGIC, VERSION, int(self.cmd), 0, len(self.payload), crc)
        return full_hdr + self.payload

    @classmethod
    def try_parse(cls, buf: bytes) -> tuple["Frame | None", int]:
        """Try to parse a frame from a buffer.

        Wire layout (per protocol.md §2):
            [header 8B: magic ver cmd flags length crc16] [payload N B]

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

        if len(buf) < start + HEADER_SIZE:
            return None, 0  # need at least full 8B header

        # Read full header (incl. CRC field at offset 6-7)
        magic2, version, cmd, flags, length, crc16_field = struct.unpack_from(
            "<4BHH", buf, start
        )
        assert magic2 == MAGIC
        if version != VERSION:
            return None, 1  # bad version, skip the magic byte
        if length > MAX_PAYLOAD:
            return None, 1  # bad length, skip

        # total = header(8) + payload(length)
        total = start + HEADER_SIZE + length
        if len(buf) < total:
            return None, 0  # need more data

        # CRC is computed over header without CRC field (6B) + payload (N B)
        body = buf[start : start + (HEADER_SIZE - CRC_SIZE)]
        body += buf[start + HEADER_SIZE : total]
        expected_crc = crc16_field
        actual_crc = crc16_ccitt(body)
        if actual_crc != expected_crc:
            return None, 1  # bad CRC, skip the magic byte

        try:
            cmd_enum = Cmd(cmd)
        except ValueError:
            cmd_enum = Cmd(0)  # unknown
        payload_start = start + HEADER_SIZE
        return cls(cmd=cmd_enum, payload=bytes(buf[payload_start : total])), total


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

    Caller is responsible for keeping total payload ≤ MAX_PAYLOAD=512B.
    For dirty regions larger than that, use build_draw_rects_chunked().

    Note: count is uint16_t (2B LE), not uint8_t, so pixel data starts at
    an even-aligned offset inside the payload. This avoids misaligned
    halfword loads on RISC-V targets like CH32X035.
    """
    if not 0 < len(rects) <= 16:
        raise ValueError(f"rect count must be 1-16, got {len(rects)}")
    body = struct.pack("<H", len(rects))     # 2-byte count
    for x, y, w, h, pixels in rects:
        body += struct.pack("<HHHH", x, y, w, h)
        body += pixels
    if len(body) > MAX_PAYLOAD:
        raise ValueError(f"frame too large: {len(body)} > {MAX_PAYLOAD}")
    return Frame(cmd=Cmd.DRAW_RECTS, payload=body)


# Single-rect payload overhead: 2 (count) + 4*2 (x,y,w,h) = 10 bytes.
# Pixel data starts at offset 10 (even-aligned). Remaining bytes are pixels:
# 2 bytes per pixel RGB565. Max pixels per chunk = (512 - 10) // 2 = 251.
_RECT_HEADER_BYTES = 2 + 4 * 2
_MAX_PIXELS_PER_RECT = (MAX_PAYLOAD - _RECT_HEADER_BYTES) // 2  # 251


def chunk_rect(x: int, y: int, w: int, h: int, pixels: bytes) -> list[Frame]:
    """Split a single (x, y, w, h, pixels) rect into ≤MAX_PAYLOAD DRAW_RECTS frames.

    Strategy: 1-row strips of width ≤251 pixels (each frame = header + at most
    503B pixels = 512B total). For full-screen 320×172: 2 cols × 172 rows = 344 frames.
    """
    if len(pixels) != w * h * 2:
        raise ValueError(f"pixels length {len(pixels)} != w*h*2 = {w*h*2}")
    if w == 0 or h == 0:
        return []
    if w * h <= _MAX_PIXELS_PER_RECT:
        # Whole rect fits in one frame
        frame = build_draw_rects([(x, y, w, h, pixels)])
        return [frame]

    # Split into columns of width ≤ _MAX_PIXELS_PER_RECT, 1 row each
    frames: list[Frame] = []
    cols = (w + _MAX_PIXELS_PER_RECT - 1) // _MAX_PIXELS_PER_RECT
    col_w = w // cols
    remainder = w - col_w * cols  # leftover pixels go to last column
    for row in range(h):
        row_off = row * w * 2
        for c in range(cols):
            cx = x + c * col_w
            cw = col_w + (1 if c == cols - 1 and remainder > 0 else 0)
            # Re-pack LE: each row's pixels are tightly packed; extract slice
            sub = bytearray(cw * 2)
            src_x = c * col_w
            for px in range(cw):
                src = row_off + (src_x + px) * 2
                sub[px * 2]     = pixels[src]
                sub[px * 2 + 1] = pixels[src + 1]
            frames.append(build_draw_rects([(cx, y + row, cw, 1, bytes(sub))]))
    return frames


def build_draw_rects_chunked(rects: list[tuple[int, int, int, int, bytes]]) -> list[Frame]:
    """Split each (potentially large) rect into multiple ≤MAX_PAYLOAD DRAW_RECTS frames.

    Returns a flat list of frames; caller sends them all sequentially.
    """
    out: list[Frame] = []
    for x, y, w, h, pixels in rects:
        out.extend(chunk_rect(x, y, w, h, pixels))
    return out


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
