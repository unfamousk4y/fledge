"""Tests for the closed-rule state machine (see DECISIONS.md, "Closed rule").

Each test is a small story: a series of feed checks over time, and the
events we expect the detector to emit.
"""

from datetime import datetime, timedelta

from fledge.detector import (
    CheckResult,
    Event,
    EventKind,
    PostingState,
    Status,
    apply_check,
)

T0 = datetime(2026, 8, 3, 14, 0)


def minutes(n: int) -> datetime:
    return T0 + timedelta(minutes=n)


def ok(at: datetime, *ids: str) -> CheckResult:
    return CheckResult(at=at, succeeded=True, seen_ids=frozenset(ids))


def failed(at: datetime) -> CheckResult:
    return CheckResult(at=at, succeeded=False, seen_ids=frozenset())


def run(*checks: CheckResult) -> tuple[dict[str, PostingState], list[Event]]:
    """Feed checks in order; return final states and every event emitted."""
    states: dict[str, PostingState] = {}
    events: list[Event] = []
    for check in checks:
        states, new_events = apply_check(states, check)
        events.extend(new_events)
    return states, events


def kinds(events: list[Event]) -> list[tuple[EventKind, str]]:
    return [(e.kind, e.posting_id) for e in events]


# --- Happy path -------------------------------------------------------------


def test_new_posting_emits_opened() -> None:
    states, events = run(ok(minutes(0), "a"))
    assert kinds(events) == [(EventKind.OPENED, "a")]
    assert states["a"].status is Status.OPEN


def test_still_open_emits_nothing() -> None:
    _, events = run(ok(minutes(0), "a"), ok(minutes(5), "a"))
    assert kinds(events) == [(EventKind.OPENED, "a")]


def test_open_then_missing_then_closed_after_30_minutes() -> None:
    states, events = run(
        ok(minutes(0), "a", "b"),
        ok(minutes(5), "b"),  # a missing (1st successful miss)
        ok(minutes(35), "b"),  # a missing again, 30 min later -> closed
    )
    assert kinds(events) == [
        (EventKind.OPENED, "a"),
        (EventKind.OPENED, "b"),
        (EventKind.MISSING, "a"),
        (EventKind.CLOSED, "a"),
    ]
    assert states["a"].status is Status.CLOSED


def test_closed_event_is_dated_when_posting_first_went_missing() -> None:
    # Best estimate of the real close time is the first check it was absent,
    # not the later check that confirmed it. This matters for the Closing Clock.
    _, events = run(ok(minutes(0), "a", "b"), ok(minutes(5), "b"), ok(minutes(40), "b"))
    closed = [e for e in events if e.kind is EventKind.CLOSED]
    assert closed[0].at == minutes(5)


# --- Failure cases: no false closures ----------------------------------------


def test_single_blip_does_not_close() -> None:
    states, events = run(
        ok(minutes(0), "a", "b"),
        ok(minutes(5), "b"),  # a missing once
        ok(minutes(10), "a", "b"),  # a back
        ok(minutes(60), "a", "b"),
    )
    assert (EventKind.CLOSED, "a") not in kinds(events)
    assert (EventKind.REAPPEARED, "a") in kinds(events)
    assert states["a"].status is Status.OPEN


def test_two_misses_less_than_30_minutes_apart_do_not_close() -> None:
    states, events = run(
        ok(minutes(0), "a", "b"),
        ok(minutes(5), "b"),
        ok(minutes(15), "b"),  # only 10 min after first miss
    )
    assert (EventKind.CLOSED, "a") not in kinds(events)
    assert states["a"].status is Status.MISSING


def test_exactly_30_minutes_closes() -> None:
    states, _ = run(ok(minutes(0), "a", "b"), ok(minutes(5), "b"), ok(minutes(35), "b"))
    assert states["a"].status is Status.CLOSED


def test_failed_check_changes_nothing() -> None:
    states_before, _ = run(ok(minutes(0), "a"))
    states_after, events = apply_check(states_before, failed(minutes(5)))
    assert events == []
    assert states_after == states_before


def test_empty_successful_response_is_treated_as_unknown() -> None:
    # Feeds sometimes return 200 OK with zero jobs during an outage.
    states_before, _ = run(ok(minutes(0), "a"))
    states_after, events = apply_check(states_before, ok(minutes(5)))
    assert events == []
    assert states_after == states_before


def test_failed_checks_between_misses_do_not_count_as_misses() -> None:
    # One successful miss at 5, then only failures: failures are not evidence,
    # so there is never a second successful miss and it must not close.
    states, events = run(
        ok(minutes(0), "a", "b"),
        ok(minutes(5), "b"),
        failed(minutes(40)),
        failed(minutes(50)),
    )
    assert (EventKind.CLOSED, "a") not in kinds(events)
    assert states["a"].status is Status.MISSING


# --- Reopening ---------------------------------------------------------------


def test_closed_posting_that_returns_emits_reopened() -> None:
    states, events = run(
        ok(minutes(0), "a", "b"),
        ok(minutes(5), "b"),
        ok(minutes(35), "b"),  # closed
        ok(minutes(120), "a", "b"),  # back with the same ID
    )
    assert kinds(events)[-1] == (EventKind.REOPENED, "a")
    assert states["a"].status is Status.OPEN


def test_reopened_posting_can_close_again() -> None:
    states, events = run(
        ok(minutes(0), "a", "b"),
        ok(minutes(5), "b"),
        ok(minutes(35), "b"),  # closed
        ok(minutes(120), "a", "b"),  # reopened
        ok(minutes(125), "b"),  # missing
        ok(minutes(160), "b"),  # closed again
    )
    assert [k for k, pid in kinds(events) if pid == "a"].count(EventKind.CLOSED) == 2
    assert states["a"].status is Status.CLOSED


def test_closed_posting_stays_closed_silently() -> None:
    _, events = run(
        ok(minutes(0), "a", "b"),
        ok(minutes(5), "b"),
        ok(minutes(35), "b"),
        ok(minutes(65), "b"),
        ok(minutes(95), "b"),
    )
    assert [k for k, pid in kinds(events) if pid == "a"] == [
        EventKind.OPENED,
        EventKind.MISSING,
        EventKind.CLOSED,
    ]


# --- Input safety ------------------------------------------------------------


def test_apply_check_does_not_mutate_input() -> None:
    states, _ = run(ok(minutes(0), "a", "b"))
    snapshot = dict(states)
    apply_check(states, ok(minutes(5), "b"))
    assert states == snapshot
