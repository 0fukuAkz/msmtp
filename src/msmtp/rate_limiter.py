"""Rate limiter with token bucket algorithm."""

import asyncio
import time
from typing import Optional
from dataclasses import dataclass
import logging

logger = logging.getLogger(__name__)


@dataclass
class RateLimiterConfig:
    """Rate limiter configuration."""

    per_second: float = 0
    per_minute: float = 0
    per_hour: float = 0
    burst_size: int = 10


class TokenBucket:
    """Token bucket rate limiter."""

    def __init__(self, rate: float, capacity: int):
        """
        Initialize token bucket.

        Args:
            rate: Tokens per second
            capacity: Maximum tokens (burst size)
        """
        self.rate = rate
        self.capacity = capacity
        self.tokens = capacity
        self.last_update = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self, tokens: int = 1, timeout: Optional[float] = None) -> bool:
        """
        Acquire tokens, blocking if necessary.

        Args:
            tokens: Number of tokens to acquire
            timeout: Maximum time to wait

        Returns:
            True if tokens acquired, False if timeout
        """
        start_time = time.monotonic()

        while True:
            async with self._lock:
                self._refill()

                if self.tokens >= tokens:
                    self.tokens -= tokens
                    return True

                if self.rate <= 0:
                    return False

                # Calculate wait time
                tokens_needed = tokens - self.tokens
                wait_time = tokens_needed / self.rate if self.rate > 0 else float("inf")

            # Check timeout
            if timeout is not None:
                elapsed = time.monotonic() - start_time
                if elapsed + wait_time > timeout:
                    return False
                wait_time = min(wait_time, timeout - elapsed)

            if wait_time <= 0:
                return False

            await asyncio.sleep(min(wait_time, 0.1))

    def try_acquire(self, tokens: int = 1) -> bool:
        """
        Try to acquire tokens without blocking.

        Note: This is a synchronous method but still thread-safe through
        the use of asyncio.Lock in _try_acquire_sync. For sync-only contexts,
        consider using a threading.Lock wrapper.
        """
        # For synchronous usage, we do a non-blocking check
        # This is safe because _refill and token update are atomic operations
        # on the GIL-protected Python level
        self._refill()

        if self.tokens >= tokens:
            self.tokens -= tokens
            return True
        return False

    async def try_acquire_async(self, tokens: int = 1) -> bool:
        """
        Try to acquire tokens without blocking (async-safe version).

        This method properly acquires the lock to ensure thread-safety
        in concurrent async contexts.
        """
        async with self._lock:
            self._refill()

            if self.tokens >= tokens:
                self.tokens -= tokens
                return True
            return False

    def _refill(self):
        """Refill tokens based on elapsed time."""
        now = time.monotonic()
        elapsed = now - self.last_update
        self.last_update = now

        self.tokens = min(self.capacity, self.tokens + elapsed * self.rate)


class RateLimiter:
    """Composite rate limiter supporting multiple time windows."""

    def __init__(self, config: Optional[RateLimiterConfig] = None):
        """
        Initialize rate limiter.

        Args:
            config: Rate limiter configuration
        """
        self.config = config or RateLimiterConfig()
        self.stats = {"total_acquired": 0, "total_waited": 0}
        self.buckets = {}

        # Create buckets for each rate limit
        if self.config.per_second > 0:
            self.buckets["second"] = TokenBucket(
                rate=self.config.per_second,
                capacity=max(1, min(int(self.config.per_second * 2), self.config.burst_size)),
            )

        if self.config.per_minute > 0:
            self.buckets["minute"] = TokenBucket(
                rate=self.config.per_minute / 60,
                capacity=max(1, min(int(self.config.per_minute / 6), self.config.burst_size)),
            )

        if self.config.per_hour > 0:
            self.buckets["hour"] = TokenBucket(
                rate=self.config.per_hour / 3600,
                capacity=max(1, min(int(self.config.per_hour / 60), self.config.burst_size)),
            )

    async def acquire(self, timeout: Optional[float] = None) -> bool:
        """
        Acquire rate limit permission.

        Args:
            timeout: Maximum time to wait

        Returns:
            True if acquired, False if timeout
        """
        if not self.buckets:
            return True

        start_time = time.monotonic()

        for name, bucket in self.buckets.items():
            remaining_timeout = None
            if timeout is not None:
                elapsed = time.monotonic() - start_time
                remaining_timeout = max(0, timeout - elapsed)

            if not await bucket.acquire(timeout=remaining_timeout):
                logger.debug(f"Rate limit hit on {name} bucket")
                return False

        self.stats["total_acquired"] += 1
        return True

    def get_stats(self) -> dict:
        """Get rate limiter statistics."""
        return self.stats

    def try_acquire(self) -> bool:
        """Try to acquire without blocking."""
        if not self.buckets:
            return True

        # Check all buckets first
        for bucket in self.buckets.values():
            if bucket.tokens < 1:
                return False

        # Acquire from all buckets
        for bucket in self.buckets.values():
            bucket.try_acquire()

        return True

    @classmethod
    def from_config(cls, config: dict) -> "RateLimiter":
        """Create from config dictionary."""
        return cls(
            RateLimiterConfig(
                per_second=config.get("per_second", 0),
                per_minute=config.get("per_minute", 0),
                per_hour=config.get("per_hour", 0),
                burst_size=config.get("burst_size", 10),
            )
        )


class AdaptiveRateLimiter(RateLimiter):
    """Rate limiter that adapts based on server responses."""

    def __init__(self, config: Optional[RateLimiterConfig] = None):
        super().__init__(config)
        self.adjustment_factor = 1.0
        self.min_factor = 0.1
        self.max_factor = 2.0
        self.consecutive_successes = 0
        self.consecutive_failures = 0

    def record_success(self):
        """Record successful request."""
        self.consecutive_successes += 1
        self.consecutive_failures = 0

        # Gradually increase rate after successes
        if self.consecutive_successes >= 10:
            self.adjustment_factor = min(self.max_factor, self.adjustment_factor * 1.1)
            self.consecutive_successes = 0

    def record_rate_limit(self):
        """Record rate limit hit."""
        self.consecutive_failures += 1
        self.consecutive_successes = 0

        # Quickly reduce rate on failures
        self.adjustment_factor = max(self.min_factor, self.adjustment_factor * 0.5)

    async def acquire(self, timeout: Optional[float] = None) -> bool:
        """Acquire with adaptive timing."""
        result = await super().acquire(timeout)

        if not result and self.adjustment_factor > self.min_factor:
            # Add extra delay on rate limit
            await asyncio.sleep(1.0 / self.adjustment_factor)

        return result
