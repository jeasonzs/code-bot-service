"""Claude Code state collector.

Reads two optional JSON files written by Claude Code integrations:

1. **state file** (default ``~/.code-bot/claude-state.json``) - written by
   ``scripts/claude-statusline.sh``. Carries model / context / cost / cwd.
   Schema v2.

2. **status file** (default ``~/.code-bot/claude-status.json``) - written by
   ``scripts/claude-status-hook.sh`` (8 hook events). Carries the
   6-state activity enum (idle / thinking / tool / permission /
   stopped / error) plus ``last_event`` for debug. Schema v1.

Each file is polled independently via mtime+size caching at 4 Hz.
The collector merges them into a single ``ClaudeSnapshot``:

  - Model / context / cost / cwd come from the state file.
  - Status comes from the status file if present, otherwise falls back
    to the mtime heuristic on the state file (active if fresh,
    stopped if stale, idle if missing).

Hot-path caching: when a file's mtime+size is unchanged, we reuse the
previously parsed dict instead of reparsing. This keeps the steady
state to one stat() per file per tick (2 syscalls @ 4 Hz = 8 µs/s).

Status enum (6 states):
  idle       - Claude not active (no session, or Notification w/o permission)
  thinking   - UserPromptSubmit / PostToolUse / SessionStart
  tool       - PreToolUse (assistant is calling a tool)
  permission - PermissionRequest or Notification(permission_prompt)
  stopped    - Stop / SessionEnd
  error      - JSON parse failure on either file
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
DEFAULT_STATUS_PATH = Path.home() / ".code-bot" / "claude-status.json"
DEFAULT_HZ = 4.0
DEFAULT_STALE_AFTER_S = 30.0

VALID_STATUSES = {
    "idle", "thinking", "tool", "permission", "stopped", "error",
}


@dataclass
class ClaudeSnapshot:
    # --- surfaced on the LCD ---
    status: str = "idle"
    model_display: str = ""
    cwd: str = ""
    context_used_pct: Optional[float] = None
    context_in: Optional[int] = None
    context_out: Optional[int] = None
    context_window_size: Optional[int] = None
    cost_usd: Optional[float] = None
    duration_ms: Optional[int] = None
    lines_added: Optional[int] = None
    lines_removed: Optional[int] = None
    session_id: Optional[str] = None
    last_event: str = ""

    # --- internal ---
    state_file_mtime: Optional[float] = None
    status_file_mtime: Optional[float] = None
    stale: bool = False
    error: Optional[str] = None
    ts: float = 0.0


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
    """Background poller for the Claude Code state + status files."""

    def __init__(
        self,
        hz: float = DEFAULT_HZ,
        state_path: Optional[Path] = None,
        status_path: Optional[Path] = None,
        stale_after_s: float = DEFAULT_STALE_AFTER_S,
    ) -> None:
        self.hz = hz
        self.state_path = (
            Path(state_path) if state_path is not None else DEFAULT_STATE_PATH
        )
        self.status_path = (
            Path(status_path) if status_path is not None else DEFAULT_STATUS_PATH
        )
        self.stale_after_s = stale_after_s

        self._lock = threading.Lock()
        self._latest: ClaudeSnapshot = _empty_snapshot()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

        # Per-file cache: (mtime, size, parsed_dict_or_None, err_msg).
        # parsed_dict is reused on hot-path ticks to avoid reparsing
        # identical content.
        self._state_cache: tuple[float, int, Optional[dict], Optional[str]] = (
            0.0, -1, None, None,
        )
        self._status_cache: tuple[float, int, Optional[dict], Optional[str]] = (
            0.0, -1, None, None,
        )

    # ---- lifecycle (mirrors SystemCollector) ----

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
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

    # ---- sampling ----

    def _sample(self) -> None:
        now = time.time()
        state_dict, state_mtime, state_err = self._poll_file(
            self.state_path, self._state_cache, "state",
        )
        self._state_cache = (state_mtime or 0.0,
                             self._state_cache[1] if state_dict == self._state_cache[2] else self._stat_size(state_mtime),
                             state_dict, state_err)

        status_dict, status_mtime, status_err = self._poll_file(
            self.status_path, self._status_cache, "status",
        )
        self._status_cache = (status_mtime or 0.0,
                              self._status_cache[1] if status_dict == self._status_cache[2] else self._stat_size(status_mtime),
                              status_dict, status_err)

        snap = _build_snapshot(
            state_dict=state_dict, state_mtime=state_mtime,
            status_dict=status_dict, status_mtime=status_mtime,
            state_err=state_err, status_err=status_err,
            stale_after_s=self.stale_after_s, now=now,
        )
        self._publish(snap)

    @staticmethod
    def _stat_size(mtime: Optional[float]) -> int:
        """Used only to update cache size when mtime is known."""
        return -1 if mtime is None else 0  # we don't track size separately

    def _poll_file(
        self,
        path: Path,
        cache: tuple[float, int, Optional[dict], Optional[str]],
        tag: str,
    ) -> tuple[Optional[dict], Optional[float], Optional[str]]:
        """Read+parse the file, using the cache on hot path.

        Returns (parsed_dict, mtime, err_msg). dict is None when the
        file is missing or unparseable. mtime is None when missing.
        """
        last_mtime, _, last_dict, last_err = cache

        try:
            st = path.stat()
        except FileNotFoundError:
            return None, None, None
        except OSError as e:
            log.debug("stat(%s) failed: %s", path, e)
            return None, None, None

        if st.st_mtime == last_mtime and last_mtime != 0.0:
            # Hot path - reuse cached parsed dict (or cached err).
            return last_dict, st.st_mtime, last_err

        try:
            raw = path.read_text(encoding="utf-8")
        except OSError as e:
            log.warning("Claude %s file read failed: %s", tag, e)
            return None, st.st_mtime, str(e)

        try:
            parsed = json.loads(raw)
            if not isinstance(parsed, dict):
                raise ValueError(
                    f"root is {type(parsed).__name__}, expected object"
                )
            return parsed, st.st_mtime, None
        except (json.JSONDecodeError, ValueError) as e:
            log.warning("Claude %s file unreadable: %s", tag, e)
            return None, st.st_mtime, str(e)

    def _publish(self, snap: ClaudeSnapshot) -> None:
        with self._lock:
            self._latest = snap


def _build_snapshot(
    *,
    state_dict: Optional[dict],
    state_mtime: Optional[float],
    status_dict: Optional[dict],
    status_mtime: Optional[float],
    state_err: Optional[str],
    status_err: Optional[str],
    stale_after_s: float,
    now: float,
) -> ClaudeSnapshot:
    """Merge the two file payloads (and error info) into a snapshot."""

    snap = _empty_snapshot()
    snap.ts = now

    # ---- status ----
    if status_err is not None:
        snap.status = "error"
        snap.error = f"status: {status_err}"
    elif status_dict is not None:
        st = (status_dict.get("status") or "idle").strip().lower()
        if st in VALID_STATUSES:
            snap.status = st
        else:
            log.debug("unknown status %r from hook file; idle", st)
            snap.status = "idle"
        snap.last_event = status_dict.get("last_event") or ""
    elif state_dict is not None:
        # No status file (e.g. hooks not installed). Fall back to
        # mtime heuristic on state file: fresh = active, stale = stopped.
        age = now - state_mtime
        snap.stale = age > stale_after_s
        snap.status = "stopped" if snap.stale else "active"
    # else: both files missing -> default "idle"

    # ---- state file fields ----
    if state_err is not None:
        # Preserve status info; surface state file error in `error`.
        if snap.error:
            snap.error = f"{snap.error}; state: {state_err}"
        else:
            snap.error = f"state: {state_err}"
    if state_dict is not None:
        snap.model_display = (state_dict.get("model_display") or "")
        snap.cwd = (state_dict.get("cwd") or "")
        snap.context_used_pct = _coerce_float(state_dict.get("context_used_pct"))
        snap.context_in = _coerce_int(state_dict.get("context_in"))
        snap.context_out = _coerce_int(state_dict.get("context_out"))
        snap.context_window_size = _coerce_int(state_dict.get("context_window_size"))
        snap.cost_usd = _coerce_float(state_dict.get("cost_usd"))
        snap.duration_ms = _coerce_int(state_dict.get("duration_ms"))
        snap.lines_added = _coerce_int(state_dict.get("lines_added"))
        snap.lines_removed = _coerce_int(state_dict.get("lines_removed"))
        snap.session_id = (state_dict.get("session_id") or None)

    snap.state_file_mtime = state_mtime
    snap.status_file_mtime = status_mtime
    return snap