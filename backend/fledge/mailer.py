"""Send one email over SMTP, using only Python's standard library.

Settings come from the environment / .env (see .env.example). The defaults
point at Mailpit, the local fake inbox; real sending uses the dedicated Fledge
Gmail account (DECISIONS.md 2026-11-24).
"""

import smtplib
import ssl
from dataclasses import dataclass
from email.message import EmailMessage
from typing import Protocol

from fledge import config


class MailerAuthError(Exception):
    """The mail server rejected our username/password. Retrying won't help."""


class Mailer(Protocol):
    def send(self, message: EmailMessage) -> None: ...


@dataclass(frozen=True)
class SmtpMailer:
    host: str
    port: int
    username: str = ""
    password: str = ""
    starttls: bool = True
    timeout: float = 20.0

    def send(self, message: EmailMessage) -> None:
        """Deliver one message. Raises MailerAuthError, or smtplib/OS errors to retry."""
        with smtplib.SMTP(self.host, self.port, timeout=self.timeout) as smtp:
            if self.starttls:
                # Encrypt the connection and check the server's certificate.
                smtp.starttls(context=ssl.create_default_context())
            if self.username:
                try:
                    smtp.login(self.username, self.password)
                except smtplib.SMTPAuthenticationError as exc:
                    # Don't include the server's reply text: it can echo the username.
                    raise MailerAuthError(f"SMTP login rejected (code {exc.smtp_code})") from None
            smtp.send_message(message)


def mailer_from_settings() -> SmtpMailer:
    return SmtpMailer(
        host=config.optional_setting("SMTP_HOST", "127.0.0.1"),
        port=int(config.optional_setting("SMTP_PORT", "1025")),
        username=config.optional_setting("SMTP_USERNAME"),
        password=config.optional_setting("SMTP_PASSWORD"),
        starttls=config.optional_setting("SMTP_STARTTLS", "no").lower() in ("yes", "true", "1"),
    )


def sender_address() -> str:
    return config.optional_setting("ALERT_FROM", "alerts@fledge.localhost")
