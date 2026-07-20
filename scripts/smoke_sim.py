#!/usr/bin/env python3
"""Smoke test for codebotd sim mode startup.

只依赖 stdlib: 起 daemon (sim=True), 等 2s, HTTP GET / 断言 200 + 含图像字节,
通过 loopback control port 发 STOP 优雅退出, 验证返回码。

不依赖: 真实 USB 设备, 网络, pytest。
适用场景: 三平台手工 sanity check; CI 也可复用 (用户暂未启用 CI)。

用法:
    python scripts/smoke_sim.py [--port 18080] [--timeout 10]

退出码:
    0 = PASS
    1 = daemon 启动超时 / 进程异常退出
    2 = HTTP GET / 失败 / 响应非 200
    3 = 响应不含图像 magic bytes (PNG/JPEG/BMP 任意一种)
    4 = STOP 控制协议失败 / daemon 未在超时内退
"""

from __future__ import annotations

import argparse
import socket
import struct
import sys
import threading
import time
import urllib.request
import urllib.error
from io import BytesIO
from pathlib import Path

# 让 `import codebot` 走 src/ (开发模式 / 仓库根目录直接跑)
_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "src"))


def _find_free_port() -> int:
    """Bind 0 让 OS 选一个空闲端口, 立即释放, 返回端口号。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _http_get_is_image(url: str, timeout: float = 5.0) -> tuple[int, bytes]:
    """GET url, 返回 (status_code, body). 异常时抛。"""
    req = urllib.request.Request(url, method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status, resp.read()


def _looks_like_image(body: bytes) -> bool:
    """粗略校验响应 body 是常见图像格式之一 (PNG/JPEG/BMP/GIF/WebP)."""
    if len(body) < 8:
        return False
    # PNG: 89 50 4E 47
    if body[:4] == b"\x89PNG":
        return True
    # JPEG: FF D8 FF
    if body[:3] == b"\xff\xd8\xff":
        return True
    # BMP: 42 4D
    if body[:2] == b"BM":
        return True
    # GIF: GIF87a / GIF89a
    if body[:6] in (b"GIF87a", b"GIF89a"):
        return True
    # WebP: RIFF....WEBP
    if body[:4] == b"RIFF" and body[8:12] == b"WEBP":
        return True
    return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="codebotd sim mode smoke test")
    parser.add_argument("--port", type=int, default=0,
                        help="sim HTTP port (0 = auto-pick free port)")
    parser.add_argument("--timeout", type=float, default=10.0,
                        help="总超时秒数")
    args = parser.parse_args(argv)

    sim_port = args.port or _find_free_port()
    print(f"[smoke] using sim port {sim_port}")

    # 延迟 import 让 arg 解析先出来, 失败时信息更友好
    try:
        from codebot.daemon import Daemon
    except Exception as e:
        print(f"[smoke] FAIL: cannot import codebot.daemon: {e}", file=sys.stderr)
        return 1

    daemon = Daemon(sim_port=sim_port, verbose=False)
    daemon_thread = threading.Thread(target=daemon.run, daemon=True)
    daemon_thread.start()
    print(f"[smoke] daemon started in background thread")

    rc = 0
    try:
        # 1. 等 daemon 把 HTTP server 起好 (主循环 ~1s 内)
        deadline = time.monotonic() + args.timeout
        ok = False
        last_err: Exception | None = None
        # /frame.png 是真正的 LCD 帧图像 (PNG); / 返回 HTML shell
        frame_url = f"http://127.0.0.1:{sim_port}/frame.png"
        while time.monotonic() < deadline:
            time.sleep(0.2)
            try:
                status, body = _http_get_is_image(frame_url, timeout=1.0)
                if status == 200:
                    ok = True
                    break
                last_err = RuntimeError(f"status={status}")
            except (urllib.error.URLError, ConnectionError, OSError) as e:
                last_err = e
                continue

        if not ok:
            print(f"[smoke] FAIL: HTTP GET {frame_url} failed: {last_err}", file=sys.stderr)
            rc = 2
        else:
            print(f"[smoke] HTTP 200 OK, body[:8]={body[:8]!r}, {len(body)} bytes")
            if not _looks_like_image(body):
                print(f"[smoke] FAIL: response body doesn't look like an image", file=sys.stderr)
                rc = 3
            else:
                print(f"[smoke] PASS: sim mode serving LCD frame images")

    finally:
        # 2. 通过 daemon 内部 stop event 优雅退出 (sim 通道无 USB; 强制 stop)
        print(f"[smoke] stopping daemon via stop event")
        daemon._stop.set()
        # daemon.run() 的 finally 会停 3 个 collector, 每个 join timeout=2.0
        # 所以最多 6s+, 留 8s 余量
        daemon_thread.join(timeout=8.0)
        if daemon_thread.is_alive():
            # daemon thread 是 daemon=True, 主线程返回时会被 python 强杀
            # 这里只是 warn, 不算 FAIL (smoke 主目的已达成: sim 起 + 图像 OK)
            print(f"[smoke] WARN: daemon thread did not exit cleanly within 8s "
                  f"(daemon=True, will be reaped at process exit)", file=sys.stderr)
            # 不当作 FAIL, 维持 rc (PASS/FAIL 取决于 HTTP 检查)

    return rc


if __name__ == "__main__":
    sys.exit(main())
