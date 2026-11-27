"""SmtpMailer's handling of login failures, using a stand-in for smtplib.SMTP
(the real SMTP conversation is covered against Mailpit in test_mailpit.py)."""

import smtplib
from email.message import EmailMessage
from typing import Any

import pytest

from fledge.mailer import MailerAuthError, SmtpMailer


class FakeSMTP:
    """Behaves like smtplib.SMTP, except login raises whatever the test sets."""

    login_error: Exception | None = None
    sent: list[EmailMessage] = []

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        pass

    def __enter__(self) -> "FakeSMTP":
        return self

    def __exit__(self, *exc: object) -> None:
        pass

    def starttls(self, **kwargs: Any) -> None:
        pass

    def login(self, user: str, password: str) -> None:
        if self.login_error is not None:
            raise self.login_error

    def send_message(self, message: EmailMessage) -> None:
        FakeSMTP.sent.append(message)


@pytest.fixture
def fake_smtp(monkeypatch: pytest.MonkeyPatch) -> type[FakeSMTP]:
    FakeSMTP.login_error, FakeSMTP.sent = None, []
    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)  # what mailer.py calls
    return FakeSMTP


GMAIL = SmtpMailer("smtp.gmail.com", 587, "fledge@example.com", "not-an-app-password")


@pytest.mark.parametrize(
    ("error", "hint"),
    [
        (smtplib.SMTPAuthenticationError(535, b"5.7.8 Username and Password not accepted"), "535"),
        (smtplib.SMTPServerDisconnected("Connection unexpectedly closed"), "app password"),
        (smtplib.SMTPNotSupportedError("SMTP AUTH extension not supported"), "Mailpit"),
    ],
)
def test_login_failures_become_a_clear_auth_error(
    fake_smtp: type[FakeSMTP], error: Exception, hint: str
) -> None:
    fake_smtp.login_error = error
    with pytest.raises(MailerAuthError, match=hint) as info:
        GMAIL.send(EmailMessage())
    assert "fledge@example.com" not in str(info.value)  # no account details in logs
    assert fake_smtp.sent == []


def test_disconnect_while_sending_is_still_a_retryable_error(
    fake_smtp: type[FakeSMTP], monkeypatch: pytest.MonkeyPatch
) -> None:
    # Only a hang-up *during login* means "bad credentials"; after a good login
    # it's an ordinary network problem, which the sender retries.
    def drop(self: FakeSMTP, message: EmailMessage) -> None:
        raise smtplib.SMTPServerDisconnected("Connection unexpectedly closed")

    monkeypatch.setattr(FakeSMTP, "send_message", drop)
    with pytest.raises(smtplib.SMTPServerDisconnected):
        GMAIL.send(EmailMessage())


def test_successful_login_sends(fake_smtp: type[FakeSMTP]) -> None:
    GMAIL.send(EmailMessage())
    assert len(fake_smtp.sent) == 1
