"""Email alerts: the pure rules, then queueing and sending against real Postgres
with a fake mailer (real SMTP is covered in test_mailpit.py)."""

import asyncio
import smtplib
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage
from typing import Any

import pytest
from sqlalchemy import Connection, Engine, insert, select, update
from sqlalchemy.exc import OperationalError

from fledge import alerts, db
from fledge.alerts import Batch, Filters, PendingAlert, Sender, build_message, matches
from fledge.classify import DegreeTag
from fledge.crawler import Drop
from fledge.mailer import MailerAuthError
from fledge.models import Posting
from fledge.subscribers import (
    SubscriberError,
    SubscriberInput,
    add_subscriber,
    list_lines,
    remove_subscriber,
)

T0 = datetime(2026, 8, 3, 18, 0, tzinfo=UTC)
MIN = timedelta(minutes=1)


def posting(external_id: str = "1", title: str = "Software Engineer Intern", **kw: Any) -> Posting:
    fields: dict[str, Any] = {
        "source": "greenhouse",
        "board": "acme",
        "external_id": external_id,
        "title": title,
        "location": "New York, NY",
        "url": f"https://boards.greenhouse.io/acme/jobs/{external_id}",
        "first_published": T0 - 4 * MIN,
        "updated_at": None,
    }
    fields.update(kw)
    return Posting(**fields)


def drop(p: Posting | None = None, degree: DegreeTag | None = None, us: bool | None = True) -> Drop:
    return Drop(p or posting(), T0, degree, us)


# --- matches (pure) -------------------------------------------------------------


def test_all_companies_subscriber_gets_a_plain_drop() -> None:
    assert matches(Filters(), board_id=1, drop=drop())


def test_company_list_limits_boards() -> None:
    picky = Filters(all_companies=False, board_ids=frozenset({2}))
    assert not matches(picky, 1, drop())
    assert matches(picky, 2, drop())


def test_advanced_degree_roles_hidden_by_default() -> None:
    assert not matches(Filters(), 1, drop(degree=DegreeTag.PHD))
    assert matches(Filters(hide_advanced_degrees=False), 1, drop(degree=DegreeTag.MASTERS))


def test_unclear_location_is_included_unless_opted_out() -> None:
    assert matches(Filters(), 1, drop(us=None))
    assert not matches(Filters(include_unclear_location=False), 1, drop(us=None))


@pytest.mark.parametrize(("attempts", "minutes"), [(1, 1), (2, 5), (3, 15), (4, 60), (30, 60)])
def test_retry_delay(attempts: int, minutes: int) -> None:
    assert alerts.retry_delay(attempts) == minutes * MIN


# --- plan_batches (pure) --------------------------------------------------------


def pending(
    outbox_id: int,
    subscriber_id: int = 1,
    last: datetime | None = None,
    title: str = "Software Engineer Intern",
    company: str = "Acme",
    **kw: Any,
) -> PendingAlert:
    fields: dict[str, Any] = {
        "outbox_id": outbox_id,
        "subscriber_id": subscriber_id,
        "email": f"s{subscriber_id}@example.com",
        "last_emailed_at": last,
        "created_at": T0,
        "attempts": 0,
        "company": company,
        "title": title,
        "location": "New York, NY",
        "url": "https://boards.greenhouse.io/acme/jobs/1",
        "first_published": T0 - 8 * MIN,
        "first_seen_at": T0,
    }
    fields.update(kw)
    return PendingAlert(**fields)


def test_never_emailed_subscriber_is_sent_immediately() -> None:
    batches, holds = alerts.plan_batches([pending(1)], T0)
    assert [b.subscriber_id for b in batches] == [1] and holds == []


def test_recently_emailed_subscriber_is_held_until_ten_minutes_after() -> None:
    batches, holds = alerts.plan_batches([pending(1, last=T0 - 3 * MIN)], T0)
    assert batches == []
    assert holds == [alerts.Hold((1,), T0 + 7 * MIN)]


def test_everything_waiting_for_a_subscriber_goes_out_as_one_email() -> None:
    rows = [pending(3, last=T0 - 10 * MIN), pending(2, last=T0 - 10 * MIN), pending(9, 2)]
    batches, _ = alerts.plan_batches(rows, T0)
    assert [(b.subscriber_id, [a.outbox_id for a in b.alerts]) for b in batches] == [
        (1, [2, 3]),
        (2, [9]),
    ]


# --- build_message (pure) -------------------------------------------------------


def message_for(*alerts_: PendingAlert) -> EmailMessage:
    return build_message(Batch(1, "s1@example.com", alerts_), "alerts@fledge.test")


def parts(message: EmailMessage) -> tuple[str, str]:
    text = message.get_body(("plain",))
    html = message.get_body(("html",))
    assert text is not None and html is not None
    return text.get_content(), html.get_content()


def test_single_alert_email() -> None:
    message = message_for(pending(1, company="Stripe", title="Data Engineer Intern"))
    assert message["Subject"] == "New: Data Engineer Intern at Stripe"
    assert message["From"] == "Fledge <alerts@fledge.test>"
    assert message["List-Unsubscribe"] == "<mailto:alerts@fledge.test?subject=STOP>"
    text, html = parts(message)
    assert "Seen 8 min after it was published" in text
    assert "https://boards.greenhouse.io/acme/jobs/1" in text
    assert "Reply STOP to unsubscribe" in text and "Reply STOP" in html


def test_grouped_subject_names_up_to_three_companies() -> None:
    rows = [pending(i, company=c) for i, c in enumerate(["A", "B", "A", "C", "D"])]
    assert message_for(*rows)["Subject"] == "5 new roles: A, B, C, +1 more"


def test_company_text_cannot_inject_html() -> None:
    evil = pending(1, title='Intern <img src=x onerror="alert(1)">', company="<b>Acme</b>")
    _, html = parts(message_for(evil))
    assert "<img" not in html and "<b>Acme" not in html
    assert "&lt;img" in html


def test_non_web_links_are_not_linked() -> None:
    _, html = parts(message_for(pending(1, url="javascript:alert(1)")))
    assert "javascript:" not in html


def test_old_postings_show_a_date_instead_of_hours() -> None:
    old = pending(1, first_published=datetime(2023, 9, 4, 12, tzinfo=UTC))
    text, _ = parts(message_for(old))
    assert "First published Sep 4, 2023" in text and " h after" not in text


def test_tags_and_late_latency() -> None:
    late = pending(1, title="PhD Intern, ML", location="Remote", first_published=T0 - 6 * 60 * MIN)
    text, _ = parts(message_for(late))
    assert "Remote · PhD · location unclear" in text
    assert "Seen 6.0 h after it was published" in text


# --- Database: subscribers and queueing -----------------------------------------


def make_board(conn: Connection, token: str = "acme", name: str = "Acme") -> int:
    board_id = db.ensure_board(conn, token)
    conn.execute(update(db.boards).where(db.boards.c.id == board_id).values(name=name))
    return board_id


def make_posting(conn: Connection, board_id: int, p: Posting) -> int:
    return int(
        conn.scalar(
            insert(db.postings)
            .values(
                board_id=board_id,
                external_id=p.external_id,
                title=p.title,
                location=p.location,
                url=p.url,
                first_published=p.first_published,
                first_seen_at=T0,
                status="open",
            )
            .returning(db.postings.c.id)
        )
    )


def outbox_rows(engine: Engine) -> list[tuple[int, str, int]]:
    o = db.outbox
    with engine.connect() as conn:
        rows = conn.execute(select(o.c.subscriber_id, o.c.status, o.c.attempts).order_by(o.c.id))
        return [tuple(r) for r in rows]


def test_add_update_list_and_remove_subscribers(engine: Engine) -> None:
    with engine.begin() as conn:
        make_board(conn, "stripe", "Stripe")
        first = add_subscriber(conn, SubscriberInput("  You@Example.com "))
        again = add_subscriber(conn, SubscriberInput("you@example.com", ("stripe",)))
        assert first == again  # same person, settings replaced
        [line] = list_lines(conn)
        assert "you@example.com" in line and "stripe" in line
        with pytest.raises(SubscriberError, match="unknown or inactive companies: nope"):
            add_subscriber(conn, SubscriberInput("x@example.com", ("nope",)))
        with pytest.raises(SubscriberError, match="doesn't look like"):
            add_subscriber(conn, SubscriberInput("not an email"))
        assert remove_subscriber(conn, "YOU@example.com")
        assert list_lines(conn) == ["no subscribers yet"]


def test_queue_matches_filters_and_never_duplicates(engine: Engine) -> None:
    with engine.begin() as conn:
        acme = make_board(conn)
        other = make_board(conn, "other", "Other")
        everyone = add_subscriber(conn, SubscriberInput("a@example.com"))
        add_subscriber(conn, SubscriberInput("b@example.com", ("other",)))  # not acme
        phd_ok = add_subscriber(conn, SubscriberInput("c@example.com", hide_advanced_degrees=False))
        intern, phd = posting("1"), posting("2", "PhD Intern, Research")
        make_posting(conn, acme, intern)
        make_posting(conn, acme, phd)
        drops = [drop(intern), drop(phd, degree=DegreeTag.PHD)]
        assert alerts.queue_alerts(conn, acme, drops, T0) == 3  # a:intern, c:intern, c:phd
        assert alerts.queue_alerts(conn, acme, drops, T0) == 0  # re-queue is a no-op
        assert other  # (b only watches "other")
    assert sorted(r[0] for r in outbox_rows(engine)) == sorted([everyone, phd_ok, phd_ok])


def test_watching_a_company_puts_it_on_the_five_minute_tier(engine: Engine) -> None:
    from fledge.worker import due_boards

    with engine.begin() as conn:
        slow = make_board(conn, "slow", "Slow")
        conn.execute(update(db.boards).values(priority=False, active=True, next_check_at=T0))
        assert [b.priority for b in due_boards(conn, T0)] == [False]
        add_subscriber(conn, SubscriberInput("a@example.com", ("slow",)))
        assert [(b.id, b.priority) for b in due_boards(conn, T0)] == [(slow, True)]


def test_removing_a_subscriber_deletes_their_queued_alerts(engine: Engine) -> None:
    with engine.begin() as conn:
        acme = make_board(conn)
        add_subscriber(conn, SubscriberInput("a@example.com"))
        make_posting(conn, acme, posting())
        alerts.queue_alerts(conn, acme, [drop()], T0)
        remove_subscriber(conn, "a@example.com")
    assert outbox_rows(engine) == []


# --- Database: the sender -------------------------------------------------------


class FakeMailer:
    def __init__(self) -> None:
        self.sent: list[EmailMessage] = []
        self.fail_with: Exception | None = None

    def send(self, message: EmailMessage) -> None:
        if self.fail_with is not None:
            raise self.fail_with
        self.sent.append(message)


class Clock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture
def setup(engine: Engine) -> tuple[Sender, FakeMailer, Clock, list[str], Any]:
    """One subscriber, one board; returns a sender and a function that queues a new role."""
    mailer, clock, lines = FakeMailer(), Clock(), list[str]()
    with engine.begin() as conn:
        board_id = make_board(conn)
        add_subscriber(conn, SubscriberInput("a@example.com"))
    counter = iter(range(1, 1000))

    def new_role(title: str = "Software Engineer Intern") -> None:
        with engine.begin() as conn:
            p = posting(str(next(counter)), title)
            make_posting(conn, board_id, p)
            alerts.queue_alerts(conn, board_id, [drop(p)], clock.now)

    sender = Sender(engine, mailer, "alerts@fledge.test", clock=clock, out=lines.append)
    return sender, mailer, clock, lines, new_role


def send(sender: Sender) -> int:
    return asyncio.run(sender.send_due())


def test_first_alert_is_sent_now_and_a_burst_is_grouped(setup: Any) -> None:
    sender, mailer, clock, lines, new_role = setup
    new_role("Data Engineer Intern")
    assert send(sender) == 1
    assert mailer.sent[0]["Subject"] == "New: Data Engineer Intern at Acme"

    clock.now = T0 + 2 * MIN
    new_role("Backend Engineer Intern")
    new_role("ML Engineer Intern")
    assert send(sender) == 0  # emailed 2 min ago: held
    clock.now = T0 + 9 * MIN
    assert send(sender) == 0  # still inside the 10-minute window
    clock.now = T0 + 10 * MIN
    assert send(sender) == 1  # one email with both
    assert mailer.sent[1]["Subject"] == "2 new roles: Acme"
    assert lines[-1] == "emailed subscriber 1: 2 alert(s)"  # an id, never the address


def test_failed_send_retries_on_schedule_then_gives_up_after_a_day(
    setup: Any, engine: Engine
) -> None:
    sender, mailer, clock, _, new_role = setup
    new_role()
    mailer.fail_with = smtplib.SMTPServerDisconnected("connection unexpectedly closed")
    assert send(sender) == 0
    [(_, status, attempts)] = outbox_rows(engine)
    assert (status, attempts) == ("pending", 1)

    clock.now = T0 + 30 * MIN  # retries at +1, +5, +15, then hourly
    while clock.now < T0 + timedelta(hours=25):
        send(sender)
        clock.now += 30 * MIN
    [(_, status, attempts)] = outbox_rows(engine)
    assert status == "failed"
    assert 20 < attempts < 30  # hourly-ish for a day, not every half hour

    mailer.fail_with = None
    assert send(sender) == 0  # failed rows are never retried


def test_wrong_password_stops_sending_without_losing_anything(setup: Any, engine: Engine) -> None:
    sender, mailer, _, lines, new_role = setup
    new_role()
    mailer.fail_with = MailerAuthError("SMTP login rejected (code 535)")
    assert send(sender) == 0
    assert sender.halted
    assert any(line.startswith("EMAIL STOPPED") for line in lines)
    assert outbox_rows(engine) == [(1, "pending", 0)]  # not counted as an attempt


def test_crash_after_sending_repeats_the_email_at_least_once(
    setup: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Documents the chosen guarantee: if the server accepted the email but we
    # couldn't record it, the next pass sends it again rather than losing it.
    sender, mailer, _, _, new_role = setup
    new_role()

    def crash(*args: Any) -> None:
        raise OperationalError("UPDATE outbox", {}, Exception("connection lost"))

    monkeypatch.setattr(alerts, "mark_sent", crash)
    with pytest.raises(OperationalError):
        send(sender)
    monkeypatch.undo()
    assert send(sender) == 1
    assert len(mailer.sent) == 2  # the same alert, twice
    assert mailer.sent[0]["Subject"] == mailer.sent[1]["Subject"]


def test_status_counts_emails(setup: Any, engine: Engine) -> None:
    sender, _, _, _, new_role = setup
    new_role()
    new_role()
    with engine.connect() as conn:
        assert alerts.outbox_counts(conn) == {"pending": 2}
    send(sender)
    with engine.connect() as conn:
        assert alerts.outbox_counts(conn) == {"sent": 2}
