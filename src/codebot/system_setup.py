"""System page setup (local or SSH host).

Standalone phase — used both from the ``codebotd setup`` wizard (via
``pages_registry`` for Add/Modify) and from any future direct
``codebotd setup-system`` command. Does NOT touch ``Config`` itself;
returns ``(rc, entry_or_None)`` so the registry can persist the result
in one place.

Returns ``(0, entry)`` on success/skip, ``(1, None)`` if the user
cancelled mid-prompt.
"""

from __future__ import annotations

from typing import Optional

from .ssh import LocalTarget, SshTarget, parse_target


def run_system_setup() -> tuple[int, Optional[dict]]:
    """Add a new system page. Returns ``(rc, entry | None)``.

    ``rc`` is 0 on success/skip (skipped means "non-interactive mode
    fell back to local"). ``entry`` is the pages-array entry dict; the
    registry appends it after we return.
    """
    from . import _ui

    if not _ui.is_interactive():
        return 0, {"type": "system", "target": "local"}

    choice = _ui.select(
        "System page source:",
        ["Local (this machine)", "SSH host"],
        default="Local (this machine)",
    )
    if choice == "Local (this machine)":
        return 0, {"type": "system", "target": "local"}

    ssh_config = _prompt_ssh()
    if ssh_config is None:
        return 1, None
    return 0, {"type": "system", "target": "ssh", "ssh_config": ssh_config}


def modify_system_entry(cfg, entry: dict) -> Optional[dict]:
    """Edit an existing system entry. Returns the updated entry, or
    ``None`` to keep the existing one (user cancelled)."""
    from . import _ui

    target = parse_target(entry)
    if isinstance(target, LocalTarget):
        choice = _ui.select(
            "System page (currently local):",
            ["Keep local", "Switch to SSH"],
            default="Keep local",
        )
        if choice == "Switch to SSH":
            ssh_config = _prompt_ssh()
            if ssh_config is None:
                return None
            return {"type": "system", "target": "ssh", "ssh_config": ssh_config}
        return entry  # no-op: stay local

    # Was SSH — re-prompt with current values as defaults.
    ssh_config = _prompt_ssh(
        default_username=target.username,
        default_host=target.host,
        default_password=target.password,
        default_port=target.port,
    )
    if ssh_config is None:
        return None
    return {"type": "system", "target": "ssh", "ssh_config": ssh_config}


def _prompt_ssh(
    *,
    default_username: str = "",
    default_host: str = "",
    default_password: Optional[str] = None,
    default_port: Optional[int] = None,
) -> Optional[dict]:
    """Prompt for username / host / optional password and test the
    connection. Returns the ``ssh_config`` dict, or ``None`` if the
    user cancelled. Tests the connection only when the user opts in
    (test failures are non-fatal — ``save anyway`` keeps the entry)."""
    from . import _ui
    from .ssh_client import open_ssh

    username = _ui.text("SSH username:", default=default_username).strip()
    if not username:
        _ui.warn("username required")
        return None
    host = _ui.text("SSH host:", default=default_host).strip()
    if not host:
        _ui.warn("host required")
        return None
    port = _prompt_port(default_port)

    # Password: only ask if there's no existing one to keep.
    password = default_password
    if password is None:
        # No separate "use password?" confirm — just prompt. Empty
        # Enter = no password (let ssh fall through to keys / agent).
        entered = _ui.password(
            "SSH password (hidden, Enter to skip):", default="",
        )
        password = entered or None

    target = SshTarget(
        username=username, host=host, password=password, port=port,
    )

    addr = f"{username}@{host}" + (f":{port}" if port else "")
    if _ui.confirm(f"Test connection to {addr}?", default=True):
        with _ui.spinner(f"Connecting to {addr} …"):
            client = None
            try:
                client = open_ssh(target, timeout=5.0)
            except Exception as e:  # noqa: BLE001
                _ui.warn(f"Connection failed: {e}")
                if not _ui.confirm("Save anyway?", default=False):
                    return None
            finally:
                if client is not None:
                    try:
                        client.close()
                    except OSError:
                        pass

    ssh_config: dict = {"username": username, "host": host}
    if port is not None:
        ssh_config["port"] = port
    if password is not None:
        ssh_config["password"] = password
    return ssh_config


def _prompt_port(default: Optional[int]) -> Optional[int]:
    """Prompt for SSH port; empty / "22" → None (paramiko default).

    Re-prompts on invalid input rather than failing the whole wizard.
    """
    from . import _ui
    hint = "22" if default is None else str(default)
    while True:
        raw = _ui.text("SSH port:", default=hint).strip()
        if not raw:
            return None
        try:
            port = int(raw)
        except ValueError:
            _ui.warn("port must be a number")
            continue
        if 1 <= port <= 65535:
            return None if port == 22 else port
        _ui.warn("port must be 1..65535")