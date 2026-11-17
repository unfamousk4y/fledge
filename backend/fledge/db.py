"""Postgres storage for crawl state, postings and their history.

Table definitions use SQLAlchemy Core: Python objects that describe tables and
build SQL, without an ORM hiding the queries. Alembic migrations (backend/alembic)
create these tables; tests/test_db.py checks the two never drift apart.

The database is the source of truth. One check is saved in ONE transaction
(`save_check`), so after a crash the database holds either the whole check or
none of it, never half.
"""

from collections.abc import Iterable, Mapping
from datetime import datetime, timedelta

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Column,
    Connection,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Integer,
    MetaData,
    Table,
    Text,
    UniqueConstraint,
    delete,
    func,
    insert,
    select,
    update,
)
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.dialects.postgresql import insert as pg_insert

from fledge.crawler import BoardCrawler, CheckReport
from fledge.detector import EventKind, PostingState, Status
from fledge.models import Posting
from fledge.sources import greenhouse

RUN_RETENTION = timedelta(days=30)

# Explicit constraint names, so migrations can refer to them later.
metadata = MetaData(
    naming_convention={
        "pk": "pk_%(table_name)s",
        "fk": "fk_%(table_name)s_%(column_0_name)s",
        "uq": "uq_%(table_name)s_%(column_0_name)s",
        "ck": "ck_%(table_name)s_%(constraint_name)s",
        "ix": "ix_%(table_name)s_%(column_0_name)s",
    }
)


def _tz(name: str, nullable: bool = True) -> Column[datetime]:
    """Every timestamp is `timestamptz`: stored as an exact instant (UTC inside)."""
    return Column(name, DateTime(timezone=True), nullable=nullable)


def _one_of(column: str, values: Iterable[str]) -> str:
    quoted = ", ".join(f"'{v}'" for v in values)
    return f"{column} IN ({quoted})"


boards = Table(
    "boards",
    metadata,
    Column("id", Integer, Identity(), primary_key=True),
    Column("source", Text, nullable=False),  # which ATS, e.g. "greenhouse"
    Column("token", Text, nullable=False),  # the company's board name, e.g. "figma"
    Column("etag", Text),
    # IDs from the last successful check. A 304 means "same as last time", so
    # after a restart we need this list to interpret the next 304.
    Column("last_seen_ids", ARRAY(Text)),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    UniqueConstraint("source", "token"),
)

postings = Table(
    "postings",
    metadata,
    Column("id", BigInteger, Identity(), primary_key=True),
    Column("board_id", ForeignKey("boards.id", ondelete="CASCADE"), nullable=False),
    Column("external_id", Text, nullable=False),  # the ATS's own job ID
    Column("title", Text, nullable=False),
    Column("location", Text, nullable=False),
    Column("url", Text, nullable=False),
    _tz("first_published"),
    _tz("updated_at"),
    _tz("first_seen_at", nullable=False),  # when Fledge first noticed it
    # Detector state (see detector.py):
    Column("status", Text, nullable=False),
    _tz("missing_since"),
    _tz("closed_at"),
    UniqueConstraint("board_id", "external_id"),
    CheckConstraint(_one_of("status", (s.value for s in Status)), name="status_valid"),
    # The detector's own invariants, enforced by the database too:
    CheckConstraint("(status = 'missing') = (missing_since IS NOT NULL)", name="missing_since"),
    CheckConstraint("(status = 'closed') = (closed_at IS NOT NULL)", name="closed_at"),
)

posting_events = Table(
    "posting_events",
    metadata,
    Column("id", BigInteger, Identity(), primary_key=True),
    Column("posting_id", ForeignKey("postings.id", ondelete="CASCADE"), nullable=False),
    Column("kind", Text, nullable=False),
    _tz("at", nullable=False),
    CheckConstraint(_one_of("kind", (k.value for k in EventKind)), name="kind_valid"),
    Index(None, "posting_id", "at"),
)

crawl_runs = Table(
    "crawl_runs",
    metadata,
    Column("id", BigInteger, Identity(), primary_key=True),
    Column("board_id", ForeignKey("boards.id", ondelete="CASCADE"), nullable=False),
    _tz("at", nullable=False),
    Column("status", Text, nullable=False),  # FetchStatus value
    Column("error", Text),
    Column("event_count", Integer, nullable=False),
    Column("skipped", Integer, nullable=False),
    CheckConstraint(
        _one_of("status", (s.value for s in greenhouse.FetchStatus)), name="status_valid"
    ),
    Index(None, "at"),  # the 30-day cleanup deletes by time
)


# --- Reading ------------------------------------------------------------------


def ensure_board(conn: Connection, token: str, source: str = greenhouse.SOURCE) -> int:
    """Return the board's row ID, creating the row the first time."""
    conn.execute(pg_insert(boards).values(source=source, token=token).on_conflict_do_nothing())
    board_id = conn.scalar(
        select(boards.c.id).where(boards.c.source == source, boards.c.token == token)
    )
    assert board_id is not None
    return int(board_id)


def load_crawler(conn: Connection, board_id: int) -> BoardCrawler:
    """Rebuild a crawler's memory from the database (startup, or after a failed save)."""
    board = conn.execute(select(boards).where(boards.c.id == board_id)).one()
    crawler = BoardCrawler(
        board.token,
        etag=board.etag,
        last_seen_ids=None if board.last_seen_ids is None else frozenset(board.last_seen_ids),
    )
    for row in conn.execute(select(postings).where(postings.c.board_id == board_id)):
        crawler.postings[row.external_id] = Posting(
            source=board.source,
            board=board.token,
            external_id=row.external_id,
            title=row.title,
            location=row.location,
            url=row.url,
            first_published=row.first_published,
            updated_at=row.updated_at,
        )
        crawler.states[row.external_id] = PostingState(Status(row.status), row.missing_since)
    return crawler


# --- Writing ------------------------------------------------------------------


def save_check(conn: Connection, board_id: int, crawler: BoardCrawler, report: CheckReport) -> None:
    """Write everything one check produced. Call inside `engine.begin()`.

    `crawler` must already hold the post-check state (check_once updates it).
    The caller owns the transaction: if anything here raises, it rolls back.
    """
    closed_at = {e.posting_id: e.at for e in report.events if e.kind is EventKind.CLOSED}

    def state_columns(external_id: str) -> dict[str, datetime | str | None]:
        state = crawler.states[external_id]
        return {
            "status": state.status.value,
            "missing_since": state.missing_since,
            "closed_at": closed_at.get(external_id) if state.status is Status.CLOSED else None,
        }

    # 1. Postings fetched this check: insert new ones, refresh known ones.
    fetched = [p for p in report.fetch.postings if p.external_id in crawler.states]
    if fetched:
        stmt = pg_insert(postings).values(
            [
                {
                    "board_id": board_id,
                    "external_id": p.external_id,
                    "title": p.title,
                    "location": p.location,
                    "url": p.url,
                    "first_published": p.first_published,
                    "updated_at": p.updated_at,
                    "first_seen_at": report.at,
                    **state_columns(p.external_id),
                }
                for p in fetched
            ]
        )
        # On conflict: refresh everything except first_seen_at, which keeps
        # the time we first noticed the posting.
        refreshed = {
            col: stmt.excluded[col]
            for col in (
                "title",
                "location",
                "url",
                "first_published",
                "updated_at",
                "status",
                "missing_since",
                "closed_at",
            )
        }
        conn.execute(
            stmt.on_conflict_do_update(
                index_elements=[postings.c.board_id, postings.c.external_id], set_=refreshed
            )
        )

    # 2. Postings whose state changed without being fetched (went missing or
    #    closed, including during a 304): update the state columns only.
    fetched_ids = {p.external_id for p in fetched}
    for external_id in {e.posting_id for e in report.events} - fetched_ids:
        conn.execute(
            update(postings)
            .where(postings.c.board_id == board_id, postings.c.external_id == external_id)
            .values(**state_columns(external_id))
        )

    _insert_events(conn, board_id, report)

    # 4. The check itself, plus what we need to interpret the next 304.
    conn.execute(
        insert(crawl_runs).values(
            board_id=board_id,
            at=report.at,
            status=report.fetch.status.value,
            error=report.fetch.error,
            event_count=len(report.events),
            skipped=report.fetch.skipped,
        )
    )
    last_seen = None if crawler.last_seen_ids is None else sorted(crawler.last_seen_ids)
    conn.execute(
        update(boards)
        .where(boards.c.id == board_id)
        .values(etag=crawler.etag, last_seen_ids=last_seen)
    )


def _insert_events(conn: Connection, board_id: int, report: CheckReport) -> None:
    """3. Append the history rows. Separate function so tests can make it fail."""
    if not report.events:
        return
    ids: Mapping[str, int] = {
        row.external_id: row.id
        for row in conn.execute(
            select(postings.c.id, postings.c.external_id).where(
                postings.c.board_id == board_id,
                postings.c.external_id.in_({e.posting_id for e in report.events}),
            )
        )
    }
    conn.execute(
        insert(posting_events),
        [
            {"posting_id": ids[e.posting_id], "kind": e.kind.value, "at": e.at}
            for e in report.events
        ],
    )


def delete_old_runs(conn: Connection, now: datetime, keep: timedelta = RUN_RETENTION) -> int:
    """Drop check records older than `keep`. Postings and events are never deleted here."""
    result = conn.execute(delete(crawl_runs).where(crawl_runs.c.at < now - keep))
    return result.rowcount
