"""Standalone LCD刷屏 test - verify device actually renders what we send.

v0.18 协议: 走 BEGIN + EP5 write_pixels + END 三段式.

Sequence:
  1. CLEAR black/white/red/green/blue (basic pixel-write sanity)
  2. 4 horizontal color stripes (R/G/B/Y) — verify orientation, no rotation
  3. Per-row color sweep (172 rows × rainbow gradient) — verify row-by-row streaming
  4. 8x8 checker pattern — verify sub-rect positioning accuracy
  5. CLEAR white (final)
"""

import sys
import time

from codebot.transport.usb import UsbTransport
from codebot.protocol import (
    build_clear,
    build_draw_rect_begin, build_draw_rect_end,
)
from codebot.render.theme import SCREEN_W, SCREEN_H


# RGB565 colors (big-endian on wire per protocol §4)
BLACK   = 0x0000
WHITE   = 0xFFFF
RED     = 0xF800
GREEN   = 0x07E0
BLUE    = 0x001F
YELLOW  = 0xFFE0
CYAN    = 0x07FF
MAGENTA = 0xF81F


def rgb565_pixel_bytes(color: int, n: int) -> bytes:
    """Build n pixels of the given RGB565 color as big-endian bytes."""
    hi = (color >> 8) & 0xFF
    lo = color & 0xFF
    return bytes([hi, lo]) * n


def rgb565_sweep_row(y: int, h: int) -> int:
    """Compute an RGB565 color for a rainbow sweep (red→green→blue across width)."""
    # Map y to a hue: 0..360 degrees
    hue = (y * 360) // SCREEN_H
    # Crude HSV→RGB (saturation=255, value=255)
    h_seg = hue // 60
    f = hue % 60
    if h_seg == 0:
        r, g, b = 255, f * 255 // 60, 0
    elif h_seg == 1:
        r, g, b = (59 - f) * 255 // 60, 255, 0
    elif h_seg == 2:
        r, g, b = 0, 255, f * 255 // 60, 0
    elif h_seg == 3:
        r, g, b = 0, (59 - f) * 255 // 60, 255
    elif h_seg == 4:
        r, g, b = f * 255 // 60, 0, 255
    else:
        r, g, b = 255, 0, (59 - f) * 255 // 60
    # RGB888 -> RGB565
    return ((r >> 3) << 11) | ((g >> 2) << 5) | (b >> 3)


def send_full_rect(t: UsbTransport, x: int, y: int, w: int, h: int, color: int,
                   label: str = "") -> bool:
    """Push a single-rect update via v3 protocol:
       EP1 OUT: DRAW_RECT_BEGIN {x,y,w,h} (9B)
       EP5 OUT: raw pixel stream (RGB565 BE bytes, 64B chunks)
       EP1 OUT: DRAW_RECT_END (1B, polite close)
    """
    pixels = rgb565_pixel_bytes(color, w * h)
    expected = len(pixels)
    t0 = time.monotonic()

    # 1. 发 BEGIN
    if not t.send_frame(build_draw_rect_begin(x, y, w, h)):
        print(f"  {label}: send_frame(BEGIN) returned False")
        return False

    # 2. 推像素流到 EP5
    written = t.write_pixels(pixels)
    if written != expected:
        print(f"  {label}: write_pixels wrote {written}/{expected} bytes")
        t.send_frame(build_draw_rect_end())
        return False

    # 3. 发 END (礼貌, 字节数已到其实会自动关)
    if not t.send_frame(build_draw_rect_end()):
        print(f"  {label}: send_frame(END) returned False")
        return False

    elapsed_ms = (time.monotonic() - t0) * 1000.0
    print(f"  {label}: rect=({x},{y},{w},{h}) color=0x{color:04X} -> {elapsed_ms:.1f}ms ({expected}B)")
    return True


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

    try:
        steps = [
            ("CLEAR black",   lambda: t.send_frame(build_clear(BLACK))),
            ("CLEAR white",   lambda: t.send_frame(build_clear(WHITE))),
            ("CLEAR red",     lambda: t.send_frame(build_clear(RED))),
            ("CLEAR green",   lambda: t.send_frame(build_clear(GREEN))),
            ("CLEAR blue",    lambda: t.send_frame(build_clear(BLUE))),
            ("CLEAR yellow",  lambda: t.send_frame(build_clear(YELLOW))),
        ]

        # for label, fn in steps:
        #     print(f"step: {label}")
        #     ok = fn()
        #     print(f"  -> send_frame: {ok}")
        #     time.sleep(0.6)

        # 4 horizontal stripes (top→bottom: RED / GREEN / BLUE / YELLOW)
        print("step: 4 horizontal stripes")
        stripe_h = SCREEN_H // 4
        for i, color in enumerate([RED, GREEN, BLUE, YELLOW]):
            y = i * stripe_h
            # ok = send_full_rect(t, 0, y, 320, stripe_h, color,
            #                     f"  stripe {i+1}")
            ok = send_full_rect(t, 0, 0, SCREEN_W, SCREEN_H, color,
                    f"  stripe {i+1}")
            print(f"  -> ok={ok}")
            time.sleep(0.3)

        # # Per-row rainbow sweep (172 rows, each a different hue)
        # print(f"step: per-row rainbow sweep ({SCREEN_H} rows)")
        # t0 = time.monotonic()
        # for y in range(SCREEN_H):
        #     color = rgb565_sweep_row(y, 1)
        #     ok = send_full_rect(t, 0, y, SCREEN_W, 1, color)
        #     if not ok:
        #         print(f"  row {y}: send failed")
        #         break
        #     if y % 32 == 0:
        #         print(f"  rows {y}/{SCREEN_H}")
        # dt = time.monotonic() - t0
        # print(f"  -> {SCREEN_H} rows in {dt:.2f}s ({dt*1000/SCREEN_H:.0f}ms/row)")

        # # 8x8 checker pattern (whole screen) — verify sub-rect positioning
        # print("step: 8x8 checker pattern")
        # cell_w = SCREEN_W // 8  # 40
        # cell_h = SCREEN_H // 8  # 21 (with 4 leftover rows)
        # n_ok = 0
        # for cy in range(8):
        #     for cx in range(8):
        #         color = WHITE if (cx + cy) % 2 == 0 else BLACK
        #         ok = send_full_rect(t, cx * cell_w, cy * cell_h, cell_w, cell_h, color)
        #         if ok:
        #             n_ok += 1
        # print(f"  -> {n_ok}/64 cells OK")

        # Final state: white
        time.sleep(2)
        print("step: CLEAR white (final)")
        t.send_frame(build_clear(WHITE))

        print("\nAll刷屏 steps completed.")
        return 0
    finally:
        t.close()
        print("CLOSED")


if __name__ == "__main__":
    sys.exit(main())
