# Mercury SMTP - Production-Grade Async Email Sender

A high-performance, production-ready async SMTP library with built-in resilience patterns:

- 🔄 **Connection Pooling**: Reusable SMTP connections with health checks
- ⚡ **Circuit Breaker**: Automatic failover when servers fail
- 🚦 **Rate Limiting**: Token bucket algorithm for send-rate control
- 🔁 **Retry Queue**: Exponential backoff for transient failures
- 📊 **Metrics**: Built-in latency and throughput tracking
- 🎯 **Load Balancing**: Weighted, round-robin, or priority-based server selection

Extracted from [MerCury](https://github.com/0fukuAkz/MerCury), a production email automation platform handling 1M+ emails/day.

---

## Installation

```bash
pip install mercury-smtp
```

**With Redis support** (for distributed rate limiting):
```bash
pip install mercury-smtp[redis]
```

---

## Quick Start

### Basic Usage

```python
import asyncio
from mercury_smtp import AsyncSMTPSender, SMTPServerConfig

# Configure SMTP server
server = SMTPServerConfig(
    host="smtp.gmail.com",
    port=587,
    username="user@gmail.com",
    password="app-password",
    use_tls=True,
)

async def send_email():
    async with AsyncSMTPSender([server]) as sender:
        result = await sender.send(
            from_addr="sender@example.com",
            to_addrs=["recipient@example.com"],
            subject="Hello from Mercury SMTP",
            body_text="Plain text body",
            body_html="<p>HTML body</p>",
        )
        print(f"Sent: {result.success}")

asyncio.run(send_email())
```

### Production Configuration

```python
from mercury_smtp import (
    AsyncSMTPSender,
    SMTPServerConfig,
    CircuitBreakerConfig,
    RateLimiterConfig,
)

# Multiple servers with circuit breakers and rate limits
servers = [
    SMTPServerConfig(
        name="primary",
        host="smtp1.example.com",
        port=587,
        username="user",
        password="pass",
        weight=10,  # Load balancing weight
        max_per_hour=10000,  # Rate limit
        circuit_breaker=CircuitBreakerConfig(
            failure_threshold=5,
            timeout_seconds=60,
        ),
    ),
    SMTPServerConfig(
        name="backup",
        host="smtp2.example.com",
        port=587,
        username="user",
        password="pass",
        weight=5,
        priority=1,  # Lower priority (fallback)
    ),
]

async def send_bulk():
    async with AsyncSMTPSender(
        servers=servers,
        rate_limiter=RateLimiterConfig(per_second=10.0),
        max_retries=3,
    ) as sender:
        # Send to multiple recipients
        results = await sender.send_bulk([
            {
                "from_addr": "sender@example.com",
                "to_addrs": ["user1@example.com"],
                "subject": "Welcome!",
                "body_text": "Hello User 1",
            },
            {
                "from_addr": "sender@example.com",
                "to_addrs": ["user2@example.com"],
                "subject": "Welcome!",
                "body_text": "Hello User 2",
            },
        ])
        
        print(f"Success: {results.success_count}/{results.total}")

asyncio.run(send_bulk())
```

---

## Key Features

### Connection Pooling

Maintains persistent SMTP connections per server with automatic health checks:

```python
from mercury_smtp import SMTPConnectionPool

pool = SMTPConnectionPool(
    server=server,
    max_connections=10,
    health_check_interval=60,  # seconds
)

# Connections are reused across sends
async with pool.acquire() as conn:
    await conn.send_message(msg)
```

**Benefits:**
- Reduces connection overhead (TLS handshake ~100-500ms saved per send)
- Health checks prevent using stale connections
- Automatic connection recycling

### Circuit Breaker

Prevents cascading failures by temporarily disabling failing servers:

```python
from mercury_smtp import CircuitBreakerConfig

config = CircuitBreakerConfig(
    failure_threshold=5,       # Open after 5 failures
    success_threshold=2,       # Close after 2 successes
    timeout_seconds=60,        # Half-open retry after 60s
    monitor_window_seconds=300,
)
```

**States:**
- **CLOSED**: Normal operation
- **OPEN**: Too many failures, stop trying (fails fast)
- **HALF_OPEN**: Testing recovery with limited traffic

### Rate Limiting

Token bucket algorithm for precise send-rate control:

```python
from mercury_smtp import RateLimiterConfig

limiter = RateLimiterConfig(
    per_second=10.0,   # 10 emails/second
    per_minute=500.0,  # 500 emails/minute
    per_hour=10000.0,  # 10K emails/hour
    burst_size=20,     # Allow bursts up to 20
)
```

**Storage backends:**
- **In-memory** (default): Single-process
- **Redis**: Distributed rate limiting across workers

### Retry Queue

Automatic retry with exponential backoff for transient errors:

```python
from mercury_smtp import RetryConfig

retry = RetryConfig(
    max_attempts=3,
    base_delay=60,      # Start with 60s
    max_delay=3600,     # Cap at 1 hour
    exponential_base=2, # Delay *= 2 each retry
)
```

**Retry delays:** 60s → 120s → 240s → permanent failure

---

## Performance Characteristics

See [docs/Performance.md](docs/Performance.md) for detailed benchmarks.

### Throughput

| Configuration | Emails/Min | Notes |
|--------------|------------|-------|
| Single connection | 500-1000 | Depends on SMTP server |
| Connection pool (10 conns) | 5000-10000 | Linear scaling |
| Multi-server (3 servers) | 15000-30000 | With load balancing |

### Latency

| Operation | P50 | P95 | P99 |
|-----------|-----|-----|-----|
| Connection handshake | 100ms | 300ms | 500ms |
| Send (pooled) | 50ms | 150ms | 300ms |
| Send (new connection) | 150ms | 450ms | 800ms |

### Resource Usage

- **Memory**: 5-10 MB baseline + ~50 KB per connection
- **CPU**: <5% (I/O bound)
- **Connections**: 1-10 per server (configurable)

---

## Advanced Usage

### Custom Error Handling

```python
from mercury_smtp import SMTPError, SMTPAuthenticationError

async def send_with_retry():
    try:
        result = await sender.send(...)
    except SMTPAuthenticationError:
        # Update credentials
        await update_smtp_credentials()
    except SMTPError as e:
        # Handle other SMTP errors
        logger.error(f"Send failed: {e}")
```

### Metrics Integration

```python
from mercury_smtp import AsyncSMTPSender

class MetricsSender(AsyncSMTPSender):
    async def _record_send(self, result):
        # Your metrics system
        prometheus.inc("emails_sent", labels={"status": result.status})
        await super()._record_send(result)
```

### Server Selection Strategy

```python
from mercury_smtp import LoadBalancingStrategy

sender = AsyncSMTPSender(
    servers=servers,
    strategy=LoadBalancingStrategy.WEIGHTED,  # or ROUND_ROBIN, PRIORITY
)
```

---

## Testing

```bash
# Run tests
pytest

# With coverage
pytest --cov=mercury_smtp --cov-report=html

# Async tests
pytest tests/test_sender.py -v
```

---

## Architecture

```
┌─────────────────────────────────────────────────────┐
│                 AsyncSMTPSender                     │
│  (Orchestration, load balancing, error recovery)    │
└─────────────────────────────────────────────────────┘
                        │
        ┌───────────────┼───────────────┐
        ▼               ▼               ▼
┌──────────────┐ ┌──────────────┐ ┌──────────────┐
│ Connection   │ │   Circuit    │ │     Rate     │
│    Pool      │ │   Breaker    │ │   Limiter    │
└──────────────┘ └──────────────┘ └──────────────┘
        │               │               │
        └───────────────┼───────────────┘
                        ▼
                ┌──────────────┐
                │  aiosmtplib  │
                │ (SMTP client)│
                └──────────────┘
```

---

## Comparison

| Feature | mercury-smtp | aiosmtplib | smtplib |
|---------|--------------|------------|---------|
| Async | ✅ | ✅ | ❌ |
| Connection pooling | ✅ | ❌ | ❌ |
| Circuit breaker | ✅ | ❌ | ❌ |
| Rate limiting | ✅ | ❌ | ❌ |
| Auto retry | ✅ | ❌ | ❌ |
| Load balancing | ✅ | ❌ | ❌ |
| Production-ready | ✅ | ⚠️ | ⚠️ |

---

## Contributing

Contributions welcome! See [CONTRIBUTING.md](../CONTRIBUTING.md) for guidelines.

---

## License

MIT License - see [LICENSE](../LICENSE) for details.

---

## Related Projects

- [MerCury](https://github.com/0fukuAkz/MerCury) - Full-featured email automation platform
- [aiosmtplib](https://github.com/cole/aiosmtplib) - Async SMTP client (underlying transport)

---

## Support

- 📖 [Documentation](docs/)
- 🐛 [Issue Tracker](https://github.com/0fukuAkz/MerCury/issues)
- 💬 [Discussions](https://github.com/0fukuAkz/MerCury/discussions)
