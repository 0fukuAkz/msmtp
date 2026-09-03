"""SMTP connection pooling with circuit breaker and load balancing."""

import asyncio
import logging
import ssl
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from types import TracebackType

import aiosmtplib

from .circuit_breaker import CircuitBreaker, CircuitBreakerConfig
from .exceptions import SMTPAuthenticationError, SMTPConnectionError
from .types import SMTPServerConfig

logger = logging.getLogger(__name__)


class ConnectionPoolException(Exception):
    """Errors related to connection pool operations."""

    pass


def _create_circuit_breaker(
    server_name: str = "default",
    *,
    failure_threshold: int = 5,
    success_threshold: int = 3,
    timeout_seconds: int = 60,
    monitor_window_seconds: int = 300,
) -> CircuitBreaker:
    """Factory function to create a circuit breaker."""
    return CircuitBreaker(
        server_name=server_name,
        config=CircuitBreakerConfig(
            failure_threshold=failure_threshold,
            success_threshold=success_threshold,
            timeout_seconds=timeout_seconds,
            monitor_window_seconds=monitor_window_seconds,
        ),
    )


@dataclass
class SMTPServerRuntime:
    """Per-process mutable runtime state for an SMTP server."""

    circuit_breaker: CircuitBreaker
    current_minute_count: int = 0
    current_hour_count: int = 0
    total_sent: int = 0
    total_failures: int = 0
    consecutive_failures: int = 0
    last_minute_reset: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    last_hour_reset: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    handshake_latencies: list[float] = field(default_factory=list)
    send_latencies: list[float] = field(default_factory=list)

    @property
    def avg_handshake_latency(self) -> float | None:
        """Get average connection handshake latency in seconds."""
        if not self.handshake_latencies:
            return None
        return sum(self.handshake_latencies) / len(self.handshake_latencies)

    @property
    def avg_send_latency(self) -> float | None:
        """Get average mail sending latency in seconds."""
        if not self.send_latencies:
            return None
        return sum(self.send_latencies) / len(self.send_latencies)

    def record_handshake(self, seconds: float) -> None:
        """Record a connection handshake latency measurement."""
        self.handshake_latencies.append(seconds)
        if len(self.handshake_latencies) > 50:
            self.handshake_latencies.pop(0)

    def record_send(self, seconds: float) -> None:
        """Record a mail sending latency measurement."""
        self.send_latencies.append(seconds)
        if len(self.send_latencies) > 50:
            self.send_latencies.pop(0)


class SMTPConnectionPool:
    """
    SMTP connection pool with health checks and circuit breaker.

    Maintains a pool of persistent SMTP connections to reduce handshake overhead.
    Includes health checks and automatic connection recycling.
    """

    def __init__(
        self,
        server: SMTPServerConfig,
        max_connections: int = 10,
        health_check_interval: int = 60,
        max_idle_time: int = 300,
    ):
        """
        Initialize connection pool.

        Args:
            server: SMTP server configuration
            max_connections: Maximum pooled connections
            health_check_interval: Seconds between health checks
            max_idle_time: Recycle connection after this many idle seconds
        """
        self.server = server
        self.max_connections = max_connections
        self.health_check_interval = health_check_interval
        self.max_idle_time = max_idle_time

        self._pool: list[aiosmtplib.SMTP] = []
        self._in_use: set[aiosmtplib.SMTP] = set()
        self._pending_count = 0  # connections being created but not yet in _in_use
        self._closed = False
        self._lock = asyncio.Lock()
        self._last_health_check = time.monotonic()

        # Runtime state
        self.runtime = SMTPServerRuntime(
            circuit_breaker=_create_circuit_breaker(server.name or server.host)
        )

    async def acquire(self) -> aiosmtplib.SMTP:
        """
        Acquire a connection from the pool.

        Returns:
            SMTP connection

        Raises:
            ConnectionPoolException: If no connections available
        """
        # Pop a candidate (or decide to create new) under the lock; do all I/O outside
        # so the lock isn't held during the 5-second NOOP health check or TLS handshake.
        candidate: aiosmtplib.SMTP | None
        async with self._lock:
            if self._pool:
                pooled = self._pool.pop()  # always SMTP, never None
                self._in_use.add(pooled)   # account for it immediately
                candidate = pooled
            elif len(self._in_use) + self._pending_count < self.max_connections:
                # Reserve a slot before releasing the lock so concurrent callers
                # cannot also observe room and all create new connections at once.
                self._pending_count += 1
                candidate = None
            else:
                raise ConnectionPoolException("No connections available")

        if candidate is not None:
            # Health check outside lock
            if await self._is_healthy(candidate):
                return candidate
            # Unhealthy: evict and retry (recurses at most pool-size times)
            async with self._lock:
                self._in_use.discard(candidate)
            try:
                await candidate.quit()
            except Exception:
                pass
            return await self.acquire()

        # create_new path — TLS handshake outside lock; slot was already reserved
        try:
            conn = await self._create_connection()
        except Exception:
            async with self._lock:
                self._pending_count -= 1
            raise
        async with self._lock:
            self._pending_count -= 1
            self._in_use.add(conn)
        return conn

    async def release(self, conn: aiosmtplib.SMTP) -> None:
        """Release a connection back to the pool."""
        async with self._lock:
            if conn not in self._in_use:
                return
            self._in_use.remove(conn)

        # Health check outside lock
        if await self._is_healthy(conn):
            async with self._lock:
                if not self._closed:
                    # Guard against a shutdown that completed while the health
                    # check was in flight — don't re-insert into a dead pool.
                    self._pool.append(conn)
                    return
        try:
            await conn.quit()
        except Exception:
            pass

    async def _create_connection(self) -> aiosmtplib.SMTP:
        """Create a new SMTP connection with SSL verification."""
        start = time.perf_counter()

        # Prepare SSL context
        tls_context = self.server.ssl_context
        if tls_context is None and (self.server.use_tls or self.server.use_ssl):
            tls_context = ssl.create_default_context()

            # Configure SSL verification
            if not self.server.verify_ssl:
                tls_context.check_hostname = False
                tls_context.verify_mode = ssl.CERT_NONE
                logger.warning(
                    "ssl_verification_disabled",
                    extra={
                        "server": self.server.name,
                        "host": self.server.host,
                        "port": self.server.port,
                    },
                )

        try:
            smtp = aiosmtplib.SMTP(
                hostname=self.server.host,
                port=self.server.port,
                timeout=self.server.timeout,
                use_tls=self.server.use_ssl,
                tls_context=tls_context,
            )

            await smtp.connect()

            if self.server.use_tls and not self.server.use_ssl:
                await smtp.starttls(tls_context=tls_context)

            if self.server.username:
                password = self.server.get_password()
                await smtp.login(self.server.username, password)

            # Record handshake latency
            latency = time.perf_counter() - start
            self.runtime.record_handshake(latency)

            logger.info(
                "smtp_connection_created",
                extra={
                    "server": self.server.name,
                    "host": self.server.host,
                    "port": self.server.port,
                    "handshake_ms": latency * 1000,
                    "use_tls": self.server.use_tls,
                    "use_ssl": self.server.use_ssl,
                    "verify_ssl": self.server.verify_ssl,
                },
            )

            return smtp

        except aiosmtplib.SMTPAuthenticationError as e:
            logger.error(
                "smtp_auth_failed",
                extra={
                    "server": self.server.name,
                    "host": self.server.host,
                    "username": self.server.username,
                    "error": str(e),
                },
            )
            raise SMTPAuthenticationError(f"Authentication failed: {e}") from e
        except Exception as e:
            logger.error(
                "smtp_connection_failed",
                extra={
                    "server": self.server.name,
                    "host": self.server.host,
                    "port": self.server.port,
                    "error_type": type(e).__name__,
                    "error": str(e)[:200],
                },
            )
            raise SMTPConnectionError(f"Connection failed: {e}") from e

    async def _is_healthy(self, conn: aiosmtplib.SMTP) -> bool:
        """Check if connection is healthy."""
        try:
            # Try NOOP command
            await asyncio.wait_for(conn.noop(), timeout=5.0)
            return True
        except Exception:
            return False

    async def close_all(self) -> None:
        """Close all connections in the pool."""
        logger.info(
            "closing_connection_pool",
            extra={
                "server": self.server.name,
                "pooled_connections": len(self._pool),
                "in_use_connections": len(self._in_use),
            },
        )

        async with self._lock:
            self._closed = True
            to_close = list(self._pool) + list(self._in_use)
            self._pool.clear()
            self._in_use.clear()

        # Close connections outside lock so we don't block concurrent release() calls
        for conn in to_close:
            try:
                await conn.quit()
            except Exception as e:
                logger.debug("connection_close_error", extra={"error": str(e)})


class AsyncConnectionPool:
    """Async context manager wrapper for connection pool."""

    def __init__(self, pool: SMTPConnectionPool):
        self.pool = pool
        self._conn: aiosmtplib.SMTP | None = None

    async def __aenter__(self) -> aiosmtplib.SMTP:
        self._conn = await self.pool.acquire()
        return self._conn

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        if self._conn:
            await self.pool.release(self._conn)
