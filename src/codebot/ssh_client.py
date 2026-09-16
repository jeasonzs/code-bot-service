"""Paramiko SSH client glue for remote collectors.

Each :class:`codebot.ssh.SshTarget` ends one ``SSHClient`` long-lived
connection — paramiko clients are not thread-safe, so collectors must
hold them on the background sampling thread and never share across
threads.

Host-key policy is ``AutoAddPolicy`` (TOFU). v1 doesn't ask before
accepting an unknown host key; v2 can swap in ``RejectPolicy`` + a one-
shot trust prompt.

Algorithm compatibility
-----------------------
``open_ssh`` broadens paramiko's preferred algorithm lists so SSH into
older appliances (routers, NAS, OpenWrt pre-21, busybox / dropbear,
embedded Linux boards) still works. Paramiko's defaults reject SHA-1 /
1024-bit DH / DSA / 3DES for security; many embedded SSH servers only
offer those. The trade-off:

  • Strict servers (modern OpenSSH) — broad list is a superset, no
    difference vs. defaults.
  • Legacy servers — broad list lets the handshake succeed.

The lists are applied once per process at import time (paramiko reads
its class-level ``_preferred_*`` lists when a Transport is constructed).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional, Tuple

import paramiko

from .ssh import SshTarget


log = logging.getLogger("codebot.ssh_client")


# Permissive algorithm lists. The legacy entries (marked "legacy") are
# what paramiko's defaults reject; everything else is the modern default.
_LEGACY_KEX = (
    "ecdh-sha2-nistp521",
    "ecdh-sha2-nistp384",
    "ecdh-sha2-nistp256",
    "diffie-hellman-group-exchange-sha256",
    "diffie-hellman-group14-sha256",
    "diffie-hellman-group-exchange-sha1",
    "diffie-hellman-group14-sha1",
    "diffie-hellman-group1-sha1",      # legacy — 1024-bit DH, weak
)
# Host-key algorithms accepted during kex negotiation. Paramiko's defaults
# exclude ssh-rsa (SHA-1 signatures), so a server that only offers ssh-rsa
# gets rejected with "no acceptable host key". ssh-dss was here too but
# paramiko 5.x dropped DSAKey entirely — see the filter in
# _apply_legacy_algorithms below.
_LEGACY_HOST_KEYS = (
    "ssh-ed25519",
    "ecdsa-sha2-nistp256",
    "ecdsa-sha2-nistp384",
    "ecdsa-sha2-nistp521",
    "rsa-sha2-512",
    "rsa-sha2-256",
    "ssh-rsa",                          # legacy — SHA-1 signature
    "ssh-dss",                          # legacy — DSA, dropped in paramiko 5.x
)
_LEGACY_CIPHERS = (
    "aes128-ctr", "aes192-ctr", "aes256-ctr",
    "aes128-cbc", "aes192-cbc", "aes256-cbc",  # CBC modes (no longer preferred)
    "3des-cbc",                         # legacy — 3DES, weak
)
_LEGACY_MACS = (
    "hmac-sha2-512", "hmac-sha2-256",
    "hmac-sha1-96", "hmac-sha1",
    "hmac-md5",                         # legacy — MD5, weak
)


def _supported(algos: tuple, info_dict: dict) -> tuple:
    """Keep only algorithms paramiko's installed version still implements.

    Setting ``_preferred_keys`` / ``_preferred_ciphers`` / ``_preferred_macs``
    on Transport validates each entry against ``_key_info`` /
    ``_cipher_info`` / ``_mac_info``. Unknown entries raise
    ``ValueError("unknown cipher")`` — paramiko's error message is the
    same regardless of which list is being set, hence the generic text.

    This filter is forward-compatible: paramiko 5.x already dropped
    ssh-dss (DSAKey removed entirely), so passing it through would
    crash on connect. Older / future versions may drop more, so we
    filter rather than hard-code.
    """
    present = tuple(a for a in algos if a in info_dict)
    dropped = sorted(set(algos) - set(present))
    if dropped:
        log.debug("ssh legacy algs dropped (paramiko doesn't implement): %s", dropped)
    return present


def _apply_legacy_algorithms() -> None:
    """Broaden paramiko's preferred algorithm lists for embedded SSH.

    Idempotent — only mutate once per process. Re-applying is a no-op.
    """
    if getattr(paramiko.Transport, "_legacy_algs_applied", False):
        return

    paramiko.Transport._preferred_kex = _LEGACY_KEX

    # _key_info maps host-key algorithm name → key class. paramiko's
    # default deliberately omits "ssh-rsa" (SHA-1 signatures), so an
    # older server that only offers ssh-rsa fails parsing with
    # ``KeyError: 'ssh-rsa'``. Patch it back in (paramiko 5.x removed
    # it entirely).
    if "ssh-rsa" not in paramiko.Transport._key_info:
        from paramiko import RSAKey
        paramiko.Transport._key_info = dict(paramiko.Transport._key_info)
        paramiko.Transport._key_info["ssh-rsa"] = RSAKey
        paramiko.Transport._key_info["ssh-rsa-cert-v01@openssh.com"] = RSAKey

    # RSAKey.HASHES is the signature-algorithm → hash map. paramiko
    # deliberately omits "ssh-rsa" (SHA-1), so even though the key
    # parses, signature verification fails. Patch it back in.
    if "ssh-rsa" not in paramiko.RSAKey.HASHES:
        from cryptography.hazmat.primitives import hashes
        paramiko.RSAKey.HASHES = dict(paramiko.RSAKey.HASHES)
        paramiko.RSAKey.HASHES["ssh-rsa"] = hashes.SHA1
        paramiko.RSAKey.HASHES["ssh-rsa-cert-v01@openssh.com"] = hashes.SHA1

    # Filter host-key / cipher / mac lists against what this paramiko
    # version still implements. SSH-DSS, 3DES-CBC, HMAC-MD5 etc. have
    # all been removed or are removable in newer paramiko releases;
    # passing them unconditionally would raise "unknown cipher" on the
    # first connect.
    #
    # The order matters: the _key_info / RSAKey.HASHES patches above
    # must run first, otherwise the filter would drop "ssh-rsa" as
    # "paramiko doesn't implement it" and we'd end up with an empty
    # host-key list that fails kex against any ssh-rsa-only server
    # (which is most OpenWrt dropbear installs).
    keys = _supported(_LEGACY_HOST_KEYS, paramiko.Transport._key_info)
    ciphers = _supported(_LEGACY_CIPHERS, paramiko.Transport._cipher_info)
    macs = _supported(_LEGACY_MACS, paramiko.Transport._mac_info)

    # _preferred_keys drives host-key negotiation. _preferred_pubkeys
    # drives pubkey auth only. Both need ssh-rsa for legacy servers.
    paramiko.Transport._preferred_keys = keys
    paramiko.Transport._preferred_pubkeys = keys
    paramiko.Transport._preferred_ciphers = ciphers
    paramiko.Transport._preferred_macs = macs

    paramiko.Transport._legacy_algs_applied = True


_apply_legacy_algorithms()


def open_ssh(target: SshTarget, *, timeout: float = 5.0) -> paramiko.SSHClient:
    """Open a long-lived ``paramiko.SSHClient`` connection.

    With ``password`` set, falls back to password auth only. Without
    it, lets paramiko try ssh-agent / default keys under
    ``~/.ssh/id_*``; if none load cleanly, surfaces the underlying
    paramiko exception (``NoValidConnectionsError`` /
    ``AuthenticationException``) — the collector will catch and mark
    the snapshot as offline.
    """
    client = paramiko.SSHClient()
    client.load_system_host_keys()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

    pkey: Optional[paramiko.PKey] = None
    if target.password is None:
        # Best-effort: try the default RSA key. paramiko will also try
        # ssh-agent (allow_agent=True) and any other key it finds in
        # ~/.ssh. Failures here are non-fatal — connect() below will
        # raise if no auth method succeeds.
        default_key = Path.home() / ".ssh" / "id_rsa"
        if default_key.exists():
            try:
                pkey = paramiko.RSAKey.from_private_key_file(str(default_key))
            except paramiko.PasswordRequiredException:
                pkey = None
            except OSError as e:
                log.debug("could not load default RSA key %s: %s", default_key, e)
                pkey = None

    client.connect(
        hostname=target.host,
        username=target.username,
        password=target.password,
        pkey=pkey,
        timeout=timeout,
        auth_timeout=timeout,
        allow_agent=target.password is None,
        look_for_keys=target.password is None,
    )
    return client


def run_remote(
    client: paramiko.SSHClient,
    cmd: str,
    *,
    timeout: float = 2.0,
    stdin_data: Optional[str] = None,
) -> Tuple[int, str, str]:
    """Run ``cmd`` and return ``(rc, stdout, stderr)``.

    Single-shot. The caller decides how to schedule this against its
    own refresh interval — ``SSHClient.exec_command`` itself spawns a
    channel per call, but the underlying TCP/SSH transport is the
    long-lived ``client`` so there's no per-call handshake cost.

    If ``stdin_data`` is given, it's written and the channel is closed
    (signaling EOF) before reading — that's how the remote Python
    helper scripts read their config via ``sys.stdin.read()``.
    """
    stdin, stdout, stderr = client.exec_command(cmd, timeout=timeout)
    stdin_written = False
    try:
        if stdin_data is not None:
            stdin.write(stdin_data)
            stdin.flush()
            stdin.close()
            stdin_written = True
        out = stdout.read().decode("utf-8", errors="replace")
        err = stderr.read().decode("utf-8", errors="replace")
        rc = stdout.channel.recv_exit_status()
    finally:
        if not stdin_written:
            stdin.close()
        stdout.close()
        stderr.close()
    return rc, out, err