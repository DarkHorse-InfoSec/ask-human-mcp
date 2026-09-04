#!/usr/bin/env python3
"""Forward a Claude Code Notification or Stop hook payload to the
ask-human-mcp /notify endpoint, augmented with the last user prompt and
the last assistant text from this turn so the operator can see in Slack what
the session is doing without going back to the terminal.

Why Python (not bash + jq): the transcript is JSONL with nested message
content blocks; bash JSON wrangling is fragile. Python is reliably
installed on the dev machines and standard-library only here.

Failure must never block the hook or the session: any unexpected error
is swallowed and the script exits 0.
"""

from __future__ import annotations

import json
import os
import ssl
import sys
import time
import urllib.request
import urllib.error


def _make_ssl_context() -> "ssl.SSLContext | None":
    """SSL context that verifies against certifi's CA bundle.

    On Windows, ssl.create_default_context() loads the system ROOT store, which
    can still hold the long-expired DST Root CA X3. OpenSSL then builds the chain
    to that expired root and fails ("certificate has expired") even for a valid
    Let's Encrypt leaf - silently breaking every server call in this hook, which
    made AFK read as OFF and stopped routing to Slack. certifi ships only current
    roots and verifies cleanly. Returns None (urllib's default) when certifi is
    absent, so machines without the problem are unaffected.
    """
    try:
        import certifi

        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return None


_HTTP_SSL_CTX = _make_ssl_context()

NOTIFY_URL = os.environ.get(
    "ASK_HUMAN_NOTIFY_URL", "http://127.0.0.1:8765/notify"
)
TIMEOUT = float(os.environ.get("ASK_HUMAN_NOTIFY_TIMEOUT", "5"))
# Auto-expire backstop: an AFK marker older than this is treated as stale and
# cleared, so a forgotten AFK-on can't keep pinging Slack indefinitely. Default
# 8h - long enough for a genuine extended absence, short enough to self-heal a
# stuck flag. Set ASK_HUMAN_AFK_MAX_AGE_SECONDS=0 to disable.
AFK_MAX_AGE_SECONDS = int(os.environ.get("ASK_HUMAN_AFK_MAX_AGE_SECONDS", str(8 * 3600)))

# AFK-mode Stop blocking. When the marker file is present, Stop events forward
# wait_for_reply=true to the server, which polls the Slack thread up to
# STOP_WAIT_SECONDS for the operator's reply, fires a reminder at
# (STOP_WAIT_SECONDS - REMINDER_LEAD_SECONDS), and returns the reply text. The
# hook then injects that reply as the next user message via Stop's documented
# {"decision":"block","reason":<reply>} contract.
AFK_MARKER_PATH = os.path.expanduser(
    os.environ.get("ASK_HUMAN_AFK_MARKER", "~/.claude/.afk")
)
STOP_WAIT_SECONDS = int(os.environ.get("ASK_HUMAN_STOP_WAIT_SECONDS", "1800"))
REMINDER_LEAD_SECONDS = int(os.environ.get("ASK_HUMAN_STOP_REMINDER_LEAD", "300"))
# HTTP timeout > server-side wait + a small buffer so the request stays open
# the entire time the server is polling Slack.
STOP_HTTP_TIMEOUT = STOP_WAIT_SECONDS + 60


def _extract_text(content) -> str:
    """Return the text payload of a message content (string or list of blocks)."""
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                t = block.get("text") or ""
                if t:
                    parts.append(t)
        return "\n".join(parts).strip()
    return ""


def _is_real_user_message(entry: dict) -> bool:
    """User entries that are actual prompts vs tool results / hook reminders.

    Tool results are wrapped as user-role messages with tool_result blocks,
    and we want the last *typed* prompt, not those.
    """
    if not isinstance(entry, dict) or entry.get("type") != "user":
        return False
    msg = entry.get("message") or {}
    content = msg.get("content")
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") in ("tool_result", "tool_use"):
                return False
    return bool(_extract_text(content))


def _read_transcript(path: str) -> list[dict]:
    entries: list[dict] = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except Exception:
                pass
    return entries


def _find_pending_tool_use(entries: list[dict]) -> dict | None:
    """Return the most recent tool_use block from the assistant.

    Notification hooks fire when a tool is awaiting permission, and the tool
    in question is the latest tool_use block in the transcript. We surface its
    `name` and `input` so the Slack message can show what's about to happen.
    """
    for entry in reversed(entries):
        if not isinstance(entry, dict) or entry.get("type") != "assistant":
            continue
        msg = entry.get("message") or {}
        content = msg.get("content") or []
        if not isinstance(content, list):
            continue
        for block in reversed(content):
            if isinstance(block, dict) and block.get("type") == "tool_use":
                return {
                    "name": block.get("name") or "",
                    "input": block.get("input") if isinstance(block.get("input"), dict) else {},
                }
    return None


def _augment_with_transcript_context(payload: dict) -> None:
    transcript_path = payload.get("transcript_path")
    if not transcript_path or not os.path.isfile(transcript_path):
        return
    try:
        entries = _read_transcript(transcript_path)
    except Exception:
        return

    event = str(payload.get("hook_event_name") or "Notification")

    if event == "Notification":
        # Notification = a tool is awaiting permission. Show what tool, what
        # input. Skip the user-prompt and the full assistant history; they're
        # already on the operator's terminal screen and just bloat the message.
        pending = _find_pending_tool_use(entries)
        if pending:
            payload["pending_tool"] = pending
        return

    # For Stop / SubagentStop: surface the trailing assistant text so the
    # server's question filter can decide whether to fire, and so a fired
    # message shows the question that ended the turn.
    last_user_idx = -1
    for i, entry in enumerate(entries):
        if _is_real_user_message(entry):
            last_user_idx = i
    asst_parts: list[str] = []
    for entry in entries[last_user_idx + 1 :]:
        if not isinstance(entry, dict) or entry.get("type") != "assistant":
            continue
        msg = entry.get("message") or {}
        text = _extract_text(msg.get("content"))
        if text:
            asst_parts.append(text)
    if asst_parts:
        payload["last_assistant_message"] = "\n\n".join(asst_parts)


def _augment_project(payload: dict) -> None:
    """Ensure the payload has a `project` field resolved from the git root,
    not the cwd basename. Server falls back to its own calc if absent."""
    if payload.get("project"):
        return
    cwd = payload.get("cwd")
    project = _project_from_cwd(str(cwd)) if cwd else None
    if project:
        payload["project"] = project


_TERMINAL_VERB = {
    "Notification": "is waiting",
    "Stop": "turn ended",
    "SubagentStop": "subagent ended",
}

_TERMINAL_ICON = {
    "Notification": "[!]",
    "Stop": "[*]",
    "SubagentStop": "[*]",
}


def _project_from_cwd(cwd: str | None) -> str | None:
    """Resolve the project label for the Slack header.

    Walks up from cwd looking for a repo marker (`.git` or `pyproject.toml`)
    so that working in a subdirectory like `DemoApp/frontend` still shows
    `[DemoApp]` in Slack instead of `[frontend]`. Falls back to cwd basename
    if no marker is found.
    """
    if not cwd:
        return None
    try:
        path = os.path.abspath(cwd)
    except Exception:
        return None
    seen: set[str] = set()
    while path and path not in seen:
        seen.add(path)
        # `.git` is usually a directory but can be a file in worktrees /
        # submodules — handle both.
        git_marker = os.path.join(path, ".git")
        if os.path.isdir(git_marker) or os.path.isfile(git_marker):
            return os.path.basename(path) or None
        parent = os.path.dirname(path)
        if parent == path:
            break
        path = parent
    trimmed = cwd.rstrip("/\\")
    for sep in ("\\", "/"):
        if sep in trimmed:
            return trimmed.rsplit(sep, 1)[-1] or None
    return trimmed or None


def _emit_terminal_summary(payload: dict) -> None:
    """Mirror the Slack message into the Claude Code terminal via stderr.

    Stderr is used (not stdout) because Stop hook stdout is fed back to the
    model as a system-reminder; we just want the human-visible status line.
    """
    event = str(payload.get("hook_event_name") or "Notification")
    cwd = payload.get("cwd")
    project = _project_from_cwd(str(cwd) if cwd else None)
    icon = _TERMINAL_ICON.get(event, "[!]")
    verb = _TERMINAL_VERB.get(event, event.lower())
    label = f"Claude Code [{project}]" if project else "Claude Code"

    lines = [f"{icon} {label} {verb} - Slack notified"]
    msg = payload.get("message")
    if msg:
        lines.append(f"  {str(msg).strip()}")
    pending = payload.get("pending_tool")
    if isinstance(pending, dict):
        name = pending.get("name") or "?"
        inp = pending.get("input") or {}
        if name == "Bash" and isinstance(inp, dict):
            cmd = str(inp.get("command") or "").strip()
            desc = str(inp.get("description") or "").strip()
            if cmd:
                cmd_short = cmd if len(cmd) <= 240 else cmd[:240].rstrip() + "..."
                lines.append(f"  $ {cmd_short}")
            if desc:
                lines.append(f"  ({desc})")
        elif isinstance(inp, dict) and inp.get("file_path"):
            lines.append(f"  {name}: {inp.get('file_path')}")
        else:
            try:
                inp_str = json.dumps(inp, ensure_ascii=False)
            except Exception:
                inp_str = str(inp)
            if len(inp_str) > 200:
                inp_str = inp_str[:200].rstrip() + "..."
            lines.append(f"  {name}: {inp_str}")
    last_asst = payload.get("last_assistant_message")
    if last_asst and event in ("Stop", "SubagentStop"):
        snippet = str(last_asst).strip()
        if len(snippet) > 300:
            snippet = "..." + snippet[-300:].lstrip()
        lines.append(f"  {snippet}")
    if cwd:
        lines.append(f"  cwd: {cwd}")

    sys.stderr.write("\n".join(lines) + "\n")
    sys.stderr.flush()


AFK_STATE_URL = os.environ.get(
    "ASK_HUMAN_AFK_URL", "http://127.0.0.1:8765/afk"
)
AFK_STATE_TIMEOUT = float(os.environ.get("ASK_HUMAN_AFK_TIMEOUT", "1.5"))
# Optional short file cache so back-to-back reads needn't GET /afk every time.
# Default 0 = always GET: a Slack/terminal toggle takes effect on the very next
# turn-end (correctness over saving a ~50ms request). Set >0 to cache for that
# many seconds if call volume matters.
AFK_CACHE_TTL = float(os.environ.get("ASK_HUMAN_AFK_CACHE_TTL", "0"))
AFK_CACHE_PATH = os.path.expanduser(
    os.environ.get("ASK_HUMAN_AFK_CACHE", "~/.claude/.afk_server_cache")
)


def _server_afk_state() -> bool | None:
    """GET /afk. Returns True/False, or None if unreachable/unparseable."""
    try:
        with urllib.request.urlopen(
            AFK_STATE_URL, timeout=AFK_STATE_TIMEOUT, context=_HTTP_SSL_CTX
        ) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        if isinstance(data, dict) and "afk" in data:
            return bool(data["afk"])
    except Exception:
        return None
    return None


def _server_afk_state_cached() -> bool | None:
    """Server AFK state with a short file cache. Returns True/False, or None
    when the server is unreachable and no fresh cache exists."""
    now = time.time()
    if AFK_CACHE_TTL > 0:
        try:
            with open(AFK_CACHE_PATH, "r", encoding="utf-8") as f:
                cached = json.load(f)
            if isinstance(cached, dict) and (now - float(cached.get("ts", 0))) < AFK_CACHE_TTL:
                return bool(cached["afk"])
        except Exception:
            pass
    val = _server_afk_state()
    if val is not None and AFK_CACHE_TTL > 0:
        try:
            with open(AFK_CACHE_PATH, "w", encoding="utf-8") as f:
                json.dump({"afk": val, "ts": now}, f)
        except Exception:
            pass
    return val


def _afk_active() -> bool:
    """AFK is a single coherent state: the SERVER is authoritative (settable
    from the Slack toggle OR a terminal cue), so a turn-end routes to Slack when
    AFK is on and stays in the terminal when off, regardless of where it was
    toggled. The local marker is only an offline fallback for when the server is
    unreachable."""
    server = _server_afk_state_cached()
    if server is not None:
        try:
            exists = os.path.exists(AFK_MARKER_PATH)
            if server and not exists:
                os.makedirs(os.path.dirname(AFK_MARKER_PATH), exist_ok=True)
                open(AFK_MARKER_PATH, "w").close()
            elif not server and exists:
                os.remove(AFK_MARKER_PATH)
        except Exception:
            pass
        return server
    # Server unreachable: fall back to the local marker (last-known), honoring
    # the auto-expire backstop so a stale marker can't pin AFK on forever.
    try:
        if not os.path.exists(AFK_MARKER_PATH):
            return False
        if AFK_MAX_AGE_SECONDS > 0:
            if time.time() - os.path.getmtime(AFK_MARKER_PATH) > AFK_MAX_AGE_SECONDS:
                os.remove(AFK_MARKER_PATH)
                return False
        return True
    except Exception:
        return False


def _emit_stop_block(reason: str) -> None:
    """Output a Stop hook decision that injects `reason` as the next user
    message and resumes the session.

    Per Claude Code's hook docs, returning `{"decision":"block","reason":"X"}`
    on stdout from a Stop hook causes Claude to continue the conversation
    with X as the user's next prompt. We use this to inject the Slack reply
    after a long wait."""
    out = {"decision": "block", "reason": reason}
    sys.stdout.write(json.dumps(out))
    sys.stdout.flush()


def _stop_wait(payload: dict) -> bool:
    """AFK-aware Stop handling. Returns True if the hook handled the event
    (reply injected or timed out), False if the caller should fall through to
    the normal fire-and-forget /notify path.

    Only top-level Stop blocks for a reply. SubagentStop fires repeatedly
    during autonomous work and would freeze the session waiting on Slack
    every time a subagent returns — that's the wrong UX. SubagentStop falls
    through to the normal fire-and-forget path.
    """
    event = str(payload.get("hook_event_name") or "")
    if event != "Stop":
        return False
    if not _afk_active():
        return False
    if bool(payload.get("stop_hook_active")):
        # Recursive Stop. Don't double-block; let the server's existing
        # stop_hook_active filter swallow it via the fire-and-forget path.
        return False

    request_payload = dict(payload)
    request_payload["wait_for_reply"] = True
    request_payload["timeout_seconds"] = STOP_WAIT_SECONDS
    request_payload["reminder_lead_seconds"] = REMINDER_LEAD_SECONDS

    sys.stderr.write(
        f"[notify-hook] AFK on — Stop blocking up to {STOP_WAIT_SECONDS}s "
        f"for Slack reply (reminder at {STOP_WAIT_SECONDS - REMINDER_LEAD_SECONDS}s).\n"
    )
    sys.stderr.flush()

    try:
        req = urllib.request.Request(
            NOTIFY_URL,
            data=json.dumps(request_payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=STOP_HTTP_TIMEOUT, context=_HTTP_SSL_CTX) as resp:
            data = resp.read().decode("utf-8")
        response = json.loads(data)
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
        sys.stderr.write(
            f"[notify-hook] /notify wait failed ({e}); falling through to "
            f"fire-and-forget.\n"
        )
        sys.stderr.flush()
        return False

    status = str(response.get("status") or "")
    if status == "answered":
        reply = str(response.get("reply") or "").strip()
        if not reply:
            return False
        sys.stderr.write(f"[notify-hook] Slack reply received — resuming session.\n")
        sys.stderr.flush()
        _emit_stop_block(reply)
        return True
    if status == "timeout":
        sys.stderr.write(
            "[notify-hook] Stop wait timed out — turn ended quietly.\n"
        )
        sys.stderr.flush()
        return True  # turn ends; no decision injected

    # error / unexpected — fall through to the bare fire-and-forget path so at
    # least the original Stop ping still tries to land in Slack.
    sys.stderr.write(
        f"[notify-hook] /notify returned status={status!r}; falling through.\n"
    )
    sys.stderr.flush()
    return False


def main() -> None:
    raw = sys.stdin.read()
    if not raw:
        sys.exit(0)
    try:
        payload = json.loads(raw)
    except Exception:
        sys.exit(0)
    if not isinstance(payload, dict):
        sys.exit(0)

    # AFK gate. Slack is the conversation surface ONLY when the operator has
    # explicitly flipped AFK on via "brb"/"afk" cues. At the keyboard the
    # terminal is the only surface — no Notification heads-ups, no Stop
    # fire-and-forget pings, nothing.
    if not _afk_active():
        sys.exit(0)

    try:
        _augment_with_transcript_context(payload)
    except Exception:
        # Augmentation is best-effort. Always still send the bare payload.
        pass

    try:
        _augment_project(payload)
    except Exception:
        pass

    # AFK-mode Stop: server polls Slack for a reply and we inject it as the
    # next user message. If this path handles the event (reply received or
    # explicit timeout), we exit before the fire-and-forget path runs.
    try:
        if _stop_wait(payload):
            sys.exit(0)
    except Exception as e:
        sys.stderr.write(f"[notify-hook] _stop_wait error: {e}; falling through.\n")
        sys.stderr.flush()

    posted = False
    try:
        req = urllib.request.Request(
            NOTIFY_URL,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=TIMEOUT, context=_HTTP_SSL_CTX) as resp:
            resp.read()
            posted = True
    except (urllib.error.URLError, TimeoutError, OSError):
        pass

    if posted:
        try:
            _emit_terminal_summary(payload)
        except Exception:
            pass

    sys.exit(0)


if __name__ == "__main__":
    main()
