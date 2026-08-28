"""Mercury SMTP - Production-grade async SMTP sender.

A high-performance async SMTP library with connection pooling, circuit breakers,
rate limiting, and automatic retry for transient failures.

Example:
    >>> import asyncio
    >>> from msmtp import AsyncSMTPSender, SMTPServerConfig
    >>>
    >>> server = SMTPServerConfig(
    ...     host="smtp.gmail.com",
    ...     port=587,
    ...     username="user@gmail.com",
    ...     password="app-password",
    ...     use_tls=True,
    ... )
    >>>
    >>> async def send():
    ...     async with AsyncSMTPSender([server]) as sender:
    ...         result = await sender.send(
    ...             from_addr="sender@example.com",
    ...             to_addrs=["recipient@example.com"],
    ...             subject="Hello",
    ...             body_text="Hello World",
    ...         )
    ...         print(f"Sent: {result.success}")
    >>>
    >>> asyncio.run(send())
"""

from .circuit_breaker import (
    CircuitBreaker,
    CircuitBreakerConfig,
    CircuitBreakerStats,
    CircuitState,
)
from .connection_pool import (
    AsyncConnectionPool,
    ConnectionPoolException,
    SMTPConnectionPool,
    SMTPServerConfig,
    SMTPServerRuntime,
)
from .exceptions import (
    MercurySMTPError,
    SMTPAuthenticationError,
    SMTPConnectionError,
    SMTPRateLimitError,
    SMTPSendError,
)
from .rate_limiter import (
    RateLimiter,
    RateLimiterConfig,
    TokenBucket,
)
from .retry_queue import (
    RetryConfig,
    RetryItem,
    RetryQueue,
    RetryStatus,
)
from .sender import (
    AsyncSMTPSender,
    BulkSendResult,
    EmailResult,
    LoadBalancingStrategy,
)
from .types import (
    SMTPServerConfig as ServerConfig,  # Alias for backwards compat
)
from .validation import (
    validate_email_address,
    validate_email_list,
    sanitize_subject,
    sanitize_header_value,
)

__version__ = "1.0.0"

__all__ = [
    # Core sender
    "AsyncSMTPSender",
    "EmailResult",
    "BulkSendResult",
    "LoadBalancingStrategy",
    # Configuration
    "SMTPServerConfig",
    "ServerConfig",
    "CircuitBreakerConfig",
    "RateLimiterConfig",
    "RetryConfig",
    # Components
    "SMTPConnectionPool",
    "AsyncConnectionPool",
    "CircuitBreaker",
    "CircuitBreakerStats",
    "CircuitState",
    "RateLimiter",
    "TokenBucket",
    "RetryQueue",
    "RetryStatus",
    "RetryItem",
    # Runtime
    "SMTPServerRuntime",
    # Exceptions
    "MercurySMTPError",
    "SMTPAuthenticationError",
    "SMTPConnectionError",
    "SMTPRateLimitError",
    "SMTPSendError",
    "ConnectionPoolException",
]
