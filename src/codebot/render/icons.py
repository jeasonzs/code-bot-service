"""Icon renderer for the SystemPage dashboard.

Two backends:
  - **Bitmap** icons: PNGs with alpha channel, loaded from
    `<repo>/code-bot-service/icons/<name>.png`. Original colors preserved
    (the `color` arg is ignored for bitmaps).
  - **PIL-primitive** icons: drawn from geometry (lines/rects/polygons).
    Used for icons we don't have bitmaps for: `terminal` (text `>_`),
    `fan`, `net` (composite of two stacked arrows via primitive — bitmaps
    `up` and `down` are used separately in SystemPage).

Kinds backed by bitmaps: cpu, mem, freq, temp, up, down, disk, stars,
streak, commits, prs, status, context.
Kinds backed by primitives: terminal, fan.
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

from .canvas import Canvas
from .theme import Color
from .widgets import get_font


# Project icons directory (sibling of fonts/).
_ICONS_DIR = Path(__file__).resolve().parent.parent.parent.parent / "icons"

# Cache: name -> PIL Image (the original 50x50 RGBA source).
_bitmap_cache: dict[str, Image.Image] = {}


def _load_bitmap(name: str) -> Image.Image:
    """Load a bitmap icon from icons/<name>.png (cached)."""
    if name not in _bitmap_cache:
        path = _ICONS_DIR / (name + ".png")
        _bitmap_cache[name] = Image.open(path).convert("RGBA")
    return _bitmap_cache[name]


def _draw_bitmap(
    canvas: Canvas, name: str, x: int, y: int, size: int,
) -> None:
    """Composite a bitmap icon at (x, y) resized to size×size with alpha.

    Uses `paste` with the alpha channel as mask, so it works on RGB
    (Canvas.image) without needing a full RGBA conversion.
    """
    img = _load_bitmap(name)
    if img.size != (size, size):
        img = img.resize((size, size), Image.LANCZOS)
    canvas.image.paste(img, (x, y), img)


def _rgb(color: Color) -> tuple[int, int, int]:
    return (color.r, color.g, color.b)


def _draw_cpu(d: ImageDraw.ImageDraw, x: int, y: int, n: int, color: Color) -> None:
    """CPU chip: rectangular body, pins on all 4 sides, inner core."""
    rgb = _rgb(color)
    body = max(4, n * 3 // 4)
    ox = x + (n - body) // 2
    oy = y + (n - body) // 2
    # Chip body outline
    d.rectangle([(ox, oy), (ox + body - 1, oy + body - 1)], outline=rgb, width=1)
    # Inner core
    core = max(2, n // 8)
    cx = ox + (body - core) // 2
    cy = oy + (body - core) // 2
    d.rectangle([(cx, cy), (cx + core - 1, cy + core - 1)], fill=rgb)
    # Pins: 3 per side at large size, 2 at small size
    pin_ext = max(1, n // 8)
    pin_count = 3 if n >= 24 else 2
    for i in range(pin_count):
        off = body * (i + 1) // (pin_count + 1)
        px = ox + off  # for vertical pins
        py = oy + off  # for horizontal pins
        # Top
        d.line([(px, max(y, oy - pin_ext)), (px, oy - 1)], fill=rgb, width=1)
        # Bottom
        d.line([(px, oy + body), (px, min(y + n - 1, oy + body + pin_ext - 1))], fill=rgb, width=1)
        # Left
        d.line([(max(x, ox - pin_ext), py), (ox - 1, py)], fill=rgb, width=1)
        # Right
        d.line([(ox + body, py), (min(x + n - 1, ox + body + pin_ext - 1), py)], fill=rgb, width=1)


def _draw_mem(d: ImageDraw.ImageDraw, x: int, y: int, n: int, color: Color) -> None:
    """RAM stick: horizontal body, surface lines, contact notches below."""
    rgb = _rgb(color)
    body_w = max(6, n * 7 // 8)
    body_h = max(3, n * 3 // 8)
    ox = x + (n - body_w) // 2
    oy = y + (n - body_h) // 2 - max(1, n // 16)
    d.rectangle([(ox, oy), (ox + body_w - 1, oy + body_h - 1)], outline=rgb, width=1)
    # Surface vertical lines (RAM chip detail)
    inner_lines = 3 if n >= 24 else 1
    for i in range(inner_lines):
        lx = ox + body_w * (i + 1) // (inner_lines + 1)
        d.line([(lx, oy + 1), (lx, oy + body_h - 2)], fill=rgb, width=1)
    # Contact notches below
    notch_h = max(1, n // 8)
    notch_count = 6 if n >= 24 else 3
    for i in range(notch_count):
        nx = ox + body_w * (i + 1) // (notch_count + 1)
        d.line([(nx, oy + body_h), (nx, min(y + n - 1, oy + body_h + notch_h - 1))], fill=rgb, width=1)


def _draw_net(
    d: ImageDraw.ImageDraw, x: int, y: int, n: int,
    color_up: Color, color_down: Color,
) -> None:
    """Two close, long offset arrows: up top-left, down bottom-right.

    Each arrow is a vertical line with a chevron tip made of 2 line segments
    (NOT a filled triangle). Arrows are close together (~30% / 70% of width)
    and use the full icon height. Both share the same color (color_up).
    The `color_down` arg is accepted for API compatibility but unused.
    """
    rgb = _rgb(color_up)

    # Layout: each arrow = chevron (top/bottom) + shaft
    chevron_h = max(3, n * 12 // 100)         # tip height
    arrow_h = n // 2                           # each arrow uses half the icon
    shaft_h = arrow_h - chevron_h              # rest is shaft
    shaft_w = max(2, n * 6 // 100)             # thin shaft
    chevron_half_w = max(3, n * 10 // 100)     # chevron half-width

    # Close horizontal positions: ~35% / ~65% of icon width
    up_cx = x + n * 35 // 100
    down_cx = x + n * 65 // 100

    # Up arrow: chevron at top (apex up), shaft below
    up_chevron_top_y = y
    up_chevron_bot_y = y + chevron_h - 1
    up_shaft_top_y = y + chevron_h
    up_shaft_bot_y = y + arrow_h - 1

    # Down arrow: shaft on top, chevron at bottom (apex down)
    down_shaft_top_y = y + arrow_h
    down_shaft_bot_y = down_shaft_top_y + shaft_h - 1
    down_chevron_top_y = down_shaft_bot_y + 1
    down_chevron_bot_y = y + n - 1

    # ---- Up arrow chevron ("^" — 2 line segments) ----
    d.line(
        [
            (up_cx - chevron_half_w, up_chevron_bot_y),
            (up_cx, up_chevron_top_y),
        ],
        fill=rgb, width=1,
    )
    d.line(
        [
            (up_cx, up_chevron_top_y),
            (up_cx + chevron_half_w, up_chevron_bot_y),
        ],
        fill=rgb, width=1,
    )
    # Up arrow shaft
    d.rectangle(
        [
            (up_cx - shaft_w // 2, up_shaft_top_y),
            (up_cx + shaft_w // 2, up_shaft_bot_y),
        ],
        fill=rgb,
    )

    # ---- Down arrow shaft ----
    d.rectangle(
        [
            (down_cx - shaft_w // 2, down_shaft_top_y),
            (down_cx + shaft_w // 2, down_shaft_bot_y),
        ],
        fill=rgb,
    )
    # Down arrow chevron ("v" — 2 line segments)
    d.line(
        [
            (down_cx - chevron_half_w, down_chevron_top_y),
            (down_cx, down_chevron_bot_y),
        ],
        fill=rgb, width=1,
    )
    d.line(
        [
            (down_cx, down_chevron_bot_y),
            (down_cx + chevron_half_w, down_chevron_top_y),
        ],
        fill=rgb, width=1,
    )


def _draw_freq(d: ImageDraw.ImageDraw, x: int, y: int, n: int, color: Color) -> None:
    """Sine wave: polyline through ~4 control points."""
    rgb = _rgb(color)
    h = max(4, n // 2)
    w = n
    oy = y + (n - h) // 2
    if n >= 24:
        # Full sine: 4 control points
        pts = [
            (x, oy + h // 2),
            (x + w // 4, oy),
            (x + w // 2, oy + h // 2),
            (x + 3 * w // 4, oy + h - 1),
            (x + w - 1, oy + h // 2),
        ]
    else:
        # Simplified 3-point wave for small icons
        pts = [
            (x, oy + h // 2),
            (x + w // 2, oy),
            (x + w - 1, oy + h // 2),
        ]
    d.line(pts, fill=rgb, width=1)


def _draw_terminal(d: ImageDraw.ImageDraw, x: int, y: int, n: int, color: Color) -> None:
    """Text `>_` rendered with mono font. Slightly nudged up for baseline."""
    font = get_font("mono", n)
    d.text((x, y - 1), ">_", fill=_rgb(color), font=font)


def _draw_thermo(d: ImageDraw.ImageDraw, x: int, y: int, n: int, color: Color) -> None:
    """Thermometer: vertical capsule outline + filled bulb at bottom."""
    rgb = _rgb(color)
    body_w = max(2, n // 4)
    body_h = max(4, n * 2 // 3)
    ox = x + (n - body_w) // 2
    oy = y + 1
    # Capsule outline
    d.rectangle([(ox, oy), (ox + body_w - 1, oy + body_h - 1)], outline=rgb, width=1)
    # Bulb (filled circle at bottom)
    bulb_r = max(2, n // 3)
    bulb_cx = x + n // 2
    bulb_cy = oy + body_h + bulb_r - 1
    d.ellipse(
        [(bulb_cx - bulb_r, bulb_cy - bulb_r), (bulb_cx + bulb_r, bulb_cy + bulb_r)],
        fill=rgb,
    )
    # Mercury column inside body (small filled rect, lower half)
    if body_w >= 3:
        m_oy = oy + body_h // 2
        d.rectangle(
            [(ox + 1, m_oy), (ox + body_w - 2, oy + body_h - 1)],
            fill=rgb,
        )


def _draw_fan(d: ImageDraw.ImageDraw, x: int, y: int, n: int, color: Color) -> None:
    """Fan: circle outline + cross/plus inside (4 blades from center)."""
    rgb = _rgb(color)
    r = max(2, n // 2 - 1)
    cx = x + n // 2
    cy = y + n // 2
    d.ellipse([(cx - r, cy - r), (cx + r, cy + r)], outline=rgb, width=1)
    # Cross inside (4 blades)
    d.line([(cx - r + 1, cy), (cx + r - 1, cy)], fill=rgb, width=1)
    d.line([(cx, cy - r + 1), (cx, cy + r - 1)], fill=rgb, width=1)


def _draw_disk(d: ImageDraw.ImageDraw, x: int, y: int, n: int, color: Color) -> None:
    """Disk: rectangle with horizontal slot line near top."""
    rgb = _rgb(color)
    body_w = max(6, n - 2)
    body_h = max(4, n - 2)
    ox = x + (n - body_w) // 2
    oy = y + (n - body_h) // 2
    d.rectangle([(ox, oy), (ox + body_w - 1, oy + body_h - 1)], outline=rgb, width=1)
    # Slot line
    slot_y = oy + max(1, body_h // 4)
    slot_pad = max(1, body_w // 6)
    d.line(
        [(ox + slot_pad, slot_y), (ox + body_w - 1 - slot_pad, slot_y)],
        fill=rgb,
        width=1,
    )


def draw_icon(
    canvas: Canvas,
    kind: str,
    x: int,
    y: int,
    color: Color,
    size: int = 12,
    color2: Color | None = None,
) -> None:
    """Draw an icon at (x, y) on the canvas.

    Bitmap-backed kinds (loaded from icons/<name>.png, original colors
    preserved, `color` ignored): cpu, mem, freq, temp, up, down, disk,
    stars, streak, commits, prs, status, context.

    Primitive-backed kinds: terminal, fan, thermo, net.

    Args:
        canvas: target Canvas
        kind: icon name
        x, y: top-left pixel position
        color: primary color (used by primitive icons; ignored for bitmaps)
        size: icon size in pixels (12 for footer, 32-40 for tile)
        color2: optional secondary color (used by "net")
    """
    # Bitmap path: prefer PNG if present and not in the primitive-only set
    primitive_only = ("terminal", "fan", "thermo", "net")
    bitmap_path = _ICONS_DIR / (kind + ".png")
    if kind not in primitive_only and bitmap_path.exists():
        _draw_bitmap(canvas, kind, x, y, size)
        return

    d = ImageDraw.Draw(canvas.image)
    if kind == "cpu":
        _draw_cpu(d, x, y, size, color)
    elif kind == "mem":
        _draw_mem(d, x, y, size, color)
    elif kind == "net":
        _draw_net(d, x, y, size, color, color2 if color2 is not None else color)
    elif kind == "freq":
        _draw_freq(d, x, y, size, color)
    elif kind == "terminal":
        _draw_terminal(d, x, y, size, color)
    elif kind == "thermo":
        _draw_thermo(d, x, y, size, color)
    elif kind == "fan":
        _draw_fan(d, x, y, size, color)
    elif kind == "disk":
        _draw_disk(d, x, y, size, color)
    else:
        raise ValueError("Unknown icon kind: " + repr(kind))
