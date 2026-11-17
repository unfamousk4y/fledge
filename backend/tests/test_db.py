"""Persistence tests against a real Postgres (see conftest.py).

Each test drives the real crawler + database through `check_and_save`, with a
fake Greenhouse server, and checks what a restarted crawler would know.
"""

import asyncio
import json
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from sqlalchemy import Connection, Engine, func, insert, select
from sqlalchemy.exc import IntegrityError, OperationalError

from fledge import db
from fledge.crawl import check_and_save
from fledge.crawler import BoardCrawler, CheckReport
from fledge.detector import EventKind, PostingState, Status

T0 = datetime(2026, 8, 3, 18, 0, tzinfo=UTC)


def job(job_id: int, title: str = "Software Engineer Intern") -> dict[str, Any]:
    return {
        "id": job_id,
        "title": title,
        "location": {"name": "New York, NY"},
        "absolute_url": f"https://boards.greenhouse.io/acme/jobs/{job_id}",
        "first_published": (T0 - timedelta(minutes=3)).isoformat(),
        "updated_at": T0.isoformat(),
    }


def ok(*jobs: dict[str, Any], etag: str = 'W/"v"') -> httpx.Response:
    return httpx.Response(200, content=json.dumps({"jobs": list(jobs)}), headers={"ETag": etag})


INTERN = job(1)
SENIOR = job(3, "Senior Software Engineer")


class Harness:
    """One board in the test database, checked against a scripted fake server."""

    def __init__(self, engine: Engine) -> None:
        self.engine = engine
        with engine.begin() as conn:
            self.board_id = db.ensure_board(conn, "acme")
        self.crawler = self.restart()
        self.requests: list[httpx.Request] = []

    def restart(self) -> BoardCrawler:
        """What a freshly started process would know: only what's in the database."""
        with self.engine.connect() as conn:
            self.crawler = db.load_crawler(conn, self.board_id)
        return self.crawler

    def check(self, minutes: int, response: httpx.Response) -> CheckReport | None:
        def handler(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            return response

        async def go() -> tuple[BoardCrawler, CheckReport | None]:
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                return await check_and_save(
                    self.engine,
                    self.board_id,
                    self.crawler,
                    client,
                    T0 + timedelta(minutes=minutes),
                )

        self.crawler, report = asyncio.run(go())
        return report

    def count(self, table: Any) -> int:
        with self.engine.connect() as conn:
            return conn.scalar(select(func.count()).select_from(table)) or 0

    def events(self) -> list[tuple[str, str]]:
        with self.engine.connect() as conn:
            rows = conn.execute(
                select(db.postings.c.external_id, db.posting_events.c.kind)
                .join(db.postings)
                .order_by(db.posting_events.c.id)
            )
            return [(r.external_id, r.kind) for r in rows]

    def posting_row(self, external_id: str) -> Any:
        with self.engine.connect() as conn:
            return conn.execute(
                select(db.postings).where(db.postings.c.external_id == external_id)
            ).one()


# --- Schema -------------------------------------------------------------------


def test_migrations_match_table_definitions(migrated_engine: Engine) -> None:
    # The hand-written migration and db.py must describe the same tables.
    # An empty diff means `alembic upgrade head` builds exactly what the code expects.
    with migrated_engine.connect() as conn:
        diff = compare_metadata(MigrationContext.configure(conn), db.metadata)
    assert diff == []


def test_database_rejects_impossible_state(engine: Engine) -> None:
    # Constraints back up the detector: "missing" without missing_since can't be stored.
    with engine.begin() as conn:
        board_id = db.ensure_board(conn, "acme")
    with pytest.raises(IntegrityError), engine.begin() as conn:
        conn.execute(
            insert(db.postings).values(
                board_id=board_id,
                external_id="1",
                title="t",
                location="",
                url="u",
                first_seen_at=T0,
                status="missing",
                missing_since=None,
            )
        )


def test_ensure_board_is_idempotent(engine: Engine) -> None:
    with engine.begin() as conn:
        first = db.ensure_board(conn, "acme")
        second = db.ensure_board(conn, "acme")
    assert first == second


# --- Save and load ------------------------------------------------------------


def test_save_then_load_gives_the_same_memory(engine: Engine) -> None:
    h = Harness(engine)
    h.check(0, ok(INTERN, SENIOR, etag='W/"v1"'))
    h.check(5, ok(SENIOR, etag='W/"v2"'))  # intern goes missing
    before = h.crawler

    after = h.restart()
    assert after.states == before.states
    assert after.states["1"] == PostingState(Status.MISSING, T0 + timedelta(minutes=5))
    assert after.postings == before.postings
    assert after.etag == 'W/"v2"'
    assert after.last_seen_ids == frozenset({"3"})


def test_restart_does_not_redo_the_baseline_and_new_postings_alert(engine: Engine) -> None:
    h = Harness(engine)
    first = h.check(0, ok(SENIOR))
    assert first is not None and first.baseline

    h.restart()  # the process was stopped; a role appeared meanwhile
    new_intern = job(4, "Data Engineer Intern")
    report = h.check(6 * 60, ok(SENIOR, new_intern))

    assert report is not None and not report.baseline
    [drop] = report.drops
    assert drop.posting.external_id == "4"
    assert drop.latency == timedelta(hours=6, minutes=3)  # honest, late latency


def test_reposting_the_same_check_keeps_one_row_and_first_seen_at(engine: Engine) -> None:
    h = Harness(engine)
    h.check(0, ok(INTERN))
    renamed = job(1, "Software Engineer Intern (Summer 2027)")
    h.check(5, ok(renamed))

    assert h.count(db.postings) == 1
    row = h.posting_row("1")
    assert row.title == "Software Engineer Intern (Summer 2027)"  # refreshed
    assert row.first_seen_at == T0  # but not reset


def test_closure_confirmed_by_a_304_after_restart(engine: Engine) -> None:
    # Saved last_seen_ids is what lets a restarted crawler interpret a 304.
    h = Harness(engine)
    h.check(0, ok(INTERN, SENIOR, etag='W/"v1"'))
    h.check(5, ok(SENIOR, etag='W/"v2"'))
    h.restart()
    report = h.check(40, httpx.Response(304))

    assert report is not None
    assert [(e.kind, e.posting_id) for e in report.events] == [(EventKind.CLOSED, "1")]
    assert h.requests[-1].headers["if-none-match"] == 'W/"v2"'
    row = h.posting_row("1")
    assert row.status == "closed"
    assert row.closed_at == T0 + timedelta(minutes=5)  # dated when it first went missing
    assert row.missing_since is None
    assert h.events() == [("1", "opened"), ("3", "opened"), ("1", "missing"), ("1", "closed")]


def test_reopened_posting_clears_closed_at(engine: Engine) -> None:
    h = Harness(engine)
    h.check(0, ok(INTERN, SENIOR))
    h.check(5, ok(SENIOR))
    h.check(40, ok(SENIOR))
    h.check(60, ok(INTERN, SENIOR))
    row = h.posting_row("1")
    assert (row.status, row.closed_at) == ("open", None)
    assert h.events()[-1] == ("1", "reopened")


def test_failed_checks_are_recorded_but_change_nothing(engine: Engine) -> None:
    h = Harness(engine)
    h.check(0, ok(INTERN))
    h.check(5, httpx.Response(503))
    assert h.count(db.crawl_runs) == 2
    with engine.connect() as conn:
        statuses = conn.scalars(select(db.crawl_runs.c.status).order_by(db.crawl_runs.c.id))
        assert list(statuses) == ["ok", "failed"]
    assert h.posting_row("1").status == "open"


# --- Crash safety -------------------------------------------------------------


def test_failed_save_rolls_back_everything_and_the_next_check_redoes_it(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    h = Harness(engine)
    h.check(0, ok(SENIOR))
    new_intern = job(4, "Data Engineer Intern")

    def crash(conn: Connection, board_id: int, report: CheckReport) -> None:
        raise OperationalError("INSERT INTO posting_events", {}, Exception("connection lost"))

    # The posting row is written first, then the crash hits the events step.
    monkeypatch.setattr(db, "_insert_events", crash)
    assert h.check(5, ok(SENIOR, new_intern)) is None  # nothing committed, nothing alerted
    assert h.count(db.postings) == 1  # the new posting's row was rolled back
    assert h.count(db.crawl_runs) == 1
    assert "4" not in h.crawler.states  # memory was reloaded, not left ahead of the DB

    monkeypatch.undo()
    report = h.check(10, ok(SENIOR, new_intern))
    assert report is not None
    assert [d.posting.external_id for d in report.drops] == ["4"]  # alerted exactly once
    assert h.events().count(("4", "opened")) == 1


# --- Retention ----------------------------------------------------------------


def test_delete_old_runs_keeps_postings_and_events(engine: Engine) -> None:
    h = Harness(engine)
    h.check(0, ok(INTERN))
    h.check(5, ok(INTERN, SENIOR))
    with engine.begin() as conn:
        removed = db.delete_old_runs(conn, now=T0 + timedelta(days=30, minutes=1))
    assert removed == 1  # only the run at T0 is older than 30 days
    assert h.count(db.crawl_runs) == 1
    assert h.count(db.postings) == 2
    assert h.count(db.posting_events) == 2
