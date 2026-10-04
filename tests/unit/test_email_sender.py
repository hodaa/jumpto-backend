"""How the SMTP sender chooses its transport security.

The mode is the difference between delivered mail and a silent failure, and the
wrong choice fails in a way that looks like a network outage, so it is pinned
here rather than left to a deploy-time discovery.
"""

from __future__ import annotations

import smtplib
from unittest.mock import MagicMock, patch

import pytest

from app.services.email import SmtpEmailSender
from app.services.ports import AuthEmail

EMAIL = AuthEmail(
    to="person@example.com",
    subject="Verify your email",
    body="https://qfza.app/verify?token=x",
)


def make_sender(port: int, use_ssl: bool | None = None) -> SmtpEmailSender:
    return SmtpEmailSender(
        host="smtp.hostinger.com",
        port=port,
        user="social@qfza.app",
        app_password="mailbox-password",
        from_email="social@qfza.app",
        use_ssl=use_ssl,
    )


class TestTransportSecurity:
    def test_port_465_uses_implicit_tls(self) -> None:
        # Hostinger documents 465/SSL. SMTP() + starttls() against it fails at
        # connect or handshake, so 465 must open an SMTP_SSL socket instead.
        assert make_sender(465).use_ssl is True

    def test_port_587_upgrades_with_starttls(self) -> None:
        # Gmail requires STARTTLS on 587. SMTP_SSL there would break the mailbox
        # that already works, so this mapping must not drift the other way.
        assert make_sender(587).use_ssl is False

    @pytest.mark.parametrize("port", [25, 2525])
    def test_other_ports_stay_starttls(self, port: int) -> None:
        assert make_sender(port).use_ssl is False

    def test_explicit_setting_overrides_the_port(self) -> None:
        # Some providers use implicit TLS on a nonstandard port, so the port
        # heuristic must be a default rather than a rule.
        assert make_sender(587, use_ssl=True).use_ssl is True
        assert make_sender(465, use_ssl=False).use_ssl is False


class TestOpening:
    def test_implicit_tls_never_calls_starttls(self) -> None:
        # starttls on an already-encrypted socket corrupts the stream.
        sender = make_sender(465)
        smtp = MagicMock()
        with (
            patch("app.services.email.smtplib.SMTP_SSL", return_value=smtp) as ssl_ctor,
            sender._open(),
        ):
            pass
        smtp.starttls.assert_not_called()
        smtp.login.assert_called_once_with("social@qfza.app", "mailbox-password")
        ssl_ctor.assert_called_once()
        assert ssl_ctor.call_args.args[:2] == ("smtp.hostinger.com", 465)

    def test_starttls_mode_never_constructs_an_ssl_socket(self) -> None:
        sender = make_sender(587)
        smtp = MagicMock()
        with (
            patch("app.services.email.smtplib.SMTP", return_value=smtp) as ctor,
            patch("app.services.email.smtplib.SMTP_SSL") as ssl_ctor,
            sender._open(),
        ):
            pass
        smtp.starttls.assert_called_once()
        smtp.login.assert_called_once()
        ctor.assert_called_once()
        ssl_ctor.assert_not_called()

    def test_login_failure_closes_the_socket(self) -> None:
        # An abandoned authenticated socket keeps a server-side session alive
        # and leaks the connection under retry load.
        sender = make_sender(587)
        smtp = MagicMock()
        smtp.login.side_effect = RuntimeError("bad credentials")
        with (
            patch("app.services.email.smtplib.SMTP", return_value=smtp),
            pytest.raises(RuntimeError),
        ):
            sender._open()
        smtp.close.assert_called_once()
        smtp.send_message.assert_not_called()

    def test_failed_handshake_closes_the_socket(self) -> None:
        sender = make_sender(587)
        smtp = MagicMock()
        smtp.starttls.side_effect = RuntimeError("handshake failed")
        with (
            patch("app.services.email.smtplib.SMTP", return_value=smtp),
            pytest.raises(RuntimeError),
        ):
            sender._open()
        smtp.close.assert_called_once()


class TestCredentials:
    @pytest.mark.parametrize(
        ("user", "app_password", "from_email"),
        [("", "pw", "a@b.c"), ("u", "", "a@b.c"), ("u", "pw", "")],
    )
    def test_unconfigured_mailbox_is_not_configured(
        self, user: str, app_password: str, from_email: str
    ) -> None:
        # A half-filled mailbox must be reported as unconfigured rather than
        # retried against a server that will reject it every time.
        sender = SmtpEmailSender(
            host="smtp.hostinger.com",
            port=465,
            user=user,
            app_password=app_password,
            from_email=from_email,
        )
        assert sender.is_configured is False

    def test_no_connect_when_unconfigured(self) -> None:
        sender = SmtpEmailSender(
            host="smtp.hostinger.com",
            port=465,
            user="",
            app_password="",
            from_email="",
        )
        with patch("app.services.email.smtplib.SMTP_SSL") as ssl_ctor:
            import asyncio

            asyncio.run(sender.send(EMAIL))
        ssl_ctor.assert_not_called()

    def test_secrets_are_not_logged_on_failure(self) -> None:
        import asyncio

        sender = make_sender(465)
        with patch("app.services.email.smtplib.SMTP_SSL", side_effect=OSError("refused")):
            asyncio.run(sender.send(EMAIL))  # must not raise

    @pytest.mark.parametrize(
        "failure",
        [OSError("refused"), smtplib.SMTPAuthenticationError(535, b"bad password")],
    )
    def test_body_is_never_logged_on_failure(self, failure: Exception) -> None:
        # The body carries a single-use reset token. Once it reaches the log it
        # is readable by anyone with log access and still works, so the token
        # must never appear in any log field - not just not be the message.
        import asyncio

        sender = make_sender(465)
        with (
            patch("app.services.email.smtplib.SMTP_SSL", side_effect=failure),
            patch("app.services.email.logger") as log,
        ):
            asyncio.run(sender.send(EMAIL))

        logged = " ".join(
            repr(field)
            for record in log.error.call_args_list + log.info.call_args_list
            for field in record.kwargs.values()
        )
        assert "token=x" not in logged
        assert EMAIL.body not in logged

    def test_body_is_never_logged_on_success(self) -> None:
        import asyncio

        sender = make_sender(465)
        smtp = MagicMock()
        # Make `with smtp:` hand back the same mock, so send_message lands on it.
        smtp.__enter__.return_value = smtp
        with (
            patch("app.services.email.smtplib.SMTP_SSL", return_value=smtp),
            patch("app.services.email.logger") as log,
        ):
            asyncio.run(sender.send(EMAIL))

        logged = " ".join(
            repr(field)
            for record in log.error.call_args_list + log.info.call_args_list
            for field in record.kwargs.values()
        )
        assert "token=x" not in logged
        assert smtp.send_message.called


class TestMessageShape:
    def test_from_uses_the_display_name_and_sender(self) -> None:
        message = make_sender(465)._build_message(EMAIL)
        assert message["From"] == "Qfza <social@qfza.app>"

    def test_message_is_plain_text(self) -> None:
        # reset links must stay clickable and scannable by mail clients
        message = make_sender(465)._build_message(EMAIL)
        assert message.get_content_type() == "text/plain"
        assert EMAIL.body in message.get_content()
