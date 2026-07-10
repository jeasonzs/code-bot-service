#!/usr/bin/env bash
# Install Claude Code statusLine that writes ~/.code-bot/claude-state.json.
# Backs up ~/.claude/settings.json (timestamped) before merging.
# Idempotent: re-running overwrites the statusLine block but preserves
# every other key in settings.json.
set -euo pipefail

HOOK_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HOOK_CMD="$HOOK_DIR/claude-statusline.sh"
STATE_FILE="${CODEBOT_STATE_FILE:-$HOME/.code-bot/claude-state.json}"

SETTINGS="$HOME/.claude/settings.json"
mkdir -p "$HOME/.claude/backups"

# 1) Backup existing settings
if [ -f "$SETTINGS" ]; then
  TS=$(date +%Y%m%d-%H%M%S)
  cp "$SETTINGS" "$HOME/.claude/backups/settings.json.$TS.bak"
  echo "Backed up $SETTINGS -> $HOME/.claude/backups/settings.json.$TS.bak"
fi

# 2) Merge statusLine block via Python (jq-free)
HOOK_CMD="$HOOK_CMD" STATE_FILE="$STATE_FILE" python3 - <<'PY'
import json
import os
from pathlib import Path

settings_path = Path(os.environ["HOME"]) / ".claude" / "settings.json"
hook_cmd = os.environ["HOOK_CMD"]
state_file = os.environ["STATE_FILE"]

existing = {}
if settings_path.exists():
    try:
        existing = json.loads(settings_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        existing = {}

existing["statusLine"] = {
    "type": "command",
    "command": hook_cmd,
}

settings_path.parent.mkdir(parents=True, exist_ok=True)
settings_path.write_text(
    json.dumps(existing, indent=2, ensure_ascii=False), encoding="utf-8"
)
print(f"Merged statusLine into {settings_path}")
PY

# 3) Ensure state file parent exists with safe perms
mkdir -p "$(dirname "$STATE_FILE")"
chmod 700 "$(dirname "$STATE_FILE")"

echo ""
echo "Statusline command: $HOOK_CMD"
echo "State file:         $STATE_FILE"
echo ""
echo "Done. Settings reload automatically; next Claude Code"
echo "interaction triggers the statusline and writes state."