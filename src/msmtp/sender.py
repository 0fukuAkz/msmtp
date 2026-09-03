"""Async SMTP sender with connection pooling and resilience patterns."""

import asyncio
import logging
import random
import time
import uuid
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from enum import Enum
from types import TracebackType
from typing import Any

import aiosmtplib

from .connection_pool import AsyncConnectionPool, SMTPConnectionPool
from .exceptions import (
    SMTPAuthenticationError,
    SMTPConnectionError,
    is_transient_error,
)
from .rate_limiter import RateLimiter, RateLimiterConfig
from .retry_queue import RetryConfig, RetryQueue
from .types import BulkSendResult, EmailResult, SMTPServerConfig
from .validation import (
    sanitize_header_value,
    sanitize_subject,
    validate_email_address,
    validate_email_list,
)

logger = logging.getLogger(__name__)


class LoadBalancingStrategy(Enum):
    """Server selection strategy for load balancing."""

    ROUND_ROBIN = "round_robin"
    WEIGHTED = "weighted"
    PRIORITY = "priority"  # Highest priority (lowest number) first


class AsyncSMTPSender:
    """
    Production-grade async SMTP sender.

    Features:
    - Connection pooling with health checks
    - Circuit breaker for failing servers
    - Rate limiting (token bucket)
    - Automatic retry with exponential backoff
    - Multi-server load balancing

    Example:
        >>> async with AsyncSMTPSender([server]) as sender:
        ...     result = await sender.send(
        ...         from_addr="sender@example.com",
        ...         to_addrs=["recipient@example.com"],
        ...         subject="Hello",
        ...         body_text="Hello World",
        ...     )
    """

    def __init__(
        self,
        servers: list[SMTPServerConfig],
        rate_limiter: RateLimiterConfig | None = None,
        retry_config: RetryConfig | None = None,
        max_retries: int = 3,
        strategy: LoadBalancingStrategy = LoadBalancingStrategy.WEIGHTED,
    ):
        """
        Initialize async SMTP sender.

        Args:
            servers: List of SMTP server configurations
            rate_limiter: Global rate limiter config
            retry_config: Retry configuration
            max_retries: Maximum retry attempts
            strategy: Load balancing strategy
        """
        self.servers = servers
        self.strategy = strategy
        self.max_retries = max_retries

        # Create connection pools per server
        self._pools: dict[str, SMTPConnectionPool] = {}
        for server in servers:
            self._pools[server.name or server.host] = SMTPConnectionPool(server)

        # Rate limiter (optional)
        self._rate_limiter: RateLimiter | None = None
        if rate_limiter:
            self._rate_limiter = RateLimiter(rate_limiter)

        # Retry queue
        self._retry_queue = RetryQueue(
            config=retry_config or RetryConfig(),
        )

        # Round-robin state
        self._round_robin_index = 0

    async def __aenter__(self) -> "AsyncSMTPSender":
        """Enter async context."""
        self._retry_queue.handler = self._retry_handler
        await self._retry_queue.start()
        logger.info(
            "smtp_sender_initialized",
            extra={
                "num_servers": len(self.servers),
                "strategy": self.strategy.value,
                "max_retries": self.max_retries,
                "rate_limiter_enabled": self._rate_limiter is not None,
            },
        )
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        """Exit async context and clean up."""
        await self.close()

    async def send(
        self,
        from_addr: str,
        to_addrs: list[str],
        subject: str,
        body_text: str | None = None,
        body_html: str | None = None,
        cc: list[str] | None = None,
        bcc: list[str] | None = None,
        reply_to: str | None = None,
        headers: dict[str, str] | None = None,
        _queue_id: str | None = None,
    ) -> EmailResult:
        """
        Send a single email.

        Args:
            from_addr: Sender email address
            to_addrs: List of recipient email addresses
            subject: Email subject
            body_text: Plain text body
            body_html: HTML body
            cc: CC recipients
            bcc: BCC recipients
            reply_to: Reply-To address
            headers: Additional email headers

        Returns:
            EmailResult with send status
        """
        start = time.perf_counter()

        # Validate email addresses
        try:
            validate_email_address(from_addr)
            validate_email_list(to_addrs)
            if cc:
                validate_email_list(cc)
            if bcc:
                validate_email_list(bcc)
            if reply_to:
                validate_email_address(reply_to)
        except ValueError as e:
            logger.error(
                "email_validation_failed",
                extra={
                    "error": str(e),
                    "from_addr": from_addr[:50] if from_addr else None,
                    "num_recipients": len(to_addrs) if to_addrs else 0,
                },
            )
            return EmailResult(
                success=False,
                error=f"Email validation failed: {e}",
                recipient=to_addrs[0] if to_addrs else None,
                attempts=0,
                timestamp=datetime.now(timezone.utc),
            )

        # Build email message
        msg = self._build_message(
            from_addr=from_addr,
            to_addrs=to_addrs,
            subject=subject,
            body_text=body_text,
            body_html=body_html,
            cc=cc,
            bcc=bcc,
            reply_to=reply_to,
            headers=headers,
        )

        # Try to send
        pool: SMTPConnectionPool | None = None
        for attempt in range(1, self.max_retries + 1):
            try:
                # Apply rate limiting
                if self._rate_limiter:
                    await self._rate_limiter.acquire()

                # Select server
                pool = self._select_server(from_addr)

                # Check circuit breaker
                if not pool.runtime.circuit_breaker.is_available():
                    cb_stats = pool.runtime.circuit_breaker.get_stats()
                    error_context = ", ".join(cb_stats.get("last_error_messages", [])[:3])
                    raise SMTPConnectionError(
                        f"Circuit breaker OPEN for {pool.server.name} - "
                        f"failures: {cb_stats.get('failure_count', 0)}, "
                        f"recent errors: {error_context or 'none'}"
                    )

                # Get connection and send
                async with AsyncConnectionPool(pool) as smtp:
                    send_start = time.perf_counter()
                    errors, response = await smtp.send_message(msg)
                    send_latency = time.perf_counter() - send_start

                    # Record latency
                    pool.runtime.record_send(send_latency)

                    # Check for send errors
                    if errors:
                        error_msg = f"Send errors: {errors}"
                        pool.runtime.circuit_breaker.record_failure(SMTPConnectionError(error_msg))
                        logger.error(
                            "smtp_send_errors",
                            extra={
                                "server": pool.server.name,
                                "errors": str(errors),
                                "recipient": to_addrs[0] if to_addrs else None,
                            },
                        )
                        raise SMTPConnectionError(error_msg)

                    # Success!
                    pool.runtime.circuit_breaker.record_success()
                    pool.runtime.total_sent += 1

                    logger.info(
                        "email_sent_successfully",
                        extra={
                            "message_id": response,
                            "recipient": to_addrs[0] if to_addrs else None,
                            "server": pool.server.name,
                            "attempts": attempt,
                            "latency_ms": (time.perf_counter() - start) * 1000,
                        },
                    )

                    return EmailResult(
                        success=True,
                        message_id=response,
                        recipient=to_addrs[0] if to_addrs else None,
                        server=pool.server.name,
                        attempts=attempt,
                        latency_ms=(time.perf_counter() - start) * 1000,
                        timestamp=datetime.now(timezone.utc),
                    )

            except (aiosmtplib.SMTPAuthenticationError, SMTPAuthenticationError) as e:
                # Auth errors are permanent
                if pool is not None:
                    pool.runtime.circuit_breaker.record_failure(e)
                logger.error(
                    "smtp_authentication_failed",
                    extra={
                        "server": pool.server.name if pool is not None else "unknown",
                        "username": pool.server.username if pool is not None else None,
                        "error": str(e),
                    },
                )
                return EmailResult(
                    success=False,
                    error=str(e),
                    recipient=to_addrs[0] if to_addrs else None,
                    attempts=attempt,
                    timestamp=datetime.now(timezone.utc),
                )

            except Exception as e:
                if pool is not None:
                    pool.runtime.circuit_breaker.record_failure(e)
                logger.warning(
                    "smtp_send_attempt_failed",
                    extra={
                        "attempt": attempt,
                        "max_retries": self.max_retries,
                        "error_type": type(e).__name__,
                        "error": str(e)[:200],
                        "is_transient": is_transient_error(e),
                        "recipient": to_addrs[0] if to_addrs else None,
                    },
                )

                # Check if transient
                if not is_transient_error(e) or attempt >= self.max_retries:
                    # Queue for background retry when transient retries are exhausted
                    # and this send was not itself triggered by the retry queue.
                    if is_transient_error(e) and _queue_id is None:
                        retry_id = str(uuid.uuid4())
                        await self._retry_queue.add(
                            retry_id,
                            {
                                "_queue_id": retry_id,
                                "from_addr": from_addr,
                                "to_addrs": to_addrs,
                                "subject": subject,
                                "body_text": body_text,
                                "body_html": body_html,
                                "cc": cc,
                                "bcc": bcc,
                                "reply_to": reply_to,
                                "headers": headers,
                            },
                            str(e),
                        )
                    return EmailResult(
                        success=False,
                        error=str(e),
                        recipient=to_addrs[0] if to_addrs else None,
                        attempts=attempt,
                        timestamp=datetime.now(timezone.utc),
                    )

                # Retry with backoff
                backoff = 2 ** (attempt - 1)  # 1s, 2s, 4s, ...
                await asyncio.sleep(backoff)

        # Max retries exceeded
        return EmailResult(
            success=False,
            error="Max retries exceeded",
            recipient=to_addrs[0] if to_addrs else None,
            attempts=self.max_retries,
            timestamp=datetime.now(timezone.utc),
        )

    async def send_bulk(
        self,
        emails: list[dict[str, Any]],
        concurrency: int = 10,
    ) -> BulkSendResult:
        """
        Send multiple emails concurrently.

        Args:
            emails: List of email dictionaries (keys: from_addr, to_addrs, subject, etc.)
            concurrency: Maximum concurrent sends

        Returns:
            BulkSendResult with aggregate stats
        """
        start = time.perf_counter()

        # Semaphore for concurrency control
        sem = asyncio.Semaphore(concurrency)

        async def send_with_semaphore(email: dict[str, Any]) -> EmailResult:
            async with sem:
                return await self.send(**email)

        # Send all emails
        results = await asyncio.gather(
            *[send_with_semaphore(email) for email in emails],
            return_exceptions=False,
        )

        # Aggregate results
        success_count = sum(1 for r in results if r.success)
        failed_count = len(results) - success_count

        return BulkSendResult(
            total=len(emails),
            success_count=success_count,
            failed_count=failed_count,
            results=list(results),
            duration_seconds=time.perf_counter() - start,
        )

    def _select_server(self, from_addr: str) -> SMTPConnectionPool:
        """
        Select an SMTP server based on strategy.

        Args:
            from_addr: Sender email address (for routing)

        Returns:
            Selected connection pool
        """
        # Filter available servers (circuit breaker not open)
        available = [
            pool for pool in self._pools.values() if pool.runtime.circuit_breaker.is_available()
        ]

        if not available:
            raise SMTPConnectionError("No available SMTP servers")

        # Prefer servers pinned to this sender address
        pinned = [p for p in available if p.server.from_email == from_addr]
        if pinned:
            available = pinned

        # Priority strategy
        if self.strategy == LoadBalancingStrategy.PRIORITY:
            # Sort by priority (ascending)
            available.sort(key=lambda p: p.server.priority)
            return available[0]

        # Weighted strategy
        if self.strategy == LoadBalancingStrategy.WEIGHTED:
            # Simple weighted random
            total_weight = sum(p.server.weight for p in available)
            if total_weight == 0:
                return available[0]

            rand = random.uniform(0, total_weight)
            current = 0
            for pool in available:
                current += pool.server.weight
                if rand <= current:
                    return pool

            return available[-1]

        # Round-robin (default)
        pool = available[self._round_robin_index % len(available)]
        self._round_robin_index += 1
        return pool

    async def _retry_handler(self, data: dict[str, Any]) -> bool:
        """Process a retry queue item by re-attempting the send."""
        result = await self.send(**data)
        return result.success

    def _build_message(
        self,
        from_addr: str,
        to_addrs: list[str],
        subject: str,
        body_text: str | None = None,
        body_html: str | None = None,
        cc: list[str] | None = None,
        bcc: list[str] | None = None,
        reply_to: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> EmailMessage:
        """Build email message with sanitization."""
        msg = EmailMessage()

        # Headers
        msg["From"] = from_addr
        msg["To"] = ", ".join(to_addrs)
        msg["Subject"] = sanitize_subject(subject)
        msg["Date"] = formatdate(localtime=True)
        msg["Message-ID"] = make_msgid()

        if cc:
            msg["Cc"] = ", ".join(cc)
        if bcc:
            msg["Bcc"] = ", ".join(bcc)
        if reply_to:
            msg["Reply-To"] = reply_to

        # Custom headers (sanitized)
        if headers:
            for key, value in headers.items():
                msg[key] = sanitize_header_value(value)

        # Body
        if body_html and body_text:
            msg.set_content(body_text)
            msg.add_alternative(body_html, subtype="html")
        elif body_html:
            msg.set_content(body_html, subtype="html")
        elif body_text:
            msg.set_content(body_text)
        else:
            msg.set_content("")

        return msg

    async def close(self) -> None:
        """Close all connection pools and cleanup resources."""
        await self._retry_queue.stop()
        logger.info(
            "smtp_sender_closing",
            extra={
                "num_pools": len(self._pools),
            },
        )

        # Close all pools concurrently
        tasks = []
        for pool in self._pools.values():
            tasks.append(pool.close_all())

        if tasks:
            results = await asyncio.gather(*tasks, return_exceptions=True)

            # Log any close errors
            for i, result in enumerate(results):
                if isinstance(result, Exception):
                    logger.warning(
                        "pool_close_error",
                        extra={
                            "pool": list(self._pools.keys())[i],
                            "error": str(result),
                        },
                    )

        logger.info("smtp_sender_closed")
