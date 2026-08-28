"""Comprehensive tests for AsyncSMTPSender: message building, retries, routing, cleanup."""

import asyncio
from unittest.mock import AsyncMock, patch

import aiosmtplib
import pytest

from msmtp import (
    AsyncSMTPSender,
    LoadBalancingStrategy,
    SMTPServerConfig,
)
from msmtp.exceptions import SMTPConnectionError

# ---------------------------------------------------------------------------
# Message building
# ---------------------------------------------------------------------------


@pytest.fixture
async def sender(smtp_server):
    """Sender instance without opening any real connections."""
    s = AsyncSMTPSender([smtp_server])
    yield s
    await s.close()


class TestBuildMessage:
    def test_text_only_body(self, sender):
        msg = sender._build_message(
            from_addr="sender@example.com",
            to_addrs=["to@example.com"],
            subject="Hi",
            body_text="hello",
        )
        assert msg.get_content().strip() == "hello"
        assert msg["From"] == "sender@example.com"
        assert msg["To"] == "to@example.com"

    def test_html_only_body(self, sender):
        msg = sender._build_message(
            from_addr="sender@example.com",
            to_addrs=["to@example.com"],
            subject="Hi",
            body_html="<b>hi</b>",
        )
        assert msg.get_content_type() == "text/html"

    def test_multipart_text_and_html(self, sender):
        msg = sender._build_message(
            from_addr="sender@example.com",
            to_addrs=["to@example.com"],
            subject="Hi",
            body_text="plain",
            body_html="<b>rich</b>",
        )
        assert msg.is_multipart()

    def test_no_body_defaults_to_empty(self, sender):
        msg = sender._build_message(
            from_addr="sender@example.com",
            to_addrs=["to@example.com"],
            subject="Hi",
        )
        assert msg.get_content().strip() == ""

    def test_cc_bcc_reply_to_headers(self, sender):
        msg = sender._build_message(
            from_addr="sender@example.com",
            to_addrs=["to@example.com"],
            subject="Hi",
            cc=["cc@example.com"],
            bcc=["bcc@example.com"],
            reply_to="reply@example.com",
        )
        assert msg["Cc"] == "cc@example.com"
        assert msg["Bcc"] == "bcc@example.com"
        assert msg["Reply-To"] == "reply@example.com"

    def test_custom_headers_sanitized(self, sender):
        msg = sender._build_message(
            from_addr="sender@example.com",
            to_addrs=["to@example.com"],
            subject="Hi",
            headers={"X-Campaign": "promo\r\nBcc: attacker@evil.com"},
        )
        assert "\r" not in msg["X-Campaign"]
        assert "\n" not in msg["X-Campaign"]

    def test_subject_sanitized(self, sender):
        msg = sender._build_message(
            from_addr="sender@example.com",
            to_addrs=["to@example.com"],
            subject="Hi\nBcc: attacker@evil.com",
        )
        assert "\n" not in msg["Subject"]

    def test_message_id_and_date_present(self, sender):
        msg = sender._build_message(
            from_addr="sender@example.com",
            to_addrs=["to@example.com"],
            subject="Hi",
        )
        assert msg["Message-ID"]
        assert msg["Date"]

    def test_unicode_subject_and_body_preserved(self, sender):
        msg = sender._build_message(
            from_addr="sender@example.com",
            to_addrs=["to@example.com"],
            subject="héllo 🎉",
            body_text="wörld 🌍",
        )
        assert "héllo" in msg["Subject"]
        assert "🎉" in msg["Subject"]
        assert "wörld" in msg.get_content()


# ---------------------------------------------------------------------------
# send() validation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestSendValidation:
    async def test_invalid_from_addr_rejected(self, sender):
        result = await sender.send(
            from_addr="not-an-email",
            to_addrs=["to@example.com"],
            subject="Hi",
            body_text="hi",
        )
        assert result.success is False
        assert result.attempts == 0
        assert "validation failed" in result.error.lower()

    async def test_invalid_recipient_rejected(self, sender):
        result = await sender.send(
            from_addr="sender@example.com",
            to_addrs=["not-an-email"],
            subject="Hi",
            body_text="hi",
        )
        assert result.success is False
        assert result.attempts == 0

    async def test_invalid_cc_rejected(self, sender):
        result = await sender.send(
            from_addr="sender@example.com",
            to_addrs=["to@example.com"],
            subject="Hi",
            body_text="hi",
            cc=["bad-cc"],
        )
        assert result.success is False

    async def test_invalid_bcc_rejected(self, sender):
        result = await sender.send(
            from_addr="sender@example.com",
            to_addrs=["to@example.com"],
            subject="Hi",
            body_text="hi",
            bcc=["bad-bcc"],
        )
        assert result.success is False

    async def test_invalid_reply_to_rejected(self, sender):
        result = await sender.send(
            from_addr="sender@example.com",
            to_addrs=["to@example.com"],
            subject="Hi",
            body_text="hi",
            reply_to="bad-reply-to",
        )
        assert result.success is False


# ---------------------------------------------------------------------------
# send() success / retry / failure paths
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestSendRetryBehavior:
    async def test_success_on_first_attempt(self, smtp_server, mock_smtp):
        with patch("msmtp.connection_pool.aiosmtplib.SMTP", return_value=mock_smtp):
            async with AsyncSMTPSender([smtp_server]) as sndr:
                result = await sndr.send(
                    from_addr="sender@example.com",
                    to_addrs=["to@example.com"],
                    subject="Hi",
                    body_text="hi",
                )

        assert result.success is True
        assert result.attempts == 1
        assert result.message_id == "message-id-123"

    async def test_transient_error_retries_then_succeeds(self, smtp_server, mock_smtp):
        mock_smtp.send_message = AsyncMock(
            side_effect=[
                aiosmtplib.SMTPConnectError("timeout"),
                ({}, "msg-id"),
            ]
        )

        with (
            patch("msmtp.connection_pool.aiosmtplib.SMTP", return_value=mock_smtp),
            patch("msmtp.sender.asyncio.sleep", new=AsyncMock()),
        ):
            async with AsyncSMTPSender([smtp_server], max_retries=3) as sndr:
                result = await sndr.send(
                    from_addr="sender@example.com",
                    to_addrs=["to@example.com"],
                    subject="Hi",
                    body_text="hi",
                )

        assert result.success is True
        assert result.attempts == 2

    async def test_non_transient_error_fails_without_retry(self, smtp_server, mock_smtp):
        mock_smtp.send_message = AsyncMock(
            side_effect=aiosmtplib.SMTPResponseException(550, "mailbox unavailable")
        )

        with (
            patch("msmtp.connection_pool.aiosmtplib.SMTP", return_value=mock_smtp),
            patch("msmtp.sender.asyncio.sleep", new=AsyncMock()) as mock_sleep,
        ):
            async with AsyncSMTPSender([smtp_server], max_retries=5) as sndr:
                result = await sndr.send(
                    from_addr="sender@example.com",
                    to_addrs=["to@example.com"],
                    subject="Hi",
                    body_text="hi",
                )

        assert result.success is False
        assert result.attempts == 1
        mock_sleep.assert_not_awaited()

    async def test_exhausts_retries_and_fails(self, smtp_server, mock_smtp):
        mock_smtp.send_message = AsyncMock(side_effect=aiosmtplib.SMTPConnectError("still down"))

        with (
            patch("msmtp.connection_pool.aiosmtplib.SMTP", return_value=mock_smtp),
            patch("msmtp.sender.asyncio.sleep", new=AsyncMock()),
        ):
            async with AsyncSMTPSender([smtp_server], max_retries=3) as sndr:
                result = await sndr.send(
                    from_addr="sender@example.com",
                    to_addrs=["to@example.com"],
                    subject="Hi",
                    body_text="hi",
                )

        assert result.success is False
        assert result.attempts == 3

    async def test_auth_error_fails_without_retry(self, smtp_server, mock_smtp):
        mock_smtp.login = AsyncMock(
            side_effect=aiosmtplib.SMTPAuthenticationError(535, "bad creds")
        )

        with patch("msmtp.connection_pool.aiosmtplib.SMTP", return_value=mock_smtp):
            async with AsyncSMTPSender([smtp_server], max_retries=5) as sndr:
                result = await sndr.send(
                    from_addr="sender@example.com",
                    to_addrs=["to@example.com"],
                    subject="Hi",
                    body_text="hi",
                )

        assert result.success is False
        assert result.attempts == 1

    async def test_partial_recipient_errors_treated_as_failure(self, smtp_server, mock_smtp):
        mock_smtp.send_message = AsyncMock(
            return_value=({"to@example.com": (550, b"mailbox unavailable")}, "250 OK")
        )

        with (
            patch("msmtp.connection_pool.aiosmtplib.SMTP", return_value=mock_smtp),
            patch("msmtp.sender.asyncio.sleep", new=AsyncMock()),
        ):
            async with AsyncSMTPSender([smtp_server], max_retries=1) as sndr:
                result = await sndr.send(
                    from_addr="sender@example.com",
                    to_addrs=["to@example.com"],
                    subject="Hi",
                    body_text="hi",
                )

        assert result.success is False
        assert "Send errors" in result.error

    async def test_zero_max_retries_returns_fallback_result(self, smtp_server):
        async with AsyncSMTPSender([smtp_server], max_retries=0) as sndr:
            result = await sndr.send(
                from_addr="sender@example.com",
                to_addrs=["to@example.com"],
                subject="Hi",
                body_text="hi",
            )

        assert result.success is False
        assert result.error == "Max retries exceeded"
        assert result.attempts == 0

    async def test_connection_failure_trips_circuit_breaker(self, smtp_server, mock_smtp):
        mock_smtp.connect = AsyncMock(side_effect=OSError("connection refused"))

        with (
            patch("msmtp.connection_pool.aiosmtplib.SMTP", return_value=mock_smtp),
            patch("msmtp.sender.asyncio.sleep", new=AsyncMock()),
        ):
            async with AsyncSMTPSender([smtp_server], max_retries=1) as sndr:
                await sndr.send(
                    from_addr="sender@example.com",
                    to_addrs=["to@example.com"],
                    subject="Hi",
                    body_text="hi",
                )

                pool = sndr._pools[smtp_server.name]
                assert pool.runtime.circuit_breaker._stats.failure_count == 1


# ---------------------------------------------------------------------------
# Server selection / load balancing
# ---------------------------------------------------------------------------


class TestSelectServer:
    def test_priority_strategy_picks_lowest_priority_number(self):
        servers = [
            SMTPServerConfig(name="low", host="a.example.com", priority=5),
            SMTPServerConfig(name="high", host="b.example.com", priority=1),
        ]
        sndr = AsyncSMTPSender(servers, strategy=LoadBalancingStrategy.PRIORITY)
        pool = sndr._select_server("from@example.com")
        assert pool.server.name == "high"

    def test_round_robin_cycles_through_servers(self):
        servers = [
            SMTPServerConfig(name="s1", host="a.example.com"),
            SMTPServerConfig(name="s2", host="b.example.com"),
        ]
        sndr = AsyncSMTPSender(servers, strategy=LoadBalancingStrategy.ROUND_ROBIN)
        picks = [sndr._select_server("from@example.com").server.name for _ in range(4)]
        assert picks == ["s1", "s2", "s1", "s2"]

    def test_weighted_strategy_uses_weight_distribution(self):
        servers = [
            SMTPServerConfig(name="s1", host="a.example.com", weight=1),
            SMTPServerConfig(name="s2", host="b.example.com", weight=9),
        ]
        sndr = AsyncSMTPSender(servers, strategy=LoadBalancingStrategy.WEIGHTED)

        with patch("random.uniform", return_value=5.0):
            pool = sndr._select_server("from@example.com")

        assert pool.server.name == "s2"  # cumulative weight of s1=1 < 5 <= 1+9

    def test_weighted_strategy_zero_total_weight_falls_back_to_first(self):
        servers = [
            SMTPServerConfig(name="s1", host="a.example.com", weight=0),
            SMTPServerConfig(name="s2", host="b.example.com", weight=0),
        ]
        sndr = AsyncSMTPSender(servers, strategy=LoadBalancingStrategy.WEIGHTED)
        pool = sndr._select_server("from@example.com")
        assert pool.server.name == "s1"

    def test_raises_when_all_circuits_open(self):
        servers = [SMTPServerConfig(name="s1", host="a.example.com")]
        sndr = AsyncSMTPSender(servers)
        sndr._pools["s1"].runtime.circuit_breaker.force_open()

        with pytest.raises(SMTPConnectionError, match="No available SMTP servers"):
            sndr._select_server("from@example.com")


# ---------------------------------------------------------------------------
# Bulk sending
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestSendBulk:
    async def test_aggregates_success_and_failure(self, smtp_server, mock_smtp):
        async def send_side_effect(msg):
            if "fail" in msg["To"]:
                raise aiosmtplib.SMTPResponseException(550, "rejected")
            return ({}, "ok")

        mock_smtp.send_message = AsyncMock(side_effect=send_side_effect)

        with patch("msmtp.connection_pool.aiosmtplib.SMTP", return_value=mock_smtp):
            async with AsyncSMTPSender([smtp_server], max_retries=1) as sndr:
                emails = [
                    {
                        "from_addr": "sender@example.com",
                        "to_addrs": ["ok@example.com"],
                        "subject": "Hi",
                        "body_text": "hi",
                    },
                    {
                        "from_addr": "sender@example.com",
                        "to_addrs": ["fail@example.com"],
                        "subject": "Hi",
                        "body_text": "hi",
                    },
                ]
                result = await sndr.send_bulk(emails, concurrency=2)

        assert result.total == 2
        assert result.success_count == 1
        assert result.failed_count == 1
        assert result.success_rate == 50.0

    async def test_concurrency_limit_respected(self, smtp_server, mock_smtp):
        in_flight = 0
        max_in_flight = 0
        lock = asyncio.Lock()

        async def send_side_effect(msg):
            nonlocal in_flight, max_in_flight
            async with lock:
                in_flight += 1
                max_in_flight = max(max_in_flight, in_flight)
            await asyncio.sleep(0.02)
            async with lock:
                in_flight -= 1
            return ({}, "ok")

        mock_smtp.send_message = AsyncMock(side_effect=send_side_effect)

        with patch("msmtp.connection_pool.aiosmtplib.SMTP", return_value=mock_smtp):
            async with AsyncSMTPSender([smtp_server], max_retries=1) as sndr:
                emails = [
                    {
                        "from_addr": "sender@example.com",
                        "to_addrs": [f"user{i}@example.com"],
                        "subject": "Hi",
                        "body_text": "hi",
                    }
                    for i in range(10)
                ]
                await sndr.send_bulk(emails, concurrency=3)

        assert max_in_flight <= 3


# ---------------------------------------------------------------------------
# Cleanup
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestClose:
    async def test_close_closes_all_pools(self, smtp_server, mock_smtp):
        with patch("msmtp.connection_pool.aiosmtplib.SMTP", return_value=mock_smtp):
            sndr = AsyncSMTPSender([smtp_server])
            await sndr.send(
                from_addr="sender@example.com",
                to_addrs=["to@example.com"],
                subject="Hi",
                body_text="hi",
            )
            await sndr.close()

        mock_smtp.quit.assert_awaited()

    async def test_close_survives_pool_close_errors(self, smtp_server):
        sndr = AsyncSMTPSender([smtp_server])
        pool = sndr._pools[smtp_server.name]
        pool.close_all = AsyncMock(side_effect=Exception("boom"))

        # Should not raise even though the pool's close_all() fails.
        await sndr.close()

    async def test_context_manager_closes_on_exit(self, smtp_server):
        async with AsyncSMTPSender([smtp_server]) as sndr:
            pool = sndr._pools[smtp_server.name]
            pool.close_all = AsyncMock()

        pool.close_all.assert_awaited_once()
