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
from .config import Config, page_enabled


log = logging.getLogger("codebot")


# ==============================================================
# Page registry (order matches the 7-segment indicator)
# ==============================================================
# Pages whose visibility is config-gated, mapped to their key under the
# `pages:` section of config.yml. Anything not listed here is always
# shown — Clock and System need no external configuration.
_CONFIG_GATED_PAGES: dict[type, str] = {
    GithubPage: "github",
    ClaudePage: "claude",
}


def make_pages(config: Optional[Config] = None) -> list:
    """Build the page list, dropping pages disabled in config.

    Collectors are wired later in ``Daemon.__init__`` (constructed with
    ``None`` here). Returning only the *enabled* pages keeps the rest of
    the daemon free of per-page conditionals: the indicator segment
    count, the next/prev modulo wrap and the refresh loops all derive
    from ``len(self._pages)``.
    """
    if config is None:
        config = Config()

    pages = []
    for page in (ClockPage(), SystemPage(collector=None),
                 GithubPage(collector=None), ClaudePage(collector=None)):
        key = _CONFIG_GATED_PAGES.get(type(page))
        if key is not None:
            page.enabled = page_enabled(config, key)
            if not page.enabled:
                log.info(
                    "Page %r hidden: set pages.%s.enabled: true in %s "
                    "(or re-run `codebotd setup`) to show it",
                    page.title, key, config.path,
                )
                continue
        pages.append(page)
    return pages


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
        # Load (and create if missing) the per-user config file *before*
        # the page registry: page visibility comes from it. The
        # collectors below share the same instance.
        self._config = Config()
        self._pages = make_pages(self._config)
        # 启动后默认显示第一页 (ClockPage)
        self._current_page = 0
        self._sys_collector = SystemCollector(hz=2.0)
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

        # USB hot-plug supervisor: 后台线程,设备掉线后按指数退避自动重连。
        # 主循环不直接重枚举,只通过 send_frame 失败时 UsbTransport.mark_closed()
        # 把 is_open 置 False,supervisor 看到后调用 _find_and_open。
        self._usb_supervisor: Optional[threading.Thread] = None
        # 设备重连后 supervisor 要发 brightness+clear 复位 LCD,这期间主循环
        # 不能并发 _flush_to_device (会闪屏)。True = supervisor 持有中,flush 让路。
        # 必须在 _find_and_open 之前置 True,关闭 is_open 翻转 → 主循环观察的窗口。
        self._usb_resyncing: bool = False

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

    def _has_page(self, cls: type) -> bool:
        """True if a page of type ``cls`` is currently registered.

        Used to skip starting collectors whose page the user disabled in
        config — those collectors never tick anyway, so skipping them
        avoids pointless network calls and file polls.
        """
        return any(isinstance(p, cls) for p in self._pages)

    def _supervise_usb(self) -> None:
        """Background thread: 设备掉线后自动重连。

        与主循环解耦:
          - 主循环负责 *检测* 断线 (send_frame 失败 → UsbTransport.mark_closed)
          - supervisor 负责 *恢复* (看到 is_open=False 就调 _find_and_open)
          - 恢复期间持 _usb_resyncing,关闭 _flush_to_device 并发路径 (防闪屏)
        """
        backoff = 1.0
        while not self._stop.is_set():
            if self._usb.is_open:
                # 设备健康:每秒看一眼,失败重置退避
                backoff = 1.0
                self._stop.wait(1.0)
                continue
            # _find_and_open 会把 is_open 翻成 True;之前先占住 _usb_resyncing,
            # 否则主循环可能在两个语句之间观察到 is_open=True → 并发 flush → 闪屏。
            # find_and_open 失败时也没害处 (主循环的 is_open 早已是 False,不会 flush)。
            self._usb_resyncing = True
            try:
                if self._find_and_open():
                    log.info("USB device reconnected; resyncing screen")
                    # 跟初始启动一致: brightness + clear 让 LCD 进入已知状态,
                    # 然后强制下一帧 find_dirty_rects 返回全幅 (mark_all_dirty),
                    # 否则 diff 命中"canvas 跟 _prev_image 一致"→ [] → 设备 LCD 残留旧图。
                    if self._usb.send_frame(build_set_brightness(80)):
                        self._stop.wait(0.1)
                        self._usb.send_frame(build_clear(0x0000))
                    self._canvas.mark_all_dirty()
                    backoff = 1.0
                    self._stop.wait(1.0)
                else:
                    log.debug("USB reconnect failed; retry in %.1fs", backoff)
                    self._stop.wait(backoff)
                    backoff = min(backoff * 2, 5.0)
            finally:
                self._usb_resyncing = False

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
        if self._usb_resyncing or not self._usb.is_open:
            # supervisor 重连后正在发 brightness+clear 让 LCD 复位;
            # 这期间主循环并发 flush 会让 BEGIN+pixels 跟 clear 撞在一起 → 闪屏。
            # 等 supervisor 完成 (清 _usb_resyncing) 再 flush。
            return
        rects = self._canvas.find_dirty_rects()
        if not rects:
            return

        n_ok = 0
        n_err = 0
        for r in rects:
            # 设备可能在上一轮 send_frame 后被 mark_closed (掉线)
            if not self._usb.is_open:
                log.warning("USB device disconnected mid-flush; aborting remaining rects")
                break
            # 1. 发 BEGIN (开窗 + 打开 EP5 OUT 数据通道)
            if not self._usb.send_frame(
                build_draw_rect_begin(r.x, r.y, r.w, r.h),
                timeout=500,
            ):
                n_err += 1
                if not self._usb.is_open:
                    # send_frame 内部已 mark_closed,supervisor 接管
                    break
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
                if not self._usb.is_open:
                    break
                self._usb.send_frame(build_draw_rect_abort(), timeout=200)
                continue

            # 3. 发 END 关 CS (固件 stateless, 不发 CS 不会拉高, 后续 SPI 命令会被当像素吞掉)
            if not self._usb.send_frame(build_draw_rect_end(), timeout=200):
                log.warning(
                    "Rect (%d,%d,%d,%d) END send failed (CS may stay LOW)",
                    r.x, r.y, r.w, r.h
                )
                n_err += 1
                if not self._usb.is_open:
                    break
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
                "Plug in the device — supervisor will auto-connect — or run "
                "`codebotd setup-driver` to install the OS-level driver/permissions."
            )

        # Hot-plug supervisor: 后台线程,设备掉线 → 指数退避重连。
        # 不论初始是否找到设备都启动;初始失败时它会持续尝试。
        self._usb_supervisor = threading.Thread(
            target=self._supervise_usb,
            daemon=True,
            name="usb-supervisor",
        )
        self._usb_supervisor.start()
        log.info("USB supervisor started (auto-reconnect enabled)")

        # Start collectors
        self._sys_collector.start()
        # Skip the network/file polls for pages that config disabled.
        if self._has_page(GithubPage):
            self._gh_collector.start()
        if self._has_page(ClaudePage):
            self._claude_collector.start()
        for p in self._pages:
            if hasattr(p, "refresh"):
                p.refresh()

        last_render = 0.0
        last_collect_refresh = 0.0
        last_ping_at = 0.0  # USB PING 心跳 (设备 2400ms timeout, 1s 间隔留 ~2s 容差)
        # sim 8fps 省 CPU / 带宽, USB 15fps 让画面顺
        # 动态求值: 设备重连后立即切换到 15fps,断开后回到 8fps
        last_render_hz = 0

        log.info("Daemon running. Press Ctrl+C to stop.")
        try:
            while not self._stop.is_set():
                now = time.time()
                # 处理触摸: sim 从 queue, USB 从 device (独立通道)
                self._drain_touch_queue()
                if self._usb.is_open:
                    self._poll_usb()
                    # 1s PING: 设备 2400ms timeout 内必收到, 容差宽
                    if now - last_ping_at >= 1.0:
                        self._usb.send_ping()
                        last_ping_at = now
                # Render at fixed rate (动态 render_hz, 设备状态变了立即切换)
                render_hz = 15 if self._usb.is_open else 8
                if render_hz != last_render_hz:
                    log.info("Render rate: %d Hz (USB %s)",
                             render_hz, "connected" if self._usb.is_open else "disconnected")
                    last_render_hz = render_hz
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
            # 等待 supervisor 线程退出 (daemon=True 主线程退出时也会被强杀,
            # 但 _stop 已 set,正常路径下它会自然退出)
            if self._usb_supervisor is not None:
                self._usb_supervisor.join(timeout=2.0)
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
        # TODO: proper daemonization (P4 will add systemd/launchd/Task Scheduler)
        log.warning("Daemon mode not fully implemented, running in foreground")
    daemon.run()
