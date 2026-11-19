"""Scheduling rules: pure functions, so every case is a one-liner with fixed inputs."""

import random
from datetime import UTC, datetime, timedelta

import pytest

from fledge.schedule import (
    BoardHealth,
    first_delay,
    is_broken,
    next_delay,
    update_health,
)
from fledge.sources.greenhouse import FetchStatus

T0 = datetime(2026, 8, 3, 18, 0, tzinfo=UTC)
MIN = timedelta(minutes=1)


class NoJitter(random.Random):
    """uniform() returns the midpoint, so delays are exact."""

    def uniform(self, a: float, b: float) -> float:
        return (a + b) / 2


def delay(priority: bool, failures: int = 0, retry_after: timedelta | None = None) -> timedelta:
    return next_delay(priority, BoardHealth(failures), T0, NoJitter(), retry_after)


def test_normal_intervals() -> None:
    assert delay(priority=True) == 5 * MIN
    assert delay(priority=False) == 30 * MIN


@pytest.mark.parametrize(
    ("failures", "expected_minutes"), [(1, 10), (2, 20), (3, 40), (4, 60), (50, 60)]
)
def test_priority_backoff_doubles_and_caps_at_one_hour(
    failures: int, expected_minutes: int
) -> None:
    assert delay(priority=True, failures=failures) == expected_minutes * MIN


def test_normal_board_backoff_hits_the_cap_immediately() -> None:
    assert delay(priority=False, failures=1) == 60 * MIN


def test_retry_after_wins_when_longer_and_is_capped_at_a_day() -> None:
    assert delay(priority=True, retry_after=2 * 60 * MIN) == 120 * MIN
    assert delay(priority=True, failures=4, retry_after=MIN) == 60 * MIN  # shorter: ignored
    assert delay(priority=True, retry_after=timedelta(days=30)) == timedelta(days=1)


def test_jitter_stays_within_ten_percent() -> None:
    rng = random.Random(42)
    delays = [next_delay(False, BoardHealth(), T0, rng) for _ in range(500)]
    assert all(27 * MIN <= d <= 33 * MIN for d in delays)
    assert len(set(delays)) > 400  # and it actually varies


def test_first_delay_spreads_boards_over_one_interval() -> None:
    rng = random.Random(7)
    firsts = [first_delay(True, rng) for _ in range(500)]
    assert all(timedelta(0) <= d < 5 * MIN for d in firsts)
    assert min(firsts) < MIN and max(firsts) > 4 * MIN


# --- health -------------------------------------------------------------------


def test_success_resets_everything() -> None:
    sick = BoardHealth(5, T0)
    assert update_health(sick, FetchStatus.OK, T0) == BoardHealth()
    assert update_health(sick, FetchStatus.NOT_MODIFIED, T0) == BoardHealth()


def test_404_streak_keeps_its_start_time() -> None:
    h = update_health(BoardHealth(), FetchStatus.NOT_FOUND, T0)
    h = update_health(h, FetchStatus.NOT_FOUND, T0 + 10 * MIN)
    assert h == BoardHealth(2, T0)


def test_timeout_during_a_404_streak_neither_resets_nor_starts_it() -> None:
    h = update_health(BoardHealth(1, T0), FetchStatus.FAILED, T0 + MIN)
    assert h == BoardHealth(2, T0)
    assert update_health(BoardHealth(), FetchStatus.FAILED, T0) == BoardHealth(1, None)


def test_broken_only_after_24_hours_of_404s() -> None:
    streak = BoardHealth(40, T0)
    assert not is_broken(streak, T0 + timedelta(hours=23, minutes=59))
    assert is_broken(streak, T0 + timedelta(hours=24))
    assert not is_broken(BoardHealth(40, None), T0 + timedelta(days=9))  # failures, not 404s


def test_broken_board_is_checked_daily() -> None:
    broken = BoardHealth(100, T0 - timedelta(hours=25))
    assert next_delay(True, broken, T0, NoJitter()) == timedelta(hours=24)
