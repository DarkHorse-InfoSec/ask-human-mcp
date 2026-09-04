"""Tests for SlackBridge using a fake AsyncWebClient."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from slack_sdk.errors import SlackApiError

from ask_human_mcp.slack import SlackBridge


class FakeResponse(dict):
    """Mimics slack_sdk's SlackResponse enough for our code paths."""

    def get(self, key: str, default: Any = None) -> Any:
        return super().get(key, default)


class FakeAsyncWebClient:
    """Records calls and returns scripted responses for the methods SlackBridge uses."""

    def __init__(self, replies_script: list[list[dict[str, Any]]] | None = None) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._replies_script = replies_script or []
        self._replies_idx = 0
        self.fail_post = False
        self.fail_react = False
        self.fail_replies_first_n = 0

    async def chat_postMessage(self, **kwargs: Any) -> FakeResponse:
        self.calls.append(("chat_postMessage", kwargs))
        if self.fail_post:
            raise SlackApiError("post failed", response={"error": "channel_not_found"})
        return FakeResponse(ok=True, channel=kwargs.get("channel", "DTEST"), ts="1700000000.000100")

    async def users_info(self, **kwargs: Any) -> FakeResponse:
        self.calls.append(("users_info", kwargs))
        if getattr(self, "fail_users_info", False):
            raise SlackApiError("user_not_found", response={"error": "user_not_found"})
        return FakeResponse(
            ok=True,
            user={
                "id": kwargs["user"],
                "name": "fakeuser",
                "profile": {"real_name": "Fake User", "email": "fake@example.com"},
            },
        )

    async def chat_getPermalink(self, **kwargs: Any) -> FakeResponse:
        self.calls.append(("chat_getPermalink", kwargs))
        return FakeResponse(ok=True, permalink="https://slack.example/p1700000000000100")

    async def conversations_replies(self, **kwargs: Any) -> FakeResponse:
        self.calls.append(("conversations_replies", kwargs))
        if self.fail_replies_first_n > 0:
            self.fail_replies_first_n -= 1
            raise SlackApiError("ratelimited", response={"error": "ratelimited"})
        if self._replies_idx >= len(self._replies_script):
            messages = [{"ts": kwargs["ts"], "user": "UROOT", "text": "parent"}]
        else:
            messages = self._replies_script[self._replies_idx]
            self._replies_idx += 1
        return FakeResponse(ok=True, messages=messages)

    async def reactions_add(self, **kwargs: Any) -> FakeResponse:
        self.calls.append(("reactions_add", kwargs))
        if self.fail_react:
            raise SlackApiError("nope", response={"error": "already_reacted"})
        return FakeResponse(ok=True)

    async def conversations_history(self, **kwargs: Any) -> FakeResponse:
        self.calls.append(("conversations_history", kwargs))
        return FakeResponse(
            ok=True,
            messages=[{"ts": kwargs["latest"], "text": "original"}],
        )

    async def chat_update(self, **kwargs: Any) -> FakeResponse:
        self.calls.append(("chat_update", kwargs))
        return FakeResponse(ok=True)

    async def auth_test(self) -> FakeResponse:
        self.calls.append(("auth_test", {}))
        return FakeResponse(ok=True, user="askhumanbot")


def _bridge(client: FakeAsyncWebClient, **kwargs: Any) -> SlackBridge:
    return SlackBridge(
        token="xoxb-fake",
        target_user_id="UTEST",
        poll_interval=kwargs.get("poll_interval", 0.01),
        client=client,  # type: ignore[arg-type]
    )


@pytest.mark.asyncio
async def test_post_question_returns_channel_and_ts() -> None:
    client = FakeAsyncWebClient()
    bridge = _bridge(client)
    posted = await bridge.post_question("DTEST", text="hi", blocks=[])
    assert posted.channel == "DTEST"
    assert posted.ts == "1700000000.000100"
    assert posted.permalink.startswith("https://")
    assert any(c[0] == "chat_postMessage" for c in client.calls)


@pytest.mark.asyncio
async def test_wait_for_reply_returns_first_user_message() -> None:
    parent_ts = "1700000000.000100"
    client = FakeAsyncWebClient(
        replies_script=[
            [{"ts": parent_ts, "user": "UROOT", "text": "parent"}],
            [
                {"ts": parent_ts, "user": "UROOT", "text": "parent"},
                {"ts": "1700000010.000200", "user": "UTEST", "text": "yes go ahead"},
            ],
        ]
    )
    bridge = _bridge(client)
    reply = await bridge.wait_for_reply("DTEST", parent_ts, timeout_seconds=5)
    assert reply is not None
    assert reply["text"] == "yes go ahead"
    assert reply["user"] == "UTEST"


@pytest.mark.asyncio
async def test_wait_for_reply_skips_bot_and_other_users() -> None:
    parent_ts = "1700000000.000100"
    client = FakeAsyncWebClient(
        replies_script=[
            [
                {"ts": parent_ts, "user": "UROOT", "text": "parent"},
                {"ts": "1700000005.000150", "user": "UOTHER", "text": "not me"},
                {"ts": "1700000006.000160", "bot_id": "B123", "text": "bot ping"},
            ],
            [
                {"ts": parent_ts, "user": "UROOT", "text": "parent"},
                {"ts": "1700000010.000200", "user": "UTEST", "text": "real answer"},
            ],
        ]
    )
    bridge = _bridge(client)
    reply = await bridge.wait_for_reply("DTEST", parent_ts, timeout_seconds=5)
    assert reply is not None
    assert reply["text"] == "real answer"


@pytest.mark.asyncio
async def test_wait_for_reply_times_out() -> None:
    client = FakeAsyncWebClient()  # default script: only parent ever appears
    bridge = _bridge(client, poll_interval=0.01)
    reply = await bridge.wait_for_reply("DTEST", "1700000000.000100", timeout_seconds=0)
    assert reply is None


@pytest.mark.asyncio
async def test_wait_for_reply_tolerates_transient_api_errors() -> None:
    parent_ts = "1700000000.000100"
    client = FakeAsyncWebClient(
        replies_script=[
            [
                {"ts": parent_ts, "user": "UROOT", "text": "parent"},
                {"ts": "1700000010.000200", "user": "UTEST", "text": "ok"},
            ],
        ]
    )
    client.fail_replies_first_n = 2
    bridge = _bridge(client)
    reply = await bridge.wait_for_reply("DTEST", parent_ts, timeout_seconds=5)
    assert reply is not None
    assert reply["text"] == "ok"


@pytest.mark.asyncio
async def test_react_swallows_already_reacted() -> None:
    client = FakeAsyncWebClient()
    client.fail_react = True
    bridge = _bridge(client)
    # Should not raise.
    await bridge.react("DTEST", "1700000000.000100", "white_check_mark")


@pytest.mark.asyncio
async def test_append_to_message_edits_via_chat_update() -> None:
    client = FakeAsyncWebClient()
    bridge = _bridge(client)
    await bridge.append_to_message("DTEST", "1700000000.000100", "_(timed out after 5m)_")
    update_calls = [c for c in client.calls if c[0] == "chat_update"]
    assert len(update_calls) == 1
    assert "timed out" in update_calls[0][1]["text"]


@pytest.mark.asyncio
async def test_auth_test_true_on_ok() -> None:
    client = FakeAsyncWebClient()
    bridge = _bridge(client)
    assert await bridge.auth_test() is True


@pytest.mark.asyncio
async def test_post_question_propagates_slack_errors() -> None:
    client = FakeAsyncWebClient()
    client.fail_post = True
    bridge = _bridge(client)
    with pytest.raises(SlackApiError):
        await bridge.post_question("DTEST", text="hi", blocks=[])


@pytest.mark.asyncio
async def test_resolve_user_identity_returns_profile_fields() -> None:
    client = FakeAsyncWebClient()
    bridge = _bridge(client)
    identity = await bridge.resolve_user_identity("UTEST")
    assert identity == {
        "name": "fakeuser",
        "real_name": "Fake User",
        "email": "fake@example.com",
    }
    assert any(c[0] == "users_info" and c[1]["user"] == "UTEST" for c in client.calls)


@pytest.mark.asyncio
async def test_resolve_user_identity_returns_none_on_api_error() -> None:
    client = FakeAsyncWebClient()
    client.fail_users_info = True  # type: ignore[attr-defined]
    bridge = _bridge(client)
    assert await bridge.resolve_user_identity("UNOBODY") is None
