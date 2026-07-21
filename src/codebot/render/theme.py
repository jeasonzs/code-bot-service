"""VSCode Dark+ theme color palette for Code Bot UI.

Mirrors the C-side VSCodeDark theme constants.
"""

from __future__ import annotations
from dataclasses import dataclass


@dataclass(frozen=True)
class Color:
    """16-bit RGB565 color (matches device-side pixel format)."""

    r: int
    g: int
    b: int

    def to_rgb565(self) -> int:
        """Convert to RGB565 16-bit integer (big-endian as sent to device)."""
        return ((self.r >> 3) << 11) | ((self.g >> 2) << 5) | (self.b >> 3)

    def to_bytes(self) -> bytes:
        """Return big-endian 2 bytes (for SPI transmission)."""
        v = self.to_rgb565()
        return bytes([(v >> 8) & 0xFF, v & 0xFF])

    @classmethod
    def from_rgb565(cls, value: int) -> "Color":
        r = ((value >> 11) & 0x1F) << 3
        g = ((value >> 5) & 0x3F) << 2
        b = (value & 0x1F) << 3
        return cls(r=r, g=g, b=b)


class VSCodeDark:
    """VSCode Dark+ theme palette."""

    # Backgrounds
    BG          = Color(30, 30, 30)       # #1E1E1E
    BG_PANEL    = Color(37, 37, 38)       # #252526
    BG_HEADER   = Color(24, 24, 24)       # #181818
    BG_HOVER    = Color(42, 45, 46)       # #2A2D2E
    BORDER      = Color(60, 60, 60)       # #3C3C3C

    # Text
    FG          = Color(212, 212, 212)    # #D4D4D4
    FG_DIM      = Color(133, 133, 133)    # #858585
    FG_DISABLED = Color(90, 90, 90)       # #5A5A5A

    # Status
    SUCCESS     = Color(140, 235, 210)    # #8CEBD2
    WARNING     = Color(220, 220, 170)    # #DCDCAA
    DANGER      = Color(244, 71, 71)      # #F44747
    INFO        = Color(140, 210, 250)    # #8CD2FA

    # Syntax (for rendered text labels)
    SYN_KEYWORD = Color(140, 210, 250)    # #8CD2FA (= INFO)
    SYN_STRING  = Color(206, 145, 120)    # #CE9178
    SYN_NUMBER  = Color(181, 206, 168)    # #B5CEA8
    SYN_COMMENT = Color(106, 153, 85)     # #6A9955
    SYN_FUNC    = Color(220, 220, 170)    # #DCDCAA
    SYN_VAR     = Color(156, 220, 254)    # #9CDCFE
    SYN_TYPE    = Color(140, 235, 210)    # #8CEBD2 (= SUCCESS)

    # Page indicator
    INDICATOR_BASE    = Color(140, 210, 250)    # #8CD2FA (= INFO)
    INDICATOR_ACTIVE  = Color(220, 220, 170)    # #DCDCAA

    # Dashboard tile accents (SystemPage 2x2 grid)
    MEM_ACCENT        = Color(197, 134, 192)    # #C586C0 (VSCode purple)
    NET_UP            = Color(140, 235, 210)    # #8CEBD2 (= SUCCESS)
    NET_DOWN          = Color(86, 192, 230)     # #56C0E6 (cyan)
    FREQ_ACCENT       = Color(140, 210, 250)    # #8CD2FA (= INFO)

    # Dotted-bar "off" cell color (slightly above BG)
    BAR_DIM           = Color(50, 50, 50)       # #323232


# Display dimensions
SCREEN_W = 320
SCREEN_H = 172
INDICATOR_H = 4  # top indicator bar height
TITLE_H = 20      # title area height
HINT_H = 8        # bottom hint area height
CONTENT_Y = INDICATOR_H + TITLE_H  # = 24
CONTENT_H = SCREEN_H - CONTENT_Y - HINT_H  # = 140
