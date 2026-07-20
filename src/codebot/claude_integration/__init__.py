"""Claude Code integration: statusline writer + lifecycle hook + installer.

Three pieces (all cross-platform, no shell dependencies):

  * ``statusline.py`` — console script entry point
    (``codebot-claude-statusline``) that reads Claude Code's statusline
    JSON from stdin and writes ``~/.code-bot/claude-state.json``.

  * ``hook.py`` — console script entry point
    (``codebot-claude-status-hook``) that reads Claude Code's lifecycle
    hook payload from stdin and writes
    ``~/.code-bot/claude-status.json``.

  * ``install.py`` — ``codebotd install-claude`` subcommand that merges
    the statusLine + 8 hooks into ``~/.claude/settings.json`` (cross-
    platform safe; uses Path.home() and platform-neutral JSON).

All three are intentionally no-bash: Claude Code invokes hook commands
via the system shell, which on Windows is cmd.exe and can't run .sh
directly. Using console_script entry points (registered by pip at
install time) gives us ``codebot-claude-statusline`` /
``codebot-claude-status-hook`` available on PATH on every platform.
"""
