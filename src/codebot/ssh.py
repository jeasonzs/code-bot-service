"""SSH target dataclasses + ``parse_target`` for remote page entries.

A page entry whose ``type`` is ``system`` or ``claude`` can have a
``target`` discriminator:

  - ``"local"`` (or absent) → :class:`LocalTarget` (no remote creds).
  - ``"ssh"`` → requires a sibling ``ssh_config:`` mapping with
    ``username``, ``host``, optional ``password``, optional ``port``.

Pages don't consume this module directly; collectors do (they build the
paramiko client from an :class:`SshTarget`). The setup layer also reaches
through ``parse_target`` to validate a freshly-typed entry before saving.

Why a separate module: both ``ssh_client`` (paramiko glue) and the
collectors / setup need the same shape, and tests want a single source of
truth for the schema.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Union


@dataclass
class LocalTarget:
    """Sentinel for "this machine" — no credentials required."""


@dataclass
class SshTarget:
    username: str
    host: str
    password: Optional[str] = None
    # ``None`` means paramiko's default (22). Out-of-range or non-int
    # values are rejected by ``parse_target`` before they get stored.
    port: Optional[int] = None


Target = Union[LocalTarget, SshTarget]


def parse_target(entry: dict) -> Target:
    """Read ``entry`` and produce a target object.

    Accepted shapes:
      - ``{"target": "local"}`` / missing                   → LocalTarget
      - ``{"target": "ssh", "ssh_config": {...}}``          → SshTarget

    Raises ``ValueError`` for unknown discriminator, missing
    ``ssh_config`` sub-dict, or an out-of-range / non-integer ``port``.
    """
    raw = entry.get("target", "local")
    if raw == "local":
        return LocalTarget()
    if raw == "ssh":
        spec = entry.get("ssh_config")
        if not isinstance(spec, dict):
            raise ValueError(
                "target=ssh requires nested ssh_config: {username, host, password?}"
            )
        try:
            username = str(spec["username"])
            host = str(spec["host"])
        except KeyError as e:
            raise ValueError(
                f"ssh_config missing required field {e.args[0]!r}"
            ) from None
        password = spec.get("password")
        port_raw = spec.get("port")
        port = _coerce_port(port_raw)
        return SshTarget(
            username=username, host=host, password=password, port=port,
        )
    raise ValueError(f"invalid target {raw!r}; expected 'local' or 'ssh'")


def _coerce_port(value) -> Optional[int]:
    """Validate an ssh_config port value.

    Accepts ``None`` (default), int 1..65535, or a string of digits in
    the same range. Anything else raises ``ValueError``.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError(f"ssh_config.port must be int, got bool {value!r}")
    if isinstance(value, int):
        port = value
    elif isinstance(value, str):
        s = value.strip()
        if not s:
            return None
        if not s.isdigit():
            raise ValueError(
                f"ssh_config.port must be digits, got {value!r}"
            )
        port = int(s)
    else:
        raise ValueError(
            f"ssh_config.port must be int or str, got {type(value).__name__}"
        )
    if not 1 <= port <= 65535:
        raise ValueError(
            f"ssh_config.port out of range (1..65535), got {port}"
        )
    return port