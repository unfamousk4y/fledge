"""Crawl one Greenhouse board on a loop and report what changed.

Flow per check:
    fetch_board -> CheckResult (every posting ID) -> detector.apply_check -> events
    -> classify which OPENED events would alert -> report

State lives in memory for now; the database replaces it in the next piece.

Run it:  python -m fledge.crawl figma --interval 300 --duration 900
"""

import argparse
import asyncio
import contextlib
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import httpx

from fledge.classify import DegreeTag, degree_tag, is_early_career_tech, is_us
from fledge.detector import CheckResult, Event, EventKind, PostingState, apply_check
from fledge.models import Posting
from fledge.sources.greenhouse import FetchResult, FetchStatus, fetch_board

USER_AGENT = "FledgeBot/0.1 (+https://github.com/unfamousk4y/fledge)"
TIMEOUT = httpx.Timeout(20.0, connect=5.0)


@dataclass(frozen=True)
class Drop:
    """An OPENED event worth alerting on: a new early-career tech role in the US."""

    posting: Posting
    seen_at: datetime
    degree: DegreeTag | None
    us: bool | None  # None = location unclear (e.g. just "Remote")

    @property
    def latency(self) -> timedelta | None:
        """How long after the company published it we noticed. Our speed metric."""
        if self.posting.first_published is None:
            return None
        return self.seen_at - self.posting.first_published


@dataclass(frozen=True)
class CheckReport:
    at: datetime
    fetch: FetchResult
    events: tuple[Event, ...]
    drops: tuple[Drop, ...]
    baseline: bool  # True for the first successful check: nothing is "new" yet


@dataclass
class BoardCrawler:
    board: str
    states: dict[str, PostingState] = field(default_factory=dict)
    postings: dict[str, Posting] = field(default_factory=dict)  # latest known copy
    etag: str | None = None
    last_seen_ids: frozenset[str] | None = None  # IDs from the last successful check

    async def check_once(self, client: httpx.AsyncClient, now: datetime) -> CheckReport:
        result = await fetch_board(client, self.board, self.etag)
        check = self._to_check(result, now)
        baseline = check.is_evidence and not self.states

        self.states, events = apply_check(self.states, check)

        drops: list[Drop] = []
        if not baseline:
            for event in events:
                if event.kind is EventKind.OPENED:
                    drop = self._as_drop(self.postings[event.posting_id], now)
                    if drop is not None:
                        drops.append(drop)
        return CheckReport(now, result, tuple(events), tuple(drops), baseline)

    def _to_check(self, result: FetchResult, now: datetime) -> CheckResult:
        if result.status is FetchStatus.OK:
            ids = frozenset(p.external_id for p in result.postings)
            self.postings.update({p.external_id: p for p in result.postings})
            self.etag = result.etag
            self.last_seen_ids = ids
            return CheckResult(now, succeeded=True, seen_ids=ids)
        if result.status is FetchStatus.NOT_MODIFIED and self.last_seen_ids is not None:
            # 304 = "same as last time": a real successful check, so time-based
            # rules (confirming a closure 30 min later) still advance.
            return CheckResult(now, succeeded=True, seen_ids=self.last_seen_ids)
        # NOT_FOUND, FAILED, or a 304 we can't interpret: not evidence.
        return CheckResult(now, succeeded=False, seen_ids=frozenset())

    @staticmethod
    def _as_drop(posting: Posting, now: datetime) -> Drop | None:
        if not is_early_career_tech(posting.title):
            return None
        us = is_us(posting.location)
        if us is False:
            return None
        return Drop(posting, now, degree_tag(posting.title), us)


def make_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=TIMEOUT, headers={"User-Agent": USER_AGENT})


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
        latency = (
            f", {drop.latency.total_seconds() / 60:.0f} min after publish" if drop.latency else ""
        )
        tag_text = f" [{', '.join(tags)}]" if tags else ""
        lines.append(f"  ALERT   {drop.posting.title}{tag_text}{latency}")
        lines.append(f"          {drop.posting.url}")
    return lines


async def run(
    board: str,
    interval: float,
    duration: float | None,
    out: Callable[[str], None] = print,
) -> None:
    crawler = BoardCrawler(board)
    max_checks = None if duration is None else checks_for(duration, interval)
    done = 0
    async with make_client() as client:
        while True:
            report = await crawler.check_once(client, datetime.now(UTC))
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
    with contextlib.suppress(KeyboardInterrupt):  # Ctrl+C exits quietly
        asyncio.run(run(args.board, args.interval, args.duration))
    return 0


if __name__ == "__main__":
    sys.exit(main())
