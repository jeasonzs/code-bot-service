"""Tests for ``codebot._ui``.

The three invariants to lock down:

  1. **Non-interactive prompts never import questionary.** This is the
     rule that makes ``--yes`` safe (no raw-mode TTY ownership during
     a sudo subprocess). The test stubs out ``questionary`` to raise
     on any call and then exercises every prompt function.

  2. **Cancellation propagates as ``WizardCancelled``.** Ctrl-C at a
     prompt is the orchestrator's signal to stop. We feed ``None`` into
     the underlying ``.ask()`` (which is what questionary does) and
     expect the wrapper to raise, not return ``None``.

  3. **Plain-text output is byte-identical to the old format.** CI
     greps for ``  [PASS] name: detail`` (the ``Check.render()`` shape).
     When the wizard is non-interactive that line must survive
     unchanged.

The interactive-path tests use a fake ``questionary`` to inject a
chosen value, so we don't need prompt_toolkit's TTY handling for tests.
"""

from __future__ import annotations

import builtins
import contextlib
import io
import sys
import unittest
from unittest import mock


# Inject a fake ``questionary`` BEFORE importing codebot._ui. Once the
# real module is imported, every prompt's path-of-least-resistance goes
# through it; in the non-interactive case we want the import to *never
# happen*, and that's the rule under test.
class _ExplodingQuestionary:
    """Any access to questionary in a non-interactive run is a test failure."""

    def __getattr__(self, name):
        raise AssertionError(
            f"questionary.{name} was called in non-interactive mode — "
            "this breaks the sudo/raw-mode ordering invariant"
        )


def _install_fake_questionary(answers: dict):
    """Return a context manager that stubs questionary.confirm/select/etc.

    ``answers`` is a dict from attribute name → value to return from
    ``.ask()``. e.g. ``{"confirm": True}`` makes any
    ``questionary.confirm(...).ask()`` return True.
    """

    class _Fake:
        def confirm(self, message, default=True):  # noqa: ARG002
            return _F("confirm", answers, default)

        def select(self, message, choices, default=None):  # noqa: ARG002
            return _F("select", answers, default)

        def text(self, message, default=""):  # noqa: ARG002
            return _F("text", answers, default)

        def password(self, message):  # noqa: ARG002
            return _F("password", answers, "")

        def path(self, message, default=""):  # noqa: ARG002
            return _F("path", answers, default)

    class _F:
        def __init__(self, kind, answers, default):
            self.kind = kind
            self.answers = answers
            self.default = default

        def ask(self):
            if self.kind in self.answers:
                return self.answers[self.kind]
            raise AssertionError(
                f"fake questionary: no answer registered for {self.kind!r}"
            )

    return mock.patch.dict(sys.modules, {"questionary": _Fake()})


class _ExplodingRich:
    def __getattr__(self, name):
        raise AssertionError(
            f"rich.{name} was called in non-interactive mode — "
            "non-TTY paths must use plain print(), not rich's Console"
        )


class NonInteractiveTests(unittest.TestCase):
    """Every prompt must return its default and never touch the heavy libs."""

    def setUp(self):
        # Reset module state between tests.
        sys.modules["questionary"] = _ExplodingQuestionary()
        sys.modules["rich"] = _ExplodingRich()
        sys.modules["rich.console"] = _ExplodingRich()
        sys.modules["rich.markup"] = _ExplodingRich()
        # Re-import the module fresh so it picks up the stubs.
        for mod in list(sys.modules):
            if mod == "codebot._ui" or mod.startswith("codebot."):
                sys.modules.pop(mod, None)
        from codebot import _ui

        self._ui = _ui
        self._ui.bind(interactive=False)

    def test_confirm_returns_default(self):
        self.assertTrue(self._ui.confirm("q?", default=True))
        self.assertFalse(self._ui.confirm("q?", default=False))

    def test_select_returns_default(self):
        self.assertEqual(
            self._ui.select("q?", ["a", "b"], default="b"), "b"
        )

    def test_text_and_password_return_default(self):
        self.assertEqual(self._ui.text("q?", default="hi"), "hi")
        self.assertEqual(self._ui.password("q?", default="sekret"), "sekret")
        self.assertEqual(self._ui.path("q?", default="/tmp"), "/tmp")

    def test_default_omitted_is_empty(self):
        """text/password/path default to '' so non-interactive code can treat
        '' as 'user skipped' without ceremony."""
        self.assertEqual(self._ui.text("q?"), "")
        self.assertEqual(self._ui.password("q?"), "")
        self.assertEqual(self._ui.path("q?"), "")

    def test_check_format_is_byte_identical_to_doctor(self):
        """CI greps for ``  [PASS] name: detail`` — this row must hold."""
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self._ui.check("usb", "PASS", "device found")
        self.assertEqual(buf.getvalue(), "  [PASS] usb: device found\n")

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self._ui.check("arch", "FAIL", "not x86_64")
        self.assertEqual(buf.getvalue(), "  [FAIL] arch: not x86_64\n")

    def test_section_uses_bracketed_prefix(self):
        """Non-interactive ``section()`` matches the old ``[setup] <title>``
        prefix so existing log scrapers keep working."""
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self._ui.section("Phase 1/5 — doctor")
        self.assertIn("[setup] Phase 1/5 — doctor", buf.getvalue())


class InteractiveTests(unittest.TestCase):
    """When interactive, questionary is called and cancellation is loud."""

    def setUp(self):
        for mod in list(sys.modules):
            if mod == "codebot._ui" or mod.startswith("codebot."):
                sys.modules.pop(mod, None)
        from codebot import _ui

        self._ui = _ui

    def test_confirm_uses_questionary(self):
        with _install_fake_questionary({"confirm": True}):
            self._ui.bind(interactive=True)
            self.assertTrue(self._ui.confirm("write to /etc/udev?", default=False))
        # Reset for next test.
        self._ui.bind(interactive=False)

    def test_select_returns_user_choice(self):
        with _install_fake_questionary({"select": "skip"}):
            self._ui.bind(interactive=True)
            self.assertEqual(
                self._ui.select("continue?", ["go", "skip"], default="go"),
                "skip",
            )
        self._ui.bind(interactive=False)

    def test_none_from_questionary_raises_wizard_cancelled(self):
        with _install_fake_questionary({"confirm": None}):
            self._ui.bind(interactive=True)
            with self.assertRaises(self._ui.WizardCancelled):
                self._ui.confirm("q?", default=True)
        self._ui.bind(interactive=False)

    def test_path_strips_whitespace(self):
        """Paths with trailing spaces are a common copy-paste accident."""
        with _install_fake_questionary({"path": "/tmp/foo.json  "}):
            self._ui.bind(interactive=True)
            self.assertEqual(self._ui.path("where?", default=""), "/tmp/foo.json")
        self._ui.bind(interactive=False)


class MarkupEscapingTests(unittest.TestCase):
    """Dynamic text must NOT be interpreted as rich markup.

    A ``[`` in a file path or error string would otherwise be parsed as a
    style tag and either disappear or raise MarkupError. Verified by
    running an interactive check() and asserting the output contains the
    raw ``[`` and ``]``.
    """

    def setUp(self):
        # Drop the rich stubs from earlier tests so the real rich is
        # importable here.
        for mod in ("rich", "rich.console", "rich.markup"):
            sys.modules.pop(mod, None)
        for mod in list(sys.modules):
            if mod == "codebot._ui" or mod.startswith("codebot."):
                sys.modules.pop(mod, None)
        from codebot import _ui

        self._ui = _ui
        self._ui.bind(interactive=True)

    def test_check_escapes_brackets_in_detail(self):
        from rich.console import Console

        buf = io.StringIO()
        fake_console = Console(
            file=buf, force_terminal=True, color_system=None, highlight=False
        )
        with mock.patch.object(self._ui, "_get_console", return_value=fake_console):
            self._ui.check("sudo", "WARN", "[sudo] password for me")
        out = buf.getvalue()
        # The literal "[sudo]" should appear in the rendered output,
        # not be consumed as a style tag.
        self.assertIn("[sudo]", out)


if __name__ == "__main__":
    unittest.main()
