# Quick Start: New Security & Testing Features

## Installation

```bash
# Create virtual environment
python -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate

# Install with dev dependencies
pip install -e ".[dev]"
```

---

## 1. Email Validation

### Basic Validation

```python
from mercury_smtp import validate_email_address, validate_email_list

# Validate single email
try:
    validate_email_address("user@example.com")
    print("✅ Valid email")
except ValueError as e:
    print(f"❌ Invalid: {e}")

# Validate multiple emails
try:
    validate_email_list(["user1@example.com", "user2@example.com"])
    print("✅ All emails valid")
except ValueError as e:
    print(f"❌ Invalid email in list: {e}")
```

### Header Sanitization

```python
from mercury_smtp import sanitize_subject, sanitize_header_value

# Prevent header injection
subject = "Important Update\nBcc: attacker@evil.com"
safe_subject = sanitize_subject(subject)
print(safe_subject)  # "Important Update Bcc: attacker@evil.com"

# Sanitize custom headers
custom_header = "Value\r\nInjected-Header: malicious"
safe_value = sanitize_header_value(custom_header)
print(safe_value)  # "Value Injected-Header: malicious"
```

### Automatic Validation in Sender

```python
from mercury_smtp import AsyncSMTPSender, SMTPServerConfig

server = SMTPServerConfig(host="smtp.example.com")

async with AsyncSMTPSender([server]) as sender:
    # Validation happens automatically
    result = await sender.send(
        from_addr="sender@example.com",
        to_addrs=["invalid-email"],  # ❌ Will fail validation
        subject="Test",
        body_text="Hello",
    )
    
    if not result.success:
        print(f"Failed: {result.error}")  # "email validation failed"
```

---

## 2. TLS/SSL Verification

### Default Secure Configuration

```python
from mercury_smtp import SMTPServerConfig

# SSL verification enabled by default
server = SMTPServerConfig(
    host="smtp.example.com",
    port=587,
    username="user@example.com",
    password="your-password",
    use_tls=True,  # Default: True
    verify_ssl=True,  # Default: True ✅ SECURE
)
```

### Custom SSL Context (Self-Signed Certificates)

```python
import ssl
from mercury_smtp import SMTPServerConfig

# Create custom SSL context for self-signed certs
ssl_context = ssl.create_default_context()
ssl_context.check_hostname = False
ssl_context.verify_mode = ssl.CERT_NONE

server = SMTPServerConfig(
    host="internal-smtp.company.local",
    ssl_context=ssl_context,  # Custom context
)
```

### Disable SSL Verification (NOT RECOMMENDED)

```python
from mercury_smtp import SMTPServerConfig

# ⚠️ INSECURE: Only for development/testing
server = SMTPServerConfig(
    host="smtp.example.com",
    verify_ssl=False,  # ⚠️ Warning will be logged
)

# Logs: "ssl_verification_disabled" with server details
```

---

## 3. Password Providers

### Secure Password Retrieval

```python
import os
from mercury_smtp import SMTPServerConfig

# Option 1: Environment variable
def get_smtp_password():
    return os.getenv("SMTP_PASSWORD")

server = SMTPServerConfig(
    host="smtp.example.com",
    username="user@example.com",
    password_provider=get_smtp_password,  # ✅ No plain-text storage
)
```

### Integration with Secret Managers

```python
from mercury_smtp import SMTPServerConfig
import boto3  # AWS Secrets Manager

def get_password_from_aws():
    client = boto3.client('secretsmanager')
    response = client.get_secret_value(SecretId='smtp-password')
    return response['SecretString']

server = SMTPServerConfig(
    host="smtp.example.com",
    username="user@example.com",
    password_provider=get_password_from_aws,
)
```

### HashiCorp Vault Example

```python
from mercury_smtp import SMTPServerConfig
import hvac  # HashiCorp Vault client

def get_password_from_vault():
    client = hvac.Client(url='https://vault.example.com')
    secret = client.secrets.kv.v2.read_secret_version(path='smtp')
    return secret['data']['data']['password']

server = SMTPServerConfig(
    host="smtp.example.com",
    username="user@example.com",
    password_provider=get_password_from_vault,
)
```

---

## 4. Structured Logging

### Basic Setup

```python
import logging
import json

# Configure JSON formatter for structured logs
class StructuredFormatter(logging.Formatter):
    def format(self, record):
        log_data = {
            "timestamp": self.formatTime(record),
            "level": record.levelname,
            "event": record.getMessage(),
        }
        # Include structured context
        if hasattr(record, 'extra'):
            log_data.update(record.extra)
        return json.dumps(log_data)

# Apply to mercury_smtp logger
logger = logging.getLogger('mercury_smtp')
handler = logging.StreamHandler()
handler.setFormatter(StructuredFormatter())
logger.addHandler(handler)
logger.setLevel(logging.INFO)
```

### Example Structured Log Output

```json
{
  "timestamp": "2025-06-01 10:30:45",
  "level": "INFO",
  "event": "email_sent_successfully",
  "message_id": "abc123@example.com",
  "recipient": "user@example.com",
  "server": "smtp.example.com:587",
  "attempts": 1,
  "latency_ms": 150.5
}
```

### Querying Structured Logs

**Splunk:**
```splunk
index=email event="email_sent_successfully" latency_ms>200
```

**Elasticsearch:**
```json
GET /logs/_search
{
  "query": {
    "bool": {
      "must": [
        {"term": {"event": "circuit_breaker_open"}},
        {"range": {"failure_count": {"gte": 5}}}
      ]
    }
  }
}
```

**Python filtering:**
```python
import json

with open('app.log') as f:
    for line in f:
        log = json.loads(line)
        if log['event'] == 'smtp_send_attempt_failed':
            print(f"Failed to {log['recipient']}: {log['error']}")
```

---

## 5. Running Tests

### Quick Test Run

```bash
# Activate virtual environment
source venv/bin/activate

# Run all tests
pytest tests/test_comprehensive.py -v

# Run with coverage
pytest --cov=mercury_smtp --cov-report=term-missing

# Run specific test category
pytest tests/test_comprehensive.py::TestEmailValidation -v
pytest tests/test_comprehensive.py::TestCircuitBreaker -v
```

### Coverage Report

```bash
# Generate HTML coverage report
pytest --cov=mercury_smtp --cov-report=html

# Open report (macOS)
open htmlcov/index.html

# Open report (Linux)
xdg-open htmlcov/index.html

# Open report (Windows)
start htmlcov/index.html
```

### Continuous Integration

**GitHub Actions example:**
```yaml
name: Tests

on: [push, pull_request]

jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v3
      - uses: actions/setup-python@v4
        with:
          python-version: '3.12'
      
      - name: Install dependencies
        run: |
          python -m pip install --upgrade pip
          pip install -e ".[dev]"
      
      - name: Run tests
        run: |
          pytest --cov=mercury_smtp --cov-report=xml
      
      - name: Upload coverage
        uses: codecov/codecov-action@v3
```

---

## 6. Migration Examples

### Before: Basic Send

```python
from mercury_smtp import AsyncSMTPSender, SMTPServerConfig

server = SMTPServerConfig(
    host="smtp.example.com",
    username="user@example.com",
    password="plain-text-password",  # ⚠️ Insecure
)

async with AsyncSMTPSender([server]) as sender:
    result = await sender.send(
        from_addr="sender@example.com",
        to_addrs=["recipient@example.com"],
        subject="Test",
        body_text="Hello",
    )
```

### After: Secure Configuration

```python
import os
import ssl
from mercury_smtp import AsyncSMTPSender, SMTPServerConfig

# Password from environment
def get_password():
    return os.getenv("SMTP_PASSWORD")

# Custom SSL for internal servers
ssl_ctx = ssl.create_default_context()
ssl_ctx.load_verify_locations('/path/to/ca-bundle.crt')

server = SMTPServerConfig(
    host="smtp.example.com",
    username="user@example.com",
    password_provider=get_password,  # ✅ Secure
    ssl_context=ssl_ctx,  # ✅ Custom CA
    verify_ssl=True,  # ✅ Verified
)

async with AsyncSMTPSender([server]) as sender:
    result = await sender.send(
        from_addr="sender@example.com",
        to_addrs=["recipient@example.com"],
        subject="Test",
        body_text="Hello",
    )
    
    # Check result
    if result.success:
        print(f"✅ Sent: {result.message_id}")
    else:
        print(f"❌ Failed: {result.error}")
```

---

## 7. Common Patterns

### Pre-Send Validation

```python
from mercury_smtp import validate_email_address, AsyncSMTPSender

async def send_with_validation(sender, from_addr, to_addrs, subject, body):
    """Send email with explicit validation."""
    
    # Validate before building message
    try:
        validate_email_address(from_addr)
        for email in to_addrs:
            validate_email_address(email)
    except ValueError as e:
        print(f"Validation failed: {e}")
        return None
    
    # Send (validation happens again automatically)
    result = await sender.send(
        from_addr=from_addr,
        to_addrs=to_addrs,
        subject=subject,
        body_text=body,
    )
    
    return result
```

### Batch Send with Validation

```python
from mercury_smtp import validate_email_list, AsyncSMTPSender

async def send_newsletter(sender, recipients, subject, body):
    """Send newsletter to validated recipients."""
    
    # Pre-validate all recipients
    valid_recipients = []
    invalid_recipients = []
    
    for email in recipients:
        try:
            validate_email_address(email)
            valid_recipients.append(email)
        except ValueError:
            invalid_recipients.append(email)
    
    print(f"Valid: {len(valid_recipients)}, Invalid: {len(invalid_recipients)}")
    
    # Send to valid recipients
    emails = [
        {
            "from_addr": "newsletter@example.com",
            "to_addrs": [recipient],
            "subject": subject,
            "body_text": body,
        }
        for recipient in valid_recipients
    ]
    
    result = await sender.send_bulk(emails, concurrency=10)
    return result, invalid_recipients
```

### Multi-Server with Failover

```python
from mercury_smtp import AsyncSMTPSender, SMTPServerConfig, LoadBalancingStrategy

# Primary and backup servers
servers = [
    SMTPServerConfig(
        name="primary",
        host="smtp-primary.example.com",
        password_provider=lambda: os.getenv("SMTP_PASSWORD_PRIMARY"),
    ),
    SMTPServerConfig(
        name="backup",
        host="smtp-backup.example.com",
        password_provider=lambda: os.getenv("SMTP_PASSWORD_BACKUP"),
    ),
]

async with AsyncSMTPSender(
    servers,
    strategy=LoadBalancingStrategy.ROUND_ROBIN,
) as sender:
    # Automatically fails over if primary is down
    result = await sender.send(...)
```

---

## 8. Debugging

### Enable Debug Logging

```python
import logging

# Enable debug logs for mercury_smtp
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)

# Or just for mercury_smtp
logger = logging.getLogger('mercury_smtp')
logger.setLevel(logging.DEBUG)
```

### Check Circuit Breaker Status

```python
async with AsyncSMTPSender([server]) as sender:
    # Get pool for server
    pool = sender._pools[server.name]
    
    # Check circuit breaker
    cb = pool.runtime.circuit_breaker
    print(f"State: {cb._stats.state}")
    print(f"Failures: {cb._stats.failure_count}")
    print(f"Last errors: {cb._stats.last_error_messages}")
```

### Inspect Connection Pool

```python
async with AsyncSMTPSender([server]) as sender:
    pool = sender._pools[server.name]
    
    print(f"Pool size: {len(pool._pool)}")
    print(f"In use: {len(pool._in_use)}")
    print(f"Handshake p50: {pool.runtime.handshake_p50:.0f}ms")
```

---

## Resources

- **Documentation:** [README.md](README.md)
- **Implementation Details:** [IMPLEMENTATION-SUMMARY.md](IMPLEMENTATION-SUMMARY.md)
- **Performance Guide:** [docs/Performance.md](docs/Performance.md)
- **Examples:** [examples/](examples/)

---

## Support

For issues or questions:
1. Check [IMPLEMENTATION-SUMMARY.md](IMPLEMENTATION-SUMMARY.md) for detailed explanations
2. Review test examples in [tests/test_comprehensive.py](tests/test_comprehensive.py)
3. Run tests with `-v` flag for detailed output
4. Enable DEBUG logging to see internal behavior
