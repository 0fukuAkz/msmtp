"""Edge case tests: malformed SMTP responses, network failures, and unusual input."""

from unittest.mock import AsyncMock, patch

import aiosmtplib
import pytest

from msmtp import AsyncSMTPSender
from msmtp.validation import sanitize_header_value, sanitize_subject, validate_email_address


@pytest.mark.asyncio
class TestMalformedResponses:
    async def test_partial_recipient_rejection_is_a_failure(self, smtp_server, mock_smtp):
        mock_smtp.send_message = AsyncMock(
            return_value=(
                {"bad@example.com": (550, b"5.1.1 User unknown")},
                "250 Message accepted",
            )
        )

        with patch("msmtp.connection_pool.aiosmtplib.SMTP", return_value=mock_smtp), patch(
            "msmtp.sender.asyncio.sleep", new=AsyncMock()
        ):
            async with AsyncSMTPSender([smtp_server], max_retries=1) as sender:
                result = await sender.send(
                    from_addr="sender@example.com",
                    to_addrs=["bad@example.com"],
                    subject="Hi",
                    body_text="hi",
                )

        assert result.success is False

    async def test_empty_errors_dict_with_empty_response_is_success(self, smtp_server, mock_smtp):
        mock_smtp.send_message = AsyncMock(return_value=({}, ""))

        with patch("msmtp.connection_pool.aiosmtplib.SMTP", return_value=mock_smtp):
            async with AsyncSMTPSender([smtp_server]) as sender:
                result = await sender.send(
                    from_addr="sender@example.com",
                    to_addrs=["to@example.com"],
                    subject="Hi",
                    body_text="hi",
                )

        assert result.success is True
        assert result.message_id == ""

    async def test_permanent_smtp_response_error_no_retry(self, smtp_server, mock_smtp):
        mock_smtp.send_message = AsyncMock(
            side_effect=aiosmtplib.SMTPResponseException(552, "mailbox full")
        )

        with patch("msmtp.connection_pool.aiosmtplib.SMTP", return_value=mock_smtp), patch(
            "msmtp.sender.asyncio.sleep", new=AsyncMock()
        ) as mock_sleep:
            async with AsyncSMTPSender([smtp_server], max_retries=4) as sender:
                result = await sender.send(
                    from_addr="sender@example.com",
                    to_addrs=["to@example.com"],
                    subject="Hi",
                    body_text="hi",
                )

        assert result.success is False
        assert result.attempts == 1
        mock_sleep.assert_not_awaited()

    async def test_transient_smtp_response_error_retries(self, smtp_server, mock_smtp):
        mock_smtp.send_message = AsyncMock(
            side_effect=aiosmtplib.SMTPResponseException(451, "temporary local problem")
        )

        with patch("msmtp.connection_pool.aiosmtplib.SMTP", return_value=mock_smtp), patch(
            "msmtp.sender.asyncio.sleep", new=AsyncMock()
        ) as mock_sleep:
            async with AsyncSMTPSender([smtp_server], max_retries=3) as sender:
                result = await sender.send(
                    from_addr="sender@example.com",
                    to_addrs=["to@example.com"],
                    subject="Hi",
                    body_text="hi",
                )

        assert result.success is False
        assert result.attempts == 3
        assert mock_sleep.await_count == 2  # slept between attempts 1->2 and 2->3


@pytest.mark.asyncio
class TestNetworkFailures:
    async def test_timeout_error_is_treated_as_transient(self, smtp_server, mock_smtp):
        mock_smtp.send_message = AsyncMock(side_effect=TimeoutError("timed out"))

        with patch("msmtp.connection_pool.aiosmtplib.SMTP", return_value=mock_smtp), patch(
            "msmtp.sender.asyncio.sleep", new=AsyncMock()
        ):
            async with AsyncSMTPSender([smtp_server], max_retries=2) as sender:
                result = await sender.send(
                    from_addr="sender@example.com",
                    to_addrs=["to@example.com"],
                    subject="Hi",
                    body_text="hi",
                )

        assert result.success is False
        assert result.attempts == 2

    async def test_connection_reset_mid_send_is_transient(self, smtp_server, mock_smtp):
        mock_smtp.send_message = AsyncMock(
            side_effect=ConnectionResetError("connection reset by peer")
        )

        with patch("msmtp.connection_pool.aiosmtplib.SMTP", return_value=mock_smtp), patch(
            "msmtp.sender.asyncio.sleep", new=AsyncMock()
        ):
            async with AsyncSMTPSender([smtp_server], max_retries=2) as sender:
                result = await sender.send(
                    from_addr="sender@example.com",
                    to_addrs=["to@example.com"],
                    subject="Hi",
                    body_text="hi",
                )

        assert result.success is False
        assert result.attempts == 2

    async def test_server_disconnected_during_connect(self, smtp_server, mock_smtp):
        mock_smtp.connect = AsyncMock(side_effect=aiosmtplib.SMTPServerDisconnected("disconnected"))

        with patch("msmtp.connection_pool.aiosmtplib.SMTP", return_value=mock_smtp), patch(
            "msmtp.sender.asyncio.sleep", new=AsyncMock()
        ):
            async with AsyncSMTPSender([smtp_server], max_retries=1) as sender:
                result = await sender.send(
                    from_addr="sender@example.com",
                    to_addrs=["to@example.com"],
                    subject="Hi",
                    body_text="hi",
                )

        assert result.success is False

    async def test_recipients_refused_exception(self, smtp_server, mock_smtp):
        refused = aiosmtplib.SMTPRecipientRefused(550, "refused", "to@example.com")
        mock_smtp.send_message = AsyncMock(
            side_effect=aiosmtplib.SMTPRecipientsRefused([refused])
        )

        with patch("msmtp.connection_pool.aiosmtplib.SMTP", return_value=mock_smtp), patch(
            "msmtp.sender.asyncio.sleep", new=AsyncMock()
        ):
            async with AsyncSMTPSender([smtp_server], max_retries=1) as sender:
                result = await sender.send(
                    from_addr="sender@example.com",
                    to_addrs=["to@example.com"],
                    subject="Hi",
                    body_text="hi",
                )

        assert result.success is False


class TestUnusualInput:
    def test_very_long_subject_is_truncated(self):
        subject = "A" * 5000
        result = sanitize_subject(subject)
        assert len(result) == 998

    def test_subject_with_only_whitespace(self):
        assert sanitize_subject("   \n\n   ") == ""

    def test_header_value_with_tabs_and_control_chars(self):
        value = "value\twith\ttabs"
        result = sanitize_header_value(value)
        assert "\t" in result  # Tabs are not stripped, only newlines/nulls.

    def test_email_with_plus_addressing(self):
        validate_email_address("user+tag@example.com")

    def test_email_with_max_length_local_part(self):
        local = "a" * 64
        validate_email_address(f"{local}@example.com")

    def test_email_exceeding_local_part_length_rejected(self):
        local = "a" * 65
        with pytest.raises(ValueError, match="exceeds 64 characters"):
            validate_email_address(f"{local}@example.com")

    def test_email_with_consecutive_dots_rejected(self):
        with pytest.raises(ValueError, match="consecutive dots"):
            validate_email_address("user..name@example.com")

    def test_email_starting_with_dot_rejected(self):
        with pytest.raises(ValueError, match="invalid start character"):
            validate_email_address(".user@example.com")


@pytest.mark.asyncio
class TestBulkEdgeCases:
    async def test_empty_email_list_returns_zero_totals(self, smtp_server):
        async with AsyncSMTPSender([smtp_server]) as sender:
            result = await sender.send_bulk([], concurrency=5)

        assert result.total == 0
        assert result.success_count == 0
        assert result.failed_count == 0
        assert result.success_rate == 0.0

    async def test_bulk_send_with_mixed_valid_and_invalid_emails(self, smtp_server, mock_smtp):
        with patch("msmtp.connection_pool.aiosmtplib.SMTP", return_value=mock_smtp):
            async with AsyncSMTPSender([smtp_server]) as sender:
                emails = [
                    {
                        "from_addr": "sender@example.com",
                        "to_addrs": ["valid@example.com"],
                        "subject": "Hi",
                        "body_text": "hi",
                    },
                    {
                        "from_addr": "sender@example.com",
                        "to_addrs": ["not-an-email"],
                        "subject": "Hi",
                        "body_text": "hi",
                    },
                ]
                result = await sender.send_bulk(emails, concurrency=2)

        assert result.total == 2
        assert result.success_count == 1
        assert result.failed_count == 1
