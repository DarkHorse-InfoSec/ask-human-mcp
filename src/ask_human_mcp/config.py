"""Runtime configuration loaded from environment variables (and optional .env file)."""

from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Configuration loaded from environment.

    Required:
        slack_bot_token: xoxb-... bot token for the Slack app.
        slack_channel_id: Default channel/DM ID where questions are posted.
        slack_user_id: Slack user ID of the human who answers (used to filter replies).

    Optional:
        ask_human_shared_secret: Bearer secret gating POST /notify and POST /afk.
        host, port: HTTP bind address for the MCP server.
        poll_interval_seconds: How often to poll conversations.replies.
        max_timeout_seconds: Hard cap on per-call timeout to prevent runaway waits.
        log_level: Structlog/standard logging level.
    """

    slack_bot_token: str = Field(..., description="Slack bot token (xoxb-...)")
    slack_channel_id: str = Field(..., description="Default Slack channel or DM ID")
    slack_user_id: str = Field(..., description="Slack user ID of the human responder")

    # Slack app signing secret (Basic Information -> App Credentials). Required
    # to verify interactive-button (Block Kit) callbacks hitting
    # /slack/interactions. When unset, the interactions endpoint refuses every
    # request (it cannot authenticate the caller) and approvals fall back to
    # threaded text replies. Never act on an unsigned interaction.
    slack_signing_secret: str = Field(
        "", description="Slack app signing secret for verifying interaction callbacks"
    )

    # Additional Slack user IDs authorized to approve, besides slack_user_id.
    # Comma-separated. Use when you have more than one Slack account in the same
    # workspace (e.g. a desktop account and a separate mobile account) so taps
    # and replies from either are honored. Each is a security-equivalent
    # approver, so only add IDs you control.
    slack_approver_ids: str = Field(
        "", description="Comma-separated extra Slack user IDs authorized to approve"
    )

    # Shared secret required on the mutating HTTP routes (POST /notify and
    # POST /afk). The local hooks present it as `Authorization: Bearer <secret>`.
    # When unset, those routes refuse every request (503) instead of running
    # unauthenticated: the server is reachable from the public internet through
    # the reverse proxy, and an open /notify lets anyone post into the Slack
    # workspace, park a session on a fabricated approval prompt, or flip AFK
    # state. Same fail-closed posture as slack_signing_secret above. Generate
    # with `openssl rand -hex 32`. Read-only routes (/health, GET /afk) stay
    # open; they leak a boolean and a connection status.
    ask_human_shared_secret: str = Field(
        "",
        description="Shared secret presented as Authorization: Bearer on POST /notify and POST /afk",
    )

    host: str = Field("127.0.0.1", description="HTTP bind host")
    port: int = Field(8765, description="HTTP bind port")

    poll_interval_seconds: float = Field(3.0, ge=0.01, le=30.0)
    max_timeout_seconds: int = Field(14400, ge=60, le=86400)
    log_level: str = Field("INFO")

    # Minimum seconds between push notifications from the same Claude Code
    # session. Prevents one paused session from spamming Slack with a flood of
    # permission prompts. Default 300 (5 min). Set to 0 to disable.
    notify_cooldown_seconds: int = Field(300, ge=0, le=86400)

    # Public hostnames that may reach the MCP through a reverse proxy.
    # Localhost is always permitted. Required when fronting the server with
    # HTTPS on a public hostname (the SDK rejects unknown Host headers as a
    # DNS rebinding mitigation). Comma-separated.
    allowed_hosts: str = Field("", description="Comma-separated extra Host header values")

    @property
    def allowed_hosts_list(self) -> list[str]:
        """Resolved list of allowed Host header values, including localhost."""
        base = ["127.0.0.1:*", "localhost:*", "[::1]:*"]
        extras = [h.strip() for h in self.allowed_hosts.split(",") if h.strip()]
        return base + extras

    @property
    def approver_id_set(self) -> set[str]:
        """Every Slack user ID whose taps/replies are authorized: the primary
        slack_user_id plus any slack_approver_ids. Used by the interaction owner
        check and the reply filter so either of the user's accounts is honored."""
        ids: set[str] = set()
        if self.slack_user_id:
            ids.add(self.slack_user_id)
        ids.update(i.strip() for i in self.slack_approver_ids.split(",") if i.strip())
        return ids

    @property
    def approver_ids_ordered(self) -> list[str]:
        """Authorized approver IDs, primary slack_user_id first, then extras,
        de-duplicated and order-preserving. Notifications @-mention every one of
        these so the push lands on whichever account the human is currently
        watching (a user with two Slack accounts in the same workspace gets
        pinged on both). Answering/tap authority already spans all of them via
        approver_id_set; this makes the *notification* span them too."""
        ordered: list[str] = []
        for uid in [self.slack_user_id, *self.slack_approver_ids.split(",")]:
            uid = uid.strip()
            if uid and uid not in ordered:
                ordered.append(uid)
        return ordered

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )


def load_settings() -> Settings:
    """Load settings, raising a clear error if required vars are missing."""
    return Settings()  # type: ignore[call-arg]
