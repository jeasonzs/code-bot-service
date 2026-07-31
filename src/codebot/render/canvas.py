"""Pillow Image canvas + dirty rect diff for efficient device updates."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from PIL import Image, ImageChops

from .theme import Color, VSCodeDark, SCREEN_W, SCREEN_H


@dataclass
class DirtyRect:
    """A dirty rectangle to send to the device."""

    x: int
    y: int
    w: int
    h: int
    pixels: bytes  # RGB565 big-endian, w*h*2 bytes

    def encode(self) -> bytes:
        """Encode as protocol payload bytes (count=1 + rect)."""
        import struct
        return struct.pack("<B", 1) + struct.pack("<HHHH", self.x, self.y, self.w, self.h) + self.pixels


class Canvas:
    """A 320x172 Pillow Image canvas with VSCode Dark+ background.

    Use draw_* methods to render content, then call diff() to get dirty rects.
    """

    def __init__(self) -> None:
        self.image: Image.Image = Image.new("RGB", (SCREEN_W, SCREEN_H), (VSCodeDark.BG.r, VSCodeDark.BG.g, VSCodeDark.BG.b))
        self._prev_image: Optional[Image.Image] = None

    def fill(self, color: Color) -> None:
        """Fill entire canvas with color."""
        self.image.paste((color.r, color.g, color.b), (0, 0, SCREEN_W, SCREEN_H))

    def paste_rect(self, color: Color, x: int, y: int, w: int, h: int) -> None:
        """Fill a rectangle with color."""
        self.image.paste((color.r, color.g, color.b), (x, y, x + w, y + h))

    def text(
        self,
        s: str,
        xy: tuple[int, int],
        color: Color,
        font: Optional[Image.Image] = None,
        size: int = 12,
    ) -> None:
        """Render text at (x, y) using a Pillow font (must be provided)."""
        from PIL import ImageDraw, ImageFont
        if font is None:
            font = ImageFont.load_default(size=size)
        draw = ImageDraw.Draw(self.image)
        draw.text(xy, s, fill=(color.r, color.g, color.b), font=font)

    def progress_bar(
        self,
        x: int, y: int, w: int, h: int,
        pct: float,
        fg: Color, bg: Color,
    ) -> None:
        """Draw a horizontal progress bar. pct: 0-100."""
        self.paste_rect(bg, x, y, w, h)
        fill_w = max(0, min(w, int(w * pct / 100)))
        if fill_w > 0:
            self.paste_rect(fg, x, y, fill_w, h)

    def to_rgb565_bytes(self, x: int = 0, y: int = 0, w: Optional[int] = None, h: Optional[int] = None) -> bytes:
        """Convert a region of the image to RGB565 big-endian bytes (pure Python)."""
        if w is None: w = SCREEN_W
        if h is None: h = SCREEN_H
        region = self.image.crop((x, y, x + w, y + h)).tobytes("raw", "RGB")
        n = len(region) // 3
        # RGB888 -> RGB565 (big-endian) — pack 2 bytes per pixel
        out = bytearray(n * 2)
        for i in range(n):
            r = region[i * 3]
            g = region[i * 3 + 1]
            b = region[i * 3 + 2]
            v = ((r >> 3) << 11) | ((g >> 2) << 5) | (b >> 3)
            out[i * 2]     = (v >> 8) & 0xFF
            out[i * 2 + 1] = v & 0xFF
        return bytes(out)

    def find_dirty_rects(self, max_rects: int = 16) -> list[DirtyRect]:
        """Find dirty rects by diffing with previous frame.

        Returns up to max_rects dirty rectangles. If more than max_rects,
        returns a single full-screen rect.
        """
        if self._prev_image is None:
            # First frame: full screen
            self._prev_image = self.image.copy()
            pixels = self.to_rgb565_bytes()
            return [DirtyRect(0, 0, SCREEN_W, SCREEN_H, pixels)]

        diff = ImageChops.difference(self._prev_image, self.image)
        bbox = diff.getbbox()
        if bbox is None:
            return []  # no change

        x0, y0, x1, y1 = bbox
        # If entire screen changed, return one full rect
        if (x1 - x0) * (y1 - y0) > (SCREEN_W * SCREEN_H * 0.5) or max_rects == 1:
            pixels = self.to_rgb565_bytes()
            self._prev_image = self.image.copy()
            return [DirtyRect(0, 0, SCREEN_W, SCREEN_H, pixels)]

        # Simple: return the bounding box of all changes
        # (Could be improved with row-based segmentation, but good enough for v1)
        pixels = self.to_rgb565_bytes(x0, y0, x1 - x0, y1 - y0)
        self._prev_image = self.image.copy()
        return [DirtyRect(x0, y0, x1 - x0, y1 - y0, pixels)]

    def mark_clean(self) -> None:
        """Mark current image as the new baseline for diffing."""
        self._prev_image = self.image.copy()

    def mark_all_dirty(self) -> None:
        """Force next find_dirty_rects() to return a full-screen rect.

        Used after device reconnect: the device LCD has stale content
        from before disconnect, but _prev_image only knows about the
        pre-disconnect state. A normal diff would return [] or partial
        rects and leave the device LCD out of sync.
        """
        self._prev_image = None

    def paint_swipe(self, prev: "Canvas", next: "Canvas", offset_px: int) -> None:
        """Compose a swipe-frame: prev page at offset_px + next page at offset_px - sign*W.

        offset_px is the finger's horizontal displacement (positive = right,
        negative = left). The whole image shifts WITH the finger; next sits
        on the side the finger is pulling from.
        Caller must have already rendered prev/next (including chrome).
        """
        self.image.paste(prev.image, (offset_px, 0))
        if offset_px >= 0:
            self.image.paste(next.image, (offset_px - SCREEN_W, 0))
        else:
            self.image.paste(next.image, (offset_px + SCREEN_W, 0))
