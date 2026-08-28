"""Type definitions and data classes for Mercury SMTP."""

import logging
import ssl
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class SMTPServerConfig:
    """
    Configuration for an SMTP server.

    Attributes:
        host: SMTP server hostname
        port: SMTP server port (25, 587, 465)
        username: SMTP authentication username
        password: SMTP authentication password
        use_tls: Use STARTTLS for encryption
        use_ssl: Use implicit SSL/TLS (port 465)
        name: Human-readable server name
        weight: Load balancing weight (higher = more traffic)
        priority: Server priority (0 = highest, used for failover)
        max_per_hour: Maximum emails per hour (0 = unlimited)
        from_email: Email address this server sends from (for routing)
        timeout: Connection timeout in seconds
        verify_ssl: Verify SSL/TLS certificates (recommended: True)
        ssl_context: Custom SSL context for advanced configuration
        password_provider: Optional callable to retrieve password from secrets manager
    """

    host: str
    port: int = 587
    username: str | None = None
    password: str | None = None
    use_tls: bool = True
    use_ssl: bool = False
    name: str | None = None
    weight: int = 10
    priority: int = 0
    max_per_hour: int = 0
    from_email: str | None = None
    timeout: int = 30
    verify_ssl: bool = True
    ssl_context: ssl.SSLContext | None = None
    password_provider: Callable[[], str] | None = None

    def __post_init__(self) -> None:
        """Set default name and validate security settings."""
        if self.name is None:
            self.name = f"{self.host}:{self.port}"

        # Security warnings
        if not self.use_tls and not self.use_ssl:
            logger.warning(
                "insecure_smtp_config",
                extra={
                    "server": self.name,
                    "host": self.host,
                    "port": self.port,
                    "issue": "TLS/SSL disabled",
                    "risk": "credentials_sent_in_plaintext",
                },
            )

        if not self.verify_ssl and (self.use_tls or self.use_ssl):
            logger.warning(
                "ssl_verification_disabled",
                extra={
                    "server": self.name,
                    "host": self.host,
                    "port": self.port,
                    "issue": "SSL certificate verification disabled",
                    "risk": "man_in_the_middle_attack",
                },
            )

    def get_password(self) -> str:
        """Get password from provider or direct value."""
        if self.password_provider:
            return self.password_provider()
        return self.password or ""

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary (excludes sensitive data)."""
        return {
            "host": self.host,
            "port": self.port,
            "username": self.username,
            "use_tls": self.use_tls,
            "use_ssl": self.use_ssl,
            "name": self.name,
            "weight": self.weight,
            "priority": self.priority,
            "max_per_hour": self.max_per_hour,
            "from_email": self.from_email,
            "timeout": self.timeout,
            "verify_ssl": self.verify_ssl,
            "has_password": bool(self.password or self.password_provider),
        }


@dataclass
class EmailResult:
    """
    Result of a single email send operation.

    Attributes:
        success: Whether the send succeeded
        message_id: SMTP message ID
        recipient: Email address of recipient
        error: Error message if failed
        server: Name of SMTP server used
        attempts: Number of send attempts
        latency_ms: Send latency in milliseconds
        timestamp: When the send completed
    """

    success: bool
    message_id: str | None = None
    recipient: str | None = None
    error: str | None = None
    server: str | None = None
    attempts: int = 1
    latency_ms: float | None = None
    timestamp: datetime | None = None

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary."""
        return {
            "success": self.success,
            "message_id": self.message_id,
            "recipient": self.recipient,
            "error": self.error,
            "server": self.server,
            "attempts": self.attempts,
            "latency_ms": self.latency_ms,
            "timestamp": self.timestamp.isoformat() if self.timestamp else None,
        }


@dataclass
class BulkSendResult:
    """
    Result of a bulk send operation.

    Attributes:
        total: Total emails attempted
        success_count: Number of successful sends
        failed_count: Number of failed sends
        results: Individual results per email
        duration_seconds: Total duration
    """

    total: int
    success_count: int = 0
    failed_count: int = 0
    results: list[EmailResult] = field(default_factory=list)
    duration_seconds: float | None = None

    @property
    def success_rate(self) -> float:
        """Calculate success rate as percentage."""
        if self.total == 0:
            return 0.0
        return (self.success_count / self.total) * 100

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary."""
        return {
            "total": self.total,
            "success_count": self.success_count,
            "failed_count": self.failed_count,
            "success_rate": self.success_rate,
            "duration_seconds": self.duration_seconds,
            "results": [r.to_dict() for r in self.results],
        }
