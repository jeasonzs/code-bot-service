"""parse_target + ssh_client.open_ssh / run_remote unit tests.

``open_ssh`` is exercised via ``unittest.mock.patch`` on
``paramiko.SSHClient`` — we don't stand up an sshd in CI.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from codebot.ssh import LocalTarget, SshTarget, parse_target
from codebot.ssh_client import open_ssh, run_remote


class TestParseTarget(unittest.TestCase):

    def test_missing_target_defaults_to_local(self) -> None:
        self.assertIsInstance(parse_target({}), LocalTarget)

    def test_explicit_local(self) -> None:
        self.assertIsInstance(parse_target({"target": "local"}), LocalTarget)

    def test_ssh_without_nested_spec_raises(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            parse_target({"target": "ssh"})
        self.assertIn("ssh_config", str(ctx.exception))

    def test_ssh_with_full_spec(self) -> None:
        t = parse_target({
            "target": "ssh",
            "ssh_config": {
                "username": "ubuntu",
                "host": "build.example.com",
                "password": "secret",
            },
        })
        self.assertIsInstance(t, SshTarget)
        self.assertEqual(t.username, "ubuntu")
        self.assertEqual(t.host, "build.example.com")
        self.assertEqual(t.password, "secret")

    def test_ssh_password_optional(self) -> None:
        t = parse_target({
            "target": "ssh",
            "ssh_config": {"username": "jdoe", "host": "laptop.lan"},
        })
        self.assertIsNone(t.password)

    def test_ssh_missing_username_raises(self) -> None:
        with self.assertRaises(ValueError):
            parse_target({"target": "ssh", "ssh_config": {"host": "x"}})

    def test_ssh_missing_host_raises(self) -> None:
        with self.assertRaises(ValueError):
            parse_target({"target": "ssh", "ssh_config": {"username": "u"}})

    def test_unknown_target_raises(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            parse_target({"target": "telnet"})
        self.assertIn("telnet", str(ctx.exception))


class TestOpenSsh(unittest.TestCase):

    def test_password_path(self) -> None:
        target = SshTarget(username="u", host="h", password="p")
        fake = MagicMock()
        with patch("codebot.ssh_client.paramiko.SSHClient", return_value=fake):
            client = open_ssh(target, timeout=1.0)
        self.assertIs(client, fake)
        kwargs = fake.connect.call_args.kwargs
        self.assertEqual(kwargs["hostname"], "h")
        self.assertEqual(kwargs["username"], "u")
        self.assertEqual(kwargs["password"], "p")
        # password → no agent / no key lookup (avoid stale agent surprise)
        self.assertFalse(kwargs["allow_agent"])
        self.assertFalse(kwargs["look_for_keys"])

    def test_no_password_enables_agent_and_keys(self) -> None:
        target = SshTarget(username="u", host="h")
        fake = MagicMock()
        # RSAKey.from_private_key_file raises (no real key in CI env)
        with patch("codebot.ssh_client.paramiko.SSHClient", return_value=fake), \
             patch("codebot.ssh_client.paramiko.RSAKey.from_private_key_file",
                   side_effect=OSError("no key")):
            client = open_ssh(target, timeout=1.0)
        self.assertIs(client, fake)
        kwargs = fake.connect.call_args.kwargs
        self.assertIsNone(kwargs["password"])
        self.assertIsNone(kwargs["pkey"])
        self.assertTrue(kwargs["allow_agent"])
        self.assertTrue(kwargs["look_for_keys"])

    def test_no_password_loads_existing_rsa(self) -> None:
        # Simulate the user having ~/.ssh/id_rsa
        target = SshTarget(username="u", host="h")
        fake_pkey = MagicMock(name="RSAKey")
        fake_client = MagicMock()
        with patch("codebot.ssh_client.paramiko.SSHClient", return_value=fake_client), \
             patch("codebot.ssh_client.paramiko.RSAKey.from_private_key_file",
                   return_value=fake_pkey), \
             patch.object(Path, "exists", return_value=True):
            open_ssh(target, timeout=1.0)
        kwargs = fake_client.connect.call_args.kwargs
        self.assertIs(kwargs["pkey"], fake_pkey)


class TestRunRemote(unittest.TestCase):

    def test_returns_rc_stdout_stderr(self) -> None:
        fake_client = MagicMock()
        fake_stdout = MagicMock()
        fake_stderr = MagicMock()
        fake_stdin = MagicMock()
        fake_stdout.read.return_value = b"out text"
        fake_stderr.read.return_value = b"err text"
        fake_stdout.channel.recv_exit_status.return_value = 7
        fake_client.exec_command.return_value = (fake_stdin, fake_stdout, fake_stderr)

        with patch("codebot.ssh_client.paramiko.SSHClient", return_value=fake_client):
            rc, out, err = run_remote(fake_client, "echo hi", timeout=1.0)

        self.assertEqual(rc, 7)
        self.assertEqual(out, "out text")
        self.assertEqual(err, "err text")
        fake_client.exec_command.assert_called_once_with("echo hi", timeout=1.0)
        fake_stdin.close.assert_called_once()
        fake_stdout.close.assert_called_once()
        fake_stderr.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()