"""Tests for SMTPConnectionPool, SMTPServerRuntime, and AsyncConnectionPool."""

import asyncio
import ssl

import aiosmtplib
import pytest
from unittest.mock import AsyncMock, patch

from msmtp import SMTPServerConfig
from msmtp.connection_pool import (
    AsyncConnectionPool,
    ConnectionPoolException,
    SMTPConnectionPool,
    SMTPServerRuntime,
    _create_circuit_breaker,
)
from msmtp.exceptions import SMTPAuthenticationError, SMTPConnectionError


@pytest.mark.asyncio
class TestConnectionAcquireRelease:
    """Test connection acquisition and release lifecycle."""

    async def test_acquire_creates_new_connection(self, smtp_server, mock_smtp):
        with patch("msmtp.connection_pool.aiosmtplib.SMTP", return_value=mock_smtp):
            pool = SMTPConnectionPool(smtp_server)
            conn = await pool.acquire()

        assert conn is mock_smtp
        assert conn in pool._in_use
        mock_smtp.connect.assert_awaited_once()

    async def test_acquire_reuses_healthy_pooled_connection(self, smtp_server, mock_smtp):
        with patch("msmtp.connection_pool.aiosmtplib.SMTP", return_value=mock_smtp):
            pool = SMTPConnectionPool(smtp_server)
            conn1 = await pool.acquire()
            await pool.release(conn1)
            conn2 = await pool.acquire()

        assert conn1 is conn2
        assert mock_smtp.connect.await_count == 1

    async def test_release_discards_unhealthy_connection(self, smtp_server, mock_smtp):
        mock_smtp.noop = AsyncMock(side_effect=Exception("connection reset"))

        with patch("msmtp.connection_pool.aiosmtplib.SMTP", return_value=mock_smtp):
            pool = SMTPConnectionPool(smtp_server)
            conn = await pool.acquire()
            await pool.release(conn)

        assert conn not in pool._pool
        assert conn not in pool._in_use
        mock_smtp.quit.assert_awaited()

    async def test_acquire_discards_unhealthy_pooled_connection_and_creates_new(
        self, smtp_server, mock_smtp
    ):
        # First connection is healthy on release, but becomes unhealthy on next acquire.
        healthy_states = iter([True, False])

        async def noop_side_effect():
            if not next(healthy_states):
                raise Exception("stale connection")

        mock_smtp.noop = AsyncMock(side_effect=noop_side_effect)

        second_mock = AsyncMock(spec=aiosmtplib.SMTP)
        second_mock.connect = AsyncMock()
        second_mock.noop = AsyncMock()
        second_mock.quit = AsyncMock()

        with patch(
            "msmtp.connection_pool.aiosmtplib.SMTP",
            side_effect=[mock_smtp, second_mock],
        ):
            pool = SMTPConnectionPool(smtp_server)
            conn1 = await pool.acquire()
            await pool.release(conn1)  # healthy -> goes back to pool
            conn2 = await pool.acquire()  # unhealthy on re-check -> discarded, new created

        assert conn2 is second_mock
        mock_smtp.quit.assert_awaited()

    async def test_acquire_raises_when_max_connections_reached(self, smtp_server, mock_smtp):
        with patch("msmtp.connection_pool.aiosmtplib.SMTP", return_value=mock_smtp):
            pool = SMTPConnectionPool(smtp_server, max_connections=1)
            await pool.acquire()

            with pytest.raises(ConnectionPoolException):
                await pool.acquire()

    async def test_release_of_unknown_connection_is_noop(self, smtp_server, mock_smtp):
        with patch("msmtp.connection_pool.aiosmtplib.SMTP", return_value=mock_smtp):
            pool = SMTPConnectionPool(smtp_server)
            # Never acquired, so it's not tracked in _in_use.
            await pool.release(mock_smtp)

        assert mock_smtp not in pool._pool


@pytest.mark.asyncio
class TestHealthChecks:
    async def test_is_healthy_true_on_successful_noop(self, smtp_server, mock_smtp):
        pool = SMTPConnectionPool(smtp_server)
        assert await pool._is_healthy(mock_smtp) is True

    async def test_is_healthy_false_on_noop_exception(self, smtp_server, mock_smtp):
        mock_smtp.noop = AsyncMock(side_effect=aiosmtplib.SMTPServerDisconnected("disconnected"))
        pool = SMTPConnectionPool(smtp_server)
        assert await pool._is_healthy(mock_smtp) is False

    async def test_is_healthy_false_on_timeout(self, smtp_server, mock_smtp):
        async def hang():
            await asyncio.sleep(10)

        mock_smtp.noop = AsyncMock(side_effect=hang)
        pool = SMTPConnectionPool(smtp_server)

        with patch("msmtp.connection_pool.asyncio.wait_for", side_effect=asyncio.TimeoutError):
            assert await pool._is_healthy(mock_smtp) is False


@pytest.mark.asyncio
class TestCreateConnection:
    async def test_starttls_used_for_use_tls(self, mock_smtp):
        server = SMTPServerConfig(host="smtp.example.com", use_tls=True, use_ssl=False)
        with patch("msmtp.connection_pool.aiosmtplib.SMTP", return_value=mock_smtp):
            pool = SMTPConnectionPool(server)
            await pool._create_connection()

        mock_smtp.starttls.assert_awaited_once()

    async def test_starttls_not_used_for_implicit_ssl(self, mock_smtp):
        server = SMTPServerConfig(host="smtp.example.com", use_tls=False, use_ssl=True)
        with patch("msmtp.connection_pool.aiosmtplib.SMTP", return_value=mock_smtp):
            pool = SMTPConnectionPool(server)
            await pool._create_connection()

        mock_smtp.starttls.assert_not_awaited()

    async def test_login_called_when_username_present(self, mock_smtp):
        server = SMTPServerConfig(
            host="smtp.example.com", username="user@example.com", password="secret"
        )
        with patch("msmtp.connection_pool.aiosmtplib.SMTP", return_value=mock_smtp):
            pool = SMTPConnectionPool(server)
            await pool._create_connection()

        mock_smtp.login.assert_awaited_once_with("user@example.com", "secret")

    async def test_login_uses_password_provider(self, mock_smtp):
        server = SMTPServerConfig(
            host="smtp.example.com",
            username="user@example.com",
            password_provider=lambda: "from-provider",
        )
        with patch("msmtp.connection_pool.aiosmtplib.SMTP", return_value=mock_smtp):
            pool = SMTPConnectionPool(server)
            await pool._create_connection()

        mock_smtp.login.assert_awaited_once_with("user@example.com", "from-provider")

    async def test_no_login_without_username(self, mock_smtp):
        server = SMTPServerConfig(host="smtp.example.com")
        with patch("msmtp.connection_pool.aiosmtplib.SMTP", return_value=mock_smtp):
            pool = SMTPConnectionPool(server)
            await pool._create_connection()

        mock_smtp.login.assert_not_awaited()

    async def test_verify_ssl_disabled_configures_insecure_context(self, mock_smtp):
        server = SMTPServerConfig(host="smtp.example.com", verify_ssl=False)

        with patch("msmtp.connection_pool.aiosmtplib.SMTP", return_value=mock_smtp) as smtp_cls:
            pool = SMTPConnectionPool(server)
            await pool._create_connection()

        tls_context = smtp_cls.call_args.kwargs["tls_context"]
        assert tls_context.check_hostname is False
        assert tls_context.verify_mode == ssl.CERT_NONE

    async def test_custom_ssl_context_is_passed_through(self, mock_smtp):
        custom_context = ssl.create_default_context()
        server = SMTPServerConfig(host="smtp.example.com", ssl_context=custom_context)

        with patch("msmtp.connection_pool.aiosmtplib.SMTP", return_value=mock_smtp) as smtp_cls:
            pool = SMTPConnectionPool(server)
            await pool._create_connection()

        assert smtp_cls.call_args.kwargs["tls_context"] is custom_context

    async def test_no_tls_context_when_tls_and_ssl_disabled(self, mock_smtp):
        server = SMTPServerConfig(host="smtp.example.com", use_tls=False, use_ssl=False)

        with patch("msmtp.connection_pool.aiosmtplib.SMTP", return_value=mock_smtp) as smtp_cls:
            pool = SMTPConnectionPool(server)
            await pool._create_connection()

        assert smtp_cls.call_args.kwargs["tls_context"] is None

    async def test_records_handshake_latency(self, mock_smtp):
        server = SMTPServerConfig(host="smtp.example.com")
        with patch("msmtp.connection_pool.aiosmtplib.SMTP", return_value=mock_smtp):
            pool = SMTPConnectionPool(server)
            await pool._create_connection()

        assert len(pool.runtime.handshake_latencies) == 1
        assert pool.runtime.avg_handshake_latency is not None

    async def test_auth_error_raises_smtp_authentication_error(self, mock_smtp):
        mock_smtp.login = AsyncMock(
            side_effect=aiosmtplib.SMTPAuthenticationError(535, "bad credentials")
        )
        server = SMTPServerConfig(host="smtp.example.com", username="u", password="p")

        with patch("msmtp.connection_pool.aiosmtplib.SMTP", return_value=mock_smtp):
            pool = SMTPConnectionPool(server)
            with pytest.raises(SMTPAuthenticationError, match="Authentication failed"):
                await pool._create_connection()

    async def test_generic_error_raises_smtp_connection_error(self, mock_smtp):
        mock_smtp.connect = AsyncMock(side_effect=OSError("network unreachable"))
        server = SMTPServerConfig(host="smtp.example.com")

        with patch("msmtp.connection_pool.aiosmtplib.SMTP", return_value=mock_smtp):
            pool = SMTPConnectionPool(server)
            with pytest.raises(SMTPConnectionError, match="Connection failed"):
                await pool._create_connection()


@pytest.mark.asyncio
class TestCloseAll:
    async def test_close_all_clears_pool_and_in_use(self, smtp_server, mock_smtp):
        with patch("msmtp.connection_pool.aiosmtplib.SMTP", return_value=mock_smtp):
            pool = SMTPConnectionPool(smtp_server)
            conn = await pool.acquire()
            await pool.release(conn)
            # Simulate a second connection still in use.
            pool._in_use.add(mock_smtp)

            await pool.close_all()

        assert pool._pool == []
        assert pool._in_use == set()

    async def test_close_all_survives_quit_errors(self, smtp_server, mock_smtp):
        mock_smtp.quit = AsyncMock(side_effect=Exception("already closed"))

        with patch("msmtp.connection_pool.aiosmtplib.SMTP", return_value=mock_smtp):
            pool = SMTPConnectionPool(smtp_server)
            conn = await pool.acquire()
            await pool.release(conn)

            # Should not raise despite quit() failing.
            await pool.close_all()

        assert pool._pool == []


class TestSMTPServerRuntime:
    def test_avg_latency_none_when_empty(self):
        runtime = SMTPServerRuntime(circuit_breaker=_create_circuit_breaker("s1"))
        assert runtime.avg_handshake_latency is None
        assert runtime.avg_send_latency is None

    def test_avg_latency_computed(self):
        runtime = SMTPServerRuntime(circuit_breaker=_create_circuit_breaker("s1"))
        runtime.record_handshake(0.1)
        runtime.record_handshake(0.3)
        runtime.record_send(0.2)

        assert runtime.avg_handshake_latency == pytest.approx(0.2)
        assert runtime.avg_send_latency == pytest.approx(0.2)

    def test_latency_lists_capped_at_50(self):
        runtime = SMTPServerRuntime(circuit_breaker=_create_circuit_breaker("s1"))
        for i in range(60):
            runtime.record_handshake(float(i))
            runtime.record_send(float(i))

        assert len(runtime.handshake_latencies) == 50
        assert len(runtime.send_latencies) == 50
        # Oldest entries should have been evicted (FIFO).
        assert runtime.handshake_latencies[0] == 10.0


@pytest.mark.asyncio
class TestAsyncConnectionPoolWrapper:
    async def test_context_manager_acquires_and_releases(self, smtp_server, mock_smtp):
        with patch("msmtp.connection_pool.aiosmtplib.SMTP", return_value=mock_smtp):
            pool = SMTPConnectionPool(smtp_server)
            async with AsyncConnectionPool(pool) as conn:
                assert conn is mock_smtp
                assert conn in pool._in_use

            assert conn not in pool._in_use
            assert conn in pool._pool
