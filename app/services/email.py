"""Transactional email over Gmail SMTP.

Delivery is offloaded to a thread so the event loop is not blocked on a network
round trip, and a failure never propagates: a failed verification mail must not
turn a successful registration into a 500.
"""

from __future__ import annotations

import asyncio
import smtplib
from email.message import EmailMessage

from app.core.logging import get_logger
from app.services.ports import AuthEmail

logger = get_logger(__name__)


class GmailSmtpEmailSender:
    """Sends mail through a Gmail account using an app password.

    The app password grants IMAP as well as SMTP, so it is a mailbox
    credential and must never be logged or committed.
    """

    def __init__(
        self,
        *,
        host: str,
        port: int,
        user: str,
        app_password: str,
        from_email: str,
        from_name: str = "Qfza",
    ) -> None:
        self.host = host
        self.port = port
        self.user = user
        self.app_password = app_password
        self.from_email = from_email
        self.from_name = from_name

    @property
    def is_configured(self) -> bool:
        """Whether enough configuration exists to attempt delivery."""
        return bool(self.user and self.app_password and self.from_email)

    def _build_message(self, email: AuthEmail) -> EmailMessage:
        message = EmailMessage()
        message["From"] = f"{self.from_name} <{self.from_email}>"
        message["To"] = email.to
        message["Subject"] = email.subject
        message.set_content(email.body)
        return message

    def _send_blocking(self, message: EmailMessage) -> None:
        with smtplib.SMTP(self.host, self.port, timeout=20) as smtp:
            smtp.starttls()
            smtp.login(self.user, self.app_password)
            smtp.send_message(message)

    async def send(self, email: AuthEmail) -> None:
        """Deliver a message, swallowing failures after logging them."""
        if not self.is_configured:
            logger.warning("Email not configured; skipping send", to=email.to)
            return
        try:
            await asyncio.to_thread(self._send_blocking, self._build_message(email))
            logger.info("Sent email", to=email.to, subject=email.subject)
        except (smtplib.SMTPException, OSError) as exc:
            # Never log the body: it carries single-use reset tokens.
            logger.error("Failed to send email", to=email.to, error=str(exc))
