"""End-to-end-ish tests for run_notify using a fake SlackBridge.

Coverage:
- /notify Slack post formatting (event types, project resolution, pending tool
  rendering, full assistant message rendering for AFK Stop pings).
- Cooldown + idle-waiting filters.
- AFK-mode Stop blocking via wait_for_reply (`_wait_for_stop_reply`).
- `_split_for_slack` chunking primitive.

The `ask_human` MCP tool, `register_for_approve` MCP tool, and `/approve`
HTTP route were removed 2026-05-05; their tests were dropped with them.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

import hashlib
import hmac

import ask_human_mcp.server as server_mod
from ask_human_mcp.config import Settings
from ask_human_mcp.server import (
    _button_decisions,
    _notify_last_ts,
    _parse_approval_reply,
    _set_afk,
    _split_for_slack,
    _verify_slack_signature,
    handle_interaction,
    run_notify,
)
from ask_human_mcp.slack import PostedMessage


@pytest.fixture(autouse=True)
def _reset_notify_state():
    """Clear module-level state between tests so they don't pollute each other:
    the per-session cooldown table, the Slack-button decision map, and the
    server-authoritative AFK state."""
    _notify_last_ts.clear()
    _button_decisions.clear()
    server_mod._afk_state = False
    server_mod._afk_control_ts = None
    yield
    _notify_last_ts.clear()
    _button_decisions.clear()
    server_mod._afk_state = False
    server_mod._afk_control_ts = None


class FakeBridge:
    """Stand-in for SlackBridge that records calls and returns scripted answers."""

    def __init__(
        self,
        *,
        reply: dict[str, Any] | None = None,
        post_raises: Exception | None = None,
        wait_raises: Exception | None = None,
    ) -> None:
        self.target_user_id = "UTEST"
        self.poll_interval = 0.01
        self._reply = reply
        self._post_raises = post_raises
        self._wait_raises = wait_raises
        self.posts: list[dict[str, Any]] = []
        self.reactions: list[tuple[str, str, str]] = []
        self.appends: list[tuple[str, str, str]] = []
        self.resolved: list[tuple[str, str, str]] = []
        self.updates: list[tuple[str, str, str, Any]] = []
        self.pins: list[tuple[str, str]] = []
        self.find_result: str | None = None

    async def auth_test(self) -> bool:
        return True

    async def post_question(
        self, channel: str, text: str, blocks: list[dict[str, Any]] | None = None
    ) -> PostedMessage:
        self.posts.append({"channel": channel, "text": text, "blocks": blocks})
        if self._post_raises is not None:
            raise self._post_raises
        return PostedMessage(channel=channel, ts="1700000000.000100", permalink="https://slack.example/p1")

    async def wait_for_reply(
        self,
        channel: str,
        thread_ts: str,
        timeout_seconds: int,
        predicate: Any = None,
    ) -> dict[str, Any] | None:
        if self._wait_raises is not None:
            raise self._wait_raises
        if predicate is not None and self._reply is not None:
            return self._reply if predicate(self._reply) else None
        return self._reply

    async def react(self, channel: str, ts: str, name: str) -> None:
        self.reactions.append((channel, ts, name))

    async def append_to_message(self, channel: str, ts: str, append_text: str) -> None:
        self.appends.append((channel, ts, append_text))

    async def resolve_approval_buttons(
        self, channel: str, ts: str, result_text: str
    ) -> None:
        self.resolved.append((channel, ts, result_text))

    async def update_message(
        self, channel: str, ts: str, text: str, blocks: Any = None
    ) -> None:
        self.updates.append((channel, ts, text, blocks))

    async def pin_message(self, channel: str, ts: str) -> None:
        self.pins.append((channel, ts))

    async def find_message_by_text(self, channel: str, needle: str) -> str | None:
        return self.find_result


def _settings() -> Settings:
    return Settings(
        slack_bot_token="xoxb-test",
        slack_channel_id="DTEST",
        slack_user_id="UTEST",
        poll_interval_seconds=0.01,
        max_timeout_seconds=60,
        log_level="WARNING",
    )


# --- /notify endpoint tests ---------------------------------------------------


@pytest.mark.asyncio
async def test_notify_posts_with_mention_and_project() -> None:
    """Notification hook payload posts to the default channel with @-mention."""
    bridge = FakeBridge(reply=None)
    result = await run_notify(
        bridge=bridge,  # type: ignore[arg-type]
        settings=_settings(),
        payload={
            "hook_event_name": "Notification",
            "cwd": "D:\\Projects\\Example\\DemoApp",
            "message": "Claude needs permission to run: git checkout",
        },
    )
    assert result["status"] == "ok"
    assert result["event"] == "Notification"
    assert len(bridge.posts) == 1
    posted = bridge.posts[0]
    assert posted["channel"] == "DTEST"
    assert "<@UTEST>" in posted["text"]
    assert "Claude Code [DemoApp]" in posted["text"]
    header = posted["blocks"][0]["text"]["text"]
    assert ":bell:" in header
    assert "Claude Code [DemoApp]" in header
    assert "is waiting" in header
    # Footer block carries the cwd so the operator can navigate to the right session.
    footer = posted["blocks"][-1]["elements"][0]["text"]
    assert "D:\\Projects\\Example\\DemoApp" in footer
    # Message body block carries the hook's reason.
    body = posted["blocks"][1]["text"]["text"]
    assert "permission" in body


@pytest.mark.asyncio
async def test_notify_stop_event_uses_finished_wording() -> None:
    """Stop events render with the checkered-flag icon, "ended turn" verb,
    and project label in the header."""
    bridge = FakeBridge(reply=None)
    result = await run_notify(
        bridge=bridge,  # type: ignore[arg-type]
        settings=_settings(),
        payload={
            "hook_event_name": "Stop",
            "cwd": "/home/user/projects/foo",
            "last_assistant_message": "Tier 1 landed. Want me to commit and open a PR?",
        },
    )
    assert result["status"] == "ok"
    assert result["event"] == "Stop"
    header = bridge.posts[0]["blocks"][0]["text"]["text"]
    assert ":checkered_flag:" in header
    assert "ended turn" in header
    assert "Claude Code [foo]" in header


@pytest.mark.asyncio
async def test_notify_skips_idle_waiting_message() -> None:
    """Claude Code fires Notification with 'Claude is waiting for your input'
    whenever the orchestrator is idle, including while waiting on its own
    subagents. That has no actionable content for the operator and must be silenced;
    only permission-prompt Notifications should reach Slack."""
    bridge = FakeBridge(reply=None)
    result = await run_notify(
        bridge=bridge,  # type: ignore[arg-type]
        settings=_settings(),
        payload={
            "hook_event_name": "Notification",
            "cwd": "/x",
            "message": "Claude is waiting for your input",
        },
    )
    assert result["status"] == "skipped"
    assert result["reason"] == "idle_waiting"
    assert bridge.posts == []


@pytest.mark.asyncio
async def test_notify_keeps_permission_message_even_with_waiting_phrase() -> None:
    """If the message somehow combines both phrases, prefer the permission
    interpretation and post - the user almost certainly wants to know."""
    bridge = FakeBridge(reply=None)
    result = await run_notify(
        bridge=bridge,  # type: ignore[arg-type]
        settings=_settings(),
        payload={
            "hook_event_name": "Notification",
            "cwd": "/x",
            "message": "Claude needs your permission to use Bash, waiting for your input",
        },
    )
    assert result["status"] == "ok"


@pytest.mark.asyncio
async def test_notify_skips_recursive_stop_hook() -> None:
    """stop_hook_active=true means Claude Code is recursively re-firing Stop;
    posting a notification each time would create a Slack notification storm."""
    bridge = FakeBridge(reply=None)
    result = await run_notify(
        bridge=bridge,  # type: ignore[arg-type]
        settings=_settings(),
        payload={
            "hook_event_name": "Stop",
            "cwd": "/tmp",
            "stop_hook_active": True,
        },
    )
    assert result["status"] == "skipped"
    assert result["reason"] == "stop_hook_active"
    assert bridge.posts == []  # nothing was sent to Slack


@pytest.mark.asyncio
async def test_notify_handles_missing_cwd_gracefully() -> None:
    bridge = FakeBridge(reply=None)
    result = await run_notify(
        bridge=bridge,  # type: ignore[arg-type]
        settings=_settings(),
        payload={"hook_event_name": "Notification"},
    )
    assert result["status"] == "ok"
    header = bridge.posts[0]["blocks"][0]["text"]["text"]
    # No project bracket when cwd is absent; falls back to "Claude Code".
    assert "Claude Code [" not in header
    assert "Claude Code" in header


@pytest.mark.asyncio
async def test_notify_uses_payload_project_over_cwd_basename() -> None:
    """When the hook supplies an explicit `project` (resolved from git root),
    the server uses it instead of the cwd basename. Working in a subdir like
    DemoApp/frontend should show [DemoApp] not [frontend]."""
    bridge = FakeBridge(reply=None)
    await run_notify(
        bridge=bridge,  # type: ignore[arg-type]
        settings=_settings(),
        payload={
            "hook_event_name": "Notification",
            "cwd": "D:\\Projects\\Example\\DemoApp\\frontend",
            "project": "DemoApp",
            "message": "Claude needs your permission to use Bash",
        },
    )
    header = bridge.posts[0]["blocks"][0]["text"]["text"]
    assert "Claude Code [DemoApp]" in header
    assert "[frontend]" not in header


@pytest.mark.asyncio
async def test_notify_renders_pending_bash_tool_for_notification() -> None:
    """Notification events should show the pending Bash command + description
    so the operator can decide whether to approve from Slack-context alone, without
    the noise of his own prompt or Claude's full back-history."""
    bridge = FakeBridge(reply=None)
    result = await run_notify(
        bridge=bridge,  # type: ignore[arg-type]
        settings=_settings(),
        payload={
            "hook_event_name": "Notification",
            "cwd": "D:\\Projects\\foo",
            "message": "Claude needs your permission to use Bash",
            "pending_tool": {
                "name": "Bash",
                "input": {
                    "command": "ssh hades 'journalctl -u forgejo-runner -f'",
                    "description": "Forgejo runner job state",
                },
            },
        },
    )
    assert result["status"] == "ok"
    block_texts = [b.get("text", {}).get("text", "") for b in bridge.posts[0]["blocks"]]
    tool_block = next(t for t in block_texts if "journalctl" in t)
    assert "ssh hades" in tool_block
    assert "Forgejo runner job state" in tool_block
    # No You-block clutter, no full assistant history.
    assert not any(t.startswith("*You:*") for t in block_texts)
    assert not any(t.startswith("*Claude:*") for t in block_texts)


@pytest.mark.asyncio
async def test_notify_renders_pending_tool_for_known_file_tools() -> None:
    """Read/Edit/Write should surface the file path."""
    bridge = FakeBridge(reply=None)
    await run_notify(
        bridge=bridge,  # type: ignore[arg-type]
        settings=_settings(),
        payload={
            "hook_event_name": "Notification",
            "cwd": "/x",
            "message": "Claude needs your permission to use Edit",
            "pending_tool": {
                "name": "Edit",
                "input": {"file_path": "/x/foo.py", "old_string": "a", "new_string": "b"},
            },
        },
    )
    block_texts = [b.get("text", {}).get("text", "") for b in bridge.posts[0]["blocks"]]
    assert any("/x/foo.py" in t and "*Edit*" in t for t in block_texts)


@pytest.mark.asyncio
async def test_notify_renders_pending_tool_falls_back_to_json_for_unknown() -> None:
    """Unknown tool names (custom MCP tools) get a JSON dump of their input."""
    bridge = FakeBridge(reply=None)
    await run_notify(
        bridge=bridge,  # type: ignore[arg-type]
        settings=_settings(),
        payload={
            "hook_event_name": "Notification",
            "message": "Claude needs your permission to use mcp__custom__do_thing",
            "pending_tool": {
                "name": "mcp__custom__do_thing",
                "input": {"target": "alpha", "force": True},
            },
        },
    )
    block_texts = [b.get("text", {}).get("text", "") for b in bridge.posts[0]["blocks"]]
    json_block = next(t for t in block_texts if "mcp__custom__do_thing" in t and "```" in t)
    assert "alpha" in json_block
    assert "true" in json_block.lower()


@pytest.mark.asyncio
async def test_notify_cooldown_suppresses_repeated_pings_for_same_session() -> None:
    """Two Stops from the same session within the cooldown window: the second
    is dropped. A different session is independent. Notifications are exempt
    (see test_notify_cooldown_exempts_notifications)."""
    bridge = FakeBridge(reply=None)
    s = _settings()
    payload = {
        "hook_event_name": "Stop",
        "session_id": "session-A",
        "cwd": "/x",
        "last_assistant_message": "done",
    }
    r1 = await run_notify(bridge=bridge, settings=s, payload=payload)  # type: ignore[arg-type]
    assert r1["status"] == "ok"
    r2 = await run_notify(bridge=bridge, settings=s, payload=payload)  # type: ignore[arg-type]
    assert r2["status"] == "skipped"
    assert r2["reason"] == "cooldown"
    assert len(bridge.posts) == 1

    # Different session is not throttled.
    other = dict(payload)
    other["session_id"] = "session-B"
    other["cwd"] = "/y"
    r3 = await run_notify(bridge=bridge, settings=s, payload=other)  # type: ignore[arg-type]
    assert r3["status"] == "ok"
    assert len(bridge.posts) == 2


@pytest.mark.asyncio
async def test_notify_cooldown_exempts_notifications() -> None:
    """Permission prompts (Notification events) bypass the cooldown - they are
    always actionable and must reach the operator even if a Stop just fired."""
    bridge = FakeBridge(reply=None)
    s = _settings()
    payload = {
        "hook_event_name": "Notification",
        "session_id": "session-A",
        "cwd": "/x",
        "message": "Claude needs your permission to use Bash",
    }
    r1 = await run_notify(bridge=bridge, settings=s, payload=payload)  # type: ignore[arg-type]
    r2 = await run_notify(bridge=bridge, settings=s, payload=payload)  # type: ignore[arg-type]
    assert r1["status"] == "ok"
    assert r2["status"] == "ok"
    assert len(bridge.posts) == 2


@pytest.mark.asyncio
async def test_notify_cooldown_disabled_when_set_to_zero() -> None:
    """notify_cooldown_seconds=0 disables throttling entirely. Uses Stop
    events because Notifications are unconditionally exempt and would pass
    this assertion vacuously."""
    bridge = FakeBridge(reply=None)
    s = Settings(
        slack_bot_token="xoxb-test",
        slack_channel_id="DTEST",
        slack_user_id="UTEST",
        notify_cooldown_seconds=0,
        log_level="WARNING",
    )
    payload = {
        "hook_event_name": "Stop",
        "session_id": "session-X",
        "cwd": "/x",
        "last_assistant_message": "done",
    }
    r1 = await run_notify(bridge=bridge, settings=s, payload=payload)  # type: ignore[arg-type]
    r2 = await run_notify(bridge=bridge, settings=s, payload=payload)  # type: ignore[arg-type]
    assert r1["status"] == "ok"
    assert r2["status"] == "ok"
    assert len(bridge.posts) == 2


@pytest.mark.asyncio
async def test_notify_returns_error_on_slack_failure() -> None:
    bridge = FakeBridge(post_raises=RuntimeError("slack down"))
    result = await run_notify(
        bridge=bridge,  # type: ignore[arg-type]
        settings=_settings(),
        payload={
            "hook_event_name": "Notification",
            "cwd": "/x/y/z",
        },
    )
    assert result["status"] == "error"
    assert "slack down" in result["detail"]


# --- /notify wait_for_reply (AFK-mode Stop blocking) ---------------------------
#
# When the local notify-hook detects ~/.claude/.afk on a Stop event, it
# forwards wait_for_reply=true so the server polls the Slack thread, fires a
# reminder `reminder_lead_seconds` before timeout, and returns the reply (or
# timeout). The hook then injects the reply back into the session via Stop's
# {"decision": "block", "reason": <reply>} contract.


class WaitReplyBridge(FakeBridge):
    """Bridge whose wait_for_reply blocks for `delay_seconds` then returns reply.

    Used to drive the wait_for_reply path of run_notify deterministically. The
    delay must exceed reminder_at so tests can verify the reminder fires before
    the reply lands.
    """

    def __init__(self, *, delay_seconds: float, reply: dict[str, Any] | None) -> None:
        super().__init__(reply=reply)
        self._delay_seconds = delay_seconds

    async def wait_for_reply(
        self,
        channel: str,
        thread_ts: str,
        timeout_seconds: int,
        predicate: Any = None,
    ) -> dict[str, Any] | None:
        actual = min(self._delay_seconds, max(0, timeout_seconds))
        await asyncio.sleep(actual)
        if actual < self._delay_seconds:
            return None
        if predicate is not None and self._reply is not None:
            return self._reply if predicate(self._reply) else None
        return self._reply


@pytest.mark.asyncio
async def test_notify_wait_returns_reply_when_user_responds() -> None:
    """AFK-mode Stop: server polls Slack and returns the reply text + permalink
    so the hook can inject it as the next user message via decision:block."""
    bridge = WaitReplyBridge(
        delay_seconds=0.05,
        reply={"user": "UTEST", "text": "ok continue with the migration"},
    )
    result = await run_notify(
        bridge=bridge,  # type: ignore[arg-type]
        settings=_settings(),
        payload={
            "hook_event_name": "Stop",
            "cwd": "/x",
            "last_assistant_message": "Done with phase 1.",
            "wait_for_reply": True,
            "timeout_seconds": 2,
            "reminder_lead_seconds": 1,
        },
    )
    assert result["status"] == "answered"
    assert result["reply"] == "ok continue with the migration"
    assert result["event"] == "Stop"
    assert ("DTEST", "1700000000.000100", "white_check_mark") in bridge.reactions


@pytest.mark.asyncio
async def test_notify_wait_returns_timeout_when_no_reply() -> None:
    """No reply within timeout → status=timeout. Slack thread gets the clock
    reaction + a "timed out" footer so the channel history is legible."""
    bridge = FakeBridge(reply=None)
    result = await run_notify(
        bridge=bridge,  # type: ignore[arg-type]
        settings=_settings(),
        payload={
            "hook_event_name": "Stop",
            "cwd": "/x",
            "wait_for_reply": True,
            "timeout_seconds": 2,
            "reminder_lead_seconds": 1,
        },
    )
    assert result["status"] == "timeout"
    assert ("DTEST", "1700000000.000100", "clock1") in bridge.reactions
    assert any("timed out" in a[2] for a in bridge.appends)


@pytest.mark.asyncio
async def test_notify_wait_fires_reminder_before_timeout() -> None:
    """Reminder ping must hit the thread `reminder_lead_seconds` before the
    deadline (the operator's spec: 25 min into a 30 min wait). Verified by setting
    a short timeout, a long bridge delay, and confirming the reminder append
    landed."""
    bridge = WaitReplyBridge(delay_seconds=10, reply=None)  # never resolves before timeout
    started = asyncio.get_event_loop().time()
    result = await run_notify(
        bridge=bridge,  # type: ignore[arg-type]
        settings=_settings(),
        payload={
            "hook_event_name": "Stop",
            "wait_for_reply": True,
            "timeout_seconds": 3,
            "reminder_lead_seconds": 2,  # reminder at 1s, timeout at 3s
        },
    )
    elapsed = asyncio.get_event_loop().time() - started
    assert 2.5 < elapsed < 4  # waited the full timeout
    assert result["status"] == "timeout"
    # The reminder must be among the appended lines, distinct from the timeout
    # marker. Reminder is identified by the alarm_clock emoji + "left until timeout".
    reminder_appends = [a for a in bridge.appends if "left until timeout" in a[2]]
    assert len(reminder_appends) == 1, f"expected 1 reminder, got {bridge.appends}"


@pytest.mark.asyncio
async def test_notify_wait_no_reminder_when_reply_arrives_first() -> None:
    """If the reply lands before the reminder deadline, the reminder task is
    cancelled cleanly and never posts."""
    bridge = WaitReplyBridge(
        delay_seconds=0.05,
        reply={"user": "UTEST", "text": "back already"},
    )
    await run_notify(
        bridge=bridge,  # type: ignore[arg-type]
        settings=_settings(),
        payload={
            "hook_event_name": "Stop",
            "wait_for_reply": True,
            "timeout_seconds": 5,
            "reminder_lead_seconds": 1,  # reminder at 4s, but reply lands at 0.05s
        },
    )
    reminder_appends = [a for a in bridge.appends if "left until timeout" in a[2]]
    assert reminder_appends == []  # reminder never fired


@pytest.mark.asyncio
async def test_notify_wait_ignored_for_non_stop_events() -> None:
    """wait_for_reply must be a no-op on Notification events; AFK reply
    injection is a Stop-only flow."""
    bridge = FakeBridge(reply=None)
    result = await run_notify(
        bridge=bridge,  # type: ignore[arg-type]
        settings=_settings(),
        payload={
            "hook_event_name": "Notification",
            "message": "Claude needs your permission to use Bash",
            "wait_for_reply": True,  # should be ignored
            "timeout_seconds": 60,
        },
    )
    # Posts as a normal Notification (status=ok, no wait), not status=timeout
    # or status=answered.
    assert result["status"] == "ok"
    assert "reply" not in result


@pytest.mark.asyncio
async def test_notify_wait_includes_reminder_lead_default_minimum() -> None:
    """When the caller doesn't set reminder_lead_seconds, default is 300s
    (5 min before timeout - the operator's 25-min/30-min spec). For very short
    test timeouts the reminder is clamped to fire at least 1s before timeout."""
    bridge = WaitReplyBridge(delay_seconds=10, reply=None)
    result = await run_notify(
        bridge=bridge,  # type: ignore[arg-type]
        settings=_settings(),
        payload={
            "hook_event_name": "Stop",
            "wait_for_reply": True,
            "timeout_seconds": 2,
            # no reminder_lead_seconds; default 300 forces the clamp logic
        },
    )
    assert result["status"] == "timeout"
    # Even with the default 300s lead, a 2s timeout must still trigger a
    # clamped reminder (timeout - 1s).
    reminder_appends = [a for a in bridge.appends if "left until timeout" in a[2]]
    assert len(reminder_appends) == 1


# --- /notify wait_for_reply (AFK-mode PreToolUse approval) --------------------
#
# When AFK is on and Claude requests a tool, the local PreToolUse hook
# forwards wait_for_reply=true with tool_name+tool_input. The server posts the
# pending tool to Slack with a reply hint, polls for a parseable y/yes/n/no
# reply, and returns {decision: allow|deny}. The hook emits Claude Code's
# hookSpecificOutput.permissionDecision JSON based on the decision.


def test_parse_approval_reply_recognizes_allow_tokens() -> None:
    for token in ["y", "yes", "Yes", "YES", "yep", "ya", "ok", "go", "allow", "approve"]:
        assert _parse_approval_reply(token) == "allow", f"failed: {token!r}"


def test_parse_approval_reply_distinguishes_always_from_yes() -> None:
    # "always" is a persistence intent. The hook needs the verdict distinct from
    # plain "allow" so it can write a matching pattern into permissions.allow.
    # Collapsing them (the previous behavior) made "always" mean "yes once" and
    # caused 20-prompts-in-a-row approval fatigue.
    for token in ["always", "Always", "ALWAYS", "forever", "persist"]:
        assert _parse_approval_reply(token) == "allow_always", f"failed: {token!r}"


def test_parse_approval_reply_multiword_always_wins_over_yes() -> None:
    # When both intents are typed together, persistence dominates.
    assert _parse_approval_reply("yes always") == "allow_always"
    assert _parse_approval_reply("always allow") == "allow_always"
    assert _parse_approval_reply("approve forever") == "allow_always"


def test_parse_approval_reply_recognizes_deny_tokens() -> None:
    for token in ["n", "no", "No", "NO", "nope", "nah", "stop", "cancel", "deny", "abort"]:
        assert _parse_approval_reply(token) == "deny", f"failed: {token!r}"


def test_parse_approval_reply_strips_trailing_punctuation() -> None:
    assert _parse_approval_reply("yes!") == "allow"
    assert _parse_approval_reply("no.") == "deny"
    assert _parse_approval_reply("yes?") == "allow"


def test_parse_approval_reply_accepts_short_multi_word() -> None:
    """yes please / no thanks / go for it parse on the first word."""
    assert _parse_approval_reply("yes please") == "allow"
    assert _parse_approval_reply("no thanks") == "deny"
    assert _parse_approval_reply("go for it") == "allow"


def test_parse_approval_reply_returns_none_for_ambiguous() -> None:
    """Free-form chatter that doesn't start with a token must NOT auto-decide."""
    assert _parse_approval_reply("hmm let me think") is None
    assert _parse_approval_reply("looks good to run it") is None  # 5 words, doesn't start with token
    assert _parse_approval_reply("") is None
    assert _parse_approval_reply("   ") is None
    assert _parse_approval_reply("maybe") is None


@pytest.mark.asyncio
async def test_notify_pretooluse_renders_tool_from_native_payload() -> None:
    """PreToolUse hook sends tool_name + tool_input directly (Claude Code's
    native payload shape). Server promotes those into pending_tool so the
    block builder treats them like a transcript-extracted tool."""
    bridge = FakeBridge(reply=None)
    result = await run_notify(
        bridge=bridge,  # type: ignore[arg-type]
        settings=_settings(),
        payload={
            "hook_event_name": "PreToolUse",
            "cwd": "D:\\Projects\\foo",
            "tool_name": "Bash",
            "tool_input": {"command": "git push origin main", "description": "publish"},
        },
    )
    assert result["status"] == "ok"
    assert result["event"] == "PreToolUse"
    blocks = bridge.posts[0]["blocks"]
    block_texts = [b.get("text", {}).get("text", "") for b in blocks]
    assert any("git push origin main" in t for t in block_texts)
    # Interactive buttons must be present: one tap works identically on mobile
    # and desktop, with no thread-vs-channel reply placement to get wrong.
    actions = next((b for b in blocks if b.get("type") == "actions"), None)
    assert actions is not None, "PreToolUse posts must include an actions (button) block"
    assert actions.get("block_id") == "approval_actions"
    values = {e.get("value") for e in actions.get("elements", [])}
    assert values == {"allow", "allow_always", "deny"}
    # Text-reply hint stays as a fallback and must list all three verdicts so a
    # phone-typed reply can pick one-shot allow, persist-allow, or deny.
    footer = next((t for t in block_texts if "reply" in t.lower()), "")
    assert footer, "PreToolUse posts must include a reply-hint footer"
    assert "yes" in footer
    assert "always" in footer
    assert "persist" in footer
    assert "no" in footer


@pytest.mark.asyncio
async def test_notify_pretooluse_wait_returns_allow_on_yes() -> None:
    bridge = WaitReplyBridge(
        delay_seconds=0.05,
        reply={"user": "UTEST", "text": "yes"},
    )
    result = await run_notify(
        bridge=bridge,  # type: ignore[arg-type]
        settings=_settings(),
        payload={
            "hook_event_name": "PreToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": "ls"},
            "wait_for_reply": True,
            "timeout_seconds": 2,
            "reminder_lead_seconds": 1,
        },
    )
    assert result["status"] == "answered"
    assert result["decision"] == "allow"
    assert result["event"] == "PreToolUse"
    assert ("DTEST", "1700000000.000100", "white_check_mark") in bridge.reactions


@pytest.mark.asyncio
async def test_notify_pretooluse_wait_returns_allow_always_on_always_reply() -> None:
    # 'always' must surface as a distinct verdict so the hook can persist a
    # matching pattern to permissions.allow. Pre-fix this returned 'allow' and
    # caused 20-prompts-in-a-row approval fatigue.
    bridge = WaitReplyBridge(
        delay_seconds=0.05,
        reply={"user": "UTEST", "text": "always"},
    )
    result = await run_notify(
        bridge=bridge,  # type: ignore[arg-type]
        settings=_settings(),
        payload={
            "hook_event_name": "PreToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": "git push origin main"},
            "wait_for_reply": True,
            "timeout_seconds": 2,
            "reminder_lead_seconds": 1,
        },
    )
    assert result["status"] == "answered"
    assert result["decision"] == "allow_always"
    assert result["event"] == "PreToolUse"
    # Both check-mark AND lock should fire so the Slack thread visibly
    # distinguishes a persist-allow from a one-shot allow.
    assert ("DTEST", "1700000000.000100", "white_check_mark") in bridge.reactions
    assert ("DTEST", "1700000000.000100", "lock") in bridge.reactions


@pytest.mark.asyncio
async def test_notify_pretooluse_wait_returns_deny_on_no() -> None:
    bridge = WaitReplyBridge(
        delay_seconds=0.05,
        reply={"user": "UTEST", "text": "no thanks"},
    )
    result = await run_notify(
        bridge=bridge,  # type: ignore[arg-type]
        settings=_settings(),
        payload={
            "hook_event_name": "PreToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": "rm -rf /tmp/cache"},
            "wait_for_reply": True,
            "timeout_seconds": 2,
            "reminder_lead_seconds": 1,
        },
    )
    assert result["status"] == "answered"
    assert result["decision"] == "deny"
    assert ("DTEST", "1700000000.000100", "no_entry_sign") in bridge.reactions


@pytest.mark.asyncio
async def test_notify_pretooluse_wait_skips_ambiguous_reply() -> None:
    """Ambiguous chatter ('hmm let me think') must NOT auto-decide. The
    predicate filters it out, the bridge returns None, and the server reports
    timeout so the hook falls through to the terminal prompt."""
    bridge = WaitReplyBridge(
        delay_seconds=0.05,
        reply={"user": "UTEST", "text": "hmm let me think about this"},
    )
    result = await run_notify(
        bridge=bridge,  # type: ignore[arg-type]
        settings=_settings(),
        payload={
            "hook_event_name": "PreToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": "ls"},
            "wait_for_reply": True,
            "timeout_seconds": 2,
            "reminder_lead_seconds": 1,
        },
    )
    assert result["status"] == "timeout"
    assert ("DTEST", "1700000000.000100", "clock1") in bridge.reactions


@pytest.mark.asyncio
async def test_notify_pretooluse_cooldown_exempt() -> None:
    """Approval prompts (PreToolUse) bypass cooldown like Notifications do -
    each tool needs its own decision."""
    bridge = FakeBridge(reply=None)
    s = _settings()
    payload = {
        "hook_event_name": "PreToolUse",
        "session_id": "session-A",
        "cwd": "/x",
        "tool_name": "Bash",
        "tool_input": {"command": "ls"},
    }
    r1 = await run_notify(bridge=bridge, settings=s, payload=payload)  # type: ignore[arg-type]
    r2 = await run_notify(bridge=bridge, settings=s, payload=payload)  # type: ignore[arg-type]
    assert r1["status"] == "ok"
    assert r2["status"] == "ok"
    assert len(bridge.posts) == 2


# --- Full assistant output rendering in Slack Stop messages ------------------
#
# the operator uses Slack as the conversation surface when AFK; he must be able to
# read the entire assistant turn there to compose a meaningful reply. These
# tests pin the no-truncation contract: a short turn renders in one block,
# a long turn splits across multiple blocks, and the concatenation of all
# rendered chunks equals the original text.


def test_split_for_slack_short_text_returns_single_chunk() -> None:
    chunks = _split_for_slack("hello world", max_chars=2800)
    assert chunks == ["hello world"]


def test_split_for_slack_empty_returns_empty_list() -> None:
    assert _split_for_slack("") == []
    assert _split_for_slack("   \n\n  ") == []


def test_split_for_slack_long_text_splits_on_line_breaks() -> None:
    """Long text prefers line-break boundaries over mid-line splits."""
    line = "a" * 100  # 100 chars
    text = "\n".join(line for _ in range(50))  # ~5050 chars total
    chunks = _split_for_slack(text, max_chars=500)
    # Multiple chunks
    assert len(chunks) > 1
    # Each chunk under the limit
    for chunk in chunks:
        assert len(chunk) <= 500
    # Concatenation preserves all the content (mod whitespace)
    rejoined = "\n".join(chunks).replace("\n", "")
    expected = text.replace("\n", "")
    assert rejoined == expected


def test_split_for_slack_oversize_single_line_char_splits() -> None:
    """A single line longer than max_chars must be char-split - there's no
    line break to fall back on."""
    text = "x" * 6000
    chunks = _split_for_slack(text, max_chars=2000)
    assert len(chunks) == 3
    assert all(len(c) <= 2000 for c in chunks)
    assert "".join(chunks) == text


@pytest.mark.asyncio
async def test_notify_stop_renders_full_assistant_message_when_short() -> None:
    """A short assistant turn renders entirely in one quoted block - no
    truncation, no '...' marker."""
    message = (
        "Phase 1 complete. Migrated 47 records. No conflicts. "
        "Tests pass. Branch fix/migrate-users ready for review."
    )
    bridge = FakeBridge(reply=None)
    await run_notify(
        bridge=bridge,  # type: ignore[arg-type]
        settings=_settings(),
        payload={
            "hook_event_name": "Stop",
            "cwd": "/x",
            "last_assistant_message": message,
        },
    )
    # Find the quoted-message block (starts with `>`).
    quoted_blocks = [
        b for b in bridge.posts[0]["blocks"]
        if b.get("type") == "section"
        and b.get("text", {}).get("text", "").startswith(">")
    ]
    assert len(quoted_blocks) == 1
    rendered = quoted_blocks[0]["text"]["text"]
    # Strip the `>` quote prefixes back out and compare to original.
    unquoted = "\n".join(line.lstrip(">") for line in rendered.splitlines())
    assert unquoted == message
    # No truncation marker.
    assert "..." not in rendered


@pytest.mark.asyncio
async def test_notify_stop_renders_full_assistant_message_when_long() -> None:
    """A long assistant turn (well past the 800-char old truncation limit)
    must render in its entirety across multiple quoted blocks."""
    paragraph = (
        "This is a long paragraph that simulates a substantial assistant "
        "explanation. " * 20  # ~ 1600 chars per paragraph
    )
    message = "\n\n".join([paragraph] * 5)  # ~8000 chars total
    bridge = FakeBridge(reply=None)
    await run_notify(
        bridge=bridge,  # type: ignore[arg-type]
        settings=_settings(),
        payload={
            "hook_event_name": "Stop",
            "cwd": "/x",
            "last_assistant_message": message,
        },
    )
    quoted_blocks = [
        b for b in bridge.posts[0]["blocks"]
        if b.get("type") == "section"
        and b.get("text", {}).get("text", "").startswith(">")
    ]
    # Must split into multiple blocks.
    assert len(quoted_blocks) >= 2
    # Concatenating the unquoted text across all quoted blocks must equal the
    # original message (modulo whitespace stripping at chunk boundaries).
    rendered_pieces: list[str] = []
    for b in quoted_blocks:
        text = b["text"]["text"]
        unquoted = "\n".join(line.lstrip(">") for line in text.splitlines())
        rendered_pieces.append(unquoted)
    # Drop whitespace-only chars at chunk boundaries before comparing.
    full_rendered = "".join(rendered_pieces).replace(" ", "").replace("\n", "")
    full_original = message.replace(" ", "").replace("\n", "")
    assert full_rendered == full_original
    # No truncation marker anywhere.
    for b in quoted_blocks:
        assert "..." not in b["text"]["text"]


# --- Slack interactive-button approvals --------------------------------------
#
# Buttons make a single tap resolve an AFK approval identically on mobile and
# desktop, eliminating the thread-vs-channel reply-placement trap that made
# mobile approvals silently fail. The /slack/interactions route verifies the
# Slack request signature, honors only the configured owner's taps, and records
# the verdict where the in-flight approval poller picks it up.


def _slack_signature(secret: str, timestamp: str, body: bytes) -> str:
    basestring = b"v0:" + timestamp.encode() + b":" + body
    digest = hmac.new(secret.encode(), basestring, hashlib.sha256).hexdigest()
    return "v0=" + digest


def test_verify_slack_signature_accepts_valid() -> None:
    import time as _t

    secret = "shhh"
    # Real current timestamp so the skew check passes.
    ts = str(int(_t.time()))
    body = b"payload=%7B%22type%22%3A%22block_actions%22%7D"
    sig = _slack_signature(secret, ts, body)
    assert _verify_slack_signature(secret, ts, body, sig) is True


def test_verify_slack_signature_rejects_tampered_body() -> None:
    import time as _t

    secret = "shhh"
    ts = str(int(_t.time()))
    body = b"payload=original"
    sig = _slack_signature(secret, ts, body)
    assert _verify_slack_signature(secret, ts, b"payload=tampered", sig) is False


def test_verify_slack_signature_rejects_stale_timestamp() -> None:
    import time as _t

    secret = "shhh"
    ts = str(int(_t.time()) - 4000)  # well outside the 300s replay window
    body = b"payload=x"
    sig = _slack_signature(secret, ts, body)
    assert _verify_slack_signature(secret, ts, body, sig) is False


def test_verify_slack_signature_rejects_missing_inputs() -> None:
    assert _verify_slack_signature("", "123", b"x", "v0=abc") is False
    assert _verify_slack_signature("secret", "", b"x", "v0=abc") is False
    assert _verify_slack_signature("secret", "123", b"x", "") is False


@pytest.mark.asyncio
async def test_handle_interaction_records_owner_decision() -> None:
    """A button tap from the configured owner records the verdict (keyed by
    message ts) and strips the buttons off the message."""
    bridge = FakeBridge(reply=None)
    data = {
        "type": "block_actions",
        "user": {"id": "UTEST"},
        "actions": [{"action_id": "approve_always", "value": "allow_always"}],
        "container": {"message_ts": "1700000000.000100", "channel_id": "DTEST"},
    }
    result = await handle_interaction(bridge=bridge, settings=_settings(), data=data)  # type: ignore[arg-type]
    assert result["decision"] == "allow_always"
    assert _button_decisions["1700000000.000100"]["decision"] == "allow_always"
    # The button row was stripped and a result line stamped.
    assert bridge.resolved and bridge.resolved[0][1] == "1700000000.000100"


@pytest.mark.asyncio
async def test_handle_interaction_ignores_non_owner() -> None:
    """A tap from any other workspace member must NOT record a decision -
    same trust boundary as the text-reply user filter."""
    bridge = FakeBridge(reply=None)
    data = {
        "type": "block_actions",
        "user": {"id": "U_SOMEONE_ELSE"},
        "actions": [{"action_id": "approve", "value": "allow"}],
        "container": {"message_ts": "1700000000.000100", "channel_id": "DTEST"},
    }
    result = await handle_interaction(bridge=bridge, settings=_settings(), data=data)  # type: ignore[arg-type]
    assert result.get("ignored") == "wrong_user"
    assert "1700000000.000100" not in _button_decisions
    assert bridge.resolved == []


@pytest.mark.asyncio
async def test_handle_interaction_accepts_second_approver_account() -> None:
    """A tap from a SECOND authorized account (slack_approver_ids) is honored,
    so a user whose mobile Slack is a different account than the primary isn't
    rejected as wrong_user (the desktop-works/mobile-doesn't bug)."""
    bridge = FakeBridge(reply=None)
    settings = Settings(
        slack_bot_token="xoxb-test",
        slack_channel_id="DTEST",
        slack_user_id="UTEST",
        slack_approver_ids="UMOBILE, UEXTRA",
        log_level="WARNING",
    )
    assert settings.approver_id_set == {"UTEST", "UMOBILE", "UEXTRA"}
    data = {
        "type": "block_actions",
        "user": {"id": "UMOBILE"},
        "actions": [{"action_id": "approve", "value": "allow"}],
        "container": {"message_ts": "1700000000.000100", "channel_id": "DTEST"},
    }
    result = await handle_interaction(bridge=bridge, settings=settings, data=data)  # type: ignore[arg-type]
    assert result["decision"] == "allow"
    assert _button_decisions["1700000000.000100"]["decision"] == "allow"


@pytest.mark.asyncio
async def test_handle_interaction_still_rejects_unauthorized_third_party() -> None:
    """Adding approver IDs must NOT open the gate to everyone: a workspace
    member who is neither the primary nor an approver is still rejected."""
    bridge = FakeBridge(reply=None)
    settings = Settings(
        slack_bot_token="xoxb-test",
        slack_channel_id="DTEST",
        slack_user_id="UTEST",
        slack_approver_ids="UMOBILE",
        log_level="WARNING",
    )
    data = {
        "type": "block_actions",
        "user": {"id": "U_RANDOM_COWORKER"},
        "actions": [{"action_id": "approve", "value": "allow"}],
        "container": {"message_ts": "1700000000.000100", "channel_id": "DTEST"},
    }
    result = await handle_interaction(bridge=bridge, settings=settings, data=data)  # type: ignore[arg-type]
    assert result.get("ignored") == "wrong_user"
    assert "1700000000.000100" not in _button_decisions


@pytest.mark.asyncio
async def test_handle_interaction_ignores_unknown_value() -> None:
    bridge = FakeBridge(reply=None)
    data = {
        "type": "block_actions",
        "user": {"id": "UTEST"},
        "actions": [{"action_id": "x", "value": "maybe"}],
        "container": {"message_ts": "1700000000.000100", "channel_id": "DTEST"},
    }
    result = await handle_interaction(bridge=bridge, settings=_settings(), data=data)  # type: ignore[arg-type]
    assert result.get("ignored") == "unknown_value"
    assert "1700000000.000100" not in _button_decisions


@pytest.mark.asyncio
async def test_pretooluse_button_decision_wins_race() -> None:
    """When a button verdict is recorded while the text-reply poll is still
    blocking, the button wins the race and its value is used verbatim (it's
    already allow/allow_always/deny, bypassing text parsing)."""
    # Text path never resolves before timeout; button decision is pre-seeded so
    # the button poll picks it up first.
    bridge = WaitReplyBridge(delay_seconds=10, reply=None)
    _button_decisions["1700000000.000100"] = {"decision": "allow_always", "user": "UTEST"}
    result = await run_notify(
        bridge=bridge,  # type: ignore[arg-type]
        settings=_settings(),
        payload={
            "hook_event_name": "PreToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": "git push origin main"},
            "wait_for_reply": True,
            "timeout_seconds": 3,
            "reminder_lead_seconds": 1,
        },
    )
    assert result["status"] == "answered"
    assert result["decision"] == "allow_always"
    assert ("DTEST", "1700000000.000100", "white_check_mark") in bridge.reactions
    assert ("DTEST", "1700000000.000100", "lock") in bridge.reactions
    # Decision was consumed (popped) so a later prompt reusing the ts can't
    # inherit it.
    assert "1700000000.000100" not in _button_decisions


# --- AFK toggle from Slack ----------------------------------------------------
#
# AFK state is server-authoritative (in-memory). A pinned control message and an
# "I'm back" button on the AFK-on announcement flip it; the Stop / PreToolUse
# reader hooks GET /afk to honor it. The local afk-trigger hook keeps the server
# in sync with terminal "brb" / "I'm back" cues via set_afk on its /notify call.


@pytest.mark.asyncio
async def test_set_afk_records_state_and_posts_control_message() -> None:
    bridge = FakeBridge(reply=None)
    changed = await _set_afk(bridge, _settings(), True)  # type: ignore[arg-type]
    assert changed is True
    assert server_mod._afk_state is True
    # The reusable pinned control message was posted (carries the sentinel) and
    # pinned best-effort.
    assert any("ask-human-afk-control" in p["text"] for p in bridge.posts)
    assert server_mod._afk_control_ts is not None
    assert bridge.pins, "control message should be pinned (best-effort)"


@pytest.mark.asyncio
async def test_set_afk_updates_existing_control_in_place() -> None:
    bridge = FakeBridge(reply=None)
    await _set_afk(bridge, _settings(), True)  # type: ignore[arg-type]
    posts_after_first = len(bridge.posts)
    # Second toggle should UPDATE the known control ts, not post a new message.
    await _set_afk(bridge, _settings(), False)  # type: ignore[arg-type]
    assert len(bridge.posts) == posts_after_first  # no new post
    assert bridge.updates, "second toggle should update the control message in place"


@pytest.mark.asyncio
async def test_handle_interaction_afk_off_button_clears_state() -> None:
    """Tapping 'I'm back' (afk_off) on an announcement flips server AFK off and
    strips the button off that announcement (it's not the control message)."""
    bridge = FakeBridge(reply=None)
    server_mod._afk_state = True
    server_mod._afk_control_ts = "9999.0001"  # a different message is the control
    data = {
        "type": "block_actions",
        "user": {"id": "UTEST"},
        "actions": [{"action_id": "afk_off", "value": "off"}],
        "container": {"message_ts": "1700000000.000100", "channel_id": "DTEST"},
    }
    result = await handle_interaction(bridge=bridge, settings=_settings(), data=data)  # type: ignore[arg-type]
    assert result["afk"] is False
    assert server_mod._afk_state is False
    # The announcement (not the control message) had its button stripped.
    assert any(r[1] == "1700000000.000100" for r in bridge.resolved)


@pytest.mark.asyncio
async def test_handle_interaction_afk_button_ignores_non_owner() -> None:
    bridge = FakeBridge(reply=None)
    server_mod._afk_state = False
    data = {
        "type": "block_actions",
        "user": {"id": "U_INTRUDER"},
        "actions": [{"action_id": "afk_on", "value": "on"}],
        "container": {"message_ts": "1700000000.000100", "channel_id": "DTEST"},
    }
    result = await handle_interaction(bridge=bridge, settings=_settings(), data=data)  # type: ignore[arg-type]
    assert result.get("ignored") == "wrong_user"
    assert server_mod._afk_state is False  # unchanged


@pytest.mark.asyncio
async def test_run_notify_set_afk_state_only_does_not_ping() -> None:
    """An OFF sync (afk_state_only) updates server state + control message but
    must NOT post an announcement ping — turning AFK off shouldn't light up
    Slack."""
    bridge = FakeBridge(reply=None)
    server_mod._afk_state = True
    server_mod._afk_control_ts = "9999.0001"  # control exists -> update, no post
    result = await run_notify(
        bridge=bridge,  # type: ignore[arg-type]
        settings=_settings(),
        payload={"set_afk": False, "afk_state_only": True},
    )
    assert result["status"] == "ok"
    assert result["afk"] is False
    assert server_mod._afk_state is False
    # No notification message was posted (only an in-place control update).
    assert bridge.posts == []
    assert bridge.updates, "control message should have been updated in place"


@pytest.mark.asyncio
async def test_run_notify_afk_announcement_carries_back_button() -> None:
    """The AFK-on announcement gets an 'I'm back' (afk_off) button so a single
    tap turns AFK off without hunting for the pinned control."""
    bridge = FakeBridge(reply=None)
    result = await run_notify(
        bridge=bridge,  # type: ignore[arg-type]
        settings=_settings(),
        payload={
            "hook_event_name": "Notification",
            "cwd": "/x",
            "message": "AFK mode ON - matched 'brb'.",
            "set_afk": True,
            "afk_announcement": True,
        },
    )
    assert result["status"] == "ok"
    assert server_mod._afk_state is True
    # Find the announcement post (the one that is NOT the control message).
    announcement = next(
        p for p in bridge.posts if "ask-human-afk-control" not in p["text"]
    )
    actions = [b for b in announcement["blocks"] if b.get("type") == "actions"]
    assert actions, "announcement must carry an actions block"
    afk_block = next(b for b in actions if b.get("block_id") == "afk_controls")
    assert afk_block["elements"][0]["action_id"] == "afk_off"
