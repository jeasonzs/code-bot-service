"""Standalone LCD刷屏 test - continuously刷 vertical color stripes.

Verifies that the device renders 8 vertical color stripes (R G B Y C M W K,
each 40px wide) without horizontal drift over time. Press Ctrl+C to stop.
"""

import sys
import time

from codebot.transport.usb import UsbTransport
from codebot.protocol import build_clear, build_draw_rect_begin, build_draw_rect_end
from codebot.render.theme import SCREEN_W, SCREEN_H


# RGB565 colors (big-endian on wire per protocol §4)
RED     = 0xF800
GREEN   = 0x07E0
BLUE    = 0x001F
YELLOW  = 0xFFE0
CYAN    = 0x07FF
MAGENTA = 0xF81F
WHITE   = 0xFFFF
BLACK   = 0x0000


def send_full_buffer(t: UsbTransport, x: int, y: int, w: int, h: int,
                     pixels: bytes) -> bool:
    """Push a pre-built RGB565 BE pixel buffer via v3 protocol.

    EP1 OUT: DRAW_RECT_BEGIN {x,y,w,h} (9B)
    EP5 OUT: raw pixel stream (RGB565 BE bytes)
    EP1 OUT: DRAW_RECT_END (1B, polite close)
    """
    expected = len(pixels)
    if not t.send_frame(build_draw_rect_begin(x, y, w, h)):
        return False
    written = t.write_pixels(pixels)
    if written != expected:
        t.send_frame(build_draw_rect_end())
        return False
    return t.send_frame(build_draw_rect_end())


def build_vertical_stripes(w: int, h: int, colors: list[int]) -> bytes:
    """Build RGB565 BE pixel buffer: w // len(colors) px wide vertical stripes."""
    stripe_w = w // len(colors)
    out = bytearray(w * h * 2)
    for y in range(h):
        row_off = y * w * 2
        for x in range(w):
            c = colors[x // stripe_w]
            off = row_off + x * 2
            out[off]     = (c >> 8) & 0xFF
            out[off + 1] = c & 0xFF
    return bytes(out)


def main() -> int:
    t = UsbTransport()
    info = t.find()
    if info is None:
        print("FAIL: device 1a86:cb0b not found")
        return 1
    print(f"FOUND: bus={info.bus} addr={info.address}")
    if not t.open():
        print("FAIL: open() returned False")
        return 2

    steps = [
        ("CLEAR black",   lambda: t.send_frame(build_clear(BLACK))),
        ("CLEAR white",   lambda: t.send_frame(build_clear(WHITE))),
        ("CLEAR red",     lambda: t.send_frame(build_clear(RED))),
        ("CLEAR green",   lambda: t.send_frame(build_clear(GREEN))),
        ("CLEAR blue",    lambda: t.send_frame(build_clear(BLUE))),
        ("CLEAR yellow",  lambda: t.send_frame(build_clear(YELLOW))),
    ]

    for label, fn in steps:
        print(f"step: {label}")
        ok = fn()
        print(f"  -> send_frame: {ok}")
        time.sleep(0.6)

    vbuf = build_vertical_stripes(SCREEN_W, SCREEN_H,
                                   [RED, GREEN, BLUE, YELLOW,
                                    CYAN, MAGENTA, WHITE, BLACK])
    print(f"LOOP: pushing 320x172 vertical stripes ({len(vbuf)}B/frame) - Ctrl+C to stop")
    n_frames = 0
    n_err = 0
    t0 = time.monotonic()
    try:
        while True:
            if not send_full_buffer(t, 0, 0, SCREEN_W, SCREEN_H, vbuf):
                n_err += 1
                print(f"  send failed (#{n_err})")
            n_frames += 1
            if n_frames % 30 == 0:
                dt = time.monotonic() - t0
                fps = n_frames / dt
                print(f"  {n_frames} frames in {dt:.1f}s = {fps:.2f} fps (err={n_err})")
    except KeyboardInterrupt:
        dt = time.monotonic() - t0
        fps = n_frames / dt if dt > 0 else 0
        print(f"\nSTOPPED after {n_frames} frames in {dt:.1f}s = {fps:.2f} fps (err={n_err})")
        return 0
    finally:
        t.close()
        print("CLOSED")


if __name__ == "__main__":
    sys.exit(main())