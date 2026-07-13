#!/usr/bin/env bash
# Thin shell wrapper around claude-status-hook.py - Claude Code invokes
# hook commands via shell and pipes the hook payload to stdin.
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec python3 "$DIR/claude-status-hook.py" "$@"