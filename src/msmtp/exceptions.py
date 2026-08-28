"""Exception hierarchy for Mercury SMTP."""


class MercurySMTPError(Exception):
    """Base exception for all Mercury SMTP errors."""

    pass


class SMTPConnectionError(MercurySMTPError):
    """Failed to connect to SMTP server."""

    pass


class SMTPAuthenticationError(MercurySMTPError):
    """SMTP authentication failed."""

    pass


class SMTPRateLimitError(MercurySMTPError):
    """SMTP server rate limit exceeded."""

    pass


class SMTPSendError(MercurySMTPError):
    """Failed to send email via SMTP."""

    pass


def is_transient_error(exc: Exception) -> bool:
    """
    Determine if an exception represents a transient error.

    Transient errors are temporary and may succeed on retry.

    Args:
        exc: Exception to check

    Returns:
        True if error is likely transient
    """
    import aiosmtplib

    # Permanent errors (don't retry)
    if isinstance(exc, SMTPAuthenticationError):
        return False

    # Connection/network errors are usually transient
    if isinstance(exc, (SMTPConnectionError, ConnectionError, TimeoutError)):
        return True

    # SMTP library errors
    if isinstance(exc, aiosmtplib.SMTPException):
        # Check status codes
        if hasattr(exc, "code"):
            code = exc.code
            # 4xx are transient, 5xx are permanent
            if code and 400 <= code < 500:
                return True
            if code and 500 <= code < 600:
                return False

        # Rate limiting is transient
        if "rate limit" in str(exc).lower():
            return True

    # Default: treat as transient
    return True


def categorize_exception(exc: Exception) -> str:
    """
    Categorize an exception for logging/metrics.

    Args:
        exc: Exception to categorize

    Returns:
        Category string (auth, connection, rate_limit, send, unknown)
    """
    if isinstance(exc, SMTPAuthenticationError):
        return "auth"
    if isinstance(exc, SMTPConnectionError):
        return "connection"
    if isinstance(exc, SMTPRateLimitError):
        return "rate_limit"
    if isinstance(exc, SMTPSendError):
        return "send"
    return "unknown"
