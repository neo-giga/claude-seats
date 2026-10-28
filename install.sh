#!/usr/bin/env bash
# install.sh - put `claude-seats` on your PATH and prepare its root directory.
#
#   ./install.sh                 symlink bin/claude-seats into ~/.local/bin
#   ./install.sh --copy          copy instead of symlink
#   ./install.sh --bin-dir DIR   another bin directory
#   ./install.sh --root DIR      another root (default: $CLAUDE_SEATS_ROOT or ~/.claude-accounts)
#
# Never edits your shell rc: it prints the lines to add.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN_DIR="$HOME/.local/bin"
ROOT="${CLAUDE_SEATS_ROOT:-$HOME/.claude-accounts}"
MODE=link
while [ $# -gt 0 ]; do
  case "$1" in
    --copy) MODE=copy; shift ;;
    --bin-dir) BIN_DIR="$2"; shift 2 ;;
    --root) ROOT="$2"; shift 2 ;;
    -h|--help) sed -n '2,10p' "$0"; exit 0 ;;
    *) echo "install.sh: unknown option $1" >&2; exit 2 ;;
  esac
done
command -v claude >/dev/null || echo "install.sh: warning: 'claude' is not on PATH; install Claude Code first" >&2
command -v python3 >/dev/null || { echo "install.sh: python3 is required" >&2; exit 1; }

mkdir -p "$BIN_DIR" "$ROOT"
if [ "$MODE" = copy ]; then
  cp "$HERE/bin/claude-seats" "$BIN_DIR/claude-seats"
else
  ln -sfn "$HERE/bin/claude-seats" "$BIN_DIR/claude-seats"
fi
chmod +x "$BIN_DIR/claude-seats"
if [ ! -e "$ROOT/accounts.json" ]; then
  cp "$HERE/accounts.example.json" "$ROOT/accounts.json"
  echo "created $ROOT/accounts.json from accounts.example.json - edit it, or use: claude-seats --add <id> <dir>"
fi
echo "installed: $BIN_DIR/claude-seats ($MODE) ; root: $ROOT"
echo
echo "Add to your shell rc if needed (this script does not edit it):"
case ":$PATH:" in *":$BIN_DIR:"*) ;; *) echo "    export PATH=\"$BIN_DIR:\$PATH\"" ;; esac
if [ "$ROOT" != "$HOME/.claude-accounts" ]; then echo "    export CLAUDE_SEATS_ROOT=\"$ROOT\""; fi
echo "    alias cc=claude-seats      # optional short name (beware: cc is also the C compiler)"
echo
echo "Then: claude-seats --status"
