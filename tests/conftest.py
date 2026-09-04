"""Shared pytest fixtures for ask-human-mcp."""

from __future__ import annotations

import os

import pytest


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Provide deterministic settings env so config.load_settings() works in tests."""
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test-token")
    monkeypatch.setenv("SLACK_CHANNEL_ID", "DTEST")
    monkeypatch.setenv("SLACK_USER_ID", "UTEST")
    monkeypatch.setenv("POLL_INTERVAL_SECONDS", "0.01")
    monkeypatch.setenv("MAX_TIMEOUT_SECONDS", "60")
    monkeypatch.setenv("LOG_LEVEL", "WARNING")
    # Make sure no real .env on disk leaks in.
    if os.path.exists(".env"):
        monkeypatch.setenv("PYDANTIC_SETTINGS_DOTENV_DISABLED", "1")
