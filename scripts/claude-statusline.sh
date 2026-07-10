#!/usr/bin/env bash
# Thin shell wrapper around claude-statusline.py - Claude Code's
# statusLine.command runs in a shell and reads our payload from stdin.
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec python3 "$DIR/claude-statusline.py" "$@"