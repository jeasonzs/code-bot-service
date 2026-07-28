"""Wizard UI primitives for ``codebotd setup`` / ``codebotd teardown``.

The only module that imports ``questionary`` or ``rich``. Every phase
(driver / claude / github / service) talks to this layer instead, which
keeps three invariants in one place:

1. **Every prompt takes a mandatory ``default=``.** In non-interactive
   mode the prompt returns that default *without importing questionary*,
   so the ``--yes`` path never enters prompt_toolkit's raw mode. See the
   sudo note below for why that matters.

2. **Ctrl-C aborts the whole wizard.** questionary's ``.ask()`` returns
   ``None`` when the user cancels; the wrappers turn that into
   ``WizardCancelled`` for the orchestrator to catch. "Skip this step" is
   always an *explicit choice* in a ``select()``, never conflated with
   cancellation.

3. **Non-interactive output is byte-identical to the old plain text.**
   ``check()`` emits ``  [PASS] name: detail`` — the same format as
   ``codebot.doctor.Check.render()`` — so CI greps keep working. Colour
   and box-drawing only appear on a real TTY.

Ordering rule for privileged subprocesses
-----------------------------------------
``codebot.os_helper.run_as_root`` spawns ``sudo`` with
``capture_output=True``, which redirects stdout/stderr but *not* stdin —
sudo reads its password from ``/dev/tty``. If prompt_toolkit still owned
the terminal in raw mode at that moment, sudo's prompt would be mangled
and the keystrokes would go to prompt_toolkit instead. So: **within a
phase, every prompt must have returned before a privileged subprocess is
spawned.** The prompts here are synchronous, so "confirm first, then
run_as_root" is enough — but don't interleave them.
"""

from __future__ import annotations

import sys
from contextlib import contextmanager
from typing import Any, Iterator, Sequence


class WizardCancelled(Exception):
    """The user pressed Ctrl-C at a prompt; abort the whole wizard."""


# Single source of truth for "may we prompt?". Set once by bind() from the
# CLI layer; every phase reads it through is_interactive() rather than
# calling isatty() itself.
INTERACTIVE: bool = False

_console = None  # lazily built rich Console


def bind(*, interactive: bool) -> None:
    """Set the wizard's interaction mode. Call once, before any phase."""
    global INTERACTIVE, _console
    INTERACTIVE = interactive
    _console = None  # force a rebuild; colour depends on the mode


def is_interactive() -> bool:
    return INTERACTIVE


def _get_console():
    """A rich Console, or None when we must stay on plain print().

    Import is deferred so that ``import codebot._ui`` stays cheap for the
    daemon, which never renders wizard UI.
    """
    global _console
    if not INTERACTIVE:
        return None
    if _console is None:
        from rich.console import Console

        _console = Console(highlight=False, soft_wrap=True)
    return _console


def _esc(s: str) -> str:
    """Escape rich markup in interpolated text.

    Everything we render is dynamic — file paths, exception strings, udev
    rule names. A literal ``[`` in any of them (``[sudo]``, a Windows
    device ID, a stray glob) would otherwise be parsed as a style tag and
    either vanish or raise MarkupError.
    """
    from rich.markup import escape

    return escape(s)


# ---- output ----

_STATUS_STYLE = {
    "PASS": ("green", "✓"),
    "FAIL": ("red", "✗"),
    "WARN": ("yellow", "!"),
    "INFO": ("dim", "·"),
}


def section(title: str) -> None:
    """A phase heading."""
    con = _get_console()
    if con is None:
        print(f"[setup] {title}")
        return
    con.print()
    con.print(f"[bold cyan]◆[/] [bold]{_esc(title)}[/]")


def check(name: str, status: str, detail: str = "") -> None:
    """One diagnostic / result row.

    Plain-text form is ``  [PASS] name: detail`` — identical to
    ``codebot.doctor.Check.render()``, which CI parses.
    """
    con = _get_console()
    if con is None:
        print(f"  [{status}] {name}: {detail}")
        return
    style, glyph = _STATUS_STYLE.get(status, ("dim", "·"))
    suffix = f" [dim]{_esc(detail)}[/]" if detail else ""
    con.print(f"  [{style}]{glyph}[/] {_esc(name)}{suffix}")


def info(msg: str) -> None:
    con = _get_console()
    if con is None:
        print(f"  {msg}")
        return
    con.print(f"  [dim]{_esc(msg)}[/]")


def warn(msg: str) -> None:
    """A non-fatal problem. Goes to stderr when we can't colour it."""
    con = _get_console()
    if con is None:
        print(f"  WARN: {msg}", file=sys.stderr)
        return
    con.print(f"  [yellow]![/] [yellow]{_esc(msg)}[/]")


def error(msg: str) -> None:
    con = _get_console()
    if con is None:
        print(f"  ERROR: {msg}", file=sys.stderr)
        return
    con.print(f"  [red]✗[/] [red]{_esc(msg)}[/]")


def hint(lines: Sequence[str]) -> None:
    """A block of follow-up instructions (commands to run, docs to read)."""
    con = _get_console()
    if con is None:
        print()
        for line in lines:
            print(f"  {line}")
        print()
        return
    con.print()
    for line in lines:
        con.print(f"  [dim]{_esc(line)}[/]")
    con.print()


def blank() -> None:
    con = _get_console()
    (con.print() if con is not None else print())


# ---- input ----
#
# Every one of these takes a mandatory keyword-only `default=`. That is
# what makes the non-interactive path safe: it returns the default before
# questionary is even imported.

def _ask(q: Any) -> Any:
    """Run a questionary prompt, mapping cancellation to WizardCancelled."""
    answer = q.ask()
    if answer is None:
        raise WizardCancelled()
    return answer


def confirm(message: str, *, default: bool) -> bool:
    if not INTERACTIVE:
        return default
    import questionary

    return bool(_ask(questionary.confirm(message, default=default)))


def select(message: str, choices: Sequence[str], *, default: str) -> str:
    if not INTERACTIVE:
        return default
    import questionary

    return str(_ask(questionary.select(message, choices=list(choices), default=default)))


def text(message: str, *, default: str = "") -> str:
    if not INTERACTIVE:
        return default
    import questionary

    return str(_ask(questionary.text(message, default=default))).strip()


def password(message: str, *, default: str = "") -> str:
    """Hidden input. Empty string is the conventional "skip" answer."""
    if not INTERACTIVE:
        return default
    import questionary

    return str(_ask(questionary.password(message))).strip()


def path(message: str, *, default: str = "") -> str:
    """Filesystem path with Tab completion."""
    if not INTERACTIVE:
        return default
    import questionary

    return str(_ask(questionary.path(message, default=default))).strip()


# ---- progress ----

@contextmanager
def spinner(message: str) -> Iterator[None]:
    """Animated status on a TTY; a single printed line otherwise.

    Degrading matters: rich's Status writes cursor-control escapes that
    would pollute a piped log.
    """
    con = _get_console()
    if con is None:
        print(f"  {message}")
        yield
        return
    with con.status(f"[cyan]{message}[/]", spinner="dots"):
        yield
