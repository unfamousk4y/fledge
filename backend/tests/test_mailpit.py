"""Real SMTP: emails go to Mailpit (a fake mail server), and we read them back
through its API. Proves the messages are valid email, not just Python objects."""

import asyncio
import json
import random
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from sqlalchemy import Engine, update

from fledge import config, db
from fledge.alerts import Batch, Sender, build_message, sample_batch
from fledge.companies import Company, sync_companies
from fledge.mailer import MailerAuthError, SmtpMailer
from fledge.subscribers import SubscriberInput, add_subscriber
from fledge.worker import Worker

T0 = datetime(2026, 8, 3, 18, 0, tzinfo=UTC)


def local_mailer() -> SmtpMailer:
    # Always Mailpit, whatever .env says, so tests can never email a real person.
    return SmtpMailer(
        host=config.optional_setting("MAILPIT_SMTP_HOST", "127.0.0.1"),
        port=1025,
        starttls=False,
    )


def inbox(api: str) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = httpx.get(f"{api}/api/v1/messages").json()["messages"]
    return [httpx.get(f"{api}/api/v1/message/{m['ID']}").json() for m in messages]


def headers(api: str, message_id: str) -> dict[str, list[str]]:
    result: dict[str, list[str]] = httpx.get(f"{api}/api/v1/message/{message_id}/headers").json()
    return result


def test_sample_email_arrives_with_text_html_and_unsubscribe(mailpit: str) -> None:
    batch: Batch = sample_batch("you@example.com", T0)
    local_mailer().send(build_message(batch, "alerts@fledge.test"))

    [message] = inbox(mailpit)
    assert message["Subject"] == "New: [Test] Software Engineer Intern at Fledge"
    assert message["To"][0]["Address"] == "you@example.com"
    assert "Seen 4 min after it was published" in message["Text"]
    assert '<a href="https://github.com/unfamousk4y/fledge">' in message["HTML"]
    assert headers(mailpit, message["ID"])["List-Unsubscribe"] == [
        "<mailto:alerts@fledge.test?subject=STOP>"
    ]


def test_new_posting_on_the_board_becomes_an_email(engine: Engine, mailpit: str) -> None:
    # The whole path: fake Greenhouse -> worker -> outbox -> sender -> SMTP -> inbox.
    jobs: list[dict[str, Any]] = [
        {
            "id": 1,
            "title": "Senior Software Engineer",
            "location": {"name": "New York, NY"},
            "absolute_url": "https://boards.greenhouse.io/acme/jobs/1",
        }
    ]

    def greenhouse(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=json.dumps({"jobs": jobs}))

    with engine.begin() as conn:
        sync_companies(conn, [Company("Acme", "greenhouse", "acme", True)], T0, random.Random(0))
        add_subscriber(conn, SubscriberInput("you@example.com"))

    sender = Sender(engine, local_mailer(), "alerts@fledge.test", clock=lambda: T0, out=print)
    woken: list[bool] = []

    async def go() -> int:
        async with httpx.AsyncClient(transport=httpx.MockTransport(greenhouse)) as client:
            worker = Worker(
                engine,
                client,
                clock=lambda: T0,
                rng=random.Random(0),
                out=print,
                on_alerts_queued=lambda: woken.append(True),
            )
            for _ in range(2):
                with engine.begin() as conn:  # make the board due now
                    conn.execute(update(db.boards).values(next_check_at=T0))
                await worker.run_due_once()
                jobs.append(
                    {
                        "id": 2,
                        "title": "Data Engineer Intern",
                        "location": {"name": "Austin, TX"},
                        "absolute_url": "https://boards.greenhouse.io/acme/jobs/2",
                        "first_published": (T0 - timedelta(minutes=6)).isoformat(),
                    }
                )
            return await sender.send_due()

    assert asyncio.run(go()) == 1
    assert woken == [True]  # the worker woke the sender instead of waiting for a poll
    [message] = inbox(mailpit)
    assert message["Subject"] == "New: Data Engineer Intern at Acme"
    assert "Austin, TX" in message["Text"]


def test_setting_a_username_for_mailpit_gives_a_clear_error(mailpit: str) -> None:
    # Real server check: Mailpit doesn't accept logins, so a leftover username
    # in .env must produce a clear message, not a vague failure.
    with_login = SmtpMailer("127.0.0.1", 1025, "someone@example.com", "x", starttls=False)
    with pytest.raises(MailerAuthError, match="leave SMTP_USERNAME"):
        with_login.send(build_message(sample_batch("you@example.com", T0), "alerts@fledge.test"))
    assert inbox(mailpit) == []
