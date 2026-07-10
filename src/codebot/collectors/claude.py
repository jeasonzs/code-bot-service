"""Claude Code state collector.

Reads the JSON state file that the Claude Code statusline command writes
to ``~/.code-bot/claude-state.json`` (path overridable). Polls the file's
``stat().st_mtime`` every 250 ms (default 4 Hz) and only parses the
content when mtime advances. This keeps the hot path to one syscall per
tick even when the LCD is idle.

Polling strategy
----------------
1. ``os.stat(state_path)`` - get current mtime + size.
2. If mtime and size match the cached ``_last_mtime`` / ``_last_size``,
   nothing changed -> skip the parse (this is the common case).
3. Otherwise ``read_text()`` + ``json.loads()``. On success, swap
   snapshot under the lock. On JSON error, keep the previous snapshot
   and flag ``status="error"`` + ``error=<msg>``.
4. If the file is older than ``stale_after_s`` (default 30 s) the
   snapshot's ``status`` is forced to ``"stopped"`` and ``stale=True``
   (so the page can grey out / dim). Statusline doesn't fire when
   Claude is idle, so this is how we detect "session ended".

State file schema (v2, written by scripts/claude-statusline.py):
  schema_version, session_id, cwd, model_id, model_display,
  context_in, context_out, context_window_size, context_used_pct,
  cost_usd, duration_ms, lines_added, lines_removed, updated_unix.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


log = logging.getLogger("codebot.claude")


DEFAULT_STATE_PATH = Path.home() / ".code-bot" / "claude-state.json"
DEFAULT_HZ = 4.0
DEFAULT_STALE_AFTER_S = 30.0

# Statusline doesn't expose event semantics, so we collapse the 6-state
# enum used by the hook-based version down to 4:
#   active  - state file fresh, Claude responding
#   idle    - no state file, user hasn't run Claude
#   stopped - state file stale (> stale_after_s), Claude not active
#   error   - JSON parse failure
VALID_STATUSES = {"active", "idle", "stopped", "error"}


@dataclass
class ClaudeSnapshot:
    # --- surfaced on the LCD ---
    status: str = "idle"
    model_display: str = ""                 # "Opus" / "Sonnet" / "Haiku"
    cwd: str = ""                           # full path; page trims to basename
    context_used_pct: Optional[float] = None  # 0-100
    context_in: Optional[int] = None         # current ctx window input tokens
    context_out: Optional[int] = None        # current ctx window output tokens
    context_window_size: Optional[int] = None
    cost_usd: Optional[float] = None         # cumulative session cost
    duration_ms: Optional[int] = None        # cumulative session duration
    lines_added: Optional[int] = None
    lines_removed: Optional[int] = None
    session_id: Optional[str] = None

    # --- internal ---
    file_mtime: Optional[float] = None
    stale: bool = False
    error: Optional[str] = None
    ts: float = 0.0

    def is_known_status(self) -> bool:
        return self.status in VALID_STATUSES


def _empty_snapshot() -> ClaudeSnapshot:
    return ClaudeSnapshot(status="idle", ts=0.0)


def _coerce_int(v) -> Optional[int]:
    if v is None:
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _coerce_float(v) -> Optional[float]:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


class ClaudeCollector:
    """Background poller for the Claude Code state file."""

    def __init__(
        self,
        hz: float = DEFAULT_HZ,
        state_path: Optional[Path] = None,
        stale_after_s: float = DEFAULT_STALE_AFTER_S,
    ) -> None:
        self.hz = hz
        self.state_path = (
            Path(state_path) if state_path is not None else DEFAULT_STATE_PATH
        )
        self.stale_after_s = stale_after_s

        self._lock = threading.Lock()
        self._latest: ClaudeSnapshot = _empty_snapshot()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

        # mtime + size caching - the hot path is one stat() call per tick.
        self._last_mtime: float = 0.0
        self._last_size: int = -1

    # ---- lifecycle (mirrors SystemCollector) ----

    def start(self) -> None:
        """Start the background sampling thread."""
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Stop the background thread."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None

    def snapshot(self) -> ClaudeSnapshot:
        """Return the latest snapshot (always returns a ClaudeSnapshot,
        never None - idle on first frame before any sample)."""
        with self._lock:
            return ClaudeSnapshot(**self._latest.__dict__)

    # ---- thread loop ----

    def _run(self) -> None:
        period = 1.0 / self.hz
        while not self._stop.is_set():
            try:
                self._sample()
            except Exception as e:  # noqa: BLE001
                log.warning("ClaudeCollector sample failed: %s", e)
            self._stop.wait(period)

    # ---- the actual sample ----

    def _sample(self) -> None:
        path = self.state_path
        try:
            st = path.stat()
        except FileNotFoundError:
            # No state file yet -> idle, no error. Reset cache so the
            # next real write is picked up even if size happens to match.
            self._last_mtime, self._last_size = 0.0, -1
            snap = _empty_snapshot()
            snap.ts = time.time()
            self._publish(snap)
            return
        except OSError as e:
            log.debug("stat(%s) failed: %s", path, e)
            return

        mtime, size = st.st_mtime, st.st_size
        if mtime == self._last_mtime and size == self._last_size:
            return  # hot path - nothing to do

        self._last_mtime, self._last_size = mtime, size

        try:
            raw = path.read_text(encoding="utf-8")
            data = json.loads(raw)
            if not isinstance(data, dict):
                raise ValueError(
                    f"state file root is {type(data).__name__}, expected object"
                )
        except (json.JSONDecodeError, ValueError, OSError) as e:
            log.warning("Claude state file unreadable: %s", e)
            snap = _empty_snapshot()
            snap.status = "error"
            snap.error = str(e)
            snap.file_mtime = mtime
            snap.ts = time.time()
            self._publish(snap)
            return

        snap = self._build_snapshot(data, mtime)
        self._publish(snap)

    def _build_snapshot(self, d: dict, mtime: float) -> ClaudeSnapshot:
        """Translate the on-disk JSON dict into a ClaudeSnapshot, with
        stale-detection applied. Statusline payload has no event
        semantics, so we derive status purely from file freshness."""
        now = time.time()

        # Stale detection: file hasn't been rewritten in a while. This is
        # how we tell "Claude is mid-response" from "Claude is idle" -
        # statusline fires after each assistant message, so an
        # up-to-date file means Claude is actively working.
        stale = (now - mtime) > self.stale_after_s
        status = "stopped" if stale else "active"

        return ClaudeSnapshot(
            status=status,
            model_display=(d.get("model_display") or ""),
            cwd=(d.get("cwd") or ""),
            context_used_pct=_coerce_float(d.get("context_used_pct")),
            context_in=_coerce_int(d.get("context_in")),
            context_out=_coerce_int(d.get("context_out")),
            context_window_size=_coerce_int(d.get("context_window_size")),
            cost_usd=_coerce_float(d.get("cost_usd")),
            duration_ms=_coerce_int(d.get("duration_ms")),
            lines_added=_coerce_int(d.get("lines_added")),
            lines_removed=_coerce_int(d.get("lines_removed")),
            session_id=(d.get("session_id") or None),
            file_mtime=mtime,
            stale=stale,
            error=None,
            ts=now,
        )

    def _publish(self, snap: ClaudeSnapshot) -> None:
        with self._lock:
            self._latest = snap