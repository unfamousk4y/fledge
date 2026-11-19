"""When to check a board next. Pure functions: no clock, no I/O, no global randomness.

Rules (DECISIONS.md, 2026-11-18):
- Normal interval: 5 min for priority companies, 30 min for the rest.
- After failures in a row: 2x the interval per failure, capped at 1 h.
- A server's Retry-After is honored when it asks for longer (capped at 1 day).
- A board that has answered 404 for 24 h straight is "broken": checked once a day
  until it answers again.
- Every delay gets +/-10% jitter so boards drift apart instead of firing together.

The caller passes `rng` (a random.Random), so tests can fix the seed.
"""

import random
from dataclasses import dataclass
from datetime import datetime, timedelta

from fledge.sources.greenhouse import FetchStatus

PRIORITY_INTERVAL = timedelta(minutes=5)
NORMAL_INTERVAL = timedelta(minutes=30)
MAX_BACKOFF = timedelta(hours=1)
BROKEN_AFTER = timedelta(hours=24)
BROKEN_INTERVAL = timedelta(hours=24)
MAX_RETRY_AFTER = timedelta(hours=24)
JITTER = 0.10


@dataclass(frozen=True)
class BoardHealth:
    consecutive_failures: int = 0
    not_found_since: datetime | None = None  # start of the current 404 streak


def update_health(health: BoardHealth, status: FetchStatus, now: datetime) -> BoardHealth:
    """Fold one check's outcome into the board's health."""
    if status in (FetchStatus.OK, FetchStatus.NOT_MODIFIED):
        return BoardHealth()  # any real answer resets everything
    failures = health.consecutive_failures + 1
    if status is FetchStatus.NOT_FOUND:
        return BoardHealth(failures, health.not_found_since or now)
    # FAILED (timeout, 5xx, garbage): proves nothing about whether the board
    # exists, so an ongoing 404 streak is neither reset nor extended.
    return BoardHealth(failures, health.not_found_since)


def is_broken(health: BoardHealth, now: datetime) -> bool:
    return health.not_found_since is not None and now - health.not_found_since >= BROKEN_AFTER


def interval(priority: bool) -> timedelta:
    return PRIORITY_INTERVAL if priority else NORMAL_INTERVAL


def next_delay(
    priority: bool,
    health: BoardHealth,
    now: datetime,
    rng: random.Random,
    retry_after: timedelta | None = None,
) -> timedelta:
    """How long to wait before the next check, given health *after* this check."""
    base = interval(priority)
    if is_broken(health, now):
        delay = BROKEN_INTERVAL
    elif health.consecutive_failures:
        # min(..., 10) keeps 2**n small; the cap applies long before that anyway.
        delay = min(base * 2 ** min(health.consecutive_failures, 10), MAX_BACKOFF)
    else:
        delay = base
    if retry_after is not None:
        delay = max(delay, min(retry_after, MAX_RETRY_AFTER))
    return delay * rng.uniform(1 - JITTER, 1 + JITTER)


def first_delay(priority: bool, rng: random.Random) -> timedelta:
    """A new board's first check: a random point within one interval, so 48
    freshly synced boards don't all fire in the first second."""
    return interval(priority) * rng.random()
