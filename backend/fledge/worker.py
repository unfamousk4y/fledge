"""The multi-board worker: checks every watched company on its own schedule.

    python -m fledge.worker sync     load data/companies.csv into the database
    python -m fledge.worker run      check due boards forever (Ctrl+C to stop)
    python -m fledge.worker status   one line per company

How one loop iteration works (`Worker.run_due_once`):
    1. ask Postgres which active boards have next_check_at <= now
    2. check them concurrently, at most MAX_IN_FLIGHT at a time (a semaphore)
    3. per board, in ONE transaction: save the check (db.save_check) and its new
       schedule + health. Database work runs in a thread so fetches keep going.
    4. an unexpected error in one board is logged, recorded as a failed run and
       backed off; the other boards are unaffected

Only one worker may run against a database at a time (a Postgres advisory
lock), or two workers could both report the same new posting.
"""

import argparse
import asyncio
import contextlib
import logging
import random
import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import partial

import httpx
from sqlalchemy import Connection, Engine, create_engine, insert, select, text, update

from fledge import companies, config, db, schedule
from fledge.crawl import format_report
from fledge.crawler import BoardCrawler, CheckReport, make_client
from fledge.db import WorkerAlreadyRunning, worker_lock
from fledge.schedule import BoardHealth
from fledge.sources.greenhouse import FetchStatus

log = logging.getLogger("fledge.worker")

MAX_IN_FLIGHT = 10
MAX_SLEEP = timedelta(seconds=30)  # wake at least this often to look for due boards
PRUNE_EVERY = timedelta(hours=24)


@dataclass(frozen=True)
class DueBoard:
    id: int
    token: str
    name: str
    priority: bool
    health: BoardHealth


def due_boards(conn: Connection, now: datetime) -> list[DueBoard]:
    b = db.boards
    rows = conn.execute(
        select(b)
        .where(b.c.active, b.c.next_check_at <= now)
        .order_by(b.c.next_check_at)  # most overdue first
    )
    return [
        DueBoard(
            r.id,
            r.token,
            r.name or r.token,
            r.priority,
            BoardHealth(r.consecutive_failures, r.not_found_since),
        )
        for r in rows
    ]


def seconds_until_next_due(conn: Connection, now: datetime) -> float:
    b = db.boards
    soonest = conn.scalar(select(b.c.next_check_at).where(b.c.active).order_by(b.c.next_check_at))
    if soonest is None:
        return MAX_SLEEP.total_seconds()
    wait: float = (soonest - now).total_seconds()
    return min(max(wait, 1.0), MAX_SLEEP.total_seconds())


def _set_schedule(
    conn: Connection, board_id: int, health: BoardHealth, next_check_at: datetime
) -> None:
    conn.execute(
        update(db.boards)
        .where(db.boards.c.id == board_id)
        .values(
            next_check_at=next_check_at,
            consecutive_failures=health.consecutive_failures,
            not_found_since=health.not_found_since,
        )
    )


@dataclass(frozen=True)
class Outcome:
    """What happened to one board in one loop iteration (for tests and logs)."""

    board: DueBoard
    report: CheckReport | None  # None if the check crashed or couldn't be saved
    health: BoardHealth
    next_check_at: datetime


class Worker:
    def __init__(
        self,
        engine: Engine,
        client: httpx.AsyncClient,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        rng: random.Random | None = None,
        out: Callable[[str], None] = print,
        max_in_flight: int = MAX_IN_FLIGHT,
    ) -> None:
        self.engine = engine
        self.client = client
        self.clock = clock
        self.rng = rng or random.Random()
        self.out = out
        self.slots = asyncio.Semaphore(max_in_flight)
        # In-memory crawler per board, so each check doesn't reload every posting.
        # Dropped whenever memory might be ahead of the database.
        self.crawlers: dict[int, BoardCrawler] = {}

    def _db[T](self, fn: Callable[[Connection], T]) -> T:
        """Run fn inside one transaction (commit on success, roll back on error)."""
        with self.engine.begin() as conn:
            return fn(conn)

    async def run_due_once(self) -> list[Outcome]:
        now = self.clock()
        boards = await asyncio.to_thread(self._db, lambda c: due_boards(c, now))
        return list(await asyncio.gather(*(self._check(b) for b in boards)))

    async def _check(self, board: DueBoard) -> Outcome:
        async with self.slots:
            try:
                return await self._check_and_save(board)
            except Exception as exc:  # one board's bug must not stop the others
                self.crawlers.pop(board.id, None)
                log.exception("check of %s crashed", board.token)
                return await self._record_crash(board, exc)

    async def _check_and_save(self, board: DueBoard) -> Outcome:
        crawler = self.crawlers.get(board.id)
        if crawler is None:
            crawler = await asyncio.to_thread(self._db, lambda c: db.load_crawler(c, board.id))
        now = self.clock()
        report = await crawler.check_once(self.client, now)
        health = schedule.update_health(board.health, report.fetch.status, now)
        next_at = now + schedule.next_delay(
            board.priority, health, now, self.rng, report.fetch.retry_after
        )

        def save(conn: Connection) -> None:
            db.save_check(conn, board.id, crawler, report)
            _set_schedule(conn, board.id, health, next_at)

        # If this raises, _check drops the cached crawler and records a crash.
        await asyncio.to_thread(self._db, save)
        self.crawlers[board.id] = crawler

        for line in format_report(report, crawler):
            self.out(f"[{board.name}] {line.strip()}")
        if schedule.is_broken(health, now):  # broken boards are checked daily, so once a day
            self.out(f"[{board.name}] board has returned 404 for 24 h+; checking once a day")
        return Outcome(board, report, health, next_at)

    async def _record_crash(self, board: DueBoard, exc: Exception) -> Outcome:
        now = self.clock()
        health = schedule.update_health(board.health, FetchStatus.FAILED, now)
        next_at = now + schedule.next_delay(board.priority, health, now, self.rng)
        self.out(f"[{board.name}] error: {type(exc).__name__}: {exc} (retry later)")

        def record(conn: Connection) -> None:
            conn.execute(
                insert(db.crawl_runs).values(
                    board_id=board.id,
                    at=now,
                    status=FetchStatus.FAILED.value,
                    error=f"crash: {type(exc).__name__}: {exc}"[:1000],
                    event_count=0,
                    skipped=0,
                )
            )
            _set_schedule(conn, board.id, health, next_at)

        try:
            await asyncio.to_thread(self._db, record)
        except Exception:
            # The database itself is failing. The board keeps its old
            # next_check_at, so it's simply retried on the next loop.
            log.exception("could not record the crash of %s", board.token)
        return Outcome(board, None, health, next_at)


async def run_forever(engine: Engine, out: Callable[[str], None] = print) -> None:
    with worker_lock(engine):
        async with make_client() as client:
            worker = Worker(engine, client, out=out)
            last_prune: datetime | None = None
            while True:
                now = worker.clock()
                if last_prune is None or now - last_prune >= PRUNE_EVERY:
                    removed = await asyncio.to_thread(
                        worker._db, partial(db.delete_old_runs, now=now)
                    )
                    if removed:
                        out(f"pruned {removed} check record(s) older than 30 days")
                    last_prune = now
                await worker.run_due_once()
                wait = await asyncio.to_thread(
                    worker._db, lambda c: seconds_until_next_due(c, worker.clock())
                )
                await asyncio.sleep(wait)


# --- status -------------------------------------------------------------------

_STATUS_SQL = text(
    """
    SELECT b.name, b.token, b.priority, b.active, b.next_check_at,
           b.consecutive_failures, b.not_found_since,
           r.at AS last_at, r.status AS last_status, r.error AS last_error,
           (SELECT count(*) FROM postings p
             WHERE p.board_id = b.id AND p.status <> 'closed') AS open_postings
    FROM boards b
    LEFT JOIN LATERAL (
        SELECT at, status, error FROM crawl_runs
        WHERE board_id = b.id ORDER BY at DESC LIMIT 1
    ) r ON true
    WHERE b.name IS NOT NULL
    ORDER BY b.active DESC, b.priority DESC, b.name
    """
)


def status_lines(conn: Connection, now: datetime) -> list[str]:
    def ago(t: datetime | None) -> str:
        if t is None:
            return "-"
        minutes = (t - now).total_seconds() / 60
        return f"in {minutes:.0f}m" if minutes >= 0 else f"{-minutes:.0f}m ago"

    lines = [
        f"{'company':<14} {'tier':<5} {'last check':<24} {'fails':>5} {'next':>9} {'open':>5}  note"
    ]
    for r in conn.execute(_STATUS_SQL):
        health = BoardHealth(r.consecutive_failures, r.not_found_since)
        notes = []
        if not r.active:
            notes.append("inactive (not in CSV)")
        if schedule.is_broken(health, now):
            notes.append("BROKEN: 404 for 24 h+")
        if r.last_error:
            notes.append(r.last_error[:60])
        last = f"{r.last_status} {ago(r.last_at)}" if r.last_at else "never"
        lines.append(
            f"{r.name[:14]:<14} {'5m' if r.priority else '30m':<5} {last:<24} "
            f"{r.consecutive_failures:>5} {ago(r.next_check_at):>9} {r.open_postings:>5}  "
            + "; ".join(notes)
        )
    return lines


# --- command line -------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Fledge multi-board worker.")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("sync", help="load data/companies.csv into the database")
    sub.add_parser("run", help="check due boards forever")
    sub.add_parser("status", help="show every company's state")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    engine = create_engine(config.database_url())
    try:
        if args.command == "sync":
            try:
                listed = companies.load_companies()
            except companies.CompanyListError as exc:
                print(f"{companies.CSV_PATH}:\n{exc}", file=sys.stderr)
                return 1
            with engine.begin() as conn:
                result = companies.sync_companies(conn, listed, datetime.now(UTC), random.Random())
            print(
                f"{len(listed)} companies in CSV: {result.added} added, "
                f"{result.updated} updated, {result.deactivated} deactivated"
            )
        elif args.command == "status":
            with engine.connect() as conn:
                for line in status_lines(conn, datetime.now(UTC)):
                    print(line)
        else:
            try:
                with contextlib.suppress(KeyboardInterrupt):
                    asyncio.run(run_forever(engine))
            except WorkerAlreadyRunning as exc:
                print(f"error: {exc}", file=sys.stderr)
                return 1
    finally:
        engine.dispose()
    return 0


if __name__ == "__main__":
    sys.exit(main())
