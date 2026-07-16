"""SimServer: HTTP server that exposes daemon's LCD render for browser viewing.

Stdlib-only (http.server + threading). Pillow used for PNG encoding.

Lifecycle:
    sim = SimServer(port=8080, width=320, height=172)
    sim.set_touch_callback(daemon._handle_touch)
    sim.start()
    # ... render loop calls sim.update_image(canvas.image) each frame ...
    sim.stop()
"""

from __future__ import annotations

import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
from typing import Callable, Optional

from PIL import Image


log = logging.getLogger("codebot.sim")


# Touch event types (mirror firmware Cmd enum values; daemon _handle_touch handles 0/1/2)
TOUCH_DOWN = 0
TOUCH_MOVE = 1
TOUCH_UP = 2
# Higher-level events the daemon's _handle_touch reacts to:
TOUCH_SWIPE_LEFT = 3
TOUCH_SWIPE_RIGHT = 4
TOUCH_LONG_PRESS = 5
TOUCH_LONG_PRESS_RELEASE = 6


class SimServer:
    """Tiny HTTP server: / serves HTML, /frame.png serves current LCD frame, /touch receives events.

    The daemon pushes Pillow Images via update_image(); the HTTP thread reads
    them under a lock and encodes to PNG on demand.
    """

    def __init__(self, port: int = 8080, width: int = 320, height: int = 172) -> None:
        self.port = port
        self.width = width
        self.height = height

        self._image_lock = threading.Lock()
        # Initial frame: black
        self._image: Image.Image = Image.new("RGB", (width, height), (0, 0, 0))

        self._touch_cb: Optional[Callable[[int, int, int], None]] = None
        self._server: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None

    def set_touch_callback(self, cb: Callable[[int, int, int], None]) -> None:
        """cb(event_type, x, y) called from HTTP thread on POST /touch."""
        self._touch_cb = cb

    def update_image(self, image: Image.Image) -> None:
        """Called by daemon render loop. Copies under lock so HTTP thread sees
        a consistent snapshot (no torn frames mid-PNG-encode)."""
        with self._image_lock:
            # Copy: Pillow Image is mutable; HTTP thread will encode this snapshot
            self._image = image.copy()

    def _snapshot_image(self) -> Image.Image:
        with self._image_lock:
            return self._image.copy()

    def start(self) -> None:
        handler = _make_handler(self)
        self._server = ThreadingHTTPServer(("127.0.0.1", self.port), handler)
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name="SimServer-HTTP",
            daemon=True,
        )
        self._thread.start()
        log.info("Sim HTTP server listening on http://127.0.0.1:%d", self.port)

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None


# ==============================================================
# HTTP handler
# ==============================================================
def _make_handler(sim: SimServer):
    """Closure-based handler so each request can call sim._snapshot_image() etc."""

    class Handler(BaseHTTPRequestHandler):
        # Suppress default per-request log (we log at start/stop only)
        def log_message(self, format: str, *args) -> None:  # noqa: A002
            pass

        def do_GET(self) -> None:
            if self.path in ("/", "/index.html"):
                body = INDEX_HTML.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return

            if self.path.startswith("/frame.png"):
                png = _encode_png(sim._snapshot_image())
                self.send_response(200)
                self.send_header("Content-Type", "image/png")
                self.send_header("Content-Length", str(len(png)))
                self.send_header("Cache-Control", "no-store, max-age=0")
                self.end_headers()
                self.wfile.write(png)
                return

            self.send_response(404)
            self.end_headers()

        def do_POST(self) -> None:
            if self.path != "/touch":
                self.send_response(404)
                self.end_headers()
                return

            length = int(self.headers.get("Content-Length", "0") or "0")
            try:
                body = self.rfile.read(length)
                data = json.loads(body.decode("utf-8"))
                event_type = int(data["event"])
                x = int(data["x"])
                y = int(data["y"])
            except Exception as e:
                self.send_response(400)
                self.send_header("Content-Type", "text/plain")
                self.end_headers()
                self.wfile.write(f"bad request: {e}".encode())
                return

            if sim._touch_cb is not None:
                try:
                    sim._touch_cb(event_type, x, y)
                except Exception as e:
                    log.exception("touch callback failed: %s", e)

            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"OK")

    return Handler


def _encode_png(image: Image.Image) -> bytes:
    """Encode Pillow Image to PNG bytes. optimize=False for speed at 8fps (sim)."""
    buf = BytesIO()
    # 320x172 is small; default compression is fine, optimize=True is slow.
    image.save(buf, format="PNG", optimize=False)
    return buf.getvalue()


# ==============================================================
# Static HTML (canvas + mouse/touch → POST /touch, polling /frame.png)
# ==============================================================
INDEX_HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>Code Bot Sim</title>
<style>
  body {
    margin: 0; padding: 24px;
    background: #1e1e1e; color: #cccccc;
    font-family: monospace;
    display: flex; flex-direction: column; align-items: center; gap: 12px;
  }
  .frame-wrap {
    border: 2px solid #3c3c3c;
    background: #000;
    line-height: 0;
  }
  canvas {
    display: block;
    /* 原生 320x172 像素, 不缩放, 鼠标坐标 = 设备坐标 */
    image-rendering: pixelated;
    image-rendering: crisp-edges;
    cursor: pointer;
  }
  .info { font-size: 12px; opacity: 0.7; }
  .btn {
    padding: 6px 14px; background: #0e639c; color: #fff;
    border: none; border-radius: 3px; cursor: pointer; font-family: inherit;
  }
  .btn:hover { background: #1177bb; }
</style>
</head>
<body>
  <div class="info">Code Bot Sim — 点击/拖动 canvas 模拟触摸, 设备坐标 1:1</div>
  <div class="frame-wrap">
    <canvas id="lcd" width="320" height="172"></canvas>
  </div>
  <div>
    <button class="btn" id="swipe-l">◀ Swipe Left</button>
    <button class="btn" id="swipe-r">Swipe Right ▶</button>
    <button class="btn" id="long-press">Long Press 测试</button>
  </div>
  <div class="info">最近事件: <span id="last-evt">无</span></div>

<script>
const W = 320, H = 172;
const canvas = document.getElementById('lcd');
const ctx = canvas.getContext('2d');
const lastEvt = document.getElementById('last-evt');

let downX = 0, downY = 0, downT = 0, isDown = false;
let longPressTimer = null;
const SWIPE_THRESHOLD = 50;
const LONG_PRESS_MS = 600;

function postTouch(event, x, y) {
  fetch('/touch', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({event, x: Math.round(x), y: Math.round(y)})
  }).catch(e => console.error('touch post failed', e));
  lastEvt.textContent = `${event} (${Math.round(x)},${Math.round(y)})`;
}

function eventCoords(e) {
  const r = canvas.getBoundingClientRect();
  return {
    x: e.clientX - r.left,
    y: e.clientY - r.top
  };
}

function onDown(e) {
  e.preventDefault();
  const {x, y} = eventCoords(e);
  isDown = true;
  downX = x; downY = y; downT = Date.now();
  postTouch(0, x, y);  // 0 = DOWN

  longPressTimer = setTimeout(() => {
    if (isDown) {
      postTouch(5, x, y);  // 5 = LONG_PRESS
      // device sends LONG_PRESS_RELEASE on UP after LONG_PRESS; we mirror.
    }
  }, LONG_PRESS_MS);
}

function onMove(e) {
  if (!isDown) return;
  e.preventDefault();
  const {x, y} = eventCoords(e);
  postTouch(1, x, y);  // 1 = MOVE
}

function onUp(e) {
  if (!isDown) return;
  e.preventDefault();
  const {x, y} = eventCoords(e);
  clearTimeout(longPressTimer);
  const dx = x - downX, dy = y - downY;
  const dist2 = dx*dx + dy*dy;
  const dur = Date.now() - downT;

  // Long press release (if we fired LONG_PRESS and held ≥ 600ms)
  if (dur >= LONG_PRESS_MS) {
    postTouch(6, downX, downY);  // 6 = LONG_PRESS_RELEASE
  } else if (dist2 >= SWIPE_THRESHOLD * SWIPE_THRESHOLD) {
    // Swipe (only if not a long press)
    if (dx < 0) postTouch(3, x, y);  // 3 = SWIPE_LEFT
    else        postTouch(4, x, y);  // 4 = SWIPE_RIGHT
  }
  postTouch(2, x, y);  // 2 = UP
  isDown = false;
}

// 鼠标 + 触屏
canvas.addEventListener('mousedown', onDown);
canvas.addEventListener('mousemove', onMove);
canvas.addEventListener('mouseup',   onUp);
canvas.addEventListener('mouseleave', onUp);

canvas.addEventListener('touchstart', e => onDown(e.touches[0]), {passive: false});
canvas.addEventListener('touchmove',  e => onMove(e.touches[0]),  {passive: false});
canvas.addEventListener('touchend',   e => onUp(e.changedTouches[0]), {passive: false});

// 高层按钮 (直接 POST, 不走 canvas 坐标)
document.getElementById('swipe-l').onclick = () => postTouch(3, 10,  H/2);
document.getElementById('swipe-r').onclick = () => postTouch(4, W-10, H/2);
document.getElementById('long-press').onclick = () => {
  postTouch(0, W/2, H/2);          // DOWN
  setTimeout(() => postTouch(5, W/2, H/2), 50);   // LONG_PRESS
  setTimeout(() => postTouch(6, W/2, H/2), 100);  // LONG_PRESS_RELEASE
  setTimeout(() => postTouch(2, W/2, H/2), 150);  // UP
};

// 帧拉取: 每 125ms 触发一次 (~8fps, 与 daemon render_hz 对齐)
//
// 关键: 不要用 `img.src = X` 轮询, 因为浏览器在新的 img.src 赋值时会
// 丢掉/取消上一次的响应, 高延迟 (SSH 端口转发场景下 >125ms 很常见)
// 会导致几乎所有请求都"失败"。
//
// 改成 fetch() + blob URL: 每个 tick 都独立发请求, 互不取消;
// 哪个先回来就把哪个画到 canvas 上。内存里可能同时积压多个 in-flight
// 请求, 但总流量 = 125ms / round-trip ≈ 几 KB, 可忽略。
const img = new Image();
img.onload = () => {
  ctx.drawImage(img, 0, 0, W, H);
  const now = new Date();
  lastEvt.textContent = 'frame @ ' + now.toLocaleTimeString() + '.' +
    String(now.getMilliseconds()).padStart(3, '0');
};
let inFlight = 0;
let lastBlobUrl = null;
async function fetchFrame() {
  const seq = ++inFlight;
  try {
    const r = await fetch('/frame.png?t=' + Date.now(), { cache: 'no-store' });
    if (!r.ok) {
      console.warn('frame fetch non-OK:', r.status, 'seq=' + seq);
      return;
    }
    const blob = await r.blob();
    const url = URL.createObjectURL(blob);
    // 释放上一个 blob URL, 避免泄漏. img 此时已经 onload 过了, 安全.
    if (lastBlobUrl) URL.revokeObjectURL(lastBlobUrl);
    lastBlobUrl = url;
    img.src = url;
  } catch (e) {
    // 高延迟下 fetch 可能 reject (network error / abort);
    // 下个 tick 会重试, 这里只打 console 不打断轮询.
    console.warn('frame fetch failed (seq=' + seq + '):', e);
  }
}
setInterval(fetchFrame, 125);
fetchFrame();
</script>
</body>
</html>
"""