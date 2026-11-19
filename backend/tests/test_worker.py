"""Worker tests: real Postgres, a fake Greenhouse server routed by board token,
and a fake clock, so no test ever sleeps."""

import asyncio
import json
import random
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from sqlalchemy import Engine, func, select, update
from sqlalchemy.exc import OperationalError

from fledge import db
from fledge.companies import Company, sync_companies
from fledge.worker import Outcome, Worker, seconds_until_next_due, status_lines

T0 = datetime(2026, 8, 3, 18, 0, tzinfo=UTC)
MIN = timedelta(minutes=1)


def job(job_id: int, title: str = "Software Engineer Intern") -> dict[str, Any]:
    return {
        "id": job_id,
        "title": title,
        "location": {"name": "New York, NY"},
        "absolute_url": f"https://boards.greenhouse.io/x/jobs/{job_id}",
        "first_published": (T0 - 3 * MIN).isoformat(),
    }


def ok(*jobs: dict[str, Any]) -> httpx.Response:
    return httpx.Response(200, content=json.dumps({"jobs": list(jobs)}))


SENIOR = job(1, "Senior Software Engineer")

Reply = httpx.Response | Exception | Callable[[], httpx.Response]


class Clock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> datetime:
        return self.now


class Harness:
    """Boards synced from Company rows; `replies[token]` decides what the fake
    server answers for that board (a response, an exception, or a callable)."""

    def __init__(self, engine: Engine, companies: list[Company], max_in_flight: int = 10) -> None:
        self.engine = engine
        self.clock = Clock()
        self.replies: dict[str, Reply] = {c.token: ok(SENIOR) for c in companies}
        self.lines: list[str] = []
        self.in_flight = 0
        self.max_seen_in_flight = 0
        self.max_in_flight = max_in_flight
        with engine.begin() as conn:
            sync_companies(conn, companies, T0, random.Random(0))
        self.make_all_due()

    def make_all_due(self) -> None:
        with self.engine.begin() as conn:
            conn.execute(update(db.boards).values(next_check_at=self.clock.now))

    async def _handler(self, request: httpx.Request) -> httpx.Response:
        token = request.url.path.split("/")[-2]
        self.in_flight += 1
        self.max_seen_in_flight = max(self.max_seen_in_flight, self.in_flight)
        try:
            await asyncio.sleep(0.05)  # slow enough that all slots overlap
            reply = self.replies[token]
            if isinstance(reply, Exception):
                raise reply
            return reply() if callable(reply) else reply
        finally:
            self.in_flight -= 1

    def run(self, worker: Worker | None = None) -> list[Outcome]:
        async def go() -> list[Outcome]:
            transport = httpx.MockTransport(self._handler)
            async with httpx.AsyncClient(transport=transport) as client:
                w = worker or self.worker(client)
                w.client = client
                return await w.run_due_once()

        return asyncio.run(go())

    def worker(self, client: httpx.AsyncClient | None = None) -> Worker:
        return Worker(
            self.engine,
            client,  # type: ignore[arg-type]  # set in run()
            clock=self.clock,
            rng=random.Random(0),
            out=self.lines.append,
            max_in_flight=self.max_in_flight,
        )

    def board(self, token: str) -> Any:
        with self.engine.connect() as conn:
            return conn.execute(select(db.boards).where(db.boards.c.token == token)).one()

    def runs(self, token: str) -> list[tuple[str, str | None]]:
        b, r = db.boards, db.crawl_runs
        with self.engine.connect() as conn:
            rows = conn.execute(
                select(r.c.status, r.c.error).join(b).where(b.c.token == token).order_by(r.c.id)
            )
            return [(row.status, row.error) for row in rows]


def company(token: str, priority: bool = True) -> Company:
    return Company(token.title(), "greenhouse", token, priority)


# --- Scheduling -----------------------------------------------------------------


def test_only_due_boards_are_checked(engine: Engine) -> None:
    h = Harness(engine, [company("due"), company("later")])
    with engine.begin() as conn:
        conn.execute(
            update(db.boards).where(db.boards.c.token == "later").values(next_check_at=T0 + MIN)
        )
    outcomes = h.run()
    assert [o.board.token for o in outcomes] == ["due"]
    assert h.runs("later") == []


def test_next_check_follows_the_tier(engine: Engine) -> None:
    h = Harness(engine, [company("fast", priority=True), company("slow", priority=False)])
    h.run()
    fast, slow = h.board("fast").next_check_at - T0, h.board("slow").next_check_at - T0
    assert 4.5 * MIN <= fast <= 5.5 * MIN
    assert 27 * MIN <= slow <= 33 * MIN


def test_never_more_than_the_cap_in_flight(engine: Engine) -> None:
    h = Harness(engine, [company(f"co{i}") for i in range(25)], max_in_flight=10)
    outcomes = h.run()
    assert len(outcomes) == 25
    assert h.max_seen_in_flight == 10  # reached the cap, never exceeded it


def test_seconds_until_next_due_is_clamped(engine: Engine) -> None:
    Harness(engine, [company("a")])  # due now
    with engine.connect() as conn:
        assert seconds_until_next_due(conn, T0) == 1.0  # overdue: at least 1 s
        assert seconds_until_next_due(conn, T0 - 10 * MIN) == 30.0  # far off: at most 30 s


# --- Failures -------------------------------------------------------------------


def test_a_crashing_board_does_not_stop_the_others(engine: Engine) -> None:
    h = Harness(engine, [company("good"), company("bad")])
    h.replies["bad"] = RuntimeError("unexpected bug")  # not an httpx error: escapes fetch
    outcomes = {o.board.token: o for o in h.run()}

    assert outcomes["good"].report is not None
    assert outcomes["bad"].report is None
    assert h.runs("bad") == [("failed", "crash: RuntimeError: unexpected bug")]
    bad = h.board("bad")
    assert bad.consecutive_failures == 1
    assert 9 * MIN <= bad.next_check_at - T0 <= 11 * MIN  # backed off (5 min x 2)


def test_429_with_retry_after_pushes_the_next_check_back(engine: Engine) -> None:
    h = Harness(engine, [company("busy")])
    h.replies["busy"] = httpx.Response(429, headers={"Retry-After": "7200"})
    h.run()
    wait = h.board("busy").next_check_at - T0
    assert 108 * MIN <= wait <= 132 * MIN  # 2 h, +/-10% jitter


def test_404_for_a_day_marks_broken_then_recovers(engine: Engine) -> None:
    h = Harness(engine, [company("gone")])
    h.replies["gone"] = httpx.Response(404)
    w = h.worker()
    for _ in range(100):  # 404 every 10-60 min (backing off) until marked broken
        [outcome] = h.run(w)
        if outcome.next_check_at - h.clock.now >= timedelta(hours=21):
            break
        h.clock.now = outcome.next_check_at  # jump to the next due time
    else:
        pytest.fail("board was never marked broken")

    assert h.clock.now - T0 >= timedelta(hours=24)  # not before a full day of 404s
    assert h.board("gone").not_found_since == T0
    assert any("404 for 24 h" in line for line in h.lines)
    with engine.connect() as conn:
        assert any("BROKEN" in line for line in status_lines(conn, h.clock.now))

    h.replies["gone"] = ok(SENIOR)  # the company fixed its board
    h.clock.now = h.board("gone").next_check_at  # a day later, the daily check
    h.run(w)
    gone = h.board("gone")
    assert (gone.consecutive_failures, gone.not_found_since) == (0, None)
    assert gone.next_check_at - h.clock.now <= 5.5 * MIN


def test_failed_save_is_redone_next_time_and_alerts_once(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    h = Harness(engine, [company("acme")])
    w = h.worker()
    h.run(w)  # baseline
    h.replies["acme"] = ok(SENIOR, job(2, "Data Engineer Intern"))

    real_save = db.save_check

    def broken_save(*args: Any) -> None:
        raise OperationalError("INSERT", {}, Exception("connection lost"))

    monkeypatch.setattr(db, "save_check", broken_save)
    h.make_all_due()
    [failed] = h.run(w)
    assert failed.report is None
    assert w.crawlers == {}  # cached memory was ahead of the DB, so it was dropped

    monkeypatch.setattr(db, "save_check", real_save)
    h.make_all_due()
    [redone] = h.run(w)
    assert redone.report is not None
    assert [d.posting.external_id for d in redone.report.drops] == ["2"]
    alerts = [line for line in h.lines if "ALERT" in line]
    assert alerts == ["[Acme] ALERT   Data Engineer Intern, 3 min after publish"]


def test_alert_lines_name_the_company(engine: Engine) -> None:
    h = Harness(engine, [company("acme"), company("other")])
    w = h.worker()
    h.run(w)
    h.replies["other"] = ok(SENIOR, job(9, "Security Engineer Intern"))
    h.make_all_due()
    h.run(w)
    assert any(line.startswith("[Other] ALERT") for line in h.lines)
    assert not any(line.startswith("[Acme] ALERT") for line in h.lines)


# --- Single worker --------------------------------------------------------------


def test_second_worker_is_refused_the_lock(engine: Engine) -> None:
    with db.worker_lock(engine), pytest.raises(db.WorkerAlreadyRunning), db.worker_lock(engine):
        pass
    with db.worker_lock(engine):  # released on exit, so it can be taken again
        pass


def test_status_lists_every_company(engine: Engine) -> None:
    h = Harness(engine, [company("acme"), company("slow", priority=False)])
    h.run()
    with engine.connect() as conn:
        lines = status_lines(conn, T0)
        open_count = conn.scalar(select(func.count()).select_from(db.postings))
    assert len(lines) == 3  # header + 2 companies
    assert lines[1].startswith("Acme") and "5m" in lines[1] and "ok" in lines[1]
    assert lines[2].startswith("Slow") and "30m" in lines[2]
    assert open_count == 2
