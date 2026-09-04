"""Async Slack client wrapper used by the ask_human tool.

Encapsulates posting, polling for replies, reactions, and message edits, so the
MCP server module stays focused on tool wiring and result shaping.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any, Callable, Iterable

from slack_sdk.errors import SlackApiError
from slack_sdk.web.async_client import AsyncWebClient

from .logging import get_logger

log = get_logger(__name__)


@dataclass(frozen=True)
class PostedMessage:
    """A message we just posted, used as the root of an answer thread."""

    channel: str
    ts: str
    permalink: str


class SlackBridge:
    """Thin async wrapper around slack_sdk's AsyncWebClient.

    Errors from the Slack API are logged and re-raised as SlackApiError unless
    the method is documented to swallow them (reactions, permalink lookup, edits).
    The polling method returns None on timeout rather than raising.
    """

    def __init__(
        self,
        token: str,
        target_user_id: str,
        poll_interval: float = 3.0,
        client: AsyncWebClient | None = None,
        extra_user_ids: set[str] | list[str] | None = None,
    ) -> None:
        self._client = client or AsyncWebClient(token=token)
        # Primary account: used for the @-mention and the startup identity check.
        self.target_user_id = target_user_id
        # All accounts whose replies count (primary + any extra approver IDs),
        # so a user with more than one Slack account is honored from either.
        self.target_user_ids = {target_user_id} | {
            u for u in (extra_user_ids or []) if u
        }
        self.poll_interval = poll_interval

    @property
    def client(self) -> AsyncWebClient:
        return self._client

    async def auth_test(self) -> bool:
        """Return True if the bot token is currently valid."""
        try:
            resp = await self._client.auth_test()
            return bool(resp.get("ok"))
        except SlackApiError as e:
            log.warning("slack.auth_test.failed", error=str(e))
            return False

    async def resolve_user_identity(self, user_id: str) -> dict[str, str] | None:
        """Look up a Slack user's display info. Returns dict or None on failure.

        Used at startup to surface SLACK_USER_ID misconfigurations: if the
        configured ID doesn't match the account that actually replies, every
        reply is silently filtered out and calls hang until timeout.
        """
        try:
            resp = await self._client.users_info(user=user_id)
            if not resp.get("ok"):
                return None
            user = resp.get("user", {}) or {}
            profile = user.get("profile", {}) or {}
            return {
                "name": user.get("name", "") or "",
                "real_name": profile.get("real_name", "") or "",
                "email": profile.get("email", "") or "",
            }
        except SlackApiError as e:
            log.warning("slack.users_info.failed", error=str(e), user_id=user_id)
            return None

    async def post_question(
        self,
        channel: str,
        text: str,
        blocks: list[dict[str, Any]] | None = None,
    ) -> PostedMessage:
        """Post the question message. Raises SlackApiError on failure."""
        resp = await self._client.chat_postMessage(
            channel=channel,
            text=text,
            blocks=blocks,
            unfurl_links=False,
            unfurl_media=False,
        )
        ch = resp["channel"]
        ts = resp["ts"]
        permalink = await self._get_permalink(ch, ts)
        return PostedMessage(channel=ch, ts=ts, permalink=permalink)

    async def _get_permalink(self, channel: str, ts: str) -> str:
        try:
            resp = await self._client.chat_getPermalink(channel=channel, message_ts=ts)
            return resp.get("permalink", "") or ""
        except SlackApiError as e:
            log.warning("slack.permalink.failed", error=str(e), channel=channel, ts=ts)
            return ""

    async def wait_for_reply(
        self,
        channel: str,
        thread_ts: str,
        timeout_seconds: int,
        predicate: Callable[[dict[str, Any]], bool] | None = None,
    ) -> dict[str, Any] | None:
        """Poll the thread until the target user replies, or timeout elapses.

        Returns the matching message dict, or None on timeout. Bot messages and
        messages from other users are ignored. The thread parent is skipped.

        When `predicate` is provided, replies that fail the predicate are also
        skipped — used by the PreToolUse approval-wait path to ignore replies
        that don't parse as a yes/no decision while still letting the user
        clarify with a subsequent message.
        """
        deadline = time.monotonic() + max(0, timeout_seconds)
        consecutive_errors = 0
        while True:
            now = time.monotonic()
            if now >= deadline:
                return None
            try:
                resp = await self._client.conversations_replies(
                    channel=channel,
                    ts=thread_ts,
                    limit=200,
                )
                consecutive_errors = 0
                match = self._first_user_reply(
                    resp.get("messages", []), thread_ts, predicate=predicate
                )
                if match is not None:
                    return match
            except SlackApiError as e:
                consecutive_errors += 1
                log.warning(
                    "slack.poll.error",
                    error=str(e),
                    consecutive_errors=consecutive_errors,
                    channel=channel,
                    thread_ts=thread_ts,
                )
                # Back off briefly on repeated API errors but keep trying until
                # the caller-specified timeout fires.
                if consecutive_errors >= 5:
                    await asyncio.sleep(min(30.0, self.poll_interval * 4))

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            await asyncio.sleep(min(self.poll_interval, remaining))

    def _first_user_reply(
        self,
        messages: Iterable[dict[str, Any]],
        thread_ts: str,
        predicate: Callable[[dict[str, Any]], bool] | None = None,
    ) -> dict[str, Any] | None:
        for msg in messages:
            if msg.get("ts") == thread_ts:
                continue
            if msg.get("bot_id"):
                continue
            if msg.get("subtype") in {"bot_message", "message_changed", "message_deleted"}:
                continue
            if msg.get("user") not in self.target_user_ids:
                continue
            if predicate is not None and not predicate(msg):
                continue
            return msg
        return None

    async def react(self, channel: str, ts: str, name: str) -> None:
        """Add a reaction. Failures are logged and swallowed."""
        try:
            await self._client.reactions_add(channel=channel, timestamp=ts, name=name)
        except SlackApiError as e:
            # already_reacted is benign; keep noise low.
            err = e.response.get("error") if e.response else str(e)
            if err != "already_reacted":
                log.warning("slack.react.failed", error=err, channel=channel, ts=ts, name=name)

    async def append_to_message(self, channel: str, ts: str, append_text: str) -> None:
        """Append text to an existing message. Failures are logged and swallowed."""
        try:
            history = await self._client.conversations_history(
                channel=channel,
                latest=ts,
                oldest=ts,
                inclusive=True,
                limit=1,
            )
            messages = history.get("messages", [])
            if not messages:
                return
            original = messages[0]
            existing = original.get("text", "")
            new_text = f"{existing}\n{append_text}".strip()
            await self._client.chat_update(channel=channel, ts=ts, text=new_text)
        except SlackApiError as e:
            log.warning("slack.edit.failed", error=str(e), channel=channel, ts=ts)

    async def resolve_approval_buttons(
        self, channel: str, ts: str, result_text: str
    ) -> None:
        """Strip the interactive approval buttons from a message and append a
        result line, so a tapped prompt can't be tapped (or text-replied) again.

        Fetches the message's existing blocks, drops the `approval_actions`
        block (the button row) and the reply-hint block, and appends a context
        block stating the chosen verdict. Failures are logged and swallowed —
        the decision is already recorded server-side, so a failed edit only
        leaves a stale button row, not a wrong outcome.
        """
        try:
            history = await self._client.conversations_history(
                channel=channel,
                latest=ts,
                oldest=ts,
                inclusive=True,
                limit=1,
            )
            messages = history.get("messages", [])
            if not messages:
                return
            original = messages[0]
            blocks = original.get("blocks") or []
            # Drop every interactive (button) row so the message can't be tapped
            # or text-replied again, regardless of which row it was (approval
            # buttons or an AFK control button on an announcement).
            kept = [b for b in blocks if b.get("type") != "actions"]
            kept.append(
                {
                    "type": "context",
                    "elements": [{"type": "mrkdwn", "text": result_text}],
                }
            )
            await self._client.chat_update(
                channel=channel,
                ts=ts,
                text=original.get("text", ""),
                blocks=kept,
            )
        except SlackApiError as e:
            log.warning("slack.resolve_buttons.failed", error=str(e), channel=channel, ts=ts)

    async def update_message(
        self,
        channel: str,
        ts: str,
        text: str,
        blocks: list[dict[str, Any]] | None = None,
    ) -> None:
        """Replace a message's text + blocks in place. Raises on API failure so
        the caller can fall back to reposting (used by the AFK control message,
        which is updated rather than re-posted on each toggle)."""
        await self._client.chat_update(
            channel=channel, ts=ts, text=text, blocks=blocks
        )

    async def pin_message(self, channel: str, ts: str) -> None:
        """Pin a message (best-effort). Needs the `pins:write` scope; if it's
        missing the failure is logged and swallowed so the control message still
        works unpinned (the user can pin it manually)."""
        try:
            await self._client.pins_add(channel=channel, timestamp=ts)
        except SlackApiError as e:
            err = e.response.get("error") if e.response else str(e)
            if err not in {"already_pinned"}:
                log.warning("slack.pin.failed", error=err, channel=channel, ts=ts)

    async def find_message_by_text(self, channel: str, needle: str) -> str | None:
        """Return the ts of the most recent channel message whose text contains
        `needle`, or None. Lets a post-restart toggle find the existing AFK
        control message (by an embedded sentinel) and reuse it instead of
        posting a duplicate. Failures are logged and return None."""
        try:
            resp = await self._client.conversations_history(channel=channel, limit=50)
            for msg in resp.get("messages", []):
                if needle in (msg.get("text") or ""):
                    return msg.get("ts")
        except SlackApiError as e:
            log.warning("slack.find_message.failed", error=str(e), channel=channel)
        return None
