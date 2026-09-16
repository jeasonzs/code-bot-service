"""Claude Code page setup (local or SSH host).

Local flow invokes ``claude_integration.install.run_install`` to write
the statusline + 8 hook blocks into ``~/.claude/settings.json`` (the
standard Claude Code install path).

SSH flow only validates the connection and saves an entry — the remote
machine needs the same install done locally on it (we don't ship code
over the wire in v1). The page will show offline until the remote's
hooks are writing state files; the daemon's snapshot parser already
handles missing-file as "idle".

Returns ``(rc, entry | None)``; the registry appends / replaces.
"""

from __future__ import annotations

from typing import Optional

from .ssh import LocalTarget, SshTarget, parse_target


def run_claude_setup() -> tuple[int, Optional[dict]]:
    """Add a new Claude page. Returns ``(rc, entry | None)``."""
    from . import _ui

    if not _ui.is_interactive():
        # Non-interactive / --yes mode: skip the install entirely.
        return 0, {"type": "claude", "target": "local"}

    choice = _ui.select(
        "Claude Code page source:",
        ["Local (this machine)", "SSH host"],
        default="Local (this machine)",
    )
    if choice == "Local (this machine)":
        entry = _install_local()
        if entry is None:
            return 1, None
        return 0, entry

    ssh_config = _prompt_ssh()
    if ssh_config is None:
        return 1, None
    return 0, {"type": "claude", "target": "ssh", "ssh_config": ssh_config}


def modify_claude_entry(cfg, entry: dict) -> Optional[dict]:
    """Edit an existing Claude entry. Returns the updated entry, or
    ``None`` to keep the existing one (user cancelled)."""
    from . import _ui

    target = parse_target(entry)
    if isinstance(target, LocalTarget):
        choice = _ui.select(
            "Claude page (currently local):",
            ["Reinstall hooks", "Switch to SSH", "Keep as-is"],
            default="Keep as-is",
        )
        if choice == "Reinstall hooks":
            new_entry = _install_local()
            if new_entry is None:
                return None
            return new_entry
        if choice == "Switch to SSH":
            ssh_config = _prompt_ssh()
            if ssh_config is None:
                return None
            return {"type": "claude", "target": "ssh", "ssh_config": ssh_config}
        return entry

    # Was SSH — re-prompt with current values.
    ssh_config = _prompt_ssh(
        default_username=target.username,
        default_host=target.host,
        default_password=target.password,
    )
    if ssh_config is None:
        return None
    return {"type": "claude", "target": "ssh", "ssh_config": ssh_config}


# ---- internals ----

def _install_local() -> Optional[dict]:
    """Install Claude hooks into ``~/.claude/settings.json`` locally."""
    from . import _ui
    from .claude_integration import install

    with _ui.spinner("Writing Claude Code hooks …"):
        rc = install.run_install()
    if rc != 0:
        _ui.warn("Claude hook install failed (see messages above)")
        if not _ui.confirm("Continue without hooks? Page will show idle.", default=False):
            return None
    else:
        _ui.check("Claude hooks", "PASS", "installed in ~/.claude/settings.json")
    return {"type": "claude", "target": "local"}


def _prompt_ssh(
    *,
    default_username: str = "",
    default_host: str = "",
    default_password: Optional[str] = None,
) -> Optional[dict]:
    """Same SSH prompt shape as :mod:`system_setup`. Reuses the
    paramiko glue to validate the connection before saving."""
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

    password = default_password
    if password is None:
        if _ui.confirm("Use password auth?", default=False):
            entered = _ui.password(
                "SSH password (hidden, Enter to skip):", default="",
            )
            password = entered or None

    target = SshTarget(username=username, host=host, password=password)

    if _ui.confirm(f"Test connection to {username}@{host}?", default=True):
        with _ui.spinner(f"Connecting to {username}@{host} …"):
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

    _ui.hint([
        f"On {host}: run `codebotd install-claude` to write the same",
        "hooks into the remote ~/.claude/settings.json. Without that,",
        "the page will show 'idle' (no state files yet).",
    ])

    ssh_config: dict = {"username": username, "host": host}
    if password is not None:
        ssh_config["password"] = password
    return ssh_config