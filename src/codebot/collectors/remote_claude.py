"""Remote (SSH) implementation of the :class:`ClaudeCollector` ABC.

Single long-lived paramiko client; one remote Python invocation per tick
does stat + (conditional) ``cat`` for both the state and status files.
We pass the local-side cache (mtime, size) as stdin JSON so the remote
script can short-circuit ``cat`` when nothing has changed — saves a
few KB per tick.

Sampling rate is intentionally lower than the local collector
(0.5 Hz vs 4 Hz) — SSH roundtrip cost dominates at higher rates and
the LCD is the bottleneck anyway.

The remote path defaults to the *remote* user's home (``~/.code-bot/...``)
because paramiko authenticates as ``SshTarget.username`` on the remote
host, not the local user.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from typing import Optional

import paramiko

from ..ssh import SshTarget
from ..ssh_client import open_ssh, run_remote
from .claude import (
    DEFAULT_STALE_AFTER_S,
    ClaudeCollector,
    ClaudeSnapshot,
    _build_snapshot,
    _empty_snapshot,
)


log = logging.getLogger("codebot.collectors.remote_claude")


# Single-file roundtrip: the remote Python script reads the cache spec
# from stdin, does an os.stat on each path, and only ``cat``s files whose
# mtime has moved. Output is one JSON line with both files' results.
# No single quotes inside — safe to wrap in shell `'...'`.
_REMOTE_SCRIPT = r"""
import json, os, sys

spec = json.loads(sys.stdin.read())

def maybe_read(path, cached_mtime, cached_size):
    '''Return [content, mtime, size, err].

    ``content`` is None when the file is missing, or when mtime matches
    the cache (hot path -- caller reuses its cached parsed dict).
    '''
    try:
        st = os.stat(path)
    except FileNotFoundError:
        return [None, None, None, None]
    except OSError as e:
        return [None, None, None, str(e)]
    if st.st_mtime == cached_mtime and cached_mtime != 0.0:
        return [None, st.st_mtime, st.st_size, None]
    try:
        with open(path, encoding="utf-8") as f:
            content = f.read()
        return [content, st.st_mtime, st.st_size, None]
    except OSError as e:
        return [None, st.st_mtime, st.st_size, str(e)]

state = maybe_read(spec["state_path"], spec["state_cached_mtime"], spec["state_cached_size"])
status = maybe_read(spec["status_path"], spec["status_cached_mtime"], spec["status_cached_size"])
print(json.dumps({"state": state, "status": status}))
"""


class RemoteClaudeCollector(ClaudeCollector):
    """Sample Claude Code state files over SSH using a long-lived paramiko client."""

    def __init__(
        self,
        target: SshTarget,
        state_path: str = "~/.code-bot/claude-state.json",
        status_path: str = "~/.code-bot/claude-status.json",
        hz: float = 0.5,
        stale_after_s: float = DEFAULT_STALE_AFTER_S,
    ) -> None:
        self._target = target
        # Strings, not Path: the remote script does os.path.expanduser
        # against the *remote* user's $HOME.
        self._state_path = state_path
        self._status_path = status_path
        self.hz = hz
        self.stale_after_s = stale_after_s

        self._lock = threading.Lock()
        self._latest: ClaudeSnapshot = _empty_snapshot()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._client: Optional[paramiko.SSHClient] = None
        self._connected = False

        # Same cache shape as LocalClaudeCollector so _build_snapshot works.
        self._state_cache: tuple[float, int, Optional[dict], Optional[str]] = (
            0.0, -1, None, None,
        )
        self._status_cache: tuple[float, int, Optional[dict], Optional[str]] = (
            0.0, -1, None, None,
        )

    # ---- lifecycle (mirrors RemoteSystemCollector) ----

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
        self._close_client()

    def snapshot(self) -> ClaudeSnapshot:
        with self._lock:
            return ClaudeSnapshot(**self._latest.__dict__)

    # ---- SSH plumbing ----

    def _close_client(self) -> None:
        if self._client is not None:
            try:
                self._client.close()
            except OSError as e:
                log.debug("ssh client close: %s", e)
            self._client = None
            self._connected = False

    def _run(self) -> None:
        period = 1.0 / self.hz
        backoff = 1.0
        while not self._stop.is_set():
            if not self._connected:
                try:
                    self._client = open_ssh(self._target, timeout=3.0)
                    self._connected = True
                    backoff = 1.0
                except (paramiko.SSHException, OSError) as e:
                    log.debug("remote claude ssh connect failed: %s", e)
                    self._connected = False
                    self._stop.wait(min(backoff, 5.0))
                    backoff = min(backoff * 2, 5.0)
                    continue
            try:
                self._sample()
            except (paramiko.SSHException, OSError) as e:
                log.debug("remote claude sample failed: %s", e)
                self._close_client()    # reconnect next tick
                self._stop.wait(min(backoff, 5.0))
                backoff = min(backoff * 2, 5.0)
                continue
            except Exception as e:  # noqa: BLE001
                # Parse / protocol error — don't tear down the connection.
                log.warning("remote claude sample parse failed: %s", e)
            backoff = 1.0
            self._stop.wait(period)

    def _sample(self) -> None:
        spec = json.dumps({
            "state_path": self._state_path,
            "status_path": self._status_path,
            "state_cached_mtime": self._state_cache[0],
            "state_cached_size": self._state_cache[1],
            "status_cached_mtime": self._status_cache[0],
            "status_cached_size": self._status_cache[1],
        })
        cmd = f"python3 -c '{_REMOTE_SCRIPT}'"
        _rc, out, _err = run_remote(
            self._client, cmd, timeout=2.0, stdin_data=spec,
        )
        self._apply(out)

    def _apply(self, out: str) -> None:
        try:
            data = json.loads(out.strip().splitlines()[-1])
        except (json.JSONDecodeError, IndexError, ValueError) as e:
            raise ValueError(f"bad remote claude JSON: {e}") from None

        self._state_cache, state_dict, state_mtime, state_err = self._update_one(
            data["state"], self._state_cache,
        )
        self._status_cache, status_dict, status_mtime, status_err = self._update_one(
            data["status"], self._status_cache,
        )

        snap = _build_snapshot(
            state_dict=state_dict, state_mtime=state_mtime,
            status_dict=status_dict, status_mtime=status_mtime,
            state_err=state_err, status_err=status_err,
            stale_after_s=self.stale_after_s, now=time.time(),
        )
        with self._lock:
            self._latest = snap

    def _update_one(
        self,
        raw: list,
        cache: tuple[float, int, Optional[dict], Optional[str]],
    ) -> tuple[
        tuple[float, int, Optional[dict], Optional[str]],
        Optional[dict], Optional[float], Optional[str],
    ]:
        """Update one cache slot from a remote script result row.

        Returns ``(new_cache, parsed_dict, mtime, err)``.
        """
        content, mtime, size, err = raw

        if err is not None:
            # stat or read failed; preserve the previous parsed_dict
            # so the snapshot still has stale data, but surface the err.
            new_cache = (
                mtime if mtime is not None else cache[0],
                size if size is not None else cache[1],
                cache[2],
                err,
            )
            return new_cache, cache[2], mtime, err

        if content is None:
            # Either file missing, or hot path (mtime unchanged).
            if mtime is None:
                return (0.0, -1, None, None), None, None, None
            return cache, cache[2], mtime, cache[3]

        # Content present: parse it fresh.
        try:
            parsed = json.loads(content)
            if not isinstance(parsed, dict):
                raise ValueError(
                    f"root is {type(parsed).__name__}, expected object"
                )
            new_cache = (mtime, size, parsed, None)
            return new_cache, parsed, mtime, None
        except (json.JSONDecodeError, ValueError) as e:
            log.warning("remote claude file unreadable: %s", e)
            new_cache = (mtime, size, None, str(e))
            return new_cache, None, mtime, str(e)