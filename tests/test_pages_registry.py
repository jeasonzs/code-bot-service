"""pages_registry.run_type_phase: per-type phase loop.

``DEFAULT_CONFIG_PATH`` is captured at module import time, so changing
``$HOME`` doesn't move it. Each test passes an explicit ``path=`` to
``Config`` instead.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


def _cfg(tmpdir: Path, pages=None):
    from codebot.config import Config
    path = tmpdir / ".code_bot" / "config.yml"
    cfg = Config(path=path)
    cfg._data["pages"] = pages if pages is not None else []
    cfg.save()
    return Config(path=path)


class TestRunTypePhase(unittest.TestCase):

    def test_non_interactive_is_noop(self) -> None:
        from codebot import pages_registry, _ui
        cfg = _cfg(Path(tempfile.mkdtemp()), pages=[])
        with patch.object(_ui, "is_interactive", return_value=False):
            rc = pages_registry.run_type_phase(
                cfg, kind="github",
                add_handler=lambda: (0, None),
                modify_handler=lambda c, e: None,
                format_entry=pages_registry.format_github_entry,
            )
        self.assertEqual(rc, 0)
        self.assertEqual(cfg.get("pages"), [])

    def test_add_then_done_persists(self) -> None:
        from codebot import pages_registry, _ui
        cfg = _cfg(Path(tempfile.mkdtemp()))

        add_handler = lambda: (0, {"type": "github", "token": "ghp_x"})

        sequence = iter(["Add a GitHub account…", "Done"])
        with patch.object(_ui, "select",
                          side_effect=lambda msg, c, default: next(sequence)), \
             patch.object(_ui, "is_interactive", return_value=True):
            rc = pages_registry.run_type_phase(
                cfg, kind="github",
                add_handler=add_handler,
                modify_handler=lambda c, e: None,
                format_entry=pages_registry.format_github_entry,
                add_label="Add a GitHub account…",
            )

        self.assertEqual(rc, 0)
        self.assertEqual(cfg.get("pages"),
                         [{"type": "github", "token": "ghp_x"}])

    def test_delete_with_confirm(self) -> None:
        from codebot import pages_registry, _ui
        cfg = _cfg(Path(tempfile.mkdtemp()), pages=[
            {"type": "github", "token": "a"},
            {"type": "github", "token": "b"},
            {"type": "github", "token": "c"},
        ])

        # All three entries format to "github" (token is opaque);
        # the menu shows three "github" labels and the mock picks the
        # first one (index 0 → original pages index 0).
        sequence = iter(["github", "Delete", "Done"])
        with patch.object(_ui, "select",
                          side_effect=lambda msg, c, default: next(sequence)), \
             patch.object(_ui, "confirm",
                          side_effect=lambda msg, default=False: True), \
             patch.object(_ui, "is_interactive", return_value=True):
            rc = pages_registry.run_type_phase(
                cfg, kind="github",
                add_handler=lambda: (0, None),
                modify_handler=lambda c, e: None,
                format_entry=pages_registry.format_github_entry,
                add_label="Add a GitHub account…",
            )

        self.assertEqual(rc, 0)
        self.assertEqual(
            [e["token"] for e in cfg.get("pages")],
            ["b", "c"],
        )

    def test_delete_cancelled_keeps_entry(self) -> None:
        from codebot import pages_registry, _ui
        cfg = _cfg(Path(tempfile.mkdtemp()),
                   pages=[{"type": "github", "token": "x"}])

        sequence = iter(["github", "Delete", "Done"])
        with patch.object(_ui, "select",
                          side_effect=lambda msg, c, default: next(sequence)), \
             patch.object(_ui, "confirm",
                          side_effect=lambda msg, default=False: False), \
             patch.object(_ui, "is_interactive", return_value=True):
            rc = pages_registry.run_type_phase(
                cfg, kind="github",
                add_handler=lambda: (0, None),
                modify_handler=lambda c, e: None,
                format_entry=pages_registry.format_github_entry,
                add_label="Add a GitHub account…",
            )

        self.assertEqual(rc, 0)
        self.assertEqual(cfg.get("pages"), [{"type": "github", "token": "x"}])

    def test_other_type_entries_passed_through(self) -> None:
        """Phase for kind=github must not touch system entries."""
        from codebot import pages_registry, _ui
        cfg = _cfg(Path(tempfile.mkdtemp()), pages=[
            {"type": "system", "target": "local"},
            {"type": "github", "token": "x"},
            {"type": "claude", "target": "local"},
        ])

        # Pick "github" — index 0 → the first github entry; system and
        # claude entries are filtered out of the menu.
        sequence = iter(["github", "Delete", "Done"])
        with patch.object(_ui, "select",
                          side_effect=lambda msg, c, default: next(sequence)), \
             patch.object(_ui, "confirm",
                          side_effect=lambda msg, default=False: True), \
             patch.object(_ui, "is_interactive", return_value=True):
            pages_registry.run_type_phase(
                cfg, kind="github",
                add_handler=lambda: (0, None),
                modify_handler=lambda c, e: None,
                format_entry=pages_registry.format_github_entry,
                add_label="Add a GitHub account…",
            )

        kinds = [e["type"] for e in cfg.get("pages")]
        self.assertEqual(kinds, ["system", "claude"])

    def test_modify_replaces_entry(self) -> None:
        from codebot import pages_registry, _ui
        cfg = _cfg(Path(tempfile.mkdtemp()),
                   pages=[{"type": "github", "token": "old"}])

        new = {"type": "github", "token": "new"}
        # 1st: pick the entry ("github"), 2nd: Modify, then Done.
        sequence = iter(["github", "Modify", "Done"])
        with patch.object(_ui, "select",
                          side_effect=lambda msg, c, default: next(sequence)), \
             patch.object(_ui, "is_interactive", return_value=True):
            pages_registry.run_type_phase(
                cfg, kind="github",
                add_handler=lambda: (0, None),
                modify_handler=lambda c, e: new,
                format_entry=pages_registry.format_github_entry,
                add_label="Add a GitHub account…",
            )

        self.assertEqual(cfg.get("pages"), [{"type": "github", "token": "new"}])


class TestFormatEntryHelpers(unittest.TestCase):

    def test_claude_local(self) -> None:
        from codebot.pages_registry import format_claude_entry
        self.assertEqual(format_claude_entry({"type": "claude", "target": "local"}),
                         "claude (local)")

    def test_claude_ssh(self) -> None:
        from codebot.pages_registry import format_claude_entry
        self.assertEqual(
            format_claude_entry({"type": "claude", "target": "ssh",
                                 "ssh_config": {"host": "b.example.com"}}),
            "claude (ssh: b.example.com)",
        )

    def test_github_with_token(self) -> None:
        from codebot.pages_registry import format_github_entry
        self.assertEqual(format_github_entry({"type": "github", "token": "x"}),
                         "github")

    def test_github_without_token(self) -> None:
        from codebot.pages_registry import format_github_entry
        self.assertEqual(format_github_entry({"type": "github", "token": ""}),
                         "github (NO TOKEN)")

    def test_system_local(self) -> None:
        from codebot.pages_registry import format_system_entry
        self.assertEqual(format_system_entry({"type": "system", "target": "local"}),
                         "system (local)")

    def test_system_ssh(self) -> None:
        from codebot.pages_registry import format_system_entry
        self.assertEqual(
            format_system_entry({"type": "system", "target": "ssh",
                                 "ssh_config": {"host": "b.example.com"}}),
            "system (ssh: b.example.com)",
        )


if __name__ == "__main__":
    unittest.main()