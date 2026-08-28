"""Production example: bulk sending with all resilience features."""

import asyncio
import os

from msmtp import (
    AsyncSMTPSender,
    CircuitBreakerConfig,
    LoadBalancingStrategy,
    RateLimiterConfig,
    RetryConfig,
    SMTPServerConfig,
)


async def main():
    """Send bulk emails with production configuration."""
    # Configure multiple SMTP servers for load balancing
    servers = [
        SMTPServerConfig(
            name="primary",
            host="smtp.sendgrid.net",
            port=587,
            username="apikey",
            password=os.getenv("SENDGRID_API_KEY", "your-api-key"),
            use_tls=True,
            weight=10,  # Higher weight = more traffic
            priority=0,  # Lower priority number = higher priority
            max_per_hour=10000,
        ),
        SMTPServerConfig(
            name="backup",
            host="smtp.gmail.com",
            port=587,
            username=os.getenv("GMAIL_USER", "backup@gmail.com"),
            password=os.getenv("GMAIL_PASS", "app-password"),
            use_tls=True,
            weight=5,  # Lower weight = less traffic
            priority=1,  # Fallback server
            max_per_hour=500,
        ),
    ]

    # Production-tuned rate limiter (allow bursts)
    rate_limiter = RateLimiterConfig(
        per_second=20.0,  # 20 emails/sec sustained
        burst_size=50,  # Allow up to 50 emails in a burst
    )

    # Retry configuration
    retry_config = RetryConfig(
        max_attempts=3,
        base_delay=60,  # 1 minute initial backoff
        max_delay=3600,  # 1 hour max backoff
        exponential_base=2,
    )

    # Create sender with production config
    async with AsyncSMTPSender(
        servers=servers,
        rate_limiter=rate_limiter,
        retry_config=retry_config,
        max_retries=3,
        strategy=LoadBalancingStrategy.WEIGHTED,
    ) as sender:
        # Prepare bulk emails
        emails = [
            {
                "from_addr": "notifications@example.com",
                "to_addrs": [f"user{i}@example.com"],
                "subject": f"Weekly Report #{i}",
                "body_text": f"Hello User {i},\n\nYour weekly report is ready.",
                "body_html": f"<p>Hello User {i},</p><p>Your <b>weekly report</b> is ready.</p>",
            }
            for i in range(100)
        ]

        # Send bulk emails (up to 10 concurrent)
        print(f"Sending {len(emails)} emails...")
        result = await sender.send_bulk(emails, concurrency=10)

        # Print results
        print(f"\nResults:")
        print(f"  Total:   {result.total}")
        print(f"  Success: {result.success_count} ({result.success_rate:.1f}%)")
        print(f"  Failed:  {result.failed_count}")
        print(f"  Duration: {result.duration_seconds:.1f}s")
        print(
            f"  Throughput: {result.success_count / result.duration_seconds:.1f} emails/sec"
        )

        # Print failed emails
        if result.failed_count > 0:
            print(f"\nFailed emails:")
            for r in result.results:
                if not r.success:
                    print(f"  - {r.recipient}: {r.error}")


if __name__ == "__main__":
    asyncio.run(main())
