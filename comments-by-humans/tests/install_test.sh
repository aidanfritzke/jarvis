#!/usr/bin/env bash
# Install comments-by-humans into a clean Claude Code home and prove it gates a session.
#
#   comments-by-humans/tests/install_test.sh [marketplace]
#
# The marketplace defaults to this checkout (a directory source). Pass owner/repo#ref to test
# the published copy, for example: aidanfritzke/jarvis#jarvis-comments-by-humans
#
# The session step needs a model credential that works without your usual home directory,
# such as ANTHROPIC_API_KEY. Without one, that step is skipped and the rest still runs.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SOURCE="${1:-$(cd "$HERE/../.." && pwd)}"
WORK="$(mktemp -d)"
trap 'cd /; rm -rf "$WORK"' EXIT
export HOME="$WORK/home"
mkdir -p "$HOME" "$WORK/project"
# When this runs inside a Claude Code session, the child sessions must not attach to it.
unset CLAUDE_CODE_SESSION_ID CLAUDE_CODE_REMOTE_SESSION_ID CLAUDE_CODE_SYNC_SESSION_REFS \
      CLAUDE_CODE_TEE_SDK_STDOUT CLAUDE_CODE_MESSAGING_SOCKET CLAUDE_CODE_MESSAGING_TOKEN \
      CLAUDE_CODE_CHILD_SESSION CLAUDE_CODE_POST_FOR_SESSION_INGRESS_V2 CLAUDECODE CLAUDE_PROJECT_DIR

step() { printf '\n== %s\n' "$*"; }
fail() { printf 'FAIL: %s\n' "$*" >&2; exit 1; }

step "requirements"
command -v claude >/dev/null || fail "claude is not on PATH"
command -v python3 >/dev/null || fail "python3 is not on PATH"
command -v git >/dev/null || fail "git is not on PATH"
claude --version
python3 --version

if [ -d "$SOURCE" ]; then
  step "validate $SOURCE"
  claude plugin validate "$SOURCE"
  claude plugin validate "$SOURCE/comments-by-humans"
fi

step "add marketplace $SOURCE"
claude plugin marketplace add "$SOURCE"

step "install comments-by-humans@jarvis"
claude plugin install comments-by-humans@jarvis
claude plugin list | tee "$WORK/list.txt"
grep -q "comments-by-humans" "$WORK/list.txt" || fail "the plugin is not listed after install"

step "the installed copy runs"
GATE="$(find "$HOME/.claude" -path '*comments-by-humans*/scripts/gate.py' | head -n 1)"
[ -n "$GATE" ] || fail "gate.py is not in the installed plugin"
echo "$GATE"
python3 "$GATE" help >/dev/null

step "a lock set by hand blocks writes and commands"
cd "$WORK/project"
git init -q .
mkdir .comments-by-humans
cat > .comments-by-humans/state.json <<'JSON'
{"mode": "build", "queue": ["c01"], "chunks": {"c01": {"id": "c01", "file": "a.py", "lines": [1, 3],
 "hash": "x", "status": "pending", "attempts": 0}}}
JSON
if claude -p "Reply with the word ready" --model haiku </dev/null >/dev/null 2>&1; then
  claude -p "Create a file hello.py containing print('hi'). Then run this shell command: touch made-by-bash.txt" \
    --dangerously-skip-permissions --model haiku </dev/null >"$WORK/session.txt" 2>&1 || true
  cat "$WORK/session.txt"
  [ ! -e hello.py ] || fail "Claude wrote hello.py while the gate was locked"
  [ ! -e made-by-bash.txt ] || fail "Claude ran a shell command while the gate was locked"
  echo "locked: no file written, no command run"
else
  echo "SKIP: no model credential works in a clean home directory"
fi

step "PASS"
