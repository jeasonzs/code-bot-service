"""daemon.make_pages: array walk + per-entry collector wiring.

``DEFAULT_CONFIG_PATH`` is captured at module import time, so changing
``$HOME`` doesn't move it. Each test passes an explicit ``path=`` to
``Config`` instead.
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


def _cfg(tmpdir: Path, pages):
    from codebot.config import Config
    path = tmpdir / ".code_bot" / "config.yml"
    cfg = Config(path=path)
    cfg._data["pages"] = pages
    cfg.save()
    return Config(path=path)


class TestMakePages(unittest.TestCase):

    def test_empty_pages_yields_only_clock(self) -> None:
        from codebot.daemon import make_pages
        cfg = _cfg(Path(tempfile.mkdtemp()), [])
        pages, collectors = make_pages(cfg)
        self.assertEqual(len(pages), 1)
        self.assertEqual(collectors, [None])
        self.assertEqual(type(pages[0]).__name__, "ClockPage")

    def test_local_system_uses_local_collector(self) -> None:
        from codebot.daemon import make_pages
        from codebot.collectors.system import LocalSystemCollector
        cfg = _cfg(Path(tempfile.mkdtemp()), [{"type": "system", "target": "local"}])
        pages, collectors = make_pages(cfg)
        self.assertEqual(len(pages), 2)
        self.assertIsInstance(collectors[1], LocalSystemCollector)

    def test_ssh_system_uses_remote_collector(self) -> None:
        from codebot.daemon import make_pages
        from codebot.collectors.remote_system import RemoteSystemCollector
        from codebot.ssh import SshTarget
        cfg = _cfg(Path(tempfile.mkdtemp()), [
            {"type": "system", "target": "ssh",
             "ssh_config": {"username": "u", "host": "h.example.com"}},
        ])
        pages, collectors = make_pages(cfg)
        self.assertEqual(len(pages), 2)
        self.assertIsInstance(collectors[1], RemoteSystemCollector)
        self.assertIsInstance(collectors[1]._target, SshTarget)

    def test_ssh_claude_uses_remote_collector(self) -> None:
        from codebot.daemon import make_pages
        from codebot.collectors.remote_claude import RemoteClaudeCollector
        cfg = _cfg(Path(tempfile.mkdtemp()), [
            {"type": "claude", "target": "ssh",
             "ssh_config": {"username": "u", "host": "h"}},
        ])
        pages, collectors = make_pages(cfg)
        self.assertEqual(len(pages), 2)
        self.assertIsInstance(collectors[1], RemoteClaudeCollector)

    def test_github_collector_takes_token(self) -> None:
        from codebot.daemon import make_pages
        from codebot.collectors.github import GithubCollector
        cfg = _cfg(Path(tempfile.mkdtemp()), [
            {"type": "github", "token": "ghp_test_xyz"},
        ])
        pages, collectors = make_pages(cfg)
        self.assertEqual(len(pages), 2)
        self.assertIsInstance(collectors[1], GithubCollector)
        self.assertEqual(collectors[1]._token, "ghp_test_xyz")

    def test_github_with_empty_token_uses_env_fallback(self) -> None:
        from codebot.daemon import make_pages
        cfg = _cfg(Path(tempfile.mkdtemp()), [{"type": "github", "token": ""}])
        with patch.dict(os.environ, {"GITHUB_TOKEN": "ghp_from_env"}):
            pages, collectors = make_pages(cfg)
        from codebot.collectors.github import GithubCollector
        self.assertIsInstance(collectors[1], GithubCollector)
        self.assertEqual(collectors[1]._token, "ghp_from_env")

    def test_github_with_no_token_and_no_env_is_skipped(self) -> None:
        from codebot.daemon import make_pages
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("GITHUB_TOKEN", None)
            cfg = _cfg(Path(tempfile.mkdtemp()), [{"type": "github", "token": ""}])
            with self.assertLogs("codebot", level="WARNING"):
                pages, _ = make_pages(cfg)
        # Skipped entry — only ClockPage.
        self.assertEqual(len(pages), 1)

    def test_custom_commands_no_collector(self) -> None:
        from codebot.daemon import make_pages
        cfg = _cfg(Path(tempfile.mkdtemp()), [
            {"type": "custom_commands",
             "items": [{"name": "X", "icon": "", "command": "x"}]},
        ])
        pages, collectors = make_pages(cfg)
        self.assertEqual(len(pages), 2)
        self.assertIsNone(collectors[1])

    def test_invalid_entries_are_skipped(self) -> None:
        from codebot.daemon import make_pages
        cfg = _cfg(Path(tempfile.mkdtemp()), [
            {"type": "bogus"},
            {"type": "github"},                                # no token, no env
            {"type": "custom_commands", "items": []},          # empty items
            {"type": "system", "target": "local"},             # valid
        ])
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("GITHUB_TOKEN", None)
            with self.assertLogs("codebot", level="WARNING"):
                pages, collectors = make_pages(cfg)
        self.assertEqual(len(pages), 2)
        self.assertEqual(type(pages[1]).__name__, "SystemPage")

    def test_multi_instance_preserves_yaml_order(self) -> None:
        from codebot.daemon import make_pages
        cfg = _cfg(Path(tempfile.mkdtemp()), [
            {"type": "system", "target": "local"},
            {"type": "claude", "target": "local"},
            {"type": "github", "token": "ghp_x"},
        ])
        pages, _ = make_pages(cfg)
        titles = [type(p).__name__ for p in pages[1:]]
        self.assertEqual(titles, ["SystemPage", "ClaudePage", "GithubPage"])

    def test_page_titles_per_instance(self) -> None:
        from codebot.daemon import make_pages
        cfg = _cfg(Path(tempfile.mkdtemp()), [
            {"type": "system", "target": "local"},
            {"type": "system", "target": "ssh",
             "ssh_config": {"username": "u", "host": "build.example.com"}},
            {"type": "github", "token": "ghp_x", "account": "me@example"},
            {"type": "custom_commands",
             "items": [{"name": "Chrome", "icon": "", "command": "x"}]},
        ])
        pages, _ = make_pages(cfg)
        self.assertEqual(pages[1].title, "System")
        self.assertEqual(pages[2].title, "System build.example.com")
        self.assertEqual(pages[3].title, "me@example")
        self.assertEqual(pages[4].title, "Chrome")


if __name__ == "__main__":
    unittest.main()