"""Code Bot daemon main loop.

Orchestrates: USB transport, page renderers, system metrics, touch handling.
"""

from __future__ import annotations

import signal
import sys
import time
import logging
import threading
from typing import Optional

from .transport.usb import UsbTransport
from .protocol import Frame, TouchEvent, build_clear, build_set_brightness, build_draw_rects
from .render.canvas import Canvas
from .render.widgets import draw_indicator, draw_title
from .render.theme import VSCodeDark, SCREEN_W, SCREEN_H
from .render.pages.system import SystemPage
from .render.pages.quick_actions import QuickActionsPage
from .render.pages.github import GithubPage
from .render.pages.claude import ClaudePage
from .render.pages.openclaw import OpenclawPage
from .render.pages.hermes import HermesPage
from .render.pages.shortcuts import ShortcutsPage
from .render.pages.custom_actions import CustomActionsPage
from .collectors.system import SystemCollector
from .actions.base import get_executor


log = logging.getLogger("codebot")


# ==============================================================
# Page registry (order matches the 7-segment indicator)
# ==============================================================
def make_pages() -> list:
    """Create the default 7-page list."""
    return [
        SystemPage(),         # 1
        QuickActionsPage(),   # 2
        GithubPage(),         # 3
        ClaudePage(),         # 4
        OpenclawPage(),       # 5
        HermesPage(),         # 6
        CustomActionsPage(),  # 7
    ]


# ==============================================================
# Main daemon
# ==============================================================
class Daemon:
    """Main daemon that owns the USB transport and render loop."""

    def __init__(self, config_path: Optional[str] = None, verbose: bool = False) -> None:
        self.verbose = verbose
        self._stop = threading.Event()
        self._usb = UsbTransport()
        self._canvas = Canvas()
        self._pages = make_pages()
        self._current_page = 0
        self._sys_collector = SystemCollector(hz=2.0)
        self._actions_page: Optional[CustomActionsPage] = None
        self._custom_subpage = 0
        for p in self._pages:
            if isinstance(p, CustomActionsPage):
                self._actions_page = p
                break

    def _setup_logging(self) -> None:
        level = logging.DEBUG if self.verbose else logging.INFO
        logging.basicConfig(
            level=level,
            format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
            datefmt="%H:%M:%S",
        )

    def _find_and_open(self) -> bool:
        """Find device and open USB."""
        log.info("Searching for Code Bot device (VID=0x%04x PID=0x%04x)...",
                 self._usb.vid, self._usb.pid)
        for _ in range(10):  # retry up to 10 times (1 sec each)
            info = self._usb.find()
            if info is not None:
                log.info("Found device: bus=%d addr=%d serial=%s",
                         info.bus, info.address, info.serial or "n/a")
                if self._usb.open():
                    log.info("USB device opened")
                    return True
                log.warning("Device found but open() failed")
            time.sleep(1.0)
        return False

    def _handle_touch(self, event_type: int, x: int, y: int) -> None:
        """Process a touch event from the device.

        event_type: 0=DOWN, 1=MOVE, 2=UP, 3=SWIPE_LEFT, 4=SWIPE_RIGHT, 5=LONG_PRESS
        """
        log.debug("Touch: type=%d x=%d y=%d", event_type, x, y)
        if event_type == TouchEvent.SWIPE_LEFT:
            self._next_page()
        elif event_type == TouchEvent.SWIPE_RIGHT:
            self._prev_page()
        elif event_type == TouchEvent.LONG_PRESS:
            # Long-press on current page - dispatch action
            self._dispatch_long_press(x, y)

    def _next_page(self) -> None:
        self._current_page = (self._current_page + 1) % len(self._pages)
        self._custom_subpage = 0
        log.info("Page: %d/%d (%s)", self._current_page + 1, len(self._pages),
                 self._pages[self._current_page].title)

    def _prev_page(self) -> None:
        self._current_page = (self._current_page - 1) % len(self._pages)
        self._custom_subpage = 0
        log.info("Page: %d/%d (%s)", self._current_page + 1, len(self._pages),
                 self._pages[self._current_page].title)

    def _dispatch_long_press(self, x: int, y: int) -> None:
        """Handle long-press: action buttons in current page."""
        page = self._pages[self._current_page]
        action_id = page.on_touch(TouchEvent.LONG_PRESS, x, y) if hasattr(page, 'on_touch') else None
        if not action_id:
            return
        if action_id.startswith("action:"):
            # Custom Actions page
            idx = int(action_id.split(":")[1])
            self._execute_custom_action(idx)
        elif action_id.startswith("quick_action:"):
            idx = int(action_id.split(":")[1])
            self._execute_quick_action(idx)

    def _execute_custom_action(self, idx: int) -> None:
        if self._actions_page is None or idx >= len(self._actions_page.actions):
            return
        action = self._actions_page.actions[idx]
        log.info("Executing custom action %d: %s (%s)", idx, action.name, action.action_type)
        executor = get_executor(action.action_type)
        if executor is None:
            log.error("Unknown action type: %s", action.action_type)
            return
        if action.action_type == "hid_keystrokes":
            # Use protocol to send HID reports
            from .protocol import build_hid_keystrokes, string_to_hid_reports
            reports = string_to_hid_reports(action.config.get("text", ""))
            frame = build_hid_keystrokes(reports)
            self._usb.send_frame(frame)
        else:
            result = executor.execute(action.config)
            log.info("Action result: %s", result)

    def _execute_quick_action(self, idx: int) -> None:
        if not isinstance(self._pages[1], QuickActionsPage):
            return
        page = self._pages[1]
        if idx >= len(page.actions):
            return
        action = page.actions[idx]
        log.warning("Executing quick action: %s (cmd: %s)", action.name, action.command)
        executor = get_executor("command")
        result = executor.execute({"command": action.command})
        log.info("Quick action result: %s", result)

    def _render_current(self) -> None:
        """Render the current page to the canvas."""
        page = self._pages[self._current_page]
        # Custom Actions supports sub-pages
        if isinstance(page, CustomActionsPage):
            page.render(self._canvas, subpage=self._custom_subpage)
        else:
            page.render(self._canvas)

    def _draw_chrome(self) -> None:
        """Draw the top indicator + title (common to all pages)."""
        # Indicator
        total = len(self._pages)
        seg_w = SCREEN_W // total
        for i in range(total):
            x = i * seg_w
            color = VSCodeDark.INDICATOR_ACTIVE if i == self._current_page else VSCodeDark.INDICATOR_BASE
            self._canvas.paste_rect(color, x, 0, seg_w - 1, 4)
        # Title
        from PIL import ImageDraw
        from .render.widgets import get_font
        draw = ImageDraw.Draw(self._canvas.image)
        font = get_font("default", 12)
        title = self._pages[self._current_page].title
        draw.text((6, 8), title, fill=(VSCodeDark.FG.r, VSCodeDark.FG.g, VSCodeDark.FG.b), font=font)
        # Sub-page indicator for Custom Actions
        if isinstance(self._pages[self._current_page], CustomActionsPage):
            sp = self._actions_page
            if sp and sp.num_subpages() > 1:
                txt = f"{self._custom_subpage + 1}/{sp.num_subpages()}"
                font2 = get_font("mono", 10)
                bbox = draw.textbbox((0, 0), txt, font=font2)
                tw = bbox[2] - bbox[0]
                draw.text((SCREEN_W - tw - 6, 8), txt, fill=(VSCodeDark.WARNING.r, VSCodeDark.WARNING.g, VSCodeDark.WARNING.b), font=font2)

    def _flush_to_device(self) -> None:
        """Send dirty rects to the device."""
        rects = self._canvas.find_dirty_rects()
        if not rects:
            return
        # Build DRAW_RECTS frame (up to 16 rects)
        rect_data = []
        for r in rects:
            rect_data.append((r.x, r.y, r.w, r.h, r.pixels))
        try:
            frame = build_draw_rects(rect_data)
            self._usb.send_frame(frame)
        except Exception as e:
            log.error("Failed to send draw_rects: %s", e)

    def _poll_usb(self) -> None:
        """Poll device for incoming touch events."""
        frames = self._usb.poll(timeout_ms=10)
        for f in frames:
            if f.cmd.name == "TOUCH_EVENT":
                # Parse payload
                if len(f.payload) >= 5:
                    event_type = f.payload[0]
                    x = f.payload[1] | (f.payload[2] << 8)
                    y = f.payload[3] | (f.payload[4] << 8)
                    self._handle_touch(event_type, x, y)
            elif f.cmd.name == "PONG":
                log.debug("PONG from device")
            elif f.cmd.name == "LOG":
                log.info("[firmware] %s", f.payload.decode("utf-8", errors="replace"))

    def _render_loop(self) -> None:
        """Main render loop: render, draw chrome, flush to device."""
        self._render_current()
        self._draw_chrome()
        self._flush_to_device()

    def _stop_handler(self, signum, frame) -> None:
        log.info("Signal %d received, stopping...", signum)
        self._stop.set()

    def run(self) -> None:
        """Run the daemon (blocks until stopped)."""
        self._setup_logging()
        signal.signal(signal.SIGINT, self._stop_handler)
        signal.signal(signal.SIGTERM, self._stop_handler)

        if not self._find_and_open():
            log.error("Could not find/open Code Bot device")
            return

        # Send initial brightness + clear
        self._usb.send_frame(build_set_brightness(80))
        time.sleep(0.1)
        self._usb.send_frame(build_clear(0x0000))

        # Start collectors
        self._sys_collector.start()
        for p in self._pages:
            if hasattr(p, "refresh"):
                p.refresh()

        last_render = 0.0
        last_collect_refresh = 0.0
        render_hz = 15

        log.info("Daemon running. Press Ctrl+C to stop.")
        try:
            while not self._stop.is_set():
                now = time.time()
                # Poll device for touch events
                self._poll_usb()
                # Render at fixed rate
                if now - last_render >= 1.0 / render_hz:
                    self._render_loop()
                    last_render = now
                # Periodic collector refresh
                if now - last_collect_refresh >= 60:  # every 60s
                    for p in self._pages:
                        if hasattr(p, "refresh"):
                            p.refresh()
                    last_collect_refresh = now
                # Sleep
                time.sleep(0.005)
        except KeyboardInterrupt:
            pass
        finally:
            self._sys_collector.stop()
            self._usb.close()
            log.info("Daemon stopped")


def run_daemon(foreground: bool = True, config_path: Optional[str] = None, verbose: bool = False) -> None:
    """Run the daemon (foreground by default)."""
    daemon = Daemon(config_path=config_path, verbose=verbose)
    if not foreground:
        # TODO: proper daemonization
        log.warning("Daemon mode not fully implemented, running in foreground")
    daemon.run()
