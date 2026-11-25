"""Email alerts: who gets which new role, and delivering them through the outbox.

Flow (DECISIONS.md 2026-11-24):
    worker check --(same transaction)--> queue_alerts: one outbox row per
    (subscriber, posting) that matches the subscriber's filters
    Sender.send_due --> plan_batches (burst grouping) --> one email per
    subscriber --> SMTP --> mark rows sent

Delivery is "at least once": rows are marked sent only after the mail server
accepts the email, so a crash in between can repeat one email, but a crash can
never silently lose an alert.

The rules (matches, plan_batches, retry_delay, build_message) are pure
functions; the database and SMTP parts are thin wrappers around them.
"""

import argparse
import asyncio
import contextlib
import logging
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage
from email.utils import make_msgid
from html import escape

from sqlalchemy import Connection, Engine, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from fledge import db
from fledge.classify import DegreeTag, degree_tag, is_us
from fledge.crawler import Drop
from fledge.mailer import Mailer, MailerAuthError, mailer_from_settings, sender_address

log = logging.getLogger("fledge.alerts")

GROUP_WINDOW = timedelta(minutes=10)
GIVE_UP_AFTER = timedelta(hours=24)
RETRY_STEPS = (timedelta(minutes=1), timedelta(minutes=5), timedelta(minutes=15))
RETRY_MAX = timedelta(hours=1)
POLL_EVERY = timedelta(seconds=30)  # backup wake-up; normally the worker wakes us


# --- Who gets what (pure) --------------------------------------------------------


@dataclass(frozen=True)
class Filters:
    all_companies: bool = True
    board_ids: frozenset[int] = frozenset()  # used when all_companies is False
    hide_advanced_degrees: bool = True
    include_unclear_location: bool = True


def matches(filters: Filters, board_id: int, drop: Drop) -> bool:
    if not filters.all_companies and board_id not in filters.board_ids:
        return False
    if filters.hide_advanced_degrees and drop.degree is not None:
        return False
    return not (drop.us is None and not filters.include_unclear_location)


def retry_delay(attempts: int) -> timedelta:
    """Wait after the Nth failed attempt: 1, 5, 15, 60 min, then hourly."""
    if attempts <= len(RETRY_STEPS):
        return RETRY_STEPS[attempts - 1]
    return RETRY_MAX


# --- Queueing (inside the worker's check transaction) ----------------------------


def queue_alerts(conn: Connection, board_id: int, drops: Sequence[Drop], now: datetime) -> int:
    """Insert outbox rows for every subscriber each drop matches. Returns rows added.

    Call inside the same transaction as db.save_check, so a check and its
    alerts are committed together or not at all.
    """
    if not drops:
        return 0
    s, sb, p = db.subscribers, db.subscriber_boards, db.postings
    watched: dict[int, set[int]] = {}
    for row in conn.execute(select(sb.c.subscriber_id, sb.c.board_id)):
        watched.setdefault(row.subscriber_id, set()).add(row.board_id)
    subscribers = {
        row.id: Filters(
            row.all_companies,
            frozenset(watched.get(row.id, ())),
            row.hide_advanced_degrees,
            row.include_unclear_location,
        )
        for row in conn.execute(select(s))
    }
    # (A query result has a keys() method, so dict(result) would misread it as
    # a mapping; build the dict explicitly.)
    posting_ids = {
        row.external_id: row.id
        for row in conn.execute(
            select(p.c.external_id, p.c.id).where(
                p.c.board_id == board_id,
                p.c.external_id.in_([d.posting.external_id for d in drops]),
            )
        )
    }
    rows = [
        {
            "subscriber_id": subscriber_id,
            "posting_id": posting_ids[drop.posting.external_id],
            "created_at": now,
            "status": "pending",
            "next_attempt_at": now,
        }
        for drop in drops
        for subscriber_id, filters in subscribers.items()
        if matches(filters, board_id, drop)
    ]
    if not rows:
        return 0
    # The unique (subscriber, posting) constraint makes re-queueing a no-op.
    # RETURNING lists only rows actually inserted (conflicts are skipped).
    # (rowcount isn't reliable for multi-row inserts: it can come back as -1.)
    inserted = conn.execute(
        pg_insert(db.outbox).values(rows).on_conflict_do_nothing().returning(db.outbox.c.id)
    )
    return len(inserted.all())


# --- Grouping (pure) --------------------------------------------------------------


@dataclass(frozen=True)
class PendingAlert:
    outbox_id: int
    subscriber_id: int
    email: str
    last_emailed_at: datetime | None
    created_at: datetime
    attempts: int
    company: str
    title: str
    location: str
    url: str
    first_published: datetime | None
    first_seen_at: datetime


@dataclass(frozen=True)
class Batch:
    """Alerts that go out together as one email."""

    subscriber_id: int
    email: str
    alerts: tuple[PendingAlert, ...]


@dataclass(frozen=True)
class Hold:
    """Alerts held back because this subscriber was emailed less than 10 min ago."""

    outbox_ids: tuple[int, ...]
    until: datetime


def plan_batches(pending: Sequence[PendingAlert], now: datetime) -> tuple[list[Batch], list[Hold]]:
    by_subscriber: dict[int, list[PendingAlert]] = {}
    for alert in pending:
        by_subscriber.setdefault(alert.subscriber_id, []).append(alert)
    batches, holds = [], []
    for subscriber_id, alerts in sorted(by_subscriber.items()):
        last = alerts[0].last_emailed_at
        if last is None or now - last >= GROUP_WINDOW:
            ordered = tuple(sorted(alerts, key=lambda a: (a.created_at, a.outbox_id)))
            batches.append(Batch(subscriber_id, alerts[0].email, ordered))
        else:
            holds.append(Hold(tuple(a.outbox_id for a in alerts), last + GROUP_WINDOW))
    return batches, holds


# --- The email itself (pure) ------------------------------------------------------


def _tags(alert: PendingAlert) -> list[str]:
    tags = []
    degree = degree_tag(alert.title)
    if degree is DegreeTag.PHD:
        tags.append("PhD")
    elif degree is DegreeTag.MASTERS:
        tags.append("Master's")
    if is_us(alert.location) is None:
        tags.append("location unclear")
    return tags


def _seen_text(alert: PendingAlert) -> str:
    if alert.first_published is None:
        return "Seen just now"
    minutes = (alert.first_seen_at - alert.first_published).total_seconds() / 60
    if minutes < 120:
        return f"Seen {max(minutes, 0):.0f} min after it was published"
    if minutes < 48 * 60:
        return f"Seen {minutes / 60:.1f} h after it was published"
    # Older than 2 days: usually reposted or long-running, so a date reads better
    # than "26713.5 h".
    published = alert.first_published
    return f"First published {published:%b} {published.day}, {published:%Y}"


def _safe_url(url: str) -> str | None:
    """Only link to web pages. A company-provided 'javascript:' URL is not a link."""
    return url if url.lower().startswith(("https://", "http://")) else None


def subject_for(alerts: Sequence[PendingAlert]) -> str:
    if len(alerts) == 1:
        return f"New: {alerts[0].title} at {alerts[0].company}"
    companies = list(dict.fromkeys(a.company for a in alerts))  # unique, in order
    shown = ", ".join(companies[:3])
    more = f", +{len(companies) - 3} more" if len(companies) > 3 else ""
    return f"{len(alerts)} new roles: {shown}{more}"


FOOTER = (
    "You're getting this because you asked Fledge for internship alerts. Reply STOP to unsubscribe."
)


def build_message(batch: Batch, sender: str) -> EmailMessage:
    text_parts, html_parts = [], []
    for a in batch.alerts:
        details = " · ".join([a.location or "Location not listed", *_tags(a)])
        url = _safe_url(a.url)
        text_parts.append(
            f"{a.title} - {a.company}\n{details}\n{_seen_text(a)}\n{url or '(no valid link)'}"
        )
        # Every company-provided value is escaped: a job title can't inject HTML.
        title_html = escape(a.title)
        if url:
            title_html = f'<a href="{escape(url, quote=True)}">{title_html}</a>'
        html_parts.append(
            f"<li><strong>{title_html}</strong> at {escape(a.company)}<br>"
            f"{escape(details)}<br><small>{escape(_seen_text(a))}</small></li>"
        )

    message = EmailMessage()
    message["Subject"] = subject_for(batch.alerts)
    message["From"] = f"Fledge <{sender}>"
    message["To"] = batch.email
    message["Message-ID"] = make_msgid(domain=sender.rsplit("@", 1)[-1])
    # Gives Gmail/Outlook their own "Unsubscribe" button, no web server needed.
    message["List-Unsubscribe"] = f"<mailto:{sender}?subject=STOP>"
    message.set_content("\n\n".join(text_parts) + f"\n\n--\n{FOOTER}\n")
    message.add_alternative(
        "<!doctype html><html><body>"
        f"<ul>{''.join(html_parts)}</ul>"
        f'<p style="color:#666;font-size:12px">{escape(FOOTER)}</p>'
        "</body></html>",
        subtype="html",
    )
    return message


# --- Database side of sending -----------------------------------------------------


def load_due(conn: Connection, now: datetime) -> list[PendingAlert]:
    o, s, p, b = db.outbox, db.subscribers, db.postings, db.boards
    rows = conn.execute(
        select(
            o.c.id.label("outbox_id"),
            o.c.subscriber_id,
            s.c.email,
            s.c.last_emailed_at,
            o.c.created_at,
            o.c.attempts,
            func.coalesce(b.c.name, b.c.token).label("company"),
            p.c.title,
            p.c.location,
            p.c.url,
            p.c.first_published,
            p.c.first_seen_at,
        )
        .join(s, s.c.id == o.c.subscriber_id)
        .join(p, p.c.id == o.c.posting_id)
        .join(b, b.c.id == p.c.board_id)
        .where(o.c.status == "pending", o.c.next_attempt_at <= now)
        .order_by(o.c.id)
    )
    return [PendingAlert(**row._asdict()) for row in rows]


def hold(conn: Connection, held: Hold) -> None:
    conn.execute(
        update(db.outbox)
        .where(db.outbox.c.id.in_(held.outbox_ids))
        .values(next_attempt_at=held.until)
    )


def mark_sent(conn: Connection, batch: Batch, now: datetime) -> None:
    ids = [a.outbox_id for a in batch.alerts]
    conn.execute(
        update(db.outbox)
        .where(db.outbox.c.id.in_(ids), db.outbox.c.status == "pending")
        .values(status="sent", sent_at=now, last_error=None)
    )
    conn.execute(
        update(db.subscribers)
        .where(db.subscribers.c.id == batch.subscriber_id)
        .values(last_emailed_at=now)
    )


def record_failure(conn: Connection, batch: Batch, error: str, now: datetime) -> None:
    for a in batch.alerts:
        attempts = a.attempts + 1
        gave_up = now - a.created_at >= GIVE_UP_AFTER
        conn.execute(
            update(db.outbox)
            .where(db.outbox.c.id == a.outbox_id)
            .values(
                attempts=attempts,
                last_error=error[:500],
                status="failed" if gave_up else "pending",
                next_attempt_at=now + retry_delay(attempts),
            )
        )


def seconds_until_next_send(conn: Connection, now: datetime) -> float:
    soonest = conn.scalar(
        select(func.min(db.outbox.c.next_attempt_at)).where(db.outbox.c.status == "pending")
    )
    if soonest is None:
        return POLL_EVERY.total_seconds()
    wait: float = (soonest - now).total_seconds()
    return min(max(wait, 0.5), POLL_EVERY.total_seconds())


def outbox_counts(conn: Connection) -> dict[str, int]:
    rows = conn.execute(
        select(db.outbox.c.status, func.count().label("n")).group_by(db.outbox.c.status)
    )
    return {row.status: row.n for row in rows}


# --- The sender loop --------------------------------------------------------------


class Sender:
    """Delivers due outbox rows. Runs as a task inside the worker process."""

    def __init__(
        self,
        engine: Engine,
        mailer: Mailer,
        sender: str,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        out: Callable[[str], None] = print,
    ) -> None:
        self.engine = engine
        self.mailer = mailer
        self.sender = sender
        self.clock = clock
        self.out = out
        self.wake = asyncio.Event()  # set by the worker right after it queues alerts
        self.halted = False  # set on a login failure: fix .env and restart

    def _db[T](self, fn: Callable[[Connection], T]) -> T:
        with self.engine.begin() as conn:
            return fn(conn)

    async def send_due(self) -> int:
        """Send everything due now. Returns the number of emails accepted."""
        if self.halted:
            return 0
        now = self.clock()
        pending = await asyncio.to_thread(self._db, lambda c: load_due(c, now))
        batches, holds = plan_batches(pending, now)
        for held in holds:
            await asyncio.to_thread(self._db, lambda c, h=held: hold(c, h))

        sent = 0
        for batch in batches:
            message = build_message(batch, self.sender)
            try:
                await asyncio.to_thread(self.mailer.send, message)
            except MailerAuthError as exc:
                # Every retry would fail the same way. Rows stay pending (no
                # attempt counted), so nothing is lost once .env is fixed.
                self.halted = True
                self.out(f"EMAIL STOPPED: {exc}. Fix SMTP_USERNAME/SMTP_PASSWORD, then restart.")
                return sent
            except Exception as exc:  # smtplib errors, timeouts, refused connections
                error = f"{type(exc).__name__}: {exc}"
                log.warning("email to subscriber %s failed: %s", batch.subscriber_id, error)
                await asyncio.to_thread(
                    self._db, lambda c, b=batch, e=error: record_failure(c, b, e, now)
                )
                continue
            # Accepted by the mail server. If this update fails (crash, DB down),
            # the rows stay pending and the email is sent again: at least once.
            await asyncio.to_thread(self._db, lambda c, b=batch: mark_sent(c, b, now))
            sent += 1
            # Subscriber id, never the address: logs shouldn't hold personal data.
            self.out(f"emailed subscriber {batch.subscriber_id}: {len(batch.alerts)} alert(s)")
        return sent

    async def run_forever(self) -> None:
        while not self.halted:
            try:
                await self.send_due()
            except Exception:  # e.g. the database is briefly down; try again later
                log.exception("sender loop error")
            wait = await asyncio.to_thread(
                self._db, lambda c: seconds_until_next_send(c, self.clock())
            )
            self.wake.clear()
            with contextlib.suppress(TimeoutError):  # timeout = time to look again anyway
                await asyncio.wait_for(self.wake.wait(), timeout=wait)


# --- Command line: send one sample email ------------------------------------------


def sample_batch(to: str, now: datetime) -> Batch:
    alert = PendingAlert(
        outbox_id=0,
        subscriber_id=0,
        email=to,
        last_emailed_at=None,
        created_at=now,
        attempts=0,
        company="Fledge",
        title="[Test] Software Engineer Intern",
        location="New York, NY",
        url="https://github.com/unfamousk4y/fledge",
        first_published=now - timedelta(minutes=4),
        first_seen_at=now,
    )
    return Batch(0, to, (alert,))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Fledge email alerts.")
    sub = parser.add_subparsers(dest="command", required=True)
    test = sub.add_parser("test", help="send one sample alert email")
    test.add_argument("to", help="recipient address (send only to yourself)")
    args = parser.parse_args(argv)

    message = build_message(sample_batch(args.to, datetime.now(UTC)), sender_address())
    try:
        mailer_from_settings().send(message)
    except MailerAuthError as exc:
        print(f"error: {exc}. Check SMTP_USERNAME/SMTP_PASSWORD in .env.", file=sys.stderr)
        return 1
    except OSError as exc:  # includes smtplib.SMTPException and refused connections
        print(f"error: could not send: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print("sent. Check the inbox (or http://localhost:8025 when using Mailpit).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
