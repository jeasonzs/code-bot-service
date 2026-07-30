"""Project-wide user config (~/.code_bot/config.yml).

Loaded once at daemon startup. The file holds per-user secrets and
preferences (currently: the GitHub PAT and per-page display toggles under
``pages.<page_name>.``). Behaviour on construction:

  • File missing       → create it with ``DEFAULTS`` (mode 600).
  • File exists        → load it. For any key in ``DEFAULTS`` that is
                         missing at any nesting level, fill it in and
                         rewrite the file. User-set values are
                         preserved; extra unknown keys round-trip
                         unchanged (forward-compat for future sections).
  • File is corrupt    → warn, treat as empty, rewrite with ``DEFAULTS``.

The on-disk schema is the source of truth, but ``DEFAULTS`` is the
source of truth for *which keys are recognised*: the migration is
unidirectional (additive only) and never removes user data.

Atomicity
---------
``save()`` writes to ``<name>.tmp`` in the same directory and then
``os.replace()``s it over the live file, so a crash mid-write can't
leave a half-formed YAML on disk.
"""

from __future__ import annotations

import copy
import logging
import os
from pathlib import Path
from typing import Any, Optional


log = logging.getLogger("codebot.config")


# Default per-user config file. Created with mode 600 on first use.
DEFAULT_CONFIG_PATH: Path = Path.home() / ".code_bot" / "config.yml"


# Current schema — the single source of truth for recognised keys.
# Missing top-level sections and missing sub-keys are auto-filled from
# this dict on load. Trailing comments are docs; they're stripped on
# save (PyYAML doesn't preserve them) but the file is still readable.
DEFAULTS: dict[str, Any] = {
    # All page-related config (credentials + display toggles + future
    # settings) lives under ``pages.<page_name>``. Adding a new per-page
    # field is purely additive — no schema break. Pages not listed here
    # (Clock / System) have no config and are always shown.
    "pages": {
        "github": {
            "token":   "__REPLACE_ME__",   # GitHub PAT; env GITHUB_TOKEN overrides
            "enabled": False,              # flipped by `codebotd setup` phase 4
        },
        "claude": {
            "enabled": False,              # flipped by `codebotd setup` phase 3
        },
    },
}


class Config:
    """``~/.code_bot/config.yml`` with auto-migration of missing keys.

    Access patterns:

      cfg.get("pages", "github", "token")           # nested look-up, default None
      cfg.get("pages", "github", "enabled", default=False)  # ... with explicit default
      cfg.get("pages", "github")                    # the whole sub-dict
      cfg.pages                                     # attribute access (sugar)
      cfg.path                                      # Path to the on-disk file
    """

    def __init__(self, path: Optional[Path] = None) -> None:
        self._path: Path = Path(path) if path else DEFAULT_CONFIG_PATH
        self._data: dict[str, Any] = self._load_or_create()

    # ---- public API ----

    @property
    def path(self) -> Path:
        """Absolute path to the on-disk config file."""
        return self._path

    def get(self, *keys: str, default: Any = None) -> Any:
        """Look up a nested key path. ``default`` is returned when any
        level is missing or a non-mapping is hit mid-path.

            cfg.get("pages", "github", "token")
            cfg.get("pages", "github", "enabled", default=False)
            cfg.get("pages", "github")           # whole sub-dict
        """
        cur: Any = self._data
        for k in keys:
            if not isinstance(cur, dict):
                return default
            if k not in cur:
                return default
            cur = cur[k]
        return cur

    def __getattr__(self, name: str) -> Any:
        # __getattr__ is only called for attrs not found normally, so
        # ``_data`` / ``_path`` (defined on the instance) never reach
        # here. Still guard against dunder / unknown keys raising
        # confusing errors.
        if name.startswith("_") or name not in DEFAULTS:
            raise AttributeError(name)
        return self._data.get(name)

    def set(self, *keys: str, value: Any) -> None:
        """Set a nested key path in memory. Call ``save()`` to persist.

            cfg.set("pages", "github", "token", value=tok)
            cfg.set("pages", "github", "enabled", value=True)

        Intermediate levels are created when missing and replaced when
        the on-disk value isn't a mapping (a corrupt file shouldn't
        block a legit write).
        """
        if not keys:
            raise ValueError("set() requires at least one key")
        d = self._data
        for k in keys[:-1]:
            nxt = d.get(k)
            if not isinstance(nxt, dict):
                nxt = {}
                d[k] = nxt
            d = nxt
        d[keys[-1]] = value

    def save(self) -> None:
        """Rewrite the on-disk file from the in-memory state. Used by
        the constructor (after a migration) and exposed for future
        callers that want to persist runtime changes."""
        self._write(self._data)

    # ---- internal: load + migrate ----

    def _load_or_create(self) -> dict[str, Any]:
        if not self._path.exists():
            self._path.parent.mkdir(parents=True, exist_ok=True)
            try:
                self._path.parent.chmod(0o700)
            except OSError as e:
                log.warning("chmod 700 on %s failed: %s", self._path.parent, e)
            data = copy.deepcopy(DEFAULTS)
            self._write(data)
            log.info("Created %s with default schema", self._path)
            return data

        loaded = self._read_yaml()
        if loaded is None:
            # read_yaml already warned; start clean.
            data = copy.deepcopy(DEFAULTS)
            self._write(data)
            return data

        if not isinstance(loaded, dict):
            log.warning(
                "%s: top-level is not a mapping (got %s); rewriting with defaults",
                self._path, type(loaded).__name__,
            )
            data = copy.deepcopy(DEFAULTS)
            self._write(data)
            return data

        # In-place migration. ``_migrate`` returns True if it added any
        # leaf key. User values in ``loaded`` are preserved.
        if _migrate(loaded):
            self._write(loaded)
            log.info("Migrated %s: filled in missing keys from schema", self._path)
        return loaded

    def _read_yaml(self) -> Optional[dict]:
        try:
            import yaml  # PyYAML is a project dep
            # newline="" 让 Python 不做 universal-newline 翻译,
            # CRLF 在 Windows 上保持原样交给 PyYAML (它内部处理).
            with open(self._path, "r", encoding="utf-8", newline="") as f:
                return yaml.safe_load(f)
        except FileNotFoundError:
            return None
        except (OSError, ImportError) as e:
            log.warning("Failed to read %s: %s", self._path, e)
            return None
        except yaml.YAMLError as e:  # type: ignore[attr-defined]
            log.warning("Failed to parse %s: %s", self._path, e)
            return None

    def _write(self, data: dict[str, Any]) -> None:
        try:
            import yaml
        except ImportError as e:
            log.error("PyYAML is required to write the config: %s", e)
            return

        # Ensure parent dir exists with 700 perms.
        self._path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self._path.parent.chmod(0o700)
        except OSError:
            pass  # best-effort

        # Atomic write: tmp + rename. Same directory is required for
        # os.replace() to be atomic on POSIX.
        tmp = self._path.with_name(self._path.name + ".tmp")
        try:
            # newline="" 让 yaml 控制换行符 (默认 LF),
            # 避免 Windows 上 Python 默认加 \r\n 干扰跨平台一致性.
            with open(tmp, "w", encoding="utf-8", newline="") as f:
                yaml.safe_dump(
                    data, f,
                    default_flow_style=False,
                    sort_keys=False,
                    allow_unicode=True,
                )
            os.chmod(tmp, 0o600)
            os.replace(tmp, self._path)
        except (OSError, yaml.YAMLError) as e:  # type: ignore[attr-defined]
            log.error("Failed to write %s: %s", self._path, e)
            # Best-effort cleanup of the tmp file.
            try:
                tmp.unlink()
            except OSError:
                pass


# ---- module-level helpers ----

def _migrate(loaded: dict[str, Any], defaults: dict[str, Any] = DEFAULTS) -> bool:
    """Fill in missing keys in ``loaded`` from ``defaults`` (recursive).

    Modifies ``loaded`` in place. Returns ``True`` if any leaf key was
    added (i.e. the file should be rewritten). User-set values in
    ``loaded`` are preserved. Extra unknown keys in ``loaded`` are
    preserved as-is.

    The ``defaults`` arg is threaded through recursion so the inner
    call iterates over the right sub-schema, not the top-level one.
    """
    changed = False
    for k, default_v in defaults.items():
        if k not in loaded:
            loaded[k] = copy.deepcopy(default_v)
            changed = True
        elif isinstance(default_v, dict) and isinstance(loaded.get(k), dict):
            if _migrate(loaded[k], default_v):
                changed = True
    return changed


# ---- page toggles ----

PAGES_SECTION = "pages"


def page_enabled(cfg: "Config", name: str, default: bool = False) -> bool:
    """``pages.<name>.enabled`` 的便捷读取。"""
    return bool(cfg.get(PAGES_SECTION, name, "enabled", default=default))


def set_page_enabled(cfg: "Config", name: str, enabled: bool) -> None:
    """持久化 ``pages.<name>.enabled``。

    Best-effort: ``Config.save()`` 在 I/O 错误时只 log 不抛（daemon 启动
    路径上不希望 read-only home 致命），调用方如果必须确保落盘应自行读
    回验证。
    """
    cfg.set(PAGES_SECTION, name, "enabled", value=bool(enabled))
    cfg.save()
    log.info("pages.%s.enabled = %s (%s)", name, enabled, cfg.path)
