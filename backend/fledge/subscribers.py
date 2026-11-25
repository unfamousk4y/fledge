"""Manage alert subscribers from the command line (until sign-in exists).

    python -m fledge.subscribers add EMAIL [--companies stripe,figma]
                                           [--include-advanced-degrees]
                                           [--exclude-unclear-location]
    python -m fledge.subscribers list
    python -m fledge.subscribers remove EMAIL

Only add people who asked for alerts. `remove` hard-deletes the subscriber and
their queued alerts (DECISIONS.md: deletion is immediate and complete).
"""

import argparse
import re
import sys
from dataclasses import dataclass

from sqlalchemy import Connection, create_engine, delete, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from fledge import config, db

# Deliberately loose: real validation is "did the email arrive". This only
# catches typos like a missing @ or a stray space.
_EMAIL = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")


class SubscriberError(ValueError):
    pass


@dataclass(frozen=True)
class SubscriberInput:
    email: str
    companies: tuple[str, ...] = ()  # board tokens; empty = all companies
    hide_advanced_degrees: bool = True
    include_unclear_location: bool = True


def normalize_email(raw: str) -> str:
    email = raw.strip().lower()
    if not _EMAIL.fullmatch(email):
        raise SubscriberError(f"{raw!r} doesn't look like an email address")
    return email


def add_subscriber(conn: Connection, sub: SubscriberInput) -> int:
    """Create the subscriber, or replace an existing one's settings. Returns its id."""
    email = normalize_email(sub.email)
    b = db.boards
    board_ids: dict[str, int] = {}
    if sub.companies:
        rows = conn.execute(
            select(b.c.token, b.c.id).where(b.c.token.in_(sub.companies), b.c.active)
        )
        board_ids = {row.token: row.id for row in rows}
        unknown = sorted(set(sub.companies) - board_ids.keys())
        if unknown:
            raise SubscriberError(
                f"unknown or inactive companies: {', '.join(unknown)} "
                "(use board tokens from data/companies.csv, e.g. stripe)"
            )

    values = {
        "email": email,
        "all_companies": not sub.companies,
        "hide_advanced_degrees": sub.hide_advanced_degrees,
        "include_unclear_location": sub.include_unclear_location,
    }
    stmt = pg_insert(db.subscribers).values(**values)
    subscriber_id = conn.scalar(
        stmt.on_conflict_do_update(
            index_elements=[db.subscribers.c.email],
            set_={k: stmt.excluded[k] for k in values if k != "email"},
        ).returning(db.subscribers.c.id)
    )
    assert subscriber_id is not None

    sb = db.subscriber_boards
    conn.execute(delete(sb).where(sb.c.subscriber_id == subscriber_id))
    if board_ids:
        conn.execute(
            pg_insert(sb).values(
                [{"subscriber_id": subscriber_id, "board_id": bid} for bid in board_ids.values()]
            )
        )
    return int(subscriber_id)


def remove_subscriber(conn: Connection, raw_email: str) -> bool:
    """Hard-delete. Their watches and queued alerts go with them (ON DELETE CASCADE)."""
    email = normalize_email(raw_email)
    result = conn.execute(delete(db.subscribers).where(db.subscribers.c.email == email))
    return result.rowcount > 0


def list_lines(conn: Connection) -> list[str]:
    s, sb, b, o = db.subscribers, db.subscriber_boards, db.boards, db.outbox
    companies = func.string_agg(b.c.token, ",").label("companies")
    watched = (
        select(sb.c.subscriber_id, companies)
        .join(b, b.c.id == sb.c.board_id)
        .group_by(sb.c.subscriber_id)
        .subquery()
    )
    pending = (
        select(o.c.subscriber_id, func.count().label("n"))
        .where(o.c.status == "pending")
        .group_by(o.c.subscriber_id)
        .subquery()
    )
    rows = conn.execute(
        select(s, watched.c.companies, pending.c.n)
        .outerjoin(watched, watched.c.subscriber_id == s.c.id)
        .outerjoin(pending, pending.c.subscriber_id == s.c.id)
        .order_by(s.c.id)
    )
    lines = []
    for r in rows:
        scope = "all companies" if r.all_companies else (r.companies or "(none)")
        flags = []
        if not r.hide_advanced_degrees:
            flags.append("incl. PhD/Master's")
        if not r.include_unclear_location:
            flags.append("US-only locations")
        last = (
            r.last_emailed_at.astimezone().strftime("%Y-%m-%d %H:%M")
            if r.last_emailed_at
            else "never"
        )
        lines.append(
            f"{r.id:>3}  {r.email:<32} {scope:<28} last email: {last}  pending: {r.n or 0}"
            + (f"  [{', '.join(flags)}]" if flags else "")
        )
    return lines or ["no subscribers yet"]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Manage Fledge alert subscribers.")
    sub = parser.add_subparsers(dest="command", required=True)
    add = sub.add_parser("add", help="add a subscriber (or update one)")
    add.add_argument("email")
    add.add_argument("--companies", default="", help="comma-separated board tokens; default all")
    add.add_argument("--include-advanced-degrees", action="store_true")
    add.add_argument("--exclude-unclear-location", action="store_true")
    sub.add_parser("list", help="show subscribers")
    remove = sub.add_parser("remove", help="delete a subscriber and their queued alerts")
    remove.add_argument("email")
    args = parser.parse_args(argv)

    engine = create_engine(config.database_url())
    try:
        with engine.begin() as conn:
            if args.command == "add":
                tokens = tuple(t.strip().lower() for t in args.companies.split(",") if t.strip())
                subscriber_id = add_subscriber(
                    conn,
                    SubscriberInput(
                        args.email,
                        tokens,
                        hide_advanced_degrees=not args.include_advanced_degrees,
                        include_unclear_location=not args.exclude_unclear_location,
                    ),
                )
                print(f"subscriber {subscriber_id} saved")
            elif args.command == "remove":
                print("removed" if remove_subscriber(conn, args.email) else "no such subscriber")
            else:
                for line in list_lines(conn):
                    print(line)
    except SubscriberError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    finally:
        engine.dispose()
    return 0


if __name__ == "__main__":
    sys.exit(main())
