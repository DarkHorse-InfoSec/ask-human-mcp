#!/usr/bin/env bash
# Register the ask-human MCP server with Claude Code on this machine.
#
# Usage:
#   ./register-mcp.sh                # uses default URL
#   ASK_HUMAN_URL=https://... ./register-mcp.sh

set -euo pipefail

# No remote default on purpose: guessing a URL registers a server that is not
# there. Point this at your own deployment, or leave it for a local server.
URL="${ASK_HUMAN_URL:-http://127.0.0.1:8765/mcp}"
NAME="ask-human"

if ! command -v claude >/dev/null 2>&1; then
    echo "error: 'claude' CLI not found on PATH. Install Claude Code first." >&2
    exit 1
fi

echo "Registering MCP server '${NAME}' -> ${URL}"
claude mcp add --transport http "${NAME}" "${URL}"

cat <<'EOF'

Registered. Two more steps:

1. Set MCP_TIMEOUT in your Claude Code env so long ask_human calls are not
   killed client-side. Add to your shell rc (or ~/.claude/settings.json env):

       export MCP_TIMEOUT=14400000   # 4 hours, in milliseconds

2. Add the following rule to ~/.claude/CLAUDE.md so Claude actually USES the
   tool instead of stopping and waiting silently:

   ----- snip -----
   ## ask-human (Slack human-in-the-loop)

   When you are blocked, need clarification, or would otherwise stop and wait
   for user input, call the `ask_human` tool from the `ask-human` MCP server
   instead of ending your turn. Use it for: ambiguous requirements, destructive
   actions needing confirmation, choices between approaches, or any time you'd
   normally ask a question and stop. Set urgency="high" for blocking issues,
   "low" for FYI-style confirmations.
   ----- snip -----

3. Verify with:
       claude mcp list
       curl -fsS "${ASK_HUMAN_URL%/mcp}/health"

EOF
