"""Comprehensive tests for Mercury SMTP with mocking."""

import asyncio
import logging
import ssl
from datetime import datetime, UTC
from unittest.mock import AsyncMock, MagicMock, patch, call

import pytest
import aiosmtplib

from msmtp import (
    AsyncSMTPSender,
    CircuitBreaker,
    CircuitBreakerConfig,
    CircuitState,
    EmailResult,
    LoadBalancingStrategy,
    RateLimiter,
    RateLimiterConfig,
    SMTPServerConfig,
    SMTPConnectionError,
    SMTPAuthenticationError,
    validate_email_address,
    validate_email_list,
    sanitize_subject,
)


class TestEmailValidation:
    """Test email validation utilities."""

    def test_valid_email(self):
        """Test valid email addresses."""
        validate_email_address("user@example.com")
        validate_email_address("test.user+tag@example.co.uk")
        validate_email_address("user123@test-domain.com")

    def test_invalid_email_format(self):
        """Test invalid email formats."""
        with pytest.raises(ValueError, match="Invalid email address format"):
            validate_email_address("not-an-email")
        
        with pytest.raises(ValueError, match="Invalid email address format"):
            validate_email_address("@example.com")
        
        with pytest.raises(ValueError, match="Invalid email address format"):
            validate_email_address("user@")

    def test_header_injection(self):
        """Test detection of header injection attempts."""
        with pytest.raises(ValueError, match="newline"):
            validate_email_address("user@example.com\nBcc: attacker@evil.com")
        
        with pytest.raises(ValueError, match="newline"):
            validate_email_address("user@example.com\r\nCc: attacker@evil.com")

    def test_null_bytes(self):
        """Test detection of null bytes."""
        with pytest.raises(ValueError, match="null bytes"):
            validate_email_address("user@example.com\x00")

    def test_email_list_validation(self):
        """Test email list validation."""
        validate_email_list(["user1@example.com", "user2@example.com"])
        
        with pytest.raises(ValueError):
            validate_email_list(["valid@example.com", "invalid"])

    def test_subject_sanitization(self):
        """Test subject sanitization."""
        assert sanitize_subject("Normal subject") == "Normal subject"
        assert sanitize_subject("Subject\nwith\nnewlines") == "Subject with newlines"
        assert sanitize_subject("Subject\x00with\x00nulls") == "Subjectwithnulls"  # Null bytes removed
        
        # Test truncation
        long_subject = "A" * 1000
        result = sanitize_subject(long_subject)
        assert len(result) == 998
        assert result.endswith("...")


class TestSMTPServerConfig:
    """Test SMTP server configuration."""

    def test_default_config(self):
        """Test default configuration."""
        config = SMTPServerConfig(host="smtp.example.com")
        assert config.port == 587
        assert config.use_tls is True
        assert config.verify_ssl is True
        assert config.name == "smtp.example.com:587"

    def test_insecure_config_warning(self, caplog):
        """Test warning for insecure configuration."""
        with caplog.at_level(logging.WARNING):
            config = SMTPServerConfig(
                host="smtp.example.com",
                use_tls=False,
                use_ssl=False,
            )
        # Structured logging puts details in `extra`, not the log message text.
        assert any(
            record.message == "insecure_smtp_config"
            and getattr(record, "issue", None) == "TLS/SSL disabled"
            for record in caplog.records
        )

    def test_ssl_verification_disabled_warning(self, caplog):
        """Test warning for disabled SSL verification."""
        with caplog.at_level(logging.WARNING):
            config = SMTPServerConfig(
                host="smtp.example.com",
                verify_ssl=False,
            )
        # Structured logging puts details in `extra`, not the log message text.
        assert any(
            record.message == "ssl_verification_disabled"
            and getattr(record, "issue", None) == "SSL certificate verification disabled"
            for record in caplog.records
        )

    def test_password_provider(self):
        """Test password provider."""
        def get_password():
            return "secret-password"
        
        config = SMTPServerConfig(
            host="smtp.example.com",
            password_provider=get_password,
        )
        assert config.get_password() == "secret-password"

    def test_direct_password(self):
        """Test direct password."""
        config = SMTPServerConfig(
            host="smtp.example.com",
            password="direct-password",
        )
        assert config.get_password() == "direct-password"


class TestCircuitBreaker:
    """Test circuit breaker pattern."""

    def test_initial_state(self):
        """Test circuit breaker starts in closed state."""
        cb = CircuitBreaker("test-server")
        assert cb.is_available()
        assert cb._stats.state == CircuitState.CLOSED

    def test_open_on_failures(self):
        """Test circuit opens after threshold failures."""
        cb = CircuitBreaker(
            "test-server",
            CircuitBreakerConfig(failure_threshold=3),
        )
        
        # Record 3 failures
        for i in range(3):
            cb.record_failure(Exception(f"Error {i}"))
        
        # Circuit should be open
        assert not cb.is_available()
        assert cb._stats.state == CircuitState.OPEN

    def test_half_open_after_timeout(self):
        """Test circuit transitions to half-open after timeout."""
        cb = CircuitBreaker(
            "test-server",
            CircuitBreakerConfig(
                failure_threshold=2,
                timeout_seconds=1,
            ),
        )
        
        # Open the circuit
        cb.record_failure(Exception("Error 1"))
        cb.record_failure(Exception("Error 2"))
        assert not cb.is_available()
        
        # Wait for timeout
        import time
        time.sleep(1.1)
        
        # Should transition to half-open
        assert cb._get_current_state() == CircuitState.HALF_OPEN

    def test_close_from_half_open(self):
        """Test circuit closes after successes in half-open state."""
        cb = CircuitBreaker(
            "test-server",
            CircuitBreakerConfig(
                failure_threshold=2,
                success_threshold=2,
                timeout_seconds=0,
            ),
        )
        
        # Open the circuit
        cb.record_failure(Exception("Error 1"))
        cb.record_failure(Exception("Error 2"))
        
        # Force half-open
        cb._stats.state = CircuitState.HALF_OPEN
        
        # Record successes
        cb.record_success()
        cb.record_success()
        
        # Should be closed
        assert cb._stats.state == CircuitState.CLOSED
        assert cb.is_available()

    def test_rolling_window_cleanup(self):
        """Test old failures are removed from rolling window."""
        cb = CircuitBreaker(
            "test-server",
            CircuitBreakerConfig(
                failure_threshold=5,
                monitor_window_seconds=2,
            ),
        )
        
        # Add some failures
        cb.record_failure(Exception("Error 1"))
        cb.record_failure(Exception("Error 2"))
        
        # Wait for window to expire
        import time
        time.sleep(2.1)
        
        # Add more failures (should not trigger open)
        cb.record_failure(Exception("Error 3"))
        
        # Circuit should still be closed (old failures cleaned up)
        assert cb.is_available()

    def test_error_message_tracking(self):
        """Test circuit breaker tracks recent error messages."""
        cb = CircuitBreaker("test-server")
        
        errors = [
            Exception("Database timeout"),
            Exception("Network error"),
            Exception("Database timeout"),  # Duplicate
        ]
        
        for err in errors:
            cb.record_failure(err)
        
        # Should track unique messages
        error_msgs = cb._stats.last_error_messages
        assert len(error_msgs) == 2  # Only unique messages
        assert any("Database timeout" in msg for msg in error_msgs)
        assert any("Network error" in msg for msg in error_msgs)


@pytest.mark.asyncio
class TestAsyncSMTPSender:
    """Test async SMTP sender with mocks."""

    @pytest.fixture
    def smtp_server(self):
        """Test SMTP server configuration."""
        return SMTPServerConfig(
            host="smtp.example.com",
            port=587,
            username="test@example.com",
            password="test-password",
            use_tls=True,
        )

    @pytest.fixture
    def mock_smtp(self):
        """Mock SMTP connection."""
        mock = AsyncMock(spec=aiosmtplib.SMTP)
        mock.send_message = AsyncMock(return_value=({}, "message-id-123"))
        mock.connect = AsyncMock()
        mock.starttls = AsyncMock()
        mock.login = AsyncMock()
        mock.quit = AsyncMock()
        mock.noop = AsyncMock()
        return mock

    async def test_send_success(self, smtp_server, mock_smtp):
        """Test successful email send."""
        with patch("msmtp.connection_pool.aiosmtplib.SMTP", return_value=mock_smtp):
            async with AsyncSMTPSender([smtp_server]) as sender:
                result = await sender.send(
                    from_addr="sender@example.com",
                    to_addrs=["recipient@example.com"],
                    subject="Test",
                    body_text="Hello World",
                )
        
        assert result.success
        assert result.message_id == "message-id-123"
        assert result.attempts == 1
        mock_smtp.send_message.assert_called_once()

    async def test_send_with_validation_error(self, smtp_server):
        """Test send with invalid email."""
        async with AsyncSMTPSender([smtp_server]) as sender:
            result = await sender.send(
                from_addr="invalid-email",
                to_addrs=["recipient@example.com"],
                subject="Test",
                body_text="Hello",
            )
        
        assert not result.success
        assert "validation failed" in result.error.lower()
        assert result.attempts == 0

    async def test_circuit_breaker_integration(self, smtp_server, mock_smtp):
        """Test circuit breaker opens after failures."""
        # Make send_message fail
        mock_smtp.send_message = AsyncMock(
            side_effect=aiosmtplib.SMTPConnectError("Connection refused")
        )
        
        with patch("msmtp.connection_pool.aiosmtplib.SMTP", return_value=mock_smtp):
            async with AsyncSMTPSender([smtp_server]) as sender:
                # Send multiple failing emails
                for _ in range(5):
                    result = await sender.send(
                        from_addr="sender@example.com",
                        to_addrs=["recipient@example.com"],
                        subject="Test",
                        body_text="Hello",
                    )
                    assert not result.success
                
                # Circuit breaker should be open
                pool = sender._pools[smtp_server.name]
                assert not pool.runtime.circuit_breaker.is_available()

    async def test_retry_on_transient_error(self, smtp_server, mock_smtp):
        """Test retry on transient errors."""
        # Fail first 2 attempts, then succeed
        mock_smtp.send_message = AsyncMock(
            side_effect=[
                aiosmtplib.SMTPConnectError("Timeout"),
                aiosmtplib.SMTPConnectError("Timeout"),
                ({}, "message-id-123"),
            ]
        )
        
        with patch("msmtp.connection_pool.aiosmtplib.SMTP", return_value=mock_smtp):
            async with AsyncSMTPSender([smtp_server], max_retries=3) as sender:
                result = await sender.send(
                    from_addr="sender@example.com",
                    to_addrs=["recipient@example.com"],
                    subject="Test",
                    body_text="Hello",
                )
        
        assert result.success
        assert result.attempts == 3
        assert mock_smtp.send_message.call_count == 3

    async def test_authentication_error_no_retry(self, smtp_server, mock_smtp):
        """Test authentication errors are not retried."""
        mock_smtp.login = AsyncMock(
            side_effect=aiosmtplib.SMTPAuthenticationError(535, "Invalid credentials")
        )
        
        with patch("msmtp.connection_pool.aiosmtplib.SMTP", return_value=mock_smtp):
            async with AsyncSMTPSender([smtp_server]) as sender:
                result = await sender.send(
                    from_addr="sender@example.com",
                    to_addrs=["recipient@example.com"],
                    subject="Test",
                    body_text="Hello",
                )
        
        assert not result.success
        assert "authentication" in result.error.lower()

    async def test_rate_limiting(self, smtp_server, mock_smtp):
        """Test rate limiting."""
        with patch("msmtp.connection_pool.aiosmtplib.SMTP", return_value=mock_smtp):
            rate_limiter = RateLimiterConfig(per_second=2.0, burst_size=1)
            async with AsyncSMTPSender([smtp_server], rate_limiter=rate_limiter) as sender:
                import time
                start = time.time()
                
                # Burst size of 1 forces every email past the first to wait
                # for a token refill at 2/sec (~0.5s each).
                for _ in range(3):
                    await sender.send(
                        from_addr="sender@example.com",
                        to_addrs=["recipient@example.com"],
                        subject="Test",
                        body_text="Hello",
                    )
                
                elapsed = time.time() - start
                # With 2/sec rate and no burst allowance, 3 emails should take
                # at least ~0.4 seconds (two refills needed).
                assert elapsed >= 0.4

    async def test_bulk_send(self, smtp_server, mock_smtp):
        """Test bulk sending."""
        with patch("msmtp.connection_pool.aiosmtplib.SMTP", return_value=mock_smtp):
            async with AsyncSMTPSender([smtp_server]) as sender:
                emails = [
                    {
                        "from_addr": "sender@example.com",
                        "to_addrs": [f"user{i}@example.com"],
                        "subject": f"Test {i}",
                        "body_text": f"Hello {i}",
                    }
                    for i in range(10)
                ]
                
                result = await sender.send_bulk(emails, concurrency=5)
        
        assert result.total == 10
        assert result.success_count == 10
        assert result.failed_count == 0
        assert result.success_rate == 100.0

    async def test_load_balancing_round_robin(self):
        """Test round-robin load balancing."""
        servers = [
            SMTPServerConfig(name="s1", host="smtp1.example.com"),
            SMTPServerConfig(name="s2", host="smtp2.example.com"),
            SMTPServerConfig(name="s3", host="smtp3.example.com"),
        ]
        
        async with AsyncSMTPSender(
            servers,
            strategy=LoadBalancingStrategy.ROUND_ROBIN
        ) as sender:
            # Select servers multiple times
            selected = [
                sender._select_server("test@example.com").server.name
                for _ in range(6)
            ]
            
            # Should cycle through servers
            assert selected[:3] == ["s1", "s2", "s3"]
            assert selected[3:6] == ["s1", "s2", "s3"]

    async def test_ssl_context_usage(self, mock_smtp):
        """Test custom SSL context is used."""
        custom_context = ssl.create_default_context()
        server = SMTPServerConfig(
            host="smtp.example.com",
            ssl_context=custom_context,
        )
        
        with patch("msmtp.connection_pool.aiosmtplib.SMTP", return_value=mock_smtp) as mock_smtp_class:
            async with AsyncSMTPSender([server]) as sender:
                await sender.send(
                    from_addr="sender@example.com",
                    to_addrs=["recipient@example.com"],
                    subject="Test",
                    body_text="Hello",
                )
            
            # Verify SSL context was passed
            call_args = mock_smtp_class.call_args
            assert call_args.kwargs["tls_context"] == custom_context

    async def test_close_cleanup(self, smtp_server, mock_smtp):
        """Test proper cleanup on close."""
        with patch("msmtp.connection_pool.aiosmtplib.SMTP", return_value=mock_smtp):
            sender = AsyncSMTPSender([smtp_server])
            
            # Send an email to create connections
            await sender.send(
                from_addr="sender@example.com",
                to_addrs=["recipient@example.com"],
                subject="Test",
                body_text="Hello",
            )
            
            # Close sender
            await sender.close()
            
            # Verify connections were closed
            mock_smtp.quit.assert_called()


@pytest.mark.asyncio
class TestConnectionPool:
    """Test connection pool."""

    async def test_connection_reuse(self, mock_smtp):
        """Test connections are reused."""
        server = SMTPServerConfig(host="smtp.example.com")
        
        with patch("msmtp.connection_pool.aiosmtplib.SMTP", return_value=mock_smtp):
            from msmtp.connection_pool import SMTPConnectionPool
            
            pool = SMTPConnectionPool(server)
            
            # Acquire and release connection
            conn1 = await pool.acquire()
            await pool.release(conn1)
            
            # Acquire again - should reuse same connection
            conn2 = await pool.acquire()
            
            assert conn1 is conn2
            assert mock_smtp.connect.call_count == 1  # Only connected once

    async def test_max_connections(self, mock_smtp):
        """Test connection pool limit."""
        server = SMTPServerConfig(host="smtp.example.com")

        # Each acquire() must create a distinct connection object, otherwise
        # the pool's `_in_use` set (which dedupes by identity) can't track
        # separate in-flight connections.
        real_smtp_cls = aiosmtplib.SMTP
        with patch(
            "msmtp.connection_pool.aiosmtplib.SMTP",
            side_effect=lambda *a, **kw: AsyncMock(spec=real_smtp_cls),
        ):
            from msmtp.connection_pool import SMTPConnectionPool, ConnectionPoolException
            
            pool = SMTPConnectionPool(server, max_connections=2)
            
            # Acquire max connections
            conn1 = await pool.acquire()
            conn2 = await pool.acquire()
            
            # Trying to acquire more should fail
            with pytest.raises(ConnectionPoolException):
                await pool.acquire()


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
