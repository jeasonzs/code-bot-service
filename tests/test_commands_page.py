"""Smoke tests for ``codebot.render.pages.commands.CommandsPage``.

Scope is intentionally narrow (per ``scope-testing-to-changes``): only
the page's render output and DOWN hit-test. Icon-cache behaviour is
exercised indirectly through the missing-icon fallback in
``render_one_item_uses_placeholder_for_missing_icon``.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

# Tests run from the repo root; make the package importable.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from codebot.render.canvas import Canvas
from codebot.render.pages.commands import (
    CommandItem,
    CommandsPage,
    _DIVIDER_X,
    _PAIR_ICON_LEFT_CX,
    _PAIR_ICON_RIGHT_CX,
    _PAIR_ICON_SIZE,
    _PAIR_ICON_Y_TOP,
)


class TestCommandsPage(unittest.TestCase):

    def test_render_with_zero_items_does_not_raise(self) -> None:
        page = CommandsPage([])
        page.render(Canvas())  # no exception

    def test_render_one_item_uses_left_slot(self) -> None:
        page = CommandsPage([CommandItem(name="Solo", icon_path="", command="solo")])
        canvas = Canvas()
        page.render(canvas)
        bg = canvas.image.getpixel((0, 0))
        self.assertEqual(bg, (30, 30, 30), "expected VSCodeDark.BG")

    def test_render_two_items_side_by_side(self) -> None:
        page = CommandsPage([
            CommandItem(name="Left",  icon_path="", command="left"),
            CommandItem(name="Right", icon_path="", command="right"),
        ])
        canvas = Canvas()
        page.render(canvas)
        border = canvas.image.getpixel((_DIVIDER_X, 80))
        self.assertEqual(border, (60, 60, 60), "expected VSCodeDark.BORDER on divider")

    def test_on_touch_down_within_left_rect_returns_action_id(self) -> None:
        page = CommandsPage([
            CommandItem(name="Left",  icon_path="", command="left-cmd"),
            CommandItem(name="Right", icon_path="", command="right-cmd"),
        ])
        x = _PAIR_ICON_LEFT_CX
        y = _PAIR_ICON_Y_TOP + _PAIR_ICON_SIZE // 2
        self.assertEqual(page.on_touch(0, x, y), "left-cmd")

    def test_on_touch_down_within_right_rect_returns_action_id(self) -> None:
        page = CommandsPage([
            CommandItem(name="Left",  icon_path="", command="left-cmd"),
            CommandItem(name="Right", icon_path="", command="right-cmd"),
        ])
        x = _PAIR_ICON_RIGHT_CX
        y = _PAIR_ICON_Y_TOP + _PAIR_ICON_SIZE // 2
        self.assertEqual(page.on_touch(0, x, y), "right-cmd")

    def test_on_touch_down_near_divider_returns_none(self) -> None:
        page = CommandsPage([
            CommandItem(name="Left",  icon_path="", command="left-cmd"),
            CommandItem(name="Right", icon_path="", command="right-cmd"),
        ])
        self.assertIsNone(page.on_touch(0, _DIVIDER_X, 80))

    def test_on_touch_down_outside_returns_none(self) -> None:
        page = CommandsPage([
            CommandItem(name="Left",  icon_path="", command="left-cmd"),
            CommandItem(name="Right", icon_path="", command="right-cmd"),
        ])
        self.assertIsNone(page.on_touch(0, 10, 10))

    def test_on_touch_long_press_returns_none(self) -> None:
        page = CommandsPage([
            CommandItem(name="Left", icon_path="", command="left-cmd"),
        ])
        self.assertIsNone(page.on_touch(5, _PAIR_ICON_LEFT_CX, 80))

    def test_render_one_item_uses_placeholder_for_missing_icon(self) -> None:
        page = CommandsPage([
            CommandItem(name="Missing", icon_path="/nonexistent/path.png", command="x"),
        ])
        canvas = Canvas()
        page.render(canvas)
        # Solo icon now lives in the left slot: top-left = (80-32, 30) = (48, 30).
        self.assertEqual(canvas.image.getpixel((48, 30)), (60, 60, 60))


if __name__ == "__main__":
    unittest.main()
