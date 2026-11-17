"""One board's crawler: fetch, detect changes, decide which new postings alert.

Pure in-memory logic plus the HTTP fetch. Saving and loading this state lives in
db.py; the command-line loop lives in crawl.py.

Flow per check:
    fetch_board -> CheckResult (every posting ID) -> detector.apply_check -> events
    -> classify which OPENED events would alert -> CheckReport
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta

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
