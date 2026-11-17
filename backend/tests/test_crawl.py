"""Scripted end-to-end tests: a fake Greenhouse server plays a sequence of
responses, and we assert exactly what the crawler concludes after each check."""

import asyncio
import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from fledge.classify import DegreeTag
from fledge.crawler import BoardCrawler, CheckReport
from fledge.detector import EventKind, Status

T0 = datetime(2026, 8, 3, 18, 0, tzinfo=UTC)


def job(job_id: int, title: str, location: str = "New York, NY") -> dict[str, Any]:
    return {
        "id": job_id,
        "title": title,
        "location": {"name": location},
        "absolute_url": f"https://boards.greenhouse.io/acme/jobs/{job_id}",
        "first_published": (T0 - timedelta(minutes=3)).isoformat(),
        "updated_at": T0.isoformat(),
    }


def ok(*jobs: dict[str, Any], etag: str = 'W/"v"') -> httpx.Response:
    return httpx.Response(200, content=json.dumps({"jobs": list(jobs)}), headers={"ETag": etag})


Step = httpx.Response | Exception


def run_script(
    steps: list[tuple[int, Step]],
) -> tuple[BoardCrawler, list[CheckReport], list[httpx.Request]]:
    """steps: (minutes after T0, response or exception) for each check."""
    responses: Iterator[Step] = iter(step for _, step in steps)
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        step = next(responses)
        if isinstance(step, Exception):
            raise step
        return step

    crawler = BoardCrawler("acme")

    async def go() -> list[CheckReport]:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return [await crawler.check_once(client, T0 + timedelta(minutes=m)) for m, _ in steps]

    return crawler, asyncio.run(go()), requests


def kinds(report: CheckReport) -> list[tuple[EventKind, str]]:
    return [(e.kind, e.posting_id) for e in report.events]


INTERN = job(1, "Software Engineer Intern")
SALES = job(2, "Early Career, Inside Sales Representative")
SENIOR = job(3, "Senior Software Engineer")


def test_first_check_is_a_baseline_with_no_alerts() -> None:
    _, [report], _ = run_script([(0, ok(INTERN, SALES))])
    assert report.baseline
    assert len(report.events) == 2  # recorded as opened...
    assert report.drops == ()  # ...but nothing alerts on startup


def test_new_intern_role_alerts_and_others_do_not() -> None:
    new_intern = job(4, "Data Engineer Intern")
    new_sales = job(5, "Sales Development Intern")
    _, reports, _ = run_script([(0, ok(SENIOR)), (5, ok(SENIOR, new_intern, new_sales))])
    [drop] = reports[1].drops
    assert drop.posting.title == "Data Engineer Intern"
    assert drop.latency == timedelta(minutes=8)  # published 3 min before T0, seen at +5


def test_non_us_drop_is_filtered_and_unclear_location_is_kept() -> None:
    toronto = job(6, "Software Engineer Intern", "Toronto, ON")
    remote = job(7, "Backend Engineer Intern", "Remote")
    _, reports, _ = run_script([(0, ok(SENIOR)), (5, ok(SENIOR, toronto, remote))])
    [drop] = reports[1].drops
    assert drop.posting.external_id == "7"
    assert drop.us is None


def test_phd_drop_is_tagged() -> None:
    phd = job(8, "PhD Intern, Machine Learning")
    _, reports, _ = run_script([(0, ok(SENIOR)), (5, ok(SENIOR, phd))])
    assert reports[1].drops[0].degree is DegreeTag.PHD


def test_closure_is_confirmed_across_a_304() -> None:
    # The intern role vanishes at +5. The board then stays unchanged, so the
    # server answers 304. That 304 must still count as the confirming check.
    crawler, reports, requests = run_script(
        [
            (0, ok(INTERN, SENIOR, etag='W/"v1"')),
            (5, ok(SENIOR, etag='W/"v2"')),
            (35, httpx.Response(304)),
        ]
    )
    assert kinds(reports[1]) == [(EventKind.MISSING, "1")]
    assert kinds(reports[2]) == [(EventKind.CLOSED, "1")]
    assert crawler.states["1"].status is Status.CLOSED
    assert requests[2].headers["if-none-match"] == 'W/"v2"'  # newest ETag sent


def test_last_intern_closing_still_closes_when_other_roles_remain() -> None:
    # The detector sees every ID, not just intern ones, so the board isn't
    # "empty" when its only intern role goes away.
    crawler, _, _ = run_script([(0, ok(INTERN, SENIOR)), (5, ok(SENIOR)), (40, ok(SENIOR))])
    assert crawler.states["1"].status is Status.CLOSED


def test_timeouts_and_errors_change_nothing() -> None:
    crawler, reports, _ = run_script(
        [
            (0, ok(INTERN, SENIOR)),
            (5, httpx.ReadTimeout("slow")),
            (10, httpx.Response(503)),
            (15, httpx.Response(200, content=b"<html>maintenance</html>")),
            (20, ok(INTERN, SENIOR)),
        ]
    )
    for report in reports[1:]:
        assert report.events == ()
    assert crawler.states["1"].status is Status.OPEN


def test_board_404_changes_nothing() -> None:
    crawler, reports, _ = run_script([(0, ok(INTERN)), (5, httpx.Response(404))])
    assert reports[1].events == ()
    assert crawler.states["1"].status is Status.OPEN


def test_first_check_failing_then_succeeding_is_still_a_baseline() -> None:
    _, reports, _ = run_script([(0, httpx.ConnectError("down")), (5, ok(INTERN))])
    assert not reports[0].baseline
    assert reports[1].baseline
    assert reports[1].drops == ()


def test_empty_board_after_baseline_is_not_a_mass_closure() -> None:
    crawler, reports, _ = run_script([(0, ok(INTERN)), (5, ok()), (40, ok())])
    assert all(r.events == () for r in reports[1:])
    assert crawler.states["1"].status is Status.OPEN


def test_checks_for_counts_the_check_at_time_zero() -> None:
    from fledge.crawl import checks_for

    assert checks_for(900, 300) == 4  # 0, 5, 10, 15 min (the live-run bug: was 3)
    assert checks_for(899, 300) == 3
    assert checks_for(0, 300) == 1
