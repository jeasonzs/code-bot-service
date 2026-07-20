"""Code Bot daemon main loop.

Orchestrates: USB transport (or sim), page renderers, system metrics, touch handling.
"""

from __future__ import annotations

import queue
import signal
import sys
import os
import time
import logging
import threading
from pathlib import Path
from typing import Optional

from . import ipc
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
from .render.pages.clock import ClockPage
from .render.pages.github import GithubPage
from .render.pages.claude import ClaudePage
from .render.pages.placeholders import OpenclawPage, HermesPage
from .render.pages.shortcuts import ShortcutsPage
from .collectors.system import SystemCollector
from .collectors.github import GithubCollector
from .collectors.claude import ClaudeCollector
from .config import Config


log = logging.getLogger("codebot")


# ==============================================================
# Page registry (order matches the 7-segment indicator)
# ==============================================================
def make_pages() -> list:
    """Create the default page list."""
    return [
        ClockPage(),                       # 1
        SystemPage(collector=None),        # 2 — collector wired in Daemon.__init__
        GithubPage(collector=None),        # 3 — collector wired in Daemon.__init__
        ClaudePage(collector=None),        # 4 — wired in Daemon.__init__
    ]


# ==============================================================
# Main daemon
# ==============================================================
class Daemon:
    """Main daemon that owns the USB transport (or sim server) and render loop."""

    def __init__(self, config_path: Optional[str] = None, verbose: bool = False,
                 sim_port: int = 8080) -> None:
        # P3.2: sim is always on (debug channel); USB is opened when device
        # is found. No more `--sim` opt-in; daemon always serves both
        # channels (or sim-only if USB device is missing).
        self.verbose = verbose
        self.sim_port = sim_port
        self._stop = threading.Event()
        self._canvas = Canvas()
        self._pages = make_pages()
        # 启动后默认显示第一页 (ClockPage)
        self._current_page = 0
        self._sys_collector = SystemCollector(hz=2.0)
        # Load (and create if missing) the per-user config file. The
        # collector uses it for the GitHub token; future subsystems can
        # share the same instance.
        self._config = Config()
        # GitHub stats refresh every 60s (well under the 5000 req/h limit
        # of a token-authenticated user). If GITHUB_TOKEN is unset the
        # collector simply never starts and the page shows "—".
        self._gh_collector = GithubCollector(refresh_interval=60.0, config=self._config)
        # Claude Code state file: 4 Hz mtime poll, default path
        # ~/.code-bot/claude-state.json (overridable for tests via
        # state_path=). 30 s stale -> status flips to "stopped".
        self._claude_collector = ClaudeCollector(
            hz=4.0,
            state_path=Path.home() / ".code-bot" / "claude-state.json",
            stale_after_s=30.0,
        )
        # Wire the shared collectors into their pages (constructed with
        # ``None`` placeholders above).
        for p in self._pages:
            if isinstance(p, SystemPage):
                p._collector = self._sys_collector
            elif isinstance(p, GithubPage):
                p._collector = self._gh_collector
            elif isinstance(p, ClaudePage):
                p._collector = self._claude_collector
        # 实验开关: True = 跳过 find_dirty_rects, 直接发全幅; 用于 A/B 验证左右抖动来源
        self._force_full_flush = True

        # Touch event queue: HTTP thread (sim) 或 USB poll 都把事件 push 进来,
        # 主循环统一 drain → _handle_touch, 避免 _handle_touch 跨线程调用
        self._touch_queue: queue.Queue = queue.Queue()

        # P3.2: dual-fan architecture — both channels always started.
        # USB.open() is attempted lazily in run(); if device missing, sim
        # still serves the user.
        from .sim import SimServer
        self._sim = SimServer(port=sim_port, width=SCREEN_W, height=SCREEN_H)
        self._sim.set_touch_callback(self._enqueue_touch_from_sim)
        self._usb = UsbTransport()

    def _setup_logging(self) -> None:
        level = logging.DEBUG if self.verbose else logging.INFO
        logging.basicConfig(
            level=level,
            format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
            datefmt="%H:%M:%S",
        )

    def _find_and_open(self) -> bool:
        """Find device and open USB.

        P3.2: kept short (3 retries × 0.5s) so a missing device doesn't
        delay daemon startup in sim-only mode. The previous 10×1s = 10s
        startup penalty was hostile to dev workflows where the device is
        unplugged most of the time.
        """
        log.info("Searching for Code Bot device (VID=0x%04x PID=0x%04x)...",
                 self._usb.vid, self._usb.pid)
        for attempt in range(3):
            info = self._usb.find()
            if info is not None:
                log.info("Found device: bus=%d addr=%d serial=%s",
                         info.bus, info.address, info.serial or "n/a")
                if self._usb.open():
                    log.info("USB device opened")
                    return True
                log.warning("Device found but open() failed")
            if attempt < 2:
                time.sleep(0.5)
        return False

    def _handle_touch(self, event_type: int, x: int, y: int) -> None:
        """Process a touch event from the device.

        event_type: 0=DOWN, 1=MOVE, 2=UP, 3=SWIPE_LEFT, 4=SWIPE_RIGHT, 5=LONG_PRESS
        """
        log.info("Touch: type=%d x=%d y=%d", event_type, x, y)
        if event_type == TouchEvent.SWIPE_LEFT:
            self._next_page()
        elif event_type == TouchEvent.SWIPE_RIGHT:
            self._prev_page()
        elif event_type == TouchEvent.LONG_PRESS:
            # Long-press on current page - dispatch action
            self._dispatch_long_press(x, y)

    def _next_page(self) -> None:
        self._current_page = (self._current_page + 1) % len(self._pages)
        log.info("Page: %d/%d (%s)", self._current_page + 1, len(self._pages),
                 self._pages[self._current_page].title)

    def _prev_page(self) -> None:
        self._current_page = (self._current_page - 1) % len(self._pages)
        log.info("Page: %d/%d (%s)", self._current_page + 1, len(self._pages),
                 self._pages[self._current_page].title)

    def _dispatch_long_press(self, x: int, y: int) -> None:
        """Handle long-press: action buttons in current page."""
        page = self._pages[self._current_page]
        # Pages may opt-in to long-press via on_touch(); base impl returns None.
        page.on_touch(TouchEvent.LONG_PRESS, x, y) if hasattr(page, 'on_touch') else None

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
        """Render the current page and fan out to sim + USB channels.

        Both channels are pushed independently — if USB is not open, only
        sim receives the frame; if USB is open, both sim (browser) and the
        device see the same frame. Channels never block each other.
        """
        self._render_current()
        if not self._pages[self._current_page].skip_chrome:
            self._draw_chrome()
        # sim 总是推 (browser 调试通道常驻)
        self._sim.update_image(self._canvas.image)
        # USB 仅在设备真连上时推
        if self._usb.is_open:
            self._flush_to_device()

    def _stop_handler(self, signum, frame) -> None:
        log.info("Signal %d received, stopping...", signum)
        self._stop.set()

    def run(self) -> None:
        """Run the daemon (blocks until stopped)."""
        self._setup_logging()
        # signal.signal 只在主线程有效; 测试/嵌入场景下被调用者手动管 stop event
        # SIGTERM 在 Windows 上不存在 (signal.SIGTERM AttributeError);
        # 在主线程外/嵌入式 Python 下注册会抛 ValueError。
        # 这里放宽到 (AttributeError, ValueError, OSError), 所有平台都安全。
        try:
            signal.signal(signal.SIGINT, self._stop_handler)
        except (AttributeError, ValueError, OSError):
            pass
        if os.name != "nt" and hasattr(signal, "SIGTERM"):
            try:
                signal.signal(signal.SIGTERM, self._stop_handler)
            except (AttributeError, ValueError, OSError):
                pass

        # P4.1: write PID file + start loopback control server (cross-platform
        # stop/status channel; replaces "send SIGTERM" plan that didn't work
        # on Windows).
        control = ipc.ControlServer(on_stop=self._stop.set)
        if control.start():
            ipc.write_pid_file(
                pid=os.getpid(),
                control_port=control.port,
                sim_port=self._sim.port,
            )
            log.info("PID file: %s", ipc.pid_file_path())
            log.info("Control channel: 127.0.0.1:%d (send 'STOP' to shut down)",
                     control.port)
        else:
            log.warning("PID file / control channel unavailable; "
                        "use Ctrl+C / task manager to stop")

        # P3.2: sim always starts; USB opens if device found, else sim-only.
        self._sim.start()
        log.info("Sim HTTP server: http://127.0.0.1:%d (always on)", self._sim.port)
        if self._find_and_open():
            log.info("USB device opened; dual-fan mode active (sim + device)")
            # Send initial brightness + clear
            self._usb.send_frame(build_set_brightness(80))
            time.sleep(0.1)
            self._usb.send_frame(build_clear(0x0000))
        else:
            log.warning(
                "Code Bot USB device not found; running in sim-only mode. "
                "Plug in the device and restart, or run `codebotd setup-driver` "
                "to install the OS-level driver/permissions."
            )

        # Start collectors
        self._sys_collector.start()
        self._gh_collector.start()
        self._claude_collector.start()
        for p in self._pages:
            if hasattr(p, "refresh"):
                p.refresh()

        last_render = 0.0
        last_collect_refresh = 0.0
        # sim 8fps 省 CPU / 带宽, USB 15fps 让画面顺
        render_hz = 8 if not self._usb.is_open else 15

        log.info("Daemon running. Press Ctrl+C to stop.")
        try:
            while not self._stop.is_set():
                now = time.time()
                # 处理触摸: sim 从 queue, USB 从 device (独立通道)
                self._drain_touch_queue()
                if self._usb.is_open:
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
            self._claude_collector.stop()
            if self._sim is not None:
                self._sim.stop()
            if self._usb is not None:
                self._usb.close()
            # P4.1: stop control server + clean up PID file
            try:
                control.stop()
            except Exception as e:  # noqa: BLE001
                log.debug("control server stop: %s", e)
            ipc.remove_pid_file()
            log.info("Daemon stopped")


def run_daemon(foreground: bool = True, config_path: Optional[str] = None,
               verbose: bool = False, sim_port: int = 8080) -> None:
    """Run the daemon (foreground by default).

    P3.2: sim channel is always started. USB channel is attempted at startup;
    if no device is found the daemon continues in sim-only mode (warning).
    """
    daemon = Daemon(config_path=config_path, verbose=verbose, sim_port=sim_port)
    if not foreground:
        # TODO: proper daemonization (P4 will add systemd/launchd/NSSM)
        log.warning("Daemon mode not fully implemented, running in foreground")
    daemon.run()
