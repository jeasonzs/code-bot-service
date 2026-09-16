"""Legacy ``pages`` dict → array migration (config._migrate_pages_array)."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from codebot.config import _migrate_pages_array


class TestMigratePagesArray(unittest.TestCase):

    def test_already_list_is_noop(self) -> None:
        loaded = {"pages": [{"type": "system", "target": "local"}]}
        self.assertFalse(_migrate_pages_array(loaded))
        self.assertEqual(loaded["pages"], [{"type": "system", "target": "local"}])

    def test_empty_dict_yields_only_system_local(self) -> None:
        loaded = {"pages": {}}
        self.assertTrue(_migrate_pages_array(loaded))
        self.assertEqual(loaded["pages"], [{"type": "system", "target": "local"}])

    def test_missing_pages_field_is_noop(self) -> None:
        loaded = {"other": 1}
        self.assertFalse(_migrate_pages_array(loaded))

    def test_github_token_carries_over(self) -> None:
        loaded = {"pages": {"github": {"token": "ghp_real", "enabled": True}}}
        _migrate_pages_array(loaded)
        self.assertIn({"type": "github", "token": "ghp_real"}, loaded["pages"])

    def test_github_placeholder_dropped(self) -> None:
        loaded = {"pages": {"github": {"token": "__REPLACE_ME__", "enabled": False}}}
        _migrate_pages_array(loaded)
        # Only the implicit system-local entry should remain.
        self.assertEqual(loaded["pages"], [{"type": "system", "target": "local"}])

    def test_claude_enabled_becomes_local_entry(self) -> None:
        loaded = {"pages": {"claude": {"enabled": True}}}
        _migrate_pages_array(loaded)
        self.assertTrue(
            any(e["type"] == "claude" and e["target"] == "local" for e in loaded["pages"])
        )

    def test_claude_disabled_is_skipped(self) -> None:
        loaded = {"pages": {"claude": {"enabled": False}}}
        _migrate_pages_array(loaded)
        self.assertFalse(any(e["type"] == "claude" for e in loaded["pages"]))

    def test_custom_commands_chunked_by_two(self) -> None:
        items = [{"name": f"a{i}", "icon": "", "command": "x"} for i in range(5)]
        loaded = {"pages": {"custom_commands": {"items": items, "enabled": True}}}
        _migrate_pages_array(loaded)
        custom_entries = [e for e in loaded["pages"] if e["type"] == "custom_commands"]
        self.assertEqual(len(custom_entries), 3)
        self.assertEqual(len(custom_entries[0]["items"]), 2)
        self.assertEqual(len(custom_entries[1]["items"]), 2)
        self.assertEqual(len(custom_entries[2]["items"]), 1)

    def test_empty_custom_commands_items_skipped(self) -> None:
        loaded = {"pages": {"custom_commands": {"items": [], "enabled": False}}}
        _migrate_pages_array(loaded)
        custom_entries = [e for e in loaded["pages"] if e["type"] == "custom_commands"]
        self.assertEqual(custom_entries, [])

    def test_full_mix(self) -> None:
        loaded = {
            "pages": {
                "github": {"token": "ghp_x", "enabled": True},
                "claude": {"enabled": True},
                "custom_commands": {
                    "items": [{"name": "C", "icon": "", "command": "x"}],
                    "enabled": True,
                },
            }
        }
        _migrate_pages_array(loaded)
        types = [e["type"] for e in loaded["pages"]]
        self.assertEqual(types, ["github", "claude", "system", "custom_commands"])
        self.assertEqual(loaded["pages"][0]["token"], "ghp_x")
        self.assertEqual(loaded["pages"][1]["target"], "local")
        self.assertEqual(loaded["pages"][2]["target"], "local")
        self.assertEqual(loaded["pages"][3]["items"][0]["name"], "C")


if __name__ == "__main__":
    unittest.main()