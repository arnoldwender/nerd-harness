#!/usr/bin/env sh
# The Nerd Harness — session-start hook.
#
# Opens every session with the boot line + a rotating law of the craft, and keeps
# the four disciplines present. Its stdout is meant to be injected into the agent's
# context at the start of a session (e.g. a Claude Code `SessionStart` hook), and it
# is also just readable output for any harness that can run a startup command.
set -eu
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)

# 1) The boot line + the law of the day (bin/law).
"$ROOT/bin/law"

# 2) The four disciplines — kept present in context, every session.
echo ""
cat "$ROOT/codex-block.md"
