"""Integration-style tests: multi-server failover and circuit breaker recovery."""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import aiosmtplib
import pytest

from msmtp import AsyncSMTPSender, LoadBalancingStrategy, SMTPServerConfig
from msmtp.circuit_breaker import CircuitState


def _make_mock_smtp(send_result=None, send_side_effect=None):
    mock = AsyncMock(spec=aiosmtplib.SMTP)
    mock.connect = AsyncMock()
    mock.starttls = AsyncMock()
    mock.login = AsyncMock()
    mock.quit = AsyncMock()
    mock.noop = AsyncMock()
    if send_side_effect is not None:
        mock.send_message = AsyncMock(side_effect=send_side_effect)
    else:
        mock.send_message = AsyncMock(return_value=send_result or ({}, "message-id"))
    return mock


@pytest.mark.asyncio
class TestMultiServerFailover:
    async def test_traffic_avoids_server_with_open_circuit(self):
        primary = SMTPServerConfig(name="primary", host="primary.example.com")
        backup = SMTPServerConfig(name="backup", host="backup.example.com")

        good_mock = _make_mock_smtp()

        def smtp_factory(*args, **kwargs):
            return good_mock

        with patch("msmtp.connection_pool.aiosmtplib.SMTP", side_effect=smtp_factory):
            async with AsyncSMTPSender(
                [primary, backup], strategy=LoadBalancingStrategy.ROUND_ROBIN
            ) as sender:
                # Simulate primary already being down.
                sender._pools["primary"].runtime.circuit_breaker.force_open()

                results = []
                for _ in range(4):
                    results.append(
                        await sender.send(
                            from_addr="sender@example.com",
                            to_addrs=["to@example.com"],
                            subject="Hi",
                            body_text="hi",
                        )
                    )

        assert all(r.success for r in results)
        # All traffic should have used the backup pool's connection.
        assert sender._pools["backup"].runtime.total_sent == 4
        assert sender._pools["primary"].runtime.total_sent == 0

    async def test_all_servers_down_returns_failure(self):
        primary = SMTPServerConfig(name="primary", host="primary.example.com")
        backup = SMTPServerConfig(name="backup", host="backup.example.com")

        with patch("msmtp.connection_pool.aiosmtplib.SMTP") as smtp_cls:
            smtp_cls.side_effect = lambda *a, **kw: _make_mock_smtp()
            async with AsyncSMTPSender([primary, backup], max_retries=1) as sender:
                sender._pools["primary"].runtime.circuit_breaker.force_open()
                sender._pools["backup"].runtime.circuit_breaker.force_open()

                result = await sender.send(
                    from_addr="sender@example.com",
                    to_addrs=["to@example.com"],
                    subject="Hi",
                    body_text="hi",
                )

        assert result.success is False
        assert "No available SMTP servers" in result.error


@pytest.mark.asyncio
class TestCircuitBreakerRecovery:
    async def test_repeated_connection_failures_open_circuit(self):
        server = SMTPServerConfig(name="s1", host="smtp.example.com")
        bad_mock = _make_mock_smtp()
        bad_mock.connect = AsyncMock(side_effect=OSError("connection refused"))

        with (
            patch("msmtp.connection_pool.aiosmtplib.SMTP", return_value=bad_mock),
            patch("msmtp.sender.asyncio.sleep", new=AsyncMock()),
        ):
            async with AsyncSMTPSender([server], max_retries=1) as sender:
                pool = sender._pools["s1"]

                # Default failure_threshold for pool circuit breakers is 5.
                for _ in range(5):
                    result = await sender.send(
                        from_addr="sender@example.com",
                        to_addrs=["to@example.com"],
                        subject="Hi",
                        body_text="hi",
                    )
                    assert result.success is False

                assert pool.runtime.circuit_breaker._stats.state == CircuitState.OPEN
                assert pool.runtime.circuit_breaker.is_available() is False

    async def test_circuit_closes_after_timeout_and_successes(self):
        server = SMTPServerConfig(name="s1", host="smtp.example.com")
        sender = AsyncSMTPSender([server])
        pool = sender._pools["s1"]
        cb = pool.runtime.circuit_breaker

        # Force the breaker open, as if repeated failures already occurred.
        cb.force_open()
        assert cb.is_available() is False

        # Simulate the timeout window having elapsed.
        cb._stats.opened_at = datetime.now(timezone.utc) - timedelta(
            seconds=cb.config.timeout_seconds + 1
        )

        # First availability check after timeout transitions to HALF_OPEN.
        assert cb.is_available() is True
        assert cb._stats.state == CircuitState.HALF_OPEN

        # Pool circuit breakers require 3 consecutive successes to close.
        cb.record_success()
        cb.record_success()
        assert cb._stats.state == CircuitState.HALF_OPEN  # not yet closed
        cb.record_success()

        assert cb._stats.state == CircuitState.CLOSED
        assert cb.is_available() is True

        await sender.close()

    async def test_failure_during_half_open_reopens_circuit(self):
        server = SMTPServerConfig(name="s1", host="smtp.example.com")
        sender = AsyncSMTPSender([server])
        pool = sender._pools["s1"]
        cb = pool.runtime.circuit_breaker

        cb.force_open()
        cb._stats.opened_at = datetime.now(timezone.utc) - timedelta(
            seconds=cb.config.timeout_seconds + 1
        )
        assert cb.is_available() is True
        assert cb._stats.state == CircuitState.HALF_OPEN

        cb.record_failure(Exception("still failing"))

        assert cb._stats.state == CircuitState.OPEN
        assert cb.is_available() is False

        await sender.close()

    async def test_send_resumes_on_recovered_server(self):
        server = SMTPServerConfig(name="s1", host="smtp.example.com")
        good_mock = _make_mock_smtp()

        with patch("msmtp.connection_pool.aiosmtplib.SMTP", return_value=good_mock):
            async with AsyncSMTPSender([server]) as sender:
                pool = sender._pools["s1"]
                cb = pool.runtime.circuit_breaker

                # Circuit was open but has since transitioned to half-open.
                cb._stats.state = CircuitState.HALF_OPEN

                result = await sender.send(
                    from_addr="sender@example.com",
                    to_addrs=["to@example.com"],
                    subject="Hi",
                    body_text="hi",
                )

        assert result.success is True
