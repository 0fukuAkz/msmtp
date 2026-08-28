"""Circuit breaker pattern for SMTP servers."""

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any

logger = logging.getLogger(__name__)


class CircuitState(Enum):
    """Circuit breaker states."""

    CLOSED = "closed"  # Normal operation
    OPEN = "open"  # Too many failures, stop trying
    HALF_OPEN = "half_open"  # Testing if service recovered


@dataclass
class CircuitBreakerConfig:
    """Configuration for circuit breaker."""

    failure_threshold: int = 5  # Open circuit after N failures
    success_threshold: int = 2  # Close circuit after N successes in half-open
    timeout_seconds: int = 60  # Time to wait before trying half-open
    monitor_window_seconds: int = 300  # Rolling window for failure counting


@dataclass
class CircuitBreakerStats:
    """Statistics for circuit breaker."""

    state: CircuitState
    failure_count: int = 0
    success_count: int = 0
    last_failure_time: datetime | None = None
    last_success_time: datetime | None = None
    opened_at: datetime | None = None
    total_opens: int = 0
    total_trips: int = 0  # Total state changes
    # Most-recent failure messages (kept small — last 5). The root cause
    # of a circuit-open used to be invisible: the breaker would log "5
    # failures in 300s" and operators had to dig through the per-recipient
    # log to find that all 5 were the same iCloud 5.7.0 reject. Keeping
    # the last few errors on the stats lets us include them in the
    # OPENING log line AND in the "No SMTP servers available" cascade
    # error so the cause is visible without log archaeology.
    last_error_messages: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "state": self.state.value,
            "failure_count": self.failure_count,
            "success_count": self.success_count,
            "last_failure_time": self.last_failure_time.isoformat()
            if self.last_failure_time
            else None,
            "last_success_time": self.last_success_time.isoformat()
            if self.last_success_time
            else None,
            "opened_at": self.opened_at.isoformat() if self.opened_at else None,
            "total_opens": self.total_opens,
            "total_trips": self.total_trips,
            "last_error_messages": list(self.last_error_messages),
        }


class CircuitBreaker:
    """
    Circuit breaker for SMTP servers.

    Prevents repeated attempts to failing servers by temporarily
    disabling them after consecutive failures.
    """

    def __init__(self, server_name: str, config: CircuitBreakerConfig | None = None):
        """
        Initialize circuit breaker.

        Args:
            server_name: Name of SMTP server being protected
            config: Circuit breaker configuration
        """
        self.server_name = server_name
        self.config = config or CircuitBreakerConfig()
        self._stats = CircuitBreakerStats(state=CircuitState.CLOSED)

        # Rolling window for failure tracking
        self._recent_failures: list[datetime] = []

    def is_available(self) -> bool:
        """
        Check if circuit allows operations.

        Returns:
            True if circuit is closed or half-open
        """
        current_state = self._get_current_state()

        if current_state == CircuitState.OPEN:
            logger.warning(
                "circuit_breaker_open",
                extra={
                    "server": self.server_name,
                    "state": current_state.value,
                    "failure_count": self._stats.failure_count,
                    "last_errors": self._stats.last_error_messages[:3],
                },
            )
            return False

        return True

    def _get_current_state(self) -> CircuitState:
        """
        Get current circuit state, handling automatic transitions.

        Returns:
            Current circuit state
        """
        # If closed, stay closed
        if self._stats.state == CircuitState.CLOSED:
            return CircuitState.CLOSED

        # If open, check if timeout elapsed
        if self._stats.state == CircuitState.OPEN:
            if self._stats.opened_at:
                elapsed = (datetime.now(timezone.utc) - self._stats.opened_at).total_seconds()
                if elapsed >= self.config.timeout_seconds:
                    # Transition to half-open
                    logger.info(
                        f"🔄 Circuit breaker transitioning to HALF-OPEN for {self.server_name} "
                        f"(timeout elapsed: {elapsed:.1f}s)"
                    )
                    self._stats.state = CircuitState.HALF_OPEN
                    self._stats.success_count = 0
                    self._stats.total_trips += 1
                    return CircuitState.HALF_OPEN
            return CircuitState.OPEN

        # If half-open, stay half-open
        return CircuitState.HALF_OPEN

    def record_success(self) -> None:
        """Record successful operation."""
        current_state = self._get_current_state()
        now = datetime.now(timezone.utc)

        self._stats.last_success_time = now

        if current_state == CircuitState.HALF_OPEN:
            self._stats.success_count += 1

            # Close circuit if enough successes
            if self._stats.success_count >= self.config.success_threshold:
                logger.info(
                    "circuit_breaker_closed",
                    extra={
                        "server": self.server_name,
                        "success_count": self._stats.success_count,
                        "success_threshold": self.config.success_threshold,
                    },
                )
                self._stats.state = CircuitState.CLOSED
                self._stats.failure_count = 0
                self._stats.success_count = 0
                self._recent_failures.clear()
                self._stats.total_trips += 1

        elif current_state == CircuitState.CLOSED:
            # Reset failure counter on success
            if self._stats.failure_count > 0:
                self._stats.failure_count = 0
                self._recent_failures.clear()

    def record_failure(self, error: Exception) -> None:
        """
        Record failed operation.

        Args:
            error: Exception that occurred
        """
        current_state = self._get_current_state()
        now = datetime.now(timezone.utc)

        self._stats.last_failure_time = now
        self._stats.failure_count += 1
        self._recent_failures.append(now)

        # Capture the error text on the stats (cap at 5 entries, FIFO).
        # The actual diagnostic value is the unique-message set — repeated
        # identical 5.7.0 rejects from iCloud occupy all 5 slots and tell
        # the operator nothing they don't already know from one. So we
        # dedupe by message text before appending.
        msg = f"{type(error).__name__}: {str(error)[:200]}"
        if msg not in self._stats.last_error_messages:
            self._stats.last_error_messages.append(msg)
            if len(self._stats.last_error_messages) > 5:
                self._stats.last_error_messages.pop(0)

        # Clean old failures outside monitoring window
        cutoff = now - timedelta(seconds=self.config.monitor_window_seconds)
        self._recent_failures = [f for f in self._recent_failures if f > cutoff]

        # Check if we should open the circuit
        if current_state == CircuitState.CLOSED:
            if len(self._recent_failures) >= self.config.failure_threshold:
                # Loud-log the root cause(s) on the same line as the OPEN
                # event. The previous log just said "5 failures in 300s"
                # with no hint of what kind of failures — operators had
                # to grep failed-emails.txt to find that all 5 were the
                # same iCloud 5.7.0 reject. Now the cause is visible
                # inline.
                causes = " | ".join(self._stats.last_error_messages) or "(no error captured)"
                logger.error(
                    "⚠️  Circuit breaker OPENING for %s "
                    "(%d failures in %ds). Recent unique errors: %s",
                    self.server_name,
                    len(self._recent_failures),
                    self.config.monitor_window_seconds,
                    causes,
                )
                self._stats.state = CircuitState.OPEN
                self._stats.opened_at = now
                self._stats.total_opens += 1
                self._stats.total_trips += 1

        elif current_state == CircuitState.HALF_OPEN:
            # Any failure in half-open immediately opens circuit
            logger.warning(
                "⚠️  Circuit breaker RE-OPENING for %s (failure during half-open state): %s",
                self.server_name,
                msg,
            )
            self._stats.state = CircuitState.OPEN
            self._stats.opened_at = now
            self._stats.success_count = 0
            self._stats.total_opens += 1
            self._stats.total_trips += 1

    def force_open(self) -> None:
        """Manually open circuit (for maintenance, etc.)."""
        logger.warning(f"🔒 Manually opening circuit for {self.server_name}")
        self._stats.state = CircuitState.OPEN
        self._stats.opened_at = datetime.now(timezone.utc)
        self._stats.total_opens += 1

    def force_close(self) -> None:
        """Manually close circuit (override)."""
        logger.info(f"🔓 Manually closing circuit for {self.server_name}")
        self._stats.state = CircuitState.CLOSED
        self._stats.failure_count = 0
        self._stats.success_count = 0
        self._recent_failures.clear()

    def get_stats(self) -> dict[str, Any]:
        """Get circuit breaker statistics."""
        current_state = self._get_current_state()

        stats = self._stats.to_dict()
        stats["state"] = current_state.value  # Get current state
        stats["recent_failures"] = len(self._recent_failures)
        stats["is_available"] = current_state != CircuitState.OPEN

        if self._stats.opened_at and current_state == CircuitState.OPEN:
            elapsed = (datetime.now(timezone.utc) - self._stats.opened_at).total_seconds()
            stats["seconds_until_half_open"] = max(0, self.config.timeout_seconds - elapsed)

        return stats

    def reset(self) -> None:
        """Reset circuit breaker to initial state."""
        logger.info(f"🔄 Resetting circuit breaker for {self.server_name}")
        self._stats = CircuitBreakerStats(state=CircuitState.CLOSED)
        self._recent_failures.clear()
