"""Tests for TokenBucket, RateLimiter, and AdaptiveRateLimiter."""

from unittest.mock import AsyncMock, patch

import pytest

from msmtp.rate_limiter import AdaptiveRateLimiter, RateLimiter, RateLimiterConfig, TokenBucket


class TestTokenBucket:
    def test_starts_full(self):
        bucket = TokenBucket(rate=10, capacity=5)
        assert bucket.tokens == 5

    @pytest.mark.asyncio
    async def test_acquire_succeeds_when_tokens_available(self):
        bucket = TokenBucket(rate=10, capacity=5)
        assert await bucket.acquire(1) is True
        assert bucket.tokens == pytest.approx(4, abs=0.01)

    @pytest.mark.asyncio
    async def test_acquire_fails_immediately_when_rate_zero_and_insufficient(self):
        bucket = TokenBucket(rate=0, capacity=1)
        bucket.tokens = 0
        assert await bucket.acquire(1) is False

    @pytest.mark.asyncio
    async def test_acquire_times_out_when_wait_exceeds_timeout(self):
        bucket = TokenBucket(rate=1, capacity=1)
        bucket.tokens = 0
        # Needs ~1s to refill 1 token, but we only allow 0.01s.
        assert await bucket.acquire(1, timeout=0.01) is False

    @pytest.mark.asyncio
    async def test_acquire_waits_and_succeeds_once_refilled(self):
        bucket = TokenBucket(rate=100, capacity=1)
        bucket.tokens = 0
        # At rate=100/s, refilling 1 token takes ~10ms - well within timeout.
        assert await bucket.acquire(1, timeout=1.0) is True

    def test_try_acquire_sync_success_and_failure(self):
        bucket = TokenBucket(rate=0, capacity=1)
        assert bucket.try_acquire(1) is True
        assert bucket.try_acquire(1) is False

    @pytest.mark.asyncio
    async def test_try_acquire_async_success_and_failure(self):
        bucket = TokenBucket(rate=0, capacity=1)
        assert await bucket.try_acquire_async(1) is True
        assert await bucket.try_acquire_async(1) is False

    def test_refill_caps_at_capacity(self):
        bucket = TokenBucket(rate=1000, capacity=5)
        bucket.tokens = 0
        bucket.last_update -= 100  # Pretend a huge amount of time passed.
        bucket._refill()
        assert bucket.tokens == 5


class TestRateLimiter:
    def test_no_config_has_no_buckets(self):
        limiter = RateLimiter()
        assert limiter.buckets == {}

    @pytest.mark.asyncio
    async def test_acquire_with_no_buckets_always_succeeds(self):
        limiter = RateLimiter()
        assert await limiter.acquire() is True
        assert limiter.stats["total_acquired"] == 0  # Short-circuited before stats update.

    def test_creates_expected_buckets(self):
        limiter = RateLimiter(
            RateLimiterConfig(per_second=10, per_minute=100, per_hour=1000, burst_size=20)
        )
        assert set(limiter.buckets.keys()) == {"second", "minute", "hour"}

    @pytest.mark.asyncio
    async def test_acquire_increments_stats_when_buckets_present(self):
        limiter = RateLimiter(RateLimiterConfig(per_second=1000, burst_size=50))
        assert await limiter.acquire() is True
        assert limiter.stats["total_acquired"] == 1

    @pytest.mark.asyncio
    async def test_acquire_fails_when_any_bucket_denies(self):
        limiter = RateLimiter(RateLimiterConfig(per_second=1000, burst_size=50))
        # Deplete and lock down the bucket so it can never refill in time.
        bucket = limiter.buckets["second"]
        bucket.tokens = 0
        bucket.rate = 0
        assert await limiter.acquire(timeout=0.01) is False

    def test_try_acquire_non_blocking_success(self):
        limiter = RateLimiter(RateLimiterConfig(per_second=10, burst_size=5))
        assert limiter.try_acquire() is True

    def test_try_acquire_non_blocking_failure_when_empty(self):
        limiter = RateLimiter(RateLimiterConfig(per_second=10, burst_size=5))
        for bucket in limiter.buckets.values():
            bucket.tokens = 0
        assert limiter.try_acquire() is False

    def test_try_acquire_with_no_buckets_succeeds(self):
        limiter = RateLimiter()
        assert limiter.try_acquire() is True

    def test_from_config_dict(self):
        limiter = RateLimiter.from_config(
            {"per_second": 5, "per_minute": 0, "per_hour": 0, "burst_size": 15}
        )
        assert "second" in limiter.buckets
        assert "minute" not in limiter.buckets

    def test_get_stats_returns_dict(self):
        limiter = RateLimiter()
        stats = limiter.get_stats()
        assert stats == {"total_acquired": 0, "total_waited": 0}


class TestAdaptiveRateLimiter:
    def test_record_success_increases_factor_after_ten(self):
        limiter = AdaptiveRateLimiter()
        for _ in range(9):
            limiter.record_success()
        assert limiter.adjustment_factor == 1.0  # Not yet triggered.

        limiter.record_success()
        assert limiter.adjustment_factor == pytest.approx(1.1)
        assert limiter.consecutive_successes == 0

    def test_record_rate_limit_halves_factor_and_resets_successes(self):
        limiter = AdaptiveRateLimiter()
        limiter.consecutive_successes = 5
        limiter.record_rate_limit()

        assert limiter.adjustment_factor == pytest.approx(0.5)
        assert limiter.consecutive_successes == 0
        assert limiter.consecutive_failures == 1

    def test_adjustment_factor_bounded_by_min(self):
        limiter = AdaptiveRateLimiter()
        for _ in range(20):
            limiter.record_rate_limit()
        assert limiter.adjustment_factor >= limiter.min_factor

    def test_adjustment_factor_bounded_by_max(self):
        limiter = AdaptiveRateLimiter()
        for _ in range(200):
            limiter.consecutive_successes = 9
            limiter.record_success()
        assert limiter.adjustment_factor <= limiter.max_factor

    @pytest.mark.asyncio
    async def test_acquire_adds_extra_delay_on_failure(self):
        limiter = AdaptiveRateLimiter(RateLimiterConfig(per_second=1000, burst_size=50))
        bucket = limiter.buckets["second"]
        bucket.tokens = 0
        bucket.rate = 0  # Force RateLimiter.acquire() to return False deterministically.

        with patch("msmtp.rate_limiter.asyncio.sleep", new=AsyncMock()) as mock_sleep:
            result = await limiter.acquire(timeout=0.01)

        assert result is False
        mock_sleep.assert_awaited()
