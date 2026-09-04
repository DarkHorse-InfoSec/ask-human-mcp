# ask-human-mcp

A FastMCP server that exposes a one-way `/notify` HTTP endpoint, driven by
Claude Code's Notification and Stop hooks, so Slack pings fire deterministically
when a session pauses for input or ends a turn. Includes an AFK-mode flow:
when `~/.claude/.afk` is present, the Stop hook blocks waiting for a Slack
reply that gets injected back into the session via `decision: block`.

Lives at `https://askhuman.example.com` once deployed.

> **Removed 2026-05-05:** the `ask_human` MCP tool, `register_for_approve` MCP tool,
> and `/approve` HTTP route. The bidirectional Slack/terminal answer race was
> unreliable in practice (free-text Slack answers didn't propagate; terminal
> answers got swallowed). The terminal is the conversation surface; Slack is for
> AFK pings only.

---

## Endpoints

### `GET /health`

```json
{"status": "ok", "slack_connected": true}
```

### `POST /notify`

Accepts a Claude Code hook payload. Posts a one-way Slack message that
@-mentions the configured user. Driven by `notify-hook.py` wired into the
Notification + Stop hooks (see `hooks/notify-hook.py`).

Notable payload fields:

| Field                    | Notes |
|--------------------------|-------|
| `hook_event_name`        | `Notification`, `Stop`, or `SubagentStop`. |
| `cwd`                    | Working directory; used as fallback for the project label. |
| `project`                | Resolved repo name from the local hook (preferred over cwd basename). |
| `message`                | Notification reason (e.g., "Claude needs permission to use Bash"). |
| `pending_tool`           | `{name, input}` for Notification permission prompts; rendered focused. |
| `last_assistant_message` | Trailing assistant text for Stop pings; rendered in full (multi-block). |
| `wait_for_reply`         | When `true` on a Stop event, blocks polling Slack for a reply. AFK mode. |
| `timeout_seconds`        | Wait timeout for AFK mode (capped by server `MAX_TIMEOUT_SECONDS`). |
| `reminder_lead_seconds`  | Reminder ping fires this many seconds before timeout. |

Response:

```json
{"status": "ok" | "skipped" | "answered" | "timeout" | "error", ...}
```

Filters:
- `stop_hook_active=true` recursive Stop is silently dropped (storm prevention).
- Notification with message `"Claude is waiting for your input"` (and not a
  permission prompt) is dropped — fires while the orchestrator waits on its
  own subagents.
- Per-session cooldown (default 300s) collapses Stop floods from the same
  session. Notifications are exempt.

---

## Local setup

Requires Python 3.11+ and `uv` (or pip).

```bash
git clone <repo> ask-human-mcp
cd ask-human-mcp

uv venv
uv pip install -e ".[dev]"

cp .env.example .env
# Edit .env with SLACK_BOT_TOKEN, SLACK_CHANNEL_ID, SLACK_USER_ID

ask-human-mcp
# Listens on 127.0.0.1:8765 by default.
```

Health check:

```bash
curl http://127.0.0.1:8765/health
# {"status": "ok", "slack_connected": true}
```

Run tests:

```bash
pytest -v
```

---

## Slack app setup

Create a Slack app from the manifest below at <https://api.slack.com/apps?new_app=1>
(choose "From a manifest"). It declares the exact scopes the bot needs, no more.

```json
{
    "display_information": {
        "name": "ask-human bridge",
        "description": "Bridges Claude Code to Slack for human-in-the-loop questions.",
        "background_color": "#1a1a1a"
    },
    "features": {
        "bot_user": {
            "display_name": "ask-human",
            "always_online": true
        }
    },
    "oauth_config": {
        "scopes": {
            "bot": [
                "chat:write",
                "chat:write.customize",
                "channels:history",
                "groups:history",
                "im:history",
                "im:write",
                "reactions:write",
                "pins:write",
                "users:read"
            ]
        }
    },
    "settings": {
        "interactivity": {
            "is_enabled": true,
            "request_url": "https://askhuman.example.com/slack/interactions"
        },
        "org_deploy_enabled": false,
        "socket_mode_enabled": false,
        "token_rotation_enabled": false
    }
}
```

After install:

1. Copy the **Bot User OAuth Token** (`xoxb-...`) from "OAuth & Permissions"
   into `SLACK_BOT_TOKEN`.
2. Open a DM with the bot (or invite it to the channel you want questions in).
   Get the channel ID from the channel details popup -> "Copy channel ID" and
   put it in `SLACK_CHANNEL_ID`.
3. Get your own user ID (your Slack profile -> "Copy member ID") and put it in
   `SLACK_USER_ID`. **This must be the account you actually reply with in
   Slack** — replies from any other user ID are silently filtered out by the
   poller (slack.py:148). If you have multiple accounts in the same workspace
   (e.g. a personal one and a work one), pick the one whose name shows up next
   to your replies in the thread, not whichever account a tool happened to
   return. To verify, the server logs `startup.identity_resolved` at boot with
   the resolved name + email — `journalctl -u ask-human-mcp -n 20 -o cat | grep
   identity_` confirms you set the right one.
4. **Enable interactive approval buttons (AFK).** When AFK is on and Claude
   needs tool permission, the Slack message carries `Approve` / `Approve
   always` / `Deny` buttons. A single tap resolves the approval identically on
   mobile and desktop — no thread-vs-channel reply placement to get wrong
   (replying in the channel instead of the thread on mobile was why mobile
   approvals silently failed). To enable:
   - In the Slack app config, go to **Interactivity & Shortcuts**, toggle it
     on, and set the **Request URL** to
     `https://askhuman.example.com/slack/interactions` (already set if
     you installed from the manifest above).
   - Copy the **Signing Secret** from **Basic Information -> App Credentials**
     into `SLACK_SIGNING_SECRET` in `/etc/ask-human-mcp/env`, then restart the
     service. The `/slack/interactions` endpoint **refuses every request**
     (HTTP 503) until this is set — it cannot verify the caller's signature, and
     never acts on an unsigned request. Approvals fall back to threaded text
     replies (`y` / `yes` / `always` / `n` / `no`) until the secret is present.
   - Only taps from an authorized account are honored; a button press from any
     other workspace member is ignored as `wrong_user`, same trust boundary as
     the text-reply filter.
   - **Multiple Slack accounts?** If you tap from more than one account in the
     same workspace (e.g. a desktop account and a separate account on your
     phone), add the others to `SLACK_APPROVER_IDS` (comma-separated) in the
     env, alongside the primary `SLACK_USER_ID`. Otherwise a tap from the
     unconfigured account is rejected as `wrong_user` — which is exactly why
     "desktop works, mobile doesn't": the mobile app was a different account.
     Find the rejected ID in the logs: `journalctl -u ask-human-mcp | grep
     wrong_user`. Each ID you add is a security-equivalent approver, so only add
     accounts you control.

### Toggle AFK from Slack

With Interactivity enabled (above), AFK mode can be flipped from Slack, not just
by typing "brb" / "I'm back" in the terminal:

- The **AFK-on announcement** carries an **I'm back (AFK off)** button — one tap
  turns AFK off.
- A single reusable **pinned "AFK control" message** shows the current state
  with a toggle button you can tap anytime. (Pinning needs the `pins:write`
  scope; without it the message still works, just unpinned.)

AFK state is **server-authoritative and in-memory** — one coherent state. The
Stop and PreToolUse reader hooks `GET /afk` and honor it directly: AFK on ->
output routes to Slack, AFK off -> terminal, no matter where it was toggled
(Slack button or a terminal "brb"/"I'm back"). The local marker is only an
offline fallback used when the server is unreachable. A typed cue keeps the
server in sync (ON via `set_afk` on `/notify`; OFF via `POST /afk`, which never
posts a message). Cost of coherence: a small `GET /afk` per turn-end and per
tool call (~50ms when healthy; falls back to the marker if the server is slow or
down). Set `ASK_HUMAN_AFK_CACHE_TTL` > 0 to cache that result for N seconds if
call volume matters — the trade-off is a toggle taking up to N seconds to apply.

**Auto AFK-on is precise, not greedy** (afk-trigger-hook, revised 2026-05-28).
It fires only for short, cue-dominant messages ("afk", "afk lunch", "heading out
10 min") or a message that opens with a punctuation-bounded shortcode ("afk,
…"). A trigger word buried in a longer sentence ("enable a **AFK** toggle", "fix
the afk hook", "afk branch needs work") does **not** trigger — a false ON would
ping Slack while you're at the desk, which is the whole thing we're avoiding.
AFK-**off** detection stays generous (turning off by mistake is the safe
direction). To go AFK deliberately, send a short cue, run `afk.sh on`, or tap the
Slack toggle. Tunables: `ASK_HUMAN_AFK_MAX_WORDS` (default 8),
`ASK_HUMAN_AFK_MAX_AGE_SECONDS` (default 8h auto-expire; 0 disables).

Caveats:
- A toggle (either direction, from Slack or terminal) takes effect on the **next
  turn-end or tool call**, since that's when a reader next checks `/afk`.
- A **service restart resets AFK to off** (in-memory state; the systemd unit
  denies disk writes). Re-toggle if needed.
- The 8h marker auto-expire (`ASK_HUMAN_AFK_MAX_AGE_SECONDS`) now only applies to
  the **offline-fallback** path (server unreachable). A forgotten AFK-on while
  the server is reachable persists until you toggle it off or the service
  restarts — the pinned control message shows the current state so it's visible.

---

## Deploying to your-server (203.0.113.10)

Run as a non-root user `askhuman` under systemd, fronted by Caddy with
auto-issued TLS for
`askhuman.example.com`.

### 1. DNS

Create an A record:

```
askhuman.example.com    A    203.0.113.10
```

### 2. System user + directories

```bash
ssh your-server

sudo useradd --system --home /opt/ask-human-mcp --shell /usr/sbin/nologin askhuman
sudo install -d -o askhuman -g askhuman -m 0755 /opt/ask-human-mcp
sudo install -d -o root -g askhuman -m 0750 /etc/ask-human-mcp
```

### 3. Code + venv

```bash
sudo -u askhuman git clone <repo-url> /opt/ask-human-mcp
cd /opt/ask-human-mcp

# Use uv (preferred) or python -m venv.
sudo -u askhuman bash -c '
    cd /opt/ask-human-mcp
    python3.11 -m venv .venv
    .venv/bin/pip install --upgrade pip
    .venv/bin/pip install -e .
'
```

### 4. Environment file

```bash
sudo install -o root -g askhuman -m 0640 /dev/null /etc/ask-human-mcp/env
sudoedit /etc/ask-human-mcp/env
# paste contents of .env.example, fill in real values
```

### 5. systemd unit

```bash
sudo cp deploy/ask-human-mcp.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now ask-human-mcp
sudo systemctl status ask-human-mcp
journalctl -u ask-human-mcp -f
```

You should see a line like:

```json
{"event": "ask_human.starting", "host": "127.0.0.1", "port": 8765, ...}
```

### 6. Caddy

If the box already runs Caddy for other sites, back up the existing
Caddyfile, append the contents of `deploy/Caddyfile.snippet`, validate, then
reload:

```bash
sudo cp /etc/caddy/Caddyfile /etc/caddy/Caddyfile.bak-$(date +%Y%m%d-%H%M%S)
sudo tee -a /etc/caddy/Caddyfile < deploy/Caddyfile.snippet
sudo caddy validate --config /etc/caddy/Caddyfile
sudo systemctl reload caddy
```

Verify:

```bash
curl -fsS https://askhuman.example.com/health
# {"status":"ok","slack_connected":true}
```

---

## Registering the MCP on a dev machine

On any machine where you run Claude Code:

```bash
./scripts/register-mcp.sh
# or, manually:
claude mcp add --transport http ask-human https://askhuman.example.com/mcp
```

### MCP_TIMEOUT (important)

`ask_human` calls can run for hours. Set the Claude Code client timeout to
4 hours (in milliseconds) so it does not kill long-running calls:

```bash
export MCP_TIMEOUT=14400000
```

Add it to your shell rc, or to the `env` block in `~/.claude/settings.json`.

### Hook wiring

The client side ships in `hooks/`. Copy it somewhere stable, point it at your
server, and wire it into Claude Code:

```bash
cp -r hooks ~/.claude/ask-human-hooks
export ASK_HUMAN_NOTIFY_URL=https://your-domain/notify   # default: localhost
export ASK_HUMAN_AFK_URL=https://your-domain/afk
```

| Hook file | Wire to | Does |
|---|---|---|
| `notify-hook.py` | `Notification`, `Stop`, `SubagentStop` | posts the ping; in AFK mode blocks on a Slack reply and returns `decision: block` |
| `afk-trigger-hook.py` | `UserPromptSubmit` | flips AFK from natural language ("brb", "i'm back") |
| `afk.sh` | manual | `bash hooks/afk.sh on\|off\|toggle\|status` |

In `~/.claude/settings.json`:

```json
{
  "hooks": {
    "Notification": [{"hooks": [{"type": "command",
      "command": "python ~/.claude/ask-human-hooks/notify-hook.py"}]}],
    "Stop": [{"hooks": [{"type": "command",
      "command": "python ~/.claude/ask-human-hooks/notify-hook.py"}]}],
    "UserPromptSubmit": [{"hooks": [{"type": "command",
      "command": "python ~/.claude/ask-human-hooks/afk-trigger-hook.py"}]}]
  }
}
```

Every URL and timeout is an `ASK_HUMAN_*` environment variable, and the defaults
point at `http://127.0.0.1:8765`, so a local server needs no configuration.

---

## Operations

### Logs

Structured JSON, one object per line, to journald:

```bash
journalctl -u ask-human-mcp -o cat | jq .
```

Each `/notify` call logs `notify.start`, then one of `notify.posted`,
`notify.skipped`, `notify.post_failed`. AFK Stop wait additionally logs
`notify.wait_started`, then `notify.wait_answered` / `notify.wait_timeout` /
`notify.reminder_sent`.

### Restart

```bash
sudo systemctl restart ask-human-mcp
```

### Health monitoring

Point your uptime checker at:

```
https://askhuman.example.com/health
```

A `200 OK` with `slack_connected: true` is healthy. A `slack_connected: false`
means the server is up but the bot token is invalid or revoked.

---

## Project layout

```
ask-human-mcp/
  pyproject.toml
  README.md
  .env.example
  src/ask_human_mcp/
    __init__.py
    server.py        # FastMCP app + /notify HTTP route + AFK Stop blocking
    slack.py         # Async Slack client wrapper (post, poll, react, edit)
    config.py        # pydantic-settings env loader
    logging.py       # structlog JSON setup
  tests/
    conftest.py
    test_slack.py    # SlackBridge against a fake AsyncWebClient
    test_tool.py     # run_notify against a fake bridge
  deploy/
    ask-human-mcp.service
    Caddyfile.snippet
  scripts/
    register-mcp.sh
  hooks/                 # client side, wired into Claude Code
    notify-hook.py       # Notification/Stop/SubagentStop -> POST /notify
    afk-trigger-hook.py  # UserPromptSubmit -> AFK on/off from natural language
    afk.sh               # manual AFK toggle
```

---

## License

MIT. See [LICENSE](LICENSE).

Copyright (c) 2026 DarkHorse Information Security LLC.
