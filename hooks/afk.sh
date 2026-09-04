#!/usr/bin/env bash
# Toggle AFK state for ask-human-mcp's PreToolUse approve-hook.
#
#   afk.sh on      → AFK on. Destructive tool calls post to Slack with two-way
#                    yes/no/always reply. No terminal prompt. Use when away
#                    from the keyboard.
#   afk.sh off     → AFK off (default). Destructive calls show the standard
#                    Claude Code terminal prompt (1/2/3); Slack still gets a
#                    heads-up via the Notification hook.
#   afk.sh         → toggle
#   afk.sh status  → print current state
#
# Implementation: presence/absence of the marker file at ~/.claude/.afk is the
# single source of truth, queried by approve-hook.py on every PreToolUse event.
# Override via ASK_HUMAN_AFK_MARKER env var if you want a different path.

set -u

AFK_FILE="${ASK_HUMAN_AFK_MARKER:-$HOME/.claude/.afk}"
# Dedicated state endpoint (NOT /notify): setting state here never posts a Slack
# message, and a server that predates this route 404s silently instead of
# pinging. Keeps a manual toggle from spamming Slack.
AFK_STATE_URL="${ASK_HUMAN_AFK_URL:-http://127.0.0.1:8765/afk}"

mkdir -p "$(dirname "$AFK_FILE")" 2>/dev/null

# Sync the server-authoritative AFK state so the Slack control message + any
# remote toggle agree with this local marker. Best-effort: never block or fail
# the toggle if the server is unreachable.
_sync_server() {
    local state="$1" body
    if [ "$state" = "on" ]; then
        body='{"afk":true}'
    else
        body='{"afk":false}'
    fi
    curl -fsS --max-time 4 -X POST -H 'Content-Type: application/json' \
        -d "$body" "$AFK_STATE_URL" >/dev/null 2>&1 || true
}

case "${1:-toggle}" in
    on)
        : > "$AFK_FILE"
        _sync_server on
        echo "AFK ON  → destructive prompts route to Slack only."
        ;;
    off)
        rm -f "$AFK_FILE"
        _sync_server off
        echo "AFK OFF → terminal prompts for destructive calls (Slack gets heads-up only)."
        ;;
    status)
        if [ -f "$AFK_FILE" ]; then
            echo "AFK ON (marker: $AFK_FILE)"
        else
            echo "AFK OFF"
        fi
        ;;
    toggle|"")
        if [ -f "$AFK_FILE" ]; then
            rm -f "$AFK_FILE"
            _sync_server off
            echo "AFK OFF (was ON) → terminal prompts."
        else
            : > "$AFK_FILE"
            _sync_server on
            echo "AFK ON  (was OFF) → Slack only."
        fi
        ;;
    *)
        echo "Usage: $0 [on|off|toggle|status]" >&2
        exit 2
        ;;
esac
