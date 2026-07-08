"""PIL-primitive icon renderer for the SystemPage 2x2 dashboard.

All icons are drawn as geometry (lines, rects, polygons, ellipses) so the
LCD needs no external font/icon assets. Each icon takes a `size` param;
same code path works for 32px tile icons and 12px footer icons.

Kinds: "cpu", "mem", "net", "freq", "terminal", "thermo", "fan", "disk".
"""

from __future__ import annotations

from PIL import ImageDraw

from .canvas import Canvas
from .theme import Color
from .widgets import get_font


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
    """Stacked up/down arrows with shafts (proper arrow shape, not just triangles).

    Top half: up arrow — triangle head on top, rectangular shaft below.
    Bottom half: down arrow — shaft on top, triangle head pointing down.
    """
    rgb_up = _rgb(color_up)
    rgb_down = _rgb(color_down)
    half = n // 2

    # Geometry within each half (height = half):
    #   head 60%, shaft 40%
    head_h = max(4, half * 3 // 5)
    shaft_h = half - head_h
    head_half_w = max(3, n // 3)   # arrowhead half-width
    shaft_w = max(2, n // 5)       # shaft width (centered)
    cx = x + n // 2
    shaft_x0 = cx - shaft_w // 2
    shaft_x1 = shaft_x0 + shaft_w - 1

    # ---- Up arrow (top half): head apex at y, base at y+head_h-1; shaft below ----
    head_apex_y = y
    head_base_y = y + head_h - 1
    d.polygon(
        [(cx, head_apex_y), (cx - head_half_w, head_base_y), (cx + head_half_w, head_base_y)],
        fill=rgb_up,
    )
    shaft_top = head_base_y + 1
    shaft_bot = shaft_top + shaft_h - 1
    d.rectangle([(shaft_x0, shaft_top), (shaft_x1, shaft_bot)], fill=rgb_up)

    # ---- Down arrow (bottom half): shaft on top, head apex at y+n-1 ----
    head_apex_y_d = y + n - 1
    head_base_y_d = head_apex_y_d - head_h + 1
    d.polygon(
        [(cx, head_apex_y_d), (cx - head_half_w, head_base_y_d), (cx + head_half_w, head_base_y_d)],
        fill=rgb_down,
    )
    shaft_top_d = y + half
    shaft_bot_d = head_base_y_d - 1
    d.rectangle([(shaft_x0, shaft_top_d), (shaft_x1, shaft_bot_d)], fill=rgb_down)


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

    Args:
        canvas: target Canvas
        kind: one of "cpu", "mem", "net", "freq", "terminal", "thermo", "fan", "disk"
        x, y: top-left pixel position
        color: primary color
        size: icon size in pixels (12 for footer, 32 for tile)
        color2: optional secondary color; only "net" uses it (down arrow)
    """
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
        raise ValueError(f"Unknown icon kind: {kind!r}")
