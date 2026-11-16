"""Closed-rule state machine: decides what happened to each posting after a feed check.

This module is pure logic (no network, no database), so every rule is easy to test.
The crawler calls `apply_check` after each check of one company's feed and the
caller stores the returned states and events.

Rules (DECISIONS.md, "Closed rule"):
- A posting is CLOSED only after it is missing from >= 2 *successful* checks
  that are >= 30 minutes apart.
- A failed check, or a "successful" check that returned zero postings, is
  UNKNOWN: it is not evidence of anything, so nothing changes.

Lifecycle of one posting:

    (new) --OPENED--> OPEN --MISSING--> MISSING --CLOSED--> CLOSED
                       ^                   |                   |
                       +----REAPPEARED-----+                   |
                       +--------------------REOPENED-----------+
"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum

CLOSE_CONFIRMATION_GAP = timedelta(minutes=30)


class Status(Enum):
    OPEN = "open"
    MISSING = "missing"  # absent from one successful check; not closed yet
    CLOSED = "closed"


class EventKind(Enum):
    OPENED = "opened"  # first time we ever saw this posting
    MISSING = "missing"  # first successful check where it was absent
    REAPPEARED = "reappeared"  # back after a short blip (was MISSING)
    CLOSED = "closed"  # absence confirmed by a second check >= 30 min later
    REOPENED = "reopened"  # back after being CLOSED


@dataclass(frozen=True)
class CheckResult:
    """The outcome of fetching one company's feed once."""

    at: datetime
    succeeded: bool
    seen_ids: frozenset[str]

    @property
    def is_evidence(self) -> bool:
        # An empty 200 OK usually means an outage on the company's side, not
        # that every job closed at once, so we refuse to learn from it.
        return self.succeeded and len(self.seen_ids) > 0


@dataclass(frozen=True)
class PostingState:
    status: Status
    missing_since: datetime | None = None  # set only while status is MISSING


@dataclass(frozen=True)
class Event:
    kind: EventKind
    posting_id: str
    at: datetime


def apply_check(
    states: Mapping[str, PostingState], check: CheckResult
) -> tuple[dict[str, PostingState], list[Event]]:
    """Return the new state of every posting plus the events this check caused.

    Does not modify `states`. Postings are processed in sorted ID order so the
    output is deterministic (same input -> same event order), which keeps tests
    and logs predictable.
    """
    new_states = dict(states)
    if not check.is_evidence:
        return new_states, []

    events: list[Event] = []

    # 1. Postings present in this check.
    for posting_id in sorted(check.seen_ids):
        previous = states.get(posting_id)
        if previous is None:
            events.append(Event(EventKind.OPENED, posting_id, check.at))
        elif previous.status is Status.MISSING:
            events.append(Event(EventKind.REAPPEARED, posting_id, check.at))
        elif previous.status is Status.CLOSED:
            events.append(Event(EventKind.REOPENED, posting_id, check.at))
        # previous OPEN -> still open, nothing to report
        new_states[posting_id] = PostingState(Status.OPEN)

    # 2. Postings we know about that are absent from this check.
    for posting_id in sorted(states.keys() - check.seen_ids):
        previous = states[posting_id]
        if previous.status is Status.OPEN:
            new_states[posting_id] = PostingState(Status.MISSING, missing_since=check.at)
            events.append(Event(EventKind.MISSING, posting_id, check.at))
        elif previous.status is Status.MISSING:
            assert previous.missing_since is not None
            if check.at - previous.missing_since >= CLOSE_CONFIRMATION_GAP:
                new_states[posting_id] = PostingState(Status.CLOSED)
                # Date the closure when it first disappeared: the best estimate
                # of the real close time, which the Closing Clock relies on.
                events.append(Event(EventKind.CLOSED, posting_id, previous.missing_since))
        # previous CLOSED -> stays closed, nothing to report

    return new_states, events
