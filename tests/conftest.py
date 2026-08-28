"""Shared pytest fixtures for the msmtp test suite."""

from unittest.mock import AsyncMock

import aiosmtplib
import pytest

from msmtp import SMTPServerConfig


@pytest.fixture
def smtp_server() -> SMTPServerConfig:
    """A single, minimally-configured SMTP server."""
    return SMTPServerConfig(
        host="smtp.example.com",
        port=587,
        username="test@example.com",
        password="test-password",
        use_tls=True,
    )


@pytest.fixture
def mock_smtp() -> AsyncMock:
    """A mocked aiosmtplib.SMTP connection with sane default return values."""
    mock = AsyncMock(spec=aiosmtplib.SMTP)
    mock.send_message = AsyncMock(return_value=({}, "message-id-123"))
    mock.connect = AsyncMock()
    mock.starttls = AsyncMock()
    mock.login = AsyncMock()
    mock.quit = AsyncMock()
    mock.noop = AsyncMock()
    return mock
