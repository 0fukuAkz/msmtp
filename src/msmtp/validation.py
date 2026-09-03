"""Email validation and security utilities."""

import logging
import re
from email.utils import parseaddr

logger = logging.getLogger(__name__)


def validate_email_address(email: str, allow_empty_name: bool = True) -> None:
    """
    Validate email address format and security.

    Args:
        email: Email address to validate
        allow_empty_name: Allow email without display name

    Raises:
        ValueError: If email is invalid or potentially unsafe
    """
    if not email or not isinstance(email, str):
        raise ValueError("Email address must be a non-empty string")

    # Check for header injection attempts
    if "\n" in email or "\r" in email:
        raise ValueError(
            f"Email address contains newline characters (header injection attempt): {email[:50]}"
        )

    if "\x00" in email:
        raise ValueError(f"Email address contains null bytes: {email[:50]}")

    # Parse email address
    name, addr = parseaddr(email)

    if not addr:
        raise ValueError(f"Invalid email address format: {email[:50]}")

    # Validate email format with regex
    # RFC 5322 simplified pattern
    email_pattern = re.compile(
        r"^[a-zA-Z0-9.!#$%&\'*+/=?^_`{|}~-]+@"
        r"[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?"
        r"(?:\.[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?)*$"
    )

    if not email_pattern.match(addr):
        raise ValueError(f"Invalid email address format: {addr}")

    # Check for suspicious patterns
    if ".." in addr:
        raise ValueError(f"Email address contains consecutive dots: {addr}")

    if addr.startswith(".") or addr.startswith("@"):
        raise ValueError(f"Email address has invalid start character: {addr}")

    if addr.endswith(".") or addr.endswith("@"):
        raise ValueError(f"Email address has invalid end character: {addr}")

    # Check local part length (max 64 chars before @)
    local_part = addr.split("@")[0]
    if len(local_part) > 64:
        raise ValueError(f"Email local part exceeds 64 characters: {local_part[:64]}")

    # Check domain part
    domain = addr.split("@")[1] if "@" in addr else ""
    if len(domain) > 253:
        raise ValueError(f"Email domain exceeds 253 characters: {domain[:50]}")

    # Log validation for security audit
    logger.debug(
        "email_validated",
        extra={
            "email_address": addr,
            "has_display_name": bool(name),
            "local_length": len(local_part),
            "domain": domain,
        },
    )


def validate_email_list(emails: list[str]) -> None:
    """
    Validate a list of email addresses.

    Args:
        emails: List of email addresses to validate

    Raises:
        ValueError: If any email is invalid
    """
    if not isinstance(emails, list):
        raise ValueError("Emails must be provided as a list")

    if not emails:
        raise ValueError("Email list cannot be empty")

    for email in emails:
        validate_email_address(email)


def sanitize_subject(subject: str) -> str:
    """
    Sanitize email subject to prevent header injection.

    Args:
        subject: Email subject line

    Returns:
        Sanitized subject
    """
    if not subject:
        return ""

    # Remove newlines and null bytes
    sanitized = subject.replace("\n", " ").replace("\r", " ").replace("\x00", "")

    # Trim excessive whitespace
    sanitized = " ".join(sanitized.split())

    # Truncate to reasonable length (998 chars per RFC 5322)
    if len(sanitized) > 998:
        logger.warning(
            "subject_truncated",
            extra={
                "original_length": len(subject),
                "truncated_length": 998,
            },
        )
        sanitized = sanitized[:995] + "..."

    return sanitized


def sanitize_header_value(value: str) -> str:
    """
    Sanitize custom header value.

    Args:
        value: Header value to sanitize

    Returns:
        Sanitized value
    """
    if not value:
        return ""

    # Remove newlines and null bytes
    sanitized = value.replace("\n", " ").replace("\r", " ").replace("\x00", "")

    # Trim whitespace
    sanitized = sanitized.strip()

    return sanitized
