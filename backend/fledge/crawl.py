"""Command line: crawl one Greenhouse board on a loop, saving every check to Postgres.

Restart-safe: on startup the crawler's memory is loaded from the database, so a
restart does not redo the baseline, and postings that appeared while it was
stopped still alert (DECISIONS.md, "Downtime").

Run it (Postgres must be up: `docker compose up -d` from the repo root):
    python -m fledge.crawl figma --interval 300 --duration 900
"""

import argparse
import asyncio
import contextlib
import sys
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import httpx
from sqlalchemy import Engine, create_engine
from sqlalchemy.exc import SQLAlchemyError

from fledge import config, db
from fledge.crawler import BoardCrawler, CheckReport, make_client


def format_report(report: CheckReport, crawler: BoardCrawler) -> list[str]:
    stamp = report.at.astimezone().strftime("%H:%M:%S")
    status = report.fetch.status.value
    lines = []
    if report.fetch.error:
        lines.append(f"{stamp}  {status}: {report.fetch.error}")
    elif report.baseline:
        lines.append(f"{stamp}  baseline: {len(report.events)} postings already open (no alerts)")
    else:
        lines.append(f"{stamp}  {status}: {len(report.events)} event(s)")
        for event in report.events:
            title = crawler.postings[event.posting_id].title
            lines.append(f"          {event.kind.value:<10} {title}")
    if report.fetch.skipped:
        lines.append(f"          warning: skipped {report.fetch.skipped} malformed job(s)")
    for drop in report.drops:
        tags = [drop.degree.value.upper()] if drop.degree else []
        if drop.us is None:
            tags.append("location unclear")
        tag_text = f" [{', '.join(tags)}]" if tags else ""
        lines.append(f"  ALERT   {drop.posting.title}{tag_text}{_latency_text(drop.latency)}")
        lines.append(f"          {drop.posting.url}")
    return lines


def _latency_text(latency: timedelta | None) -> str:
    if latency is None:
        return ""
    minutes = latency.total_seconds() / 60
    if minutes < 120:
        return f", {minutes:.0f} min after publish"
    return f", {minutes / 60:.1f} h after publish"


async def check_and_save(
    engine: Engine,
    board_id: int,
    crawler: BoardCrawler,
    client: httpx.AsyncClient,
    now: datetime,
) -> tuple[BoardCrawler, CheckReport | None]:
    """Run one check and commit it. Returns the crawler to use next time.

    If saving fails, the transaction rolls back and the in-memory crawler (which
    already moved ahead) is thrown away and reloaded from the database. The next
    check then redoes this one's work, so events and alerts are neither lost
    nor duplicated. The report is returned only once it is committed: nothing
    alerts from a check the database doesn't have.
    """
    report = await crawler.check_once(client, now)
    try:
        with engine.begin() as conn:
            db.save_check(conn, board_id, crawler, report)
    except SQLAlchemyError:
        with engine.connect() as conn:
            return db.load_crawler(conn, board_id), None
    return crawler, report


async def run(
    board: str,
    interval: float,
    duration: float | None,
    engine: Engine,
    out: Callable[[str], None] = print,
) -> None:
    with engine.begin() as conn:
        board_id = db.ensure_board(conn, board)
        removed = db.delete_old_runs(conn, datetime.now(UTC))
        crawler = db.load_crawler(conn, board_id)
    known = len(crawler.states)
    out(
        f"loaded {known} known posting(s) for {board}"
        + (f"; pruned {removed} old run(s)" if removed else "")
    )

    max_checks = None if duration is None else checks_for(duration, interval)
    done = 0
    async with make_client() as client:
        while True:
            crawler, report = await check_and_save(
                engine, board_id, crawler, client, datetime.now(UTC)
            )
            if report is None:
                out("warning: could not save this check; reloaded state, will retry next check")
            else:
                for line in format_report(report, crawler):
                    out(line)
            done += 1
            if max_checks is not None and done >= max_checks:
                return
            await asyncio.sleep(interval)


def checks_for(duration: float, interval: float) -> int:
    """How many checks fit in `duration`, counting the one at time 0.

    Counting checks (not comparing clock times) avoids losing the last check to
    a few milliseconds of drift: 900 s at a 300 s interval is 4 checks (0, 5, 10, 15 min).
    """
    return int(duration // interval) + 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Watch one Greenhouse board for changes.")
    parser.add_argument("board", help="Greenhouse board token, e.g. figma")
    parser.add_argument("--interval", type=float, default=300, help="seconds between checks")
    parser.add_argument("--duration", type=float, default=None, help="stop after N seconds")
    args = parser.parse_args(argv)
    if args.interval < 60:
        parser.error("--interval must be at least 60 seconds (be polite to company servers)")
    engine = create_engine(config.database_url())
    try:
        with contextlib.suppress(KeyboardInterrupt):  # Ctrl+C exits quietly
            asyncio.run(run(args.board, args.interval, args.duration, engine))
    finally:
        engine.dispose()
    return 0


if __name__ == "__main__":
    sys.exit(main())
