"""Basic example: sending a single email."""

import asyncio
import os

from msmtp import AsyncSMTPSender, SMTPServerConfig


async def main():
    """Send a single email via Gmail."""
    # Configure SMTP server (Gmail with app password)
    server = SMTPServerConfig(
        host="smtp.gmail.com",
        port=587,
        username=os.getenv("SMTP_USER", "your-email@gmail.com"),
        password=os.getenv("SMTP_PASS", "your-app-password"),
        use_tls=True,
    )

    # Create sender and send email
    async with AsyncSMTPSender([server]) as sender:
        result = await sender.send(
            from_addr="sender@example.com",
            to_addrs=["recipient@example.com"],
            subject="Hello from Mercury SMTP",
            body_text="This is a plain text email.",
            body_html="<p>This is an <b>HTML</b> email.</p>",
        )

        if result.success:
            print(f"✓ Email sent successfully!")
            print(f"  Message ID: {result.message_id}")
            print(f"  Server: {result.server}")
            print(f"  Latency: {result.latency_ms:.0f}ms")
        else:
            print(f"✗ Failed to send email: {result.error}")


if __name__ == "__main__":
    asyncio.run(main())
