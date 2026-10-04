"""Transactional email over SMTP.

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


class SmtpEmailSender:
    """Sends mail through an SMTP mailbox.

    Providers disagree about how to secure the connection: Gmail requires
    STARTTLS on 587 (an encrypted upgrade of a plain socket), while Hostinger
    documents implicit TLS on 465 (the socket is encrypted from the first byte,
    which Python spells ``SMTP_SSL``). Using the wrong one for a provider fails
    at connect or at handshake, so the mode is chosen from the port unless the
    caller overrides it.

    The password grants IMAP as well as SMTP, so it is a mailbox credential and
    must never be logged or committed.
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
        use_ssl: bool | None = None,
    ) -> None:
        self.host = host
        self.port = port
        self.user = user
        self.app_password = app_password
        self.from_email = from_email
        self.from_name = from_name
        # Implicit TLS is the convention for 465; STARTTLS for everything else.
        # None keeps the mapping so the common case needs no extra setting.
        self.use_ssl = (port == 465) if use_ssl is None else use_ssl

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

    def _open(self) -> smtplib.SMTP:
        """Open an authenticated SMTP session on an encrypted socket.

        Both paths are encrypted: ``SMTP_SSL`` negotiates TLS before the SMTP
        greeting, ``starttls`` upgrades after it. Credentials only ever cross
        the encrypted channel.
        """
        if self.use_ssl:
            smtp = smtplib.SMTP_SSL(self.host, self.port, timeout=20)
        else:
            smtp = smtplib.SMTP(self.host, self.port, timeout=20)
        try:
            if not self.use_ssl:
                smtp.starttls()
            smtp.login(self.user, self.app_password)
        except Exception:
            # Do not leave a half-authenticated socket open on a failed upgrade.
            smtp.close()
            raise
        return smtp

    def _send_blocking(self, message: EmailMessage) -> None:
        with self._open() as smtp:
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
