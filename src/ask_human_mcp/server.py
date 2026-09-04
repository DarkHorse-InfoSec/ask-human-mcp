"""FastMCP server exposing the `/notify` HTTP endpoint over streamable HTTP.

One-way push from Claude Code's Notification and Stop hooks to Slack so
the operator gets a deterministic ping when a session pauses for input or ends a
turn. AFK-aware Stop blocking: when the local notify-hook detects
~/.claude/.afk, it forwards `wait_for_reply=true` and the server polls the
Slack thread for a reply that the hook then injects back into the session
via Stop's `decision: block` contract.

Run with: `ask-human-mcp` (entry point) or `python -m ask_human_mcp.server`.

History note (2026-05-05): the `ask_human` MCP tool, `register_for_approve`
MCP tool, and `/approve` HTTP route were removed. The Slack/terminal race
they relied on was unreliable in practice (free-text Slack answers didn't
propagate; terminal answers were sometimes ignored). For interactive
questions, ask in chat. The terminal is the conversation surface; Slack is
for AFK pings.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import time
from typing import Any
from urllib.parse import parse_qs

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.requests import Request
from starlette.responses import JSONResponse

from .config import Settings, load_settings
from .logging import configure_logging, get_logger
from .slack import SlackBridge


# Module-level state. Populated by `build_app()` so tests can inject a fake bridge.
_settings: Settings | None = None
_bridge: SlackBridge | None = None
# Per-session timestamp of the last successful notify post. Used to throttle
# repeated pings from the same paused session (Slack flood mitigation).
_notify_last_ts: dict[str, float] = {}
# Approval decisions delivered via Slack interactive buttons, keyed by the
# posted message ts. The /slack/interactions route writes here when the operator
# taps Approve / Approve always / Deny; the in-flight _wait_for_approval_reply
# poller pops the entry and returns it. Decoupling the button callback (a
# separate inbound HTTP request from Slack) from the blocked /notify request
# this way lets a single tap resolve the approval on any device, with no
# thread-vs-channel reply placement to get wrong.
_button_decisions: dict[str, dict[str, Any]] = {}
# Valid button verdicts. Mirrors the `value` set on each approval button.
_BUTTON_DECISIONS = {"allow", "allow_always", "deny"}

# AFK state, server-authoritative and in-memory: the systemd unit denies all
# disk writes, so this resets to False (= terminal mode, the safe default) on a
# service restart. Toggled by signed Slack buttons (handle_interaction) and by
# the local afk-trigger hook via `set_afk` in its /notify call. The reader hooks
# (Stop, PreToolUse) GET /afk and treat it as authoritative, but only consult it
# when their local marker is already on — so there's zero added latency at the
# keyboard.
_afk_state: bool = False
# ts of the single reusable pinned "AFK control" message so each toggle updates
# it in place instead of posting a new one. Lost on restart; the next toggle
# re-finds it by sentinel text (see _AFK_CONTROL_SENTINEL) before reposting.
_afk_control_ts: str | None = None
# Embedded in the control message's fallback text so a post-restart toggle can
# find and reuse the existing control message instead of duplicating it.
_AFK_CONTROL_SENTINEL = "ask-human-afk-control"

log = get_logger(__name__)

NOTIFY_ICON = {
    "Notification": ":bell:",
    "Stop": ":checkered_flag:",
    "SubagentStop": ":checkered_flag:",
    "PreToolUse": ":lock:",
}

NOTIFY_VERB = {
    "Notification": "is waiting",
    "Stop": "ended turn",
    "SubagentStop": "subagent ended",
    "PreToolUse": "needs approval",
}


# Reply tokens that map to permission decisions for the AFK PreToolUse path.
# Kept generous so phone-typed replies parse: case-insensitive, trailing
# punctuation stripped, single-token or two-word "yes please" / "no thanks"
# accepted. Anything else falls through to ambiguous and the poller keeps
# waiting for a clearer reply.
_APPROVE_TOKENS = {
    "y", "yes", "yep", "yeah", "ya", "yup", "yas",
    "ok", "okay", "k", "go", "run", "allow",
    "approve", "approved", "sure",
}
_ALWAYS_TOKENS = {
    "always", "forever", "persist",
}
_DENY_TOKENS = {
    "n", "no", "nope", "nah",
    "stop", "cancel", "deny", "denied",
    "abort", "halt", "kill",
}


def _parse_approval_reply(text: str) -> str | None:
    """Map a Slack reply to 'allow' / 'allow_always' / 'deny'; None if ambiguous.

    'always' (and synonyms) returns 'allow_always' so the hook can persist a
    matching pattern into permissions.allow. Plain 'yes'/'y'/'ok' returns
    'allow' (one-shot). Two-word "yes always" / "always allow" parses as
    allow_always because the always-intent dominates.

    Accepts case-insensitive replies with trailing punctuation stripped.
    Multi-word replies (up to 3 tokens) parse on the constituent words.
    Anything longer or with no recognized token returns None so the poller
    keeps waiting for a clearer reply.
    """
    if not text:
        return None
    norm = text.strip().lower().rstrip("!.?,;:")
    if not norm:
        return None
    if norm in _ALWAYS_TOKENS:
        return "allow_always"
    if norm in _APPROVE_TOKENS:
        return "allow"
    if norm in _DENY_TOKENS:
        return "deny"
    words = norm.split()
    if 1 <= len(words) <= 3:
        # allow_always wins over plain allow when both intents are present
        # ("yes always", "always allow", "approve forever").
        if any(w in _ALWAYS_TOKENS for w in words):
            if any(w in _APPROVE_TOKENS or w in _ALWAYS_TOKENS for w in words):
                if not any(w in _DENY_TOKENS for w in words):
                    return "allow_always"
        first = words[0]
        if first in _APPROVE_TOKENS:
            return "allow"
        if first in _DENY_TOKENS:
            return "deny"
    return None


def _get_bridge() -> SlackBridge:
    if _bridge is None:
        raise RuntimeError("ask-human MCP not initialized: call build_app() first")
    return _bridge


def _get_settings() -> Settings:
    if _settings is None:
        raise RuntimeError("ask-human MCP not initialized: call build_app() first")
    return _settings


def _project_from_cwd(cwd: str | None) -> str | None:
    """Return the basename of cwd, handling both POSIX and Windows separators."""
    if not cwd:
        return None
    trimmed = cwd.rstrip("/\\")
    if not trimmed:
        return None
    for sep in ("\\", "/"):
        if sep in trimmed:
            return trimmed.rsplit(sep, 1)[-1] or None
    return trimmed


# Slack mrkdwn blocks cap at 3000 chars. We chunk slightly under that so the
# `>` quote prefix per line doesn't overflow the limit on dense text.
SLACK_BLOCK_CHAR_LIMIT = 2800


def _split_for_slack(text: str, max_chars: int = SLACK_BLOCK_CHAR_LIMIT) -> list[str]:
    """Split text into chunks that fit a single Slack mrkdwn block.

    Used to render the full assistant turn in Stop notifications without
    aggressive truncation: the operator reads these messages to compose his Slack
    reply when AFK, so seeing the entire output matters more than message
    brevity. Splits prefer line-break boundaries; lines longer than max_chars
    are char-split as a fallback.
    """
    text = text.strip()
    if not text:
        return []
    if len(text) <= max_chars:
        return [text]

    chunks: list[str] = []
    current = ""
    for line in text.splitlines(keepends=True):
        if len(line) > max_chars:
            # Single line longer than limit: flush, then char-split the line.
            if current:
                chunks.append(current.rstrip())
                current = ""
            for i in range(0, len(line), max_chars):
                piece = line[i : i + max_chars]
                if i + max_chars < len(line):
                    chunks.append(piece)
                else:
                    current = piece  # tail of the long line - try to merge with what follows
            continue
        if len(current) + len(line) > max_chars:
            chunks.append(current.rstrip())
            current = line
        else:
            current += line
    if current.strip():
        chunks.append(current.rstrip())
    return chunks


def _format_pending_tool(pending: Any) -> str:
    """Render a pending tool_use block as the focused 'about to happen' line.

    Mirrors what Claude Code shows in the terminal permission prompt: command
    + description for Bash, file path for Read/Edit/Write, pattern for
    Glob/Grep, and a JSON dump of the input for everything else (custom MCP
    tools, etc.).
    """
    if not isinstance(pending, dict):
        return ""
    name = str(pending.get("name") or "")
    inp = pending.get("input") if isinstance(pending.get("input"), dict) else {}

    if name == "Bash":
        cmd = str(inp.get("command") or "").strip()
        desc = str(inp.get("description") or "").strip()
        if len(cmd) > 1500:
            cmd = cmd[:1500].rstrip() + "..."
        parts: list[str] = []
        if cmd:
            parts.append(f"```\n{cmd}\n```")
        if desc:
            parts.append(f"_{desc}_")
        return "\n".join(parts) or f"`{name}`"

    if name in {"Read", "Edit", "Write", "NotebookEdit"}:
        path = inp.get("file_path") or inp.get("notebook_path") or ""
        # Fenced code blocks render Windows backslashes literally; inline
        # backticks let Slack mangle `\b`, `\a`, etc. as escape sequences.
        return f"*{name}*\n```\n{path}\n```" if path else f"*{name}*"

    if name in {"Glob", "Grep"}:
        pattern = inp.get("pattern") or ""
        path = inp.get("path") or ""
        suffix = f"\nin\n```\n{path}\n```" if path else ""
        return f"*{name}*\n```\n{pattern}\n```{suffix}"

    # Generic fallback: pretty-print the input dict so MCP tools and anything
    # we don't special-case still surface their arguments.
    try:
        as_json = json.dumps(inp, indent=2, ensure_ascii=False)
    except Exception:
        as_json = str(inp)
    if len(as_json) > 1200:
        as_json = as_json[:1200].rstrip() + "..."
    if as_json.strip() in {"{}", ""}:
        return f"*{name}*"
    return f"*{name}*\n```\n{as_json}\n```"


def _mentions(user_ids: str | list[str]) -> str:
    """Render a space-joined `<@id>` mention string from one or more user IDs.

    Accepts a single ID (legacy callers) or a list. Mentioning every authorized
    approver — not just the primary — is what makes Slack raise a push
    notification on each of the user's accounts, so the ping reaches whichever
    account they currently have open. An empty/blank list yields an empty
    string (no mention) rather than a stray `<@>`.
    """
    ids = [user_ids] if isinstance(user_ids, str) else list(user_ids)
    return " ".join(f"<@{uid}>" for uid in ids if uid and uid.strip())


def _build_notify_blocks(
    event: str,
    cwd: str | None,
    project: str | None,
    message: str | None,
    user_ids: str | list[str],
    pending_tool: Any = None,
    last_assistant_message: str | None = None,
    afk_back_button: bool = False,
) -> list[dict[str, Any]]:
    """Format Slack blocks for a hook-driven notification (one-way, no reply).

    Notification events render the pending tool_use (the action awaiting
    permission). Stop events render the trailing assistant text (the question
    that ended the turn). Neither echoes the user's own prompt back: it's on
    the operator's screen already and bloats the Slack message.
    """
    icon = NOTIFY_ICON.get(event, ":bell:")
    verb = NOTIFY_VERB.get(event, "needs attention")
    label = f"Claude Code [{project}]" if project else "Claude Code"
    header = f"{icon} *{label} {verb}*  {_mentions(user_ids)}"

    blocks: list[dict[str, Any]] = [
        {"type": "section", "text": {"type": "mrkdwn", "text": header}},
    ]
    if message:
        snippet = message if len(message) <= 2800 else message[:2800] + "..."
        blocks.append(
            {"type": "section", "text": {"type": "mrkdwn", "text": snippet}}
        )
    if pending_tool:
        formatted = _format_pending_tool(pending_tool)
        if formatted:
            blocks.append(
                {"type": "section", "text": {"type": "mrkdwn", "text": formatted}}
            )
    if event == "PreToolUse":
        # Interactive buttons: one tap, identical on mobile and desktop, no
        # thread-vs-channel reply placement to get wrong. block_id is fixed so
        # the interactions handler can strip this row once a choice is made.
        blocks.append(
            {
                "type": "actions",
                "block_id": "approval_actions",
                "elements": [
                    {
                        "type": "button",
                        "action_id": "approve",
                        "text": {"type": "plain_text", "text": "Approve"},
                        "style": "primary",
                        "value": "allow",
                    },
                    {
                        "type": "button",
                        "action_id": "approve_always",
                        "text": {"type": "plain_text", "text": "Approve always"},
                        "value": "allow_always",
                    },
                    {
                        "type": "button",
                        "action_id": "deny",
                        "text": {"type": "plain_text", "text": "Deny"},
                        "style": "danger",
                        "value": "deny",
                    },
                ],
            }
        )
        # Text-reply hint stays as a fallback for clients where buttons don't
        # render, and so a typed reply still parses: y/yes (once), always
        # (persist), n/no (deny).
        blocks.append(
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": "_Tap a button, or reply *y* / *yes* (once), *always* (persist), *n* / *no* to deny._",
                },
            }
        )
    if afk_back_button:
        # "I'm back" affordance on the AFK-on announcement so a single tap turns
        # AFK off without hunting for the pinned control message. Same block_id
        # as the control message; the interactions handler strips it after a tap.
        blocks.append(
            {
                "type": "actions",
                "block_id": "afk_controls",
                "elements": [
                    {
                        "type": "button",
                        "action_id": "afk_off",
                        "text": {"type": "plain_text", "text": "I'm back (AFK off)"},
                        "style": "primary",
                        "value": "off",
                    }
                ],
            }
        )
    if last_assistant_message and event in {"Stop", "SubagentStop"}:
        # Render the FULL assistant turn - Slack is the conversation surface
        # when AFK. Split into multiple section blocks if it exceeds Slack's
        # ~3000-char-per-block limit. Quote with `>` for visual distinction
        # from header/metadata blocks.
        for chunk in _split_for_slack(last_assistant_message):
            quoted = ">" + chunk.replace(chr(10), chr(10) + ">")
            blocks.append(
                {"type": "section", "text": {"type": "mrkdwn", "text": quoted}}
            )
    if cwd:
        blocks.append(
            {"type": "context", "elements": [{"type": "mrkdwn", "text": f"`cwd: {cwd}`"}]}
        )
    return blocks


def _fallback_notify_text(
    event: str, project: str | None, user_ids: str | list[str]
) -> str:
    """Notification-bar text for the hook-driven message."""
    icon = NOTIFY_ICON.get(event, ":bell:")
    verb = NOTIFY_VERB.get(event, "needs attention")
    label = f"Claude Code [{project}]" if project else "Claude Code"
    return f"{_mentions(user_ids)} {icon} {label} {verb}"


async def run_notify(
    *,
    bridge: SlackBridge,
    settings: Settings,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """Core implementation of the /notify endpoint.

    Posts a one-way "Claude Code is waiting / turn ended" message to Slack so
    the operator gets a deterministic push regardless of whether Claude itself
    decided to call any tool. Driven by Claude Code's Notification and Stop
    hooks; the payload schema mirrors what Claude Code sends to those hooks.

    AFK-mode Stop wait: when the local hook sets `wait_for_reply=true`, this
    function blocks waiting for a Slack reply (`_wait_for_stop_reply`) so the
    hook can inject the reply back into the calling session via decision:block.
    """
    event = str(payload.get("hook_event_name") or "Notification")
    raw_cwd = payload.get("cwd")
    cwd = str(raw_cwd) if raw_cwd is not None else None
    raw_message = payload.get("message")
    message = str(raw_message) if raw_message is not None else None
    raw_asst = payload.get("last_assistant_message")
    last_assistant_message = str(raw_asst).strip() if raw_asst else None
    pending_tool = payload.get("pending_tool")
    if not isinstance(pending_tool, dict):
        pending_tool = None
    # PreToolUse hooks pass tool_name + tool_input directly (Claude Code's
    # native payload shape). Promote them into the pending_tool dict so the
    # block builder treats them the same as the transcript-extracted form.
    if pending_tool is None and payload.get("tool_name"):
        tool_input = payload.get("tool_input")
        pending_tool = {
            "name": str(payload.get("tool_name") or ""),
            "input": tool_input if isinstance(tool_input, dict) else {},
        }

    # Server-authoritative AFK state. The local afk-trigger hook sends `set_afk`
    # to keep the server in sync with terminal "brb" / "I'm back" cues, so a
    # Slack toggle and a typed cue agree. `afk_state_only` means "just update the
    # state + control message, don't post a ping" (used for OFF transitions,
    # which shouldn't light up Slack).
    set_afk = payload.get("set_afk")
    if set_afk is not None:
        await _set_afk(bridge, settings, bool(set_afk))
    if payload.get("afk_state_only"):
        return {"status": "ok", "afk": _afk_state}

    # Stop hooks fire recursively when a Stop hook itself triggers another Stop;
    # Claude Code sets stop_hook_active=true on the recursive call so we can
    # bail out and avoid a notification storm.
    if event in {"Stop", "SubagentStop"} and bool(payload.get("stop_hook_active")):
        log.info("notify.skipped", hook_event=event, reason="stop_hook_active")
        return {"status": "skipped", "reason": "stop_hook_active"}

    # Claude Code fires Notification with message "Claude is waiting for your
    # input" whenever the orchestrator is idle - including when it's just
    # waiting on its own subagents to finish. Those pings have no actionable
    # content for the operator and should be dropped. Only keep Notifications that
    # are about a tool needing permission (message contains "permission").
    if event == "Notification" and message:
        msg_lower = message.lower()
        if "waiting for your input" in msg_lower and "permission" not in msg_lower:
            log.info("notify.skipped", hook_event=event, reason="idle_waiting")
            return {"status": "skipped", "reason": "idle_waiting"}

    # Per-session cooldown: collapse a flood of pings from the same paused
    # session into a single ping per N seconds. the operator doesn't need to be
    # re-pinged while he hasn't yet responded to the previous one. Permission
    # prompts (Notification events) are exempt - they are always actionable
    # and must reach the operator even if a Stop just fired moments ago.
    cooldown = max(0, int(settings.notify_cooldown_seconds))
    cooldown_key = (
        str(payload.get("session_id"))
        if payload.get("session_id")
        else (cwd or "default")
    )
    # Notification and PreToolUse are always actionable (user must approve or
    # deny a specific tool) - exempt them from the per-session cooldown.
    if cooldown > 0 and event not in {"Notification", "PreToolUse"}:
        now_mono = time.monotonic()
        last_ts = _notify_last_ts.get(cooldown_key)
        if last_ts is not None and (now_mono - last_ts) < cooldown:
            log.info(
                "notify.skipped",
                hook_event=event,
                reason="cooldown",
                key=cooldown_key,
                seconds_since_last=int(now_mono - last_ts),
                cooldown=cooldown,
            )
            return {
                "status": "skipped",
                "reason": "cooldown",
                "seconds_since_last": int(now_mono - last_ts),
            }

    # Prefer the hook-supplied project name (resolved from the git root) over
    # cwd-basename. Working in a subdirectory like DemoApp/frontend should
    # show "[DemoApp]" not "[frontend]" - but only the local hook has
    # filesystem access to walk up to the git root.
    project = str(payload.get("project") or "").strip() or _project_from_cwd(cwd)
    # Mention every authorized approver, not just the primary, so the push
    # notification lands on whichever of the user's Slack accounts is currently
    # open. Both accounts can already answer (approver_id_set); this makes them
    # both get notified too.
    mention_ids = settings.approver_ids_ordered
    blocks = _build_notify_blocks(
        event,
        cwd,
        project,
        message,
        mention_ids,
        pending_tool=pending_tool,
        last_assistant_message=last_assistant_message or None,
        afk_back_button=bool(payload.get("afk_announcement")),
    )
    text = _fallback_notify_text(event, project, mention_ids)

    log.info(
        "notify.start",
        hook_event=event,
        project=project,
        cwd=cwd,
        has_message=bool(message),
    )

    try:
        posted = await bridge.post_question(
            settings.slack_channel_id, text=text, blocks=blocks
        )
    except Exception as e:
        log.error("notify.post_failed", error=str(e), hook_event=event)
        return {"status": "error", "detail": f"Slack post failed: {e}"}

    if cooldown > 0:
        _notify_last_ts[cooldown_key] = time.monotonic()
    log.info("notify.posted", hook_event=event, ts=posted.ts, cooldown_key=cooldown_key)

    # AFK-mode wait path: when the local hook detects AFK is on, it sets
    # wait_for_reply=true so this server-side call blocks waiting for the operator's
    # Slack reply. Stop / SubagentStop replies are injected as the next user
    # message; PreToolUse replies are parsed into an allow/deny permission
    # decision returned to the hook.
    if bool(payload.get("wait_for_reply")):
        if event in {"Stop", "SubagentStop"}:
            return await _wait_for_stop_reply(
                bridge=bridge,
                posted=posted,
                event=event,
                timeout_seconds=int(payload.get("timeout_seconds") or 1800),
                max_timeout=settings.max_timeout_seconds,
                reminder_lead_seconds=int(
                    payload.get("reminder_lead_seconds") or 300
                ),
            )
        if event == "PreToolUse":
            return await _wait_for_approval_reply(
                bridge=bridge,
                posted=posted,
                timeout_seconds=int(payload.get("timeout_seconds") or 1800),
                max_timeout=settings.max_timeout_seconds,
                reminder_lead_seconds=int(
                    payload.get("reminder_lead_seconds") or 300
                ),
            )

    return {
        "status": "ok",
        "event": event,
        "ts": posted.ts,
        "permalink": posted.permalink,
    }


async def _wait_for_stop_reply(
    *,
    bridge: SlackBridge,
    posted: Any,
    event: str,
    timeout_seconds: int,
    max_timeout: int,
    reminder_lead_seconds: int,
) -> dict[str, Any]:
    """Poll the Slack thread for the operator's reply with a configurable timeout
    and a reminder ping that fires `reminder_lead_seconds` before the deadline.

    the operator's spec for the AFK Stop flow: total wait 30 min, reminder at 25 min.
    Implementation generalizes to other (timeout, lead) pairs so tests can use
    short values.
    """
    capped_timeout = max(1, min(timeout_seconds, max_timeout))
    # Reminder fires `lead` seconds before the deadline. Floor at 1s so very
    # short test timeouts still get a reminder, ceiling at the deadline minus
    # 1s so we never schedule a reminder at-or-after timeout.
    reminder_at = max(1, capped_timeout - max(1, reminder_lead_seconds))
    reminder_at = min(reminder_at, max(1, capped_timeout - 1))

    started = time.monotonic()

    async def _send_reminder() -> None:
        try:
            await asyncio.sleep(reminder_at)
        except asyncio.CancelledError:
            raise
        try:
            remaining_minutes = max(1, (capped_timeout - reminder_at) // 60)
            await bridge.append_to_message(
                posted.channel,
                posted.ts,
                f":alarm_clock: _(still waiting - {remaining_minutes}m left until timeout)_",
            )
            log.info(
                "notify.reminder_sent",
                ts=posted.ts,
                hook_event=event,
                remaining_seconds=capped_timeout - reminder_at,
            )
        except Exception as e:  # defensive: reminder must not break the wait
            log.warning("notify.reminder_failed", error=str(e), ts=posted.ts)

    reminder_task = asyncio.create_task(_send_reminder(), name="notify.reminder")

    log.info(
        "notify.wait_started",
        hook_event=event,
        ts=posted.ts,
        timeout_seconds=capped_timeout,
        reminder_at=reminder_at,
    )
    try:
        try:
            reply = await bridge.wait_for_reply(
                channel=posted.channel,
                thread_ts=posted.ts,
                timeout_seconds=capped_timeout,
            )
        except asyncio.CancelledError:
            await bridge.append_to_message(
                channel=posted.channel,
                ts=posted.ts,
                append_text=":warning: Stop-wait cancelled by client.",
            )
            raise
    finally:
        reminder_task.cancel()
        try:
            await reminder_task
        except (asyncio.CancelledError, Exception):
            pass

    elapsed = int(time.monotonic() - started)

    if reply is None:
        await bridge.react(posted.channel, posted.ts, "clock1")
        await bridge.append_to_message(
            posted.channel,
            posted.ts,
            f"_(timed out after {max(1, elapsed // 60)}m - turn ended)_",
        )
        log.info(
            "notify.wait_timeout",
            hook_event=event,
            ts=posted.ts,
            elapsed_seconds=elapsed,
        )
        return {
            "status": "timeout",
            "event": event,
            "ts": posted.ts,
            "permalink": posted.permalink,
            "elapsed_seconds": elapsed,
        }

    text = (reply.get("text") or "").strip()
    await bridge.react(posted.channel, posted.ts, "white_check_mark")
    log.info(
        "notify.wait_answered",
        hook_event=event,
        ts=posted.ts,
        elapsed_seconds=elapsed,
        reply_chars=len(text),
    )
    return {
        "status": "answered",
        "event": event,
        "ts": posted.ts,
        "permalink": posted.permalink,
        "elapsed_seconds": elapsed,
        "reply": text,
    }


async def _wait_for_approval_reply(
    *,
    bridge: SlackBridge,
    posted: Any,
    timeout_seconds: int,
    max_timeout: int,
    reminder_lead_seconds: int,
) -> dict[str, Any]:
    """Wait for an approval decision on a PreToolUse prompt, via either path.

    Two resolution channels race, first-to-resolve wins:
      - a Slack interactive-button tap recorded by /slack/interactions
        (authoritative; its value is already allow/allow_always/deny), or
      - a parseable y/n text reply in the thread (predicate skips ambiguous
        chatter like "hmm let me think" so the user can clarify).
    Buttons make a single tap work identically on mobile and desktop with no
    thread-vs-channel placement to get wrong. Returns a `decision` of
    allow/allow_always/deny, or `timeout` on deadline.
    """
    capped_timeout = max(1, min(timeout_seconds, max_timeout))
    reminder_at = max(1, capped_timeout - max(1, reminder_lead_seconds))
    reminder_at = min(reminder_at, max(1, capped_timeout - 1))

    started = time.monotonic()

    async def _send_reminder() -> None:
        try:
            await asyncio.sleep(reminder_at)
        except asyncio.CancelledError:
            raise
        try:
            remaining_minutes = max(1, (capped_timeout - reminder_at) // 60)
            await bridge.append_to_message(
                posted.channel,
                posted.ts,
                f":alarm_clock: _(still waiting on approval - {remaining_minutes}m left)_",
            )
            log.info(
                "notify.approval_reminder_sent",
                ts=posted.ts,
                remaining_seconds=capped_timeout - reminder_at,
            )
        except Exception as e:
            log.warning("notify.approval_reminder_failed", error=str(e), ts=posted.ts)

    reminder_task = asyncio.create_task(_send_reminder(), name="notify.approval_reminder")

    def _is_parseable(msg: dict[str, Any]) -> bool:
        return _parse_approval_reply(msg.get("text") or "") is not None

    async def _await_text() -> dict[str, Any] | None:
        """Resolve when the user posts a parseable y/n text reply (or timeout)."""
        return await bridge.wait_for_reply(
            channel=posted.channel,
            thread_ts=posted.ts,
            timeout_seconds=capped_timeout,
            predicate=_is_parseable,
        )

    async def _await_button() -> dict[str, Any] | None:
        """Resolve when /slack/interactions records a button tap for this ts."""
        poll = max(0.05, float(getattr(bridge, "poll_interval", 1.0) or 1.0))
        deadline = time.monotonic() + capped_timeout
        while time.monotonic() < deadline:
            entry = _button_decisions.pop(posted.ts, None)
            if entry and entry.get("decision") in _BUTTON_DECISIONS:
                return entry
            await asyncio.sleep(poll)
        return None

    log.info(
        "notify.approval_wait_started",
        ts=posted.ts,
        timeout_seconds=capped_timeout,
        reminder_at=reminder_at,
    )

    text_task = asyncio.create_task(_await_text(), name="notify.approval_text")
    button_task = asyncio.create_task(_await_button(), name="notify.approval_button")

    async def _cancel(*tasks: asyncio.Task) -> None:
        for t in tasks:
            t.cancel()
        for t in tasks:
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass

    decision = "ask"
    reply_text = ""
    via = "text"
    timed_out = False
    try:
        try:
            done, _pending = await asyncio.wait(
                {text_task, button_task}, return_when=asyncio.FIRST_COMPLETED
            )
        except asyncio.CancelledError:
            await _cancel(text_task, button_task)
            await bridge.append_to_message(
                channel=posted.channel,
                ts=posted.ts,
                append_text=":warning: Approval-wait cancelled by client.",
            )
            raise

        button_result = (
            button_task.result()
            if button_task in done and not button_task.cancelled()
            else None
        )
        text_result = (
            text_task.result()
            if text_task in done and not text_task.cancelled()
            else None
        )

        # A button tap is authoritative and bypasses text parsing (its value is
        # already one of allow/allow_always/deny). A parseable text reply is the
        # fallback. Either task finishing with None means that channel saw
        # nothing within the deadline -> timeout.
        if button_result and button_result.get("decision") in _BUTTON_DECISIONS:
            decision = str(button_result["decision"])
            via = "button"
            reply_text = f"Slack button: {decision}"
        elif text_result is not None:
            reply_text = (text_result.get("text") or "").strip()
            decision = _parse_approval_reply(reply_text) or "ask"
            via = "text"
        else:
            timed_out = True
    finally:
        await _cancel(text_task, button_task)
        reminder_task.cancel()
        try:
            await reminder_task
        except (asyncio.CancelledError, Exception):
            pass
        # Drop any stale button decision for this prompt so a late tap can't
        # leak into a subsequent approval that happens to reuse the ts.
        _button_decisions.pop(posted.ts, None)

    elapsed = int(time.monotonic() - started)

    if timed_out:
        await bridge.react(posted.channel, posted.ts, "clock1")
        await bridge.append_to_message(
            posted.channel,
            posted.ts,
            f"_(timed out after {max(1, elapsed // 60)}m - falling through to terminal prompt)_",
        )
        log.info(
            "notify.approval_wait_timeout",
            ts=posted.ts,
            elapsed_seconds=elapsed,
        )
        return {
            "status": "timeout",
            "event": "PreToolUse",
            "ts": posted.ts,
            "permalink": posted.permalink,
            "elapsed_seconds": elapsed,
        }

    if decision == "allow_always":
        # Lock emoji communicates "persisted" at-a-glance in Slack history; the
        # check-mark stays so the visual still reads as "approved" alongside
        # one-shot allows.
        await bridge.react(posted.channel, posted.ts, "white_check_mark")
        await bridge.react(posted.channel, posted.ts, "lock")
    elif decision == "allow":
        await bridge.react(posted.channel, posted.ts, "white_check_mark")
    else:
        await bridge.react(posted.channel, posted.ts, "no_entry_sign")
    log.info(
        "notify.approval_answered",
        ts=posted.ts,
        elapsed_seconds=elapsed,
        decision=decision,
        via=via,
        reply_chars=len(reply_text),
    )
    return {
        "status": "answered",
        "event": "PreToolUse",
        "ts": posted.ts,
        "permalink": posted.permalink,
        "elapsed_seconds": elapsed,
        "decision": decision,
        "reply": reply_text,
    }


# Reject interaction callbacks whose timestamp is more than this many seconds
# from now (Slack's recommended replay-attack window).
_SIGNATURE_MAX_SKEW_SECONDS = 300

_BUTTON_RESULT_TEXT = {
    "allow": ":white_check_mark: *Approved* (this once) by <@{user}>",
    "allow_always": ":lock: *Approved — always* by <@{user}>",
    "deny": ":no_entry_sign: *Denied* by <@{user}>",
}


def _verify_slack_signature(
    signing_secret: str,
    timestamp: str,
    body: bytes,
    signature: str,
) -> bool:
    """Verify a Slack request signature (v0 scheme).

    Slack signs each request with `v0=HMAC_SHA256(signing_secret, "v0:<ts>:<body>")`.
    We recompute and constant-time compare, and reject stale timestamps to
    blunt replay. Returns False on any missing/malformed input rather than
    raising — the caller treats False as "reject".
    """
    if not signing_secret or not timestamp or not signature:
        return False
    try:
        skew = abs(time.time() - int(timestamp))
    except (TypeError, ValueError):
        return False
    if skew > _SIGNATURE_MAX_SKEW_SECONDS:
        return False
    basestring = b"v0:" + timestamp.encode("utf-8") + b":" + body
    digest = hmac.new(
        signing_secret.encode("utf-8"), basestring, hashlib.sha256
    ).hexdigest()
    expected = "v0=" + digest
    return hmac.compare_digest(expected, signature)


_BEARER_PREFIX = "bearer "


def _bearer_token(auth_header: str) -> str:
    """Pull the token out of an `Authorization: Bearer <token>` header.

    Returns "" for a missing, malformed, or non-Bearer header rather than
    raising; the caller treats "" as reject. The scheme match is
    case-insensitive per RFC 7235.
    """
    if not auth_header or not auth_header.lower().startswith(_BEARER_PREFIX):
        return ""
    return auth_header[len(_BEARER_PREFIX) :].strip()


def _auth_error(
    request: Request, settings: Settings, route: str
) -> JSONResponse | None:
    """Gate a mutating HTTP route on the shared secret.

    Returns None when the caller is authorized, or the JSONResponse to send
    when it is not. Fail-closed on purpose: with no secret configured we cannot
    authenticate anyone, so we refuse (503) rather than serve the request. That
    mirrors /slack/interactions with no signing secret, and it means a
    misconfigured deploy is loud (every ping 503s, logged as
    `auth.not_configured`) instead of quietly reachable by anyone who guesses
    the hostname.

    Read-only routes (/health, GET /afk) deliberately stay open.
    """
    secret = settings.ask_human_shared_secret
    if not secret:
        log.warning(
            "auth.not_configured",
            route=route,
            hint=(
                "ASK_HUMAN_SHARED_SECRET is unset; refusing mutating requests. "
                "Generate one with `openssl rand -hex 32`, set it in the "
                "service env file, and give the hooks the same value."
            ),
        )
        return JSONResponse(
            {"status": "error", "detail": "shared secret not configured"},
            status_code=503,
        )
    presented = _bearer_token(request.headers.get("Authorization", ""))
    # Compare as bytes: hmac.compare_digest raises TypeError on non-ASCII str.
    if not presented or not hmac.compare_digest(
        presented.encode("utf-8"), secret.encode("utf-8")
    ):
        log.warning("auth.rejected", route=route, presented_token=bool(presented))
        return JSONResponse(
            {"status": "error", "detail": "unauthorized"},
            status_code=401,
            headers={"WWW-Authenticate": "Bearer"},
        )
    return None


def _afk_control_blocks(afk_on: bool) -> list[dict[str, Any]]:
    """Blocks for the reusable AFK control message: a status line plus a single
    toggle button whose label/value reflects the action (turn it off when on,
    on when off)."""
    if afk_on:
        status = (
            ":large_green_circle: *AFK is ON* — Slack is the active channel. "
            "Stop and tool-approval prompts route here."
        )
        button = {
            "type": "button",
            "action_id": "afk_off",
            "text": {"type": "plain_text", "text": "I'm back (AFK off)"},
            "style": "primary",
            "value": "off",
        }
    else:
        status = ":white_circle: *AFK is OFF* — terminal-only. No Slack pings."
        button = {
            "type": "button",
            "action_id": "afk_on",
            "text": {"type": "plain_text", "text": "Set AFK on"},
            "value": "on",
        }
    return [
        {"type": "section", "text": {"type": "mrkdwn", "text": status}},
        {"type": "actions", "block_id": "afk_controls", "elements": [button]},
    ]


async def _refresh_afk_control(bridge: SlackBridge, settings: Settings) -> None:
    """Post or update the single pinned AFK control message to reflect
    `_afk_state`. Updates in place when the ts is known; otherwise re-finds the
    message by sentinel (surviving a restart that cleared the in-memory ts)
    before posting a fresh one. All Slack failures are swallowed — the toggle
    already took effect in `_afk_state`; a stale control message is cosmetic."""
    global _afk_control_ts
    blocks = _afk_control_blocks(_afk_state)
    text = f"{_AFK_CONTROL_SENTINEL} | AFK is {'ON' if _afk_state else 'OFF'}"

    if _afk_control_ts:
        try:
            await bridge.update_message(
                settings.slack_channel_id, _afk_control_ts, text, blocks
            )
            return
        except Exception as e:
            log.warning("afk.control_update_failed", error=str(e), ts=_afk_control_ts)
            _afk_control_ts = None

    try:
        found = await bridge.find_message_by_text(
            settings.slack_channel_id, _AFK_CONTROL_SENTINEL
        )
    except Exception:
        found = None
    if found:
        _afk_control_ts = found
        try:
            await bridge.update_message(
                settings.slack_channel_id, found, text, blocks
            )
            return
        except Exception as e:
            log.warning("afk.control_update_failed", error=str(e), ts=found)
            _afk_control_ts = None

    try:
        posted = await bridge.post_question(
            settings.slack_channel_id, text=text, blocks=blocks
        )
        _afk_control_ts = posted.ts
        await bridge.pin_message(settings.slack_channel_id, posted.ts)
    except Exception as e:
        log.warning("afk.control_post_failed", error=str(e))


async def _set_afk(bridge: SlackBridge, settings: Settings, new_state: bool) -> bool:
    """Set the server AFK state and refresh the control message. Returns whether
    the state actually changed."""
    global _afk_state
    changed = new_state != _afk_state
    _afk_state = new_state
    try:
        await _refresh_afk_control(bridge, settings)
    except Exception as e:  # control message is cosmetic; never fail the toggle
        log.warning("afk.refresh_failed", error=str(e))
    log.info("afk.set", afk=new_state, changed=changed)
    return changed


async def handle_interaction(
    *,
    bridge: SlackBridge,
    settings: Settings,
    data: dict[str, Any],
) -> dict[str, Any]:
    """Process a verified Slack block_actions payload from an approval button.

    Records the chosen verdict in `_button_decisions` keyed by the message ts
    (which the in-flight approval poller is watching), then strips the buttons
    off the message and stamps the result. Only the configured owner's taps are
    honored — a button press from any other workspace member is ignored, same
    as the text-reply path filters by user id.
    """
    if data.get("type") != "block_actions":
        return {"ok": True, "ignored": "not block_actions"}

    user_id = str((data.get("user") or {}).get("id") or "")
    if user_id not in settings.approver_id_set:
        log.warning(
            "interactions.wrong_user",
            user_id=user_id,
            authorized=sorted(settings.approver_id_set),
        )
        return {"ok": True, "ignored": "wrong_user"}

    actions = data.get("actions") or []
    if not actions or not isinstance(actions, list):
        return {"ok": True, "ignored": "no_actions"}
    action_id = str(actions[0].get("action_id") or "")
    value = str(actions[0].get("value") or "")

    container = data.get("container") or {}
    message = data.get("message") or {}
    message_ts = str(container.get("message_ts") or message.get("ts") or "")
    channel_id = str(
        (data.get("channel") or {}).get("id")
        or container.get("channel_id")
        or settings.slack_channel_id
    )

    # AFK toggle buttons (control message or an "I'm back" button on an
    # announcement). These flip the server-authoritative AFK state.
    if action_id in {"afk_on", "afk_off"}:
        new_state = action_id == "afk_on"
        await _set_afk(bridge, settings, new_state)
        # If the tap came from a transient announcement (not the persistent
        # control message, which _set_afk just refreshed in place), strip its
        # button so it can't be re-tapped.
        if message_ts and message_ts != _afk_control_ts:
            stamp = (
                ":large_green_circle: *AFK on* by <@{u}>"
                if new_state
                else ":white_circle: *Back — AFK off* by <@{u}>"
            ).format(u=user_id)
            try:
                await bridge.resolve_approval_buttons(channel_id, message_ts, stamp)
            except Exception as e:
                log.warning("interactions.afk_stamp_failed", error=str(e), ts=message_ts)
        log.info("interactions.afk_toggle", afk=new_state, user_id=user_id)
        return {"ok": True, "afk": new_state}

    # Approval buttons: value is the verdict.
    if value not in _BUTTON_DECISIONS:
        return {"ok": True, "ignored": "unknown_value"}
    if not message_ts:
        return {"ok": True, "ignored": "no_message_ts"}

    # Record the decision first so the poller resolves even if the cosmetic
    # button-strip below fails.
    _button_decisions[message_ts] = {"decision": value, "user": user_id}
    log.info("interactions.recorded", ts=message_ts, decision=value, user_id=user_id)

    result_text = _BUTTON_RESULT_TEXT.get(value, "*Recorded* by <@{user}>").format(
        user=user_id
    )
    try:
        await bridge.resolve_approval_buttons(channel_id, message_ts, result_text)
    except Exception as e:  # cosmetic only; decision already recorded
        log.warning("interactions.resolve_failed", error=str(e), ts=message_ts)

    return {"ok": True, "decision": value, "ts": message_ts}


def build_app(
    settings: Settings | None = None,
    bridge: SlackBridge | None = None,
) -> FastMCP:
    """Build and return the FastMCP app. Idempotent within a process."""
    global _settings, _bridge

    _settings = settings or load_settings()
    configure_logging(_settings.log_level)

    _bridge = bridge or SlackBridge(
        token=_settings.slack_bot_token,
        target_user_id=_settings.slack_user_id,
        poll_interval=_settings.poll_interval_seconds,
        extra_user_ids=_settings.approver_id_set,
    )

    transport_security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=_settings.allowed_hosts_list,
    )

    mcp = FastMCP(
        "ask-human",
        host=_settings.host,
        port=_settings.port,
        transport_security=transport_security,
    )

    @mcp.custom_route("/health", methods=["GET"])
    async def health(_request: Request) -> JSONResponse:
        connected = await _get_bridge().auth_test()
        return JSONResponse(
            {
                "status": "ok",
                "slack_connected": connected,
            }
        )

    @mcp.custom_route("/afk", methods=["GET"])
    async def afk(_request: Request) -> JSONResponse:
        """Current server-authoritative AFK state. The Stop / PreToolUse reader
        hooks poll this (only while their local marker is on) to pick up a
        remote 'I'm back' toggle. Read-only and unauthenticated — it leaks only
        a boolean, the same trust level as /health."""
        return JSONResponse({"afk": _afk_state})

    @mcp.custom_route("/afk", methods=["POST"])
    async def afk_set(request: Request) -> JSONResponse:
        """Set AFK state without posting a notification. Used by the local
        afk-trigger hook / afk.sh to sync the server when a terminal cue flips
        AFK (especially OFF, which must not ping Slack). Distinct from /notify
        on purpose: a server predating this route 404s here instead of posting a
        spurious message, so the hooks are safe to ship before the server is
        deployed. Refreshes the pinned control message to reflect the new state.

        Authenticated: flipping AFK changes how every session behaves and
        reposts the pinned Slack control message."""
        denied = _auth_error(request, _get_settings(), "/afk")
        if denied is not None:
            return denied
        try:
            payload = await request.json()
        except Exception as e:
            return JSONResponse(
                {"status": "error", "detail": f"invalid JSON: {e}"}, status_code=400
            )
        if not isinstance(payload, dict) or "afk" not in payload:
            return JSONResponse(
                {"status": "error", "detail": "expected {\"afk\": bool}"},
                status_code=400,
            )
        await _set_afk(_get_bridge(), _get_settings(), bool(payload["afk"]))
        return JSONResponse({"status": "ok", "afk": _afk_state})

    @mcp.custom_route("/notify", methods=["POST"])
    async def notify(request: Request) -> JSONResponse:
        """One-way push from Claude Code's Notification / Stop hooks to Slack.

        Posts a "Claude Code is waiting" / "turn ended" message that
        @-mentions the user so a push notification fires deterministically.
        The hook script forwards Claude Code's raw hook payload here; we
        parse hook_event_name, cwd, message, and (for AFK Stop) wait_for_reply.

        Authenticated: this route posts attacker-controlled text into the Slack
        workspace and can hold a request open for the full wait window.
        """
        denied = _auth_error(request, _get_settings(), "/notify")
        if denied is not None:
            return denied
        try:
            payload = await request.json()
        except Exception as e:
            return JSONResponse(
                {"status": "error", "detail": f"invalid JSON: {e}"},
                status_code=400,
            )
        if not isinstance(payload, dict):
            return JSONResponse(
                {"status": "error", "detail": "payload must be a JSON object"},
                status_code=400,
            )
        result = await run_notify(
            bridge=_get_bridge(),
            settings=_get_settings(),
            payload=payload,
        )
        # All non-error statuses get 200. The original code returned 502 for
        # "answered" and "timeout", which broke AFK Stop reply injection
        # because urlopen raises HTTPError on 5xx and the hook caught it as
        # a network failure. Accept any status that isn't a hard error.
        status_code = 200 if result.get("status") in {
            "ok", "skipped", "answered", "timeout"
        } else 502
        return JSONResponse(result, status_code=status_code)

    @mcp.custom_route("/slack/interactions", methods=["POST"])
    async def slack_interactions(request: Request) -> JSONResponse:
        """Receive Slack interactive-button callbacks for AFK approvals.

        Slack POSTs an `application/x-www-form-urlencoded` body with a single
        `payload` field (URL-encoded JSON). We verify the request signature
        against SLACK_SIGNING_SECRET before trusting anything, then record the
        button verdict for the waiting /notify request. Configure the app's
        Interactivity Request URL to `<base>/slack/interactions`.
        """
        settings = _get_settings()
        raw = await request.body()

        if not settings.slack_signing_secret:
            # No secret configured -> we can't authenticate the caller. Refuse
            # rather than act on an unsigned request. Approvals fall back to
            # threaded text replies until the secret is set.
            log.warning("interactions.no_signing_secret")
            return JSONResponse(
                {"ok": False, "detail": "interactions not configured"},
                status_code=503,
            )

        timestamp = request.headers.get("X-Slack-Request-Timestamp", "")
        signature = request.headers.get("X-Slack-Signature", "")
        if not _verify_slack_signature(
            settings.slack_signing_secret, timestamp, raw, signature
        ):
            log.warning("interactions.bad_signature")
            return JSONResponse({"ok": False, "detail": "bad signature"}, status_code=401)

        try:
            payload_field = parse_qs(raw.decode("utf-8")).get("payload", [None])[0]
            if not payload_field:
                raise ValueError("missing payload field")
            data = json.loads(payload_field)
        except (ValueError, json.JSONDecodeError) as e:
            return JSONResponse(
                {"ok": False, "detail": f"invalid payload: {e}"}, status_code=400
            )

        # Slack URL-verification handshake (sent once when you save the URL).
        if isinstance(data, dict) and data.get("type") == "url_verification":
            return JSONResponse({"challenge": data.get("challenge", "")})

        result = await handle_interaction(
            bridge=_get_bridge(), settings=settings, data=data
        )
        return JSONResponse(result, status_code=200)

    return mcp


async def _startup_identity_check() -> None:
    """Resolve SLACK_USER_ID once at process start so misconfigs surface in
    journalctl (e.g. wrong account, missing users:read scope) before any
    /notify call posts to a channel where the user filter rejects everyone.
    """
    bridge = _get_bridge()
    settings = _get_settings()
    identity = await bridge.resolve_user_identity(settings.slack_user_id)
    if identity is None:
        log.warning(
            "startup.identity_unresolved",
            slack_user_id=settings.slack_user_id,
            hint=(
                "users.info failed for SLACK_USER_ID; replies from this user "
                "will not be matched. Verify the ID belongs to the workspace "
                "and the bot has the users:read scope."
            ),
        )
    else:
        log.info(
            "startup.identity_resolved",
            slack_user_id=settings.slack_user_id,
            name=identity.get("name"),
            real_name=identity.get("real_name"),
            email=identity.get("email"),
        )


def main() -> None:
    """Entry point for the `ask-human-mcp` console script."""
    app = build_app()
    s = _get_settings()
    log.info(
        "ask_human.starting",
        host=s.host,
        port=s.port,
        auth_configured=bool(s.ask_human_shared_secret),
    )
    if not s.ask_human_shared_secret:
        # Loud at boot, not only on the first refused request: without this the
        # symptom is "notifications stopped" on some other machine, hours later.
        log.warning(
            "startup.auth_not_configured",
            hint=(
                "ASK_HUMAN_SHARED_SECRET is unset: POST /notify and POST /afk "
                "will refuse every request with 503."
            ),
        )
    asyncio.run(_startup_identity_check())
    app.run(transport="streamable-http")


if __name__ == "__main__":
    main()
