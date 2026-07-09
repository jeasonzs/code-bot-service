"""Code Bot daemon main loop.

Orchestrates: USB transport (or sim), page renderers, system metrics, touch handling.
"""

from __future__ import annotations

import queue
import signal
import sys
import time
import logging
import threading
from typing import Optional

from .transport.usb import UsbTransport
from .protocol import (
    Frame, TouchEvent, Cmd,
    build_clear, build_set_brightness,
    build_draw_rect_begin, build_draw_rect_end, build_draw_rect_abort,
)
from .render.canvas import Canvas, DirtyRect
from .render.widgets import draw_indicator, draw_title
from .render.theme import VSCodeDark, SCREEN_W, SCREEN_H
from .render.pages.system import SystemPage
from .render.pages.quick_actions import QuickActionsPage
from .render.pages.github import GithubPage
from .render.pages.placeholders import ClaudePage, OpenclawPage, HermesPage
from .render.pages.shortcuts import ShortcutsPage
from .render.pages.custom_actions import CustomActionsPage
from .collectors.system import SystemCollector
from .collectors.github import GithubCollector
from .config import Config
from .actions.base import get_executor


log = logging.getLogger("codebot")


# ==============================================================
# Page registry (order matches the 7-segment indicator)
# ==============================================================
def make_pages() -> list:
    """Create the default 7-page list."""
    return [
        SystemPage(collector=None),  # 1 — collector wired in Daemon.__init__
        QuickActionsPage(),   # 2
        GithubPage(collector=None),  # 3 — collector wired in Daemon.__init__
        ClaudePage(),         # 4
        OpenclawPage(),       # 5
        HermesPage(),         # 6
        CustomActionsPage(),  # 7
    ]


# ==============================================================
# Main daemon
# ==============================================================
class Daemon:
    """Main daemon that owns the USB transport (or sim server) and render loop."""

    def __init__(self, config_path: Optional[str] = None, verbose: bool = False,
                 sim: bool = False, sim_port: int = 8080) -> None:
        self.verbose = verbose
        self.sim = sim
        self._stop = threading.Event()
        self._canvas = Canvas()
        self._pages = make_pages()
        # 临时: 启动后默认显示 GitHub 页面 (index=2). 改回 0 恢复 SystemPage.
        self._current_page = 2
        self._sys_collector = SystemCollector(hz=2.0)
        # Load (and create if missing) the per-user config file. The
        # collector uses it for the GitHub token; future subsystems can
        # share the same instance.
        self._config = Config()
        # GitHub stats refresh every 60s (well under the 5000 req/h limit
        # of a token-authenticated user). If GITHUB_TOKEN is unset the
        # collector simply never starts and the page shows "—".
        self._gh_collector = GithubCollector(refresh_interval=60.0, config=self._config)
        # Wire the shared collectors into their pages (constructed with
        # ``None`` placeholders above).
        for p in self._pages:
            if isinstance(p, SystemPage):
                p._collector = self._sys_collector
            elif isinstance(p, GithubPage):
                p._collector = self._gh_collector
        self._actions_page: Optional[CustomActionsPage] = None
        self._custom_subpage = 0
        # 实验开关: True = 跳过 find_dirty_rects, 直接发全幅; 用于 A/B 验证左右抖动来源
        self._force_full_flush = True
        for p in self._pages:
            if isinstance(p, CustomActionsPage):
                self._actions_page = p
                break

        # Touch event queue: HTTP thread (sim) 或 USB poll 都把事件 push 进来,
        # 主循环统一 drain → _handle_touch, 避免 _handle_touch 跨线程调用
        self._touch_queue: queue.Queue = queue.Queue()

        if self.sim:
            from .sim import SimServer  # avoid hard dep when USB-only
            self._sim = SimServer(port=sim_port, width=SCREEN_W, height=SCREEN_H)
            self._sim.set_touch_callback(self._enqueue_touch_from_sim)
            self._usb = None
        else:
            self._usb = UsbTransport()
            self._sim = None

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
        # 注: v3 不再用 CMD_HID_KEYSTROKES 自定义命令. 设备检测触摸后自己构造
        # 标准 HID Keyboard report 发到 host (走 EP3 IN). host 端不需要任何 service.
        if action.action_type == "hid_keystrokes":
            log.warning(
                "Action '%s' is type 'hid_keystrokes' (deprecated in v3). "
                "The device now generates HID reports autonomously from touch; "
                "this action no longer sends any host-side keystroke.",
                action.name,
            )
            return
        executor = get_executor(action.action_type)
        if executor is None:
            log.error("Unknown action type: %s", action.action_type)
            return
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

    def _enqueue_touch_from_sim(self, event_type: int, x: int, y: int) -> None:
        """Called from HTTP thread by SimServer. Push to queue; main loop drains."""
        self._touch_queue.put((event_type, x, y))

    def _drain_touch_queue(self) -> None:
        """Drain queued touch events into the existing _handle_touch (main thread only)."""
        while True:
            try:
                event_type, x, y = self._touch_queue.get_nowait()
            except queue.Empty:
                return
            self._handle_touch(event_type, x, y)

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
        """Send dirty rects to the device via v3 protocol (BEGIN + EP5 stream + END).

        v3 flow per rect:
          1. EP1 OUT: DRAW_RECT_BEGIN {x,y,w,h}  (9B)
          2. EP5 OUT: raw pixel data (64B/包, 任意包数)
          3. EP1 OUT: DRAW_RECT_END  (1B, 必须发: 固件 stateless, host 不发 CS 不拉高)
        """
        rects = self._canvas.find_dirty_rects()
        if not rects:
            return

        n_ok = 0
        n_err = 0
        for r in rects:
            # 1. 发 BEGIN (开窗 + 打开 EP5 OUT 数据通道)
            if not self._usb.send_frame(
                build_draw_rect_begin(r.x, r.y, r.w, r.h),
                timeout=500,
            ):
                n_err += 1
                self._usb.send_frame(build_draw_rect_abort(), timeout=200)
                continue

            # 2. EP5 OUT 推像素流
            written = self._usb.write_pixels(r.pixels)
            expected = r.w * r.h * 2
            if written != expected:
                log.warning(
                    "Rect (%d,%d,%d,%d) wrote %d/%d bytes",
                    r.x, r.y, r.w, r.h, written, expected,
                )
                n_err += 1
                self._usb.send_frame(build_draw_rect_abort(), timeout=200)
                continue

            # 3. 发 END 关 CS (固件 stateless, 不发 CS 不会拉高, 后续 SPI 命令会被当像素吞掉)
            if not self._usb.send_frame(build_draw_rect_end(), timeout=200):
                log.warning(
                    "Rect (%d,%d,%d,%d) END send failed (CS may stay LOW)",
                    r.x, r.y, r.w, r.h
                )
                n_err += 1
                continue

            n_ok += 1

        if n_err:
            log.warning("Draw flush: %d ok, %d failed", n_ok, n_err)

    def _poll_usb(self) -> None:
        """Poll device for incoming touch events."""
        frames = self._usb.poll(timeout_ms=10)
        for f in frames:
            if f.cmd == Cmd.TOUCH_EVENT:
                try:
                    rep = f.decode_touch()
                    self._handle_touch(int(rep.event_type), rep.x, rep.y)
                except (ValueError, struct.error) as e:
                    log.warning("Bad TOUCH_EVENT frame: %s", e)
            elif f.cmd == Cmd.PONG:
                log.debug("PONG from device")
            elif f.cmd == Cmd.LOG:
                log.info("[firmware] %s", f.payload.decode("utf-8", errors="replace"))

    def _render_loop(self) -> None:
        """Main render loop: render, draw chrome, push to device or sim."""
        self._render_current()
        if not self._pages[self._current_page].skip_chrome:
            self._draw_chrome()
        if self.sim:
            # sim: 直接 publish full frame (Pillow Image), SimServer 处理锁
            self._sim.update_image(self._canvas.image)
        else:
            self._flush_to_device()

    def _stop_handler(self, signum, frame) -> None:
        log.info("Signal %d received, stopping...", signum)
        self._stop.set()

    def run(self) -> None:
        """Run the daemon (blocks until stopped)."""
        self._setup_logging()
        # signal.signal 只在主线程有效; 测试/嵌入场景下被调用者手动管 stop event
        try:
            signal.signal(signal.SIGINT, self._stop_handler)
            signal.signal(signal.SIGTERM, self._stop_handler)
        except ValueError:
            pass

        if self.sim:
            log.info("Starting in SIMULATION mode (no USB device required)")
            self._sim.start()
            log.info("Open http://127.0.0.1:%d in a browser to view", self._sim.port)
        else:
            if not self._find_and_open():
                log.error("Could not find/open Code Bot device")
                return
            # Send initial brightness + clear
            self._usb.send_frame(build_set_brightness(80))
            time.sleep(0.1)
            self._usb.send_frame(build_clear(0x0000))

        # Start collectors
        self._sys_collector.start()
        self._gh_collector.start()
        for p in self._pages:
            if hasattr(p, "refresh"):
                p.refresh()

        last_render = 0.0
        last_collect_refresh = 0.0
        # sim 8fps 省 CPU / 带宽, USB 15fps 让画面顺
        render_hz = 8 if self.sim else 15

        log.info("Daemon running. Press Ctrl+C to stop.")
        try:
            while not self._stop.is_set():
                now = time.time()
                # 处理触摸: sim 从 queue, USB 从 device
                self._drain_touch_queue()
                if not self.sim:
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
            self._gh_collector.stop()
            if self._sim is not None:
                self._sim.stop()
            if self._usb is not None:
                self._usb.close()
            log.info("Daemon stopped")


def run_daemon(foreground: bool = True, config_path: Optional[str] = None,
               verbose: bool = False, sim: bool = False, sim_port: int = 8080) -> None:
    """Run the daemon (foreground by default)."""
    daemon = Daemon(config_path=config_path, verbose=verbose, sim=sim, sim_port=sim_port)
    if not foreground:
        # TODO: proper daemonization
        log.warning("Daemon mode not fully implemented, running in foreground")
    daemon.run()
