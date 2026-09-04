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
# POST /afk requires the shared secret as a bearer token. Env first, then the
# secret file (one line, chmod 600).
SECRET_FILE="${ASK_HUMAN_SECRET_FILE:-$HOME/.claude/.ask-human-secret}"

mkdir -p "$(dirname "$AFK_FILE")" 2>/dev/null

_shared_secret() {
    if [ -n "${ASK_HUMAN_SHARED_SECRET:-}" ]; then
        printf '%s' "$ASK_HUMAN_SHARED_SECRET"
    elif [ -r "$SECRET_FILE" ]; then
        tr -d '\r\n' < "$SECRET_FILE"
    fi
}

# Sync the server-authoritative AFK state so the Slack control message + any
# remote toggle agree with this local marker. Best-effort: never block or fail
# the toggle if the server is unreachable. An auth refusal is called out
# though, because a silently unsynced toggle is how "Slack still thinks I'm
# AFK" happens.
_sync_server() {
    local state="$1" body secret code
    local auth=()
    if [ "$state" = "on" ]; then
        body='{"afk":true}'
    else
        body='{"afk":false}'
    fi
    secret="$(_shared_secret)"
    [ -n "$secret" ] && auth=(-H "Authorization: Bearer $secret")
    code="$(curl -sS -o /dev/null -w '%{http_code}' --max-time 4 -X POST \
        -H 'Content-Type: application/json' ${auth[@]+"${auth[@]}"} \
        -d "$body" "$AFK_STATE_URL" 2>/dev/null || true)"
    case "$code" in
        401) echo "warning: server rejected the shared secret (401); AFK state not synced." >&2 ;;
        503) echo "warning: server has no shared secret configured (503); AFK state not synced." >&2 ;;
    esac
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
