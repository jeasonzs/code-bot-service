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
        """Convert a region of the image to RGB565 big-endian bytes."""
        if w is None: w = SCREEN_W
        if h is None: h = SCREEN_H
        region = self.image.crop((x, y, x + w, y + h))
        # Pillow RGB888 -> RGB565
        # 320x172x2 = 110 KB
        r5 = (region.tobytes("raw", "R")[::3] >> 3).astype("uint8")
        g6 = (region.tobytes("raw", "G")[::3] >> 2).astype("uint8")
        b5 = (region.tobytes("raw", "B")[::3] >> 3).astype("uint8")
        # Pack: RRRRRGGGGGGBBBBB -> big-endian
        import numpy as np
        r5 = np.frombuffer(region.tobytes("raw", "R"), dtype=np.uint8)[::3] >> 3
        g6 = np.frombuffer(region.tobytes("raw", "G"), dtype=np.uint8)[::3] >> 2
        b5 = np.frombuffer(region.tobytes("raw", "B"), dtype=np.uint8)[::3] >> 3
        rgb565 = (r5.astype(np.uint16) << 11) | (g6.astype(np.uint16) << 5) | b5.astype(np.uint16)
        return rgb565.astype(">u2").tobytes()  # big-endian uint16

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
