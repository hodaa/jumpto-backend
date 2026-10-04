"""SMTP settings that look configured but can never deliver.

These are the failures that produce no mail and no useful error: the app starts,
the sender reports itself configured, and the send fails somewhere inside the
network stack. Each test here corresponds to a mistake that was actually made.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.api.v1_auth import get_email_sender
from app.core.config import Settings


class TestSmtpUserMustBeAnAddress:
    def test_a_hostname_in_the_username_slot_is_rejected(self) -> None:
        # SMTP_HOST left unset while the hostname was pasted into SMTP_USER.
        # Everything else is set, so without this check the app starts and
        # quietly attempts mail against smtp.gmail.com as a nonexistent user.
        with pytest.raises(ValidationError) as exc:
            Settings(smtp_user="smtp.hostinger.com")
        assert "SMTP_HOST" in str(exc.value)

    def test_the_error_names_both_keys(self) -> None:
        # The fix has to be guessable from the message alone.
        with pytest.raises(ValidationError) as exc:
            Settings(smtp_user="smtp.gmail.com")
        message = str(exc.value)
        assert "SMTP_USER" in message
        assert "you@example.com" in message

    @pytest.mark.parametrize("address", ["social@qfza.app", "a@b.co", "x+tag@qfza.app"])
    def test_real_addresses_are_accepted(self, address: str) -> None:
        assert Settings(smtp_user=address).smtp_user == address

    def test_empty_is_allowed(self) -> None:
        # Mail is optional: an empty username must not stop the app from
        # booting for people who never enable it.
        settings = Settings(smtp_user="")
        assert settings.smtp_user == ""
        assert get_email_sender(settings).is_configured is False


class TestResolvedTransport:
    def test_hostinger_settings_select_implicit_tls(self) -> None:
        settings = Settings(
            smtp_host="smtp.hostinger.com",
            smtp_port=465,
            smtp_user="social@qfza.app",
            smtp_app_password="mailbox-password",
            email_from="social@qfza.app",
        )
        sender = get_email_sender(settings)
        assert (sender.host, sender.port) == ("smtp.hostinger.com", 465)
        assert sender.use_ssl is True
        assert sender.is_configured is True

    def test_gmail_defaults_still_select_starttls(self) -> None:
        # The 465 change must not have broken the mailbox that already worked.
        # Host and port are passed explicitly: Settings() reads the real .env,
        # so relying on the defaults would test whatever the operator configured.
        settings = Settings(
            smtp_host="smtp.gmail.com",
            smtp_port=587,
            smtp_user="q@gmail.com",
            smtp_app_password="pw",
            email_from="q@gmail.com",
        )
        assert (settings.smtp_host, settings.smtp_port) == ("smtp.gmail.com", 587)
        assert get_email_sender(settings).use_ssl is False

    def test_explicit_ssl_flag_overrides_the_port(self) -> None:
        settings = Settings(
            smtp_port=2525,
            smtp_use_ssl=True,
            smtp_user="social@qfza.app",
            smtp_app_password="pw",
            email_from="social@qfza.app",
        )
        assert get_email_sender(settings).use_ssl is True

    def test_sender_falls_back_to_the_login_when_from_is_unset(self) -> None:
        # A separate EMAIL_FROM that disagrees with the mailbox is a silent
        # deliverability problem, so an unset one must not invent a new address.
        settings = Settings(
            smtp_host="smtp.hostinger.com",
            smtp_port=465,
            smtp_user="social@qfza.app",
            smtp_app_password="pw",
        )
        assert get_email_sender(settings).from_email == "social@qfza.app"
