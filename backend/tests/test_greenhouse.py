"""Tests for the Greenhouse parser and fetcher.

Fixtures are synthetic but shaped like real API responses (checked 2026-11-16),
so we never copy a company's actual postings into the repo.
"""

import asyncio
import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import httpx
import pytest

from fledge.sources.greenhouse import (
    FetchResult,
    FetchStatus,
    ParseError,
    fetch_board,
    parse_jobs,
)


def job(
    job_id: int = 1001, title: str = "Software Engineer Intern", **extra: Any
) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": job_id,
        "internal_job_id": job_id + 500,
        "title": title,
        "updated_at": "2026-10-27T13:40:12-04:00",
        "first_published": "2026-10-01T09:00:00-04:00",
        "requisition_id": "R-1",
        "location": {"name": "San Francisco, CA"},
        "absolute_url": f"https://boards.greenhouse.io/acme/jobs/{job_id}",
        "language": "en",
        "metadata": None,
    }
    base.update(extra)
    return base


def body(*jobs: dict[str, Any]) -> bytes:
    return json.dumps({"jobs": list(jobs), "meta": {"total": len(jobs)}}).encode()


# --- parse_jobs ---------------------------------------------------------------


def test_parses_core_fields() -> None:
    [p] = parse_jobs(body(job()), "acme").postings
    assert p.source == "greenhouse"
    assert p.board == "acme"
    assert p.external_id == "1001"
    assert p.title == "Software Engineer Intern"
    assert p.location == "San Francisco, CA"
    assert p.url.endswith("/1001")
    assert p.first_published == datetime(2026, 10, 1, 9, tzinfo=timezone(timedelta(hours=-4)))
    assert p.first_published.astimezone(UTC).hour == 13


def test_missing_or_bad_optional_fields_become_empty() -> None:
    [p] = parse_jobs(
        body(job(location=None, first_published=None, updated_at="not a date")), "acme"
    ).postings
    assert p.location == ""
    assert p.first_published is None
    assert p.updated_at is None


def test_first_published_absent_entirely() -> None:
    j = job()
    del j["first_published"]
    [p] = parse_jobs(body(j), "acme").postings
    assert p.first_published is None


@pytest.mark.parametrize(
    "bad_job",
    [
        job(id=None),
        job(id="1001"),  # string ID: not the documented type
        job(id=True),  # bool sneaks past isinstance(int) checks
        job(title=""),
        job(title="   "),
        job(absolute_url=None),
        "not an object",
    ],
)
def test_malformed_job_is_skipped_and_counted(bad_job: Any) -> None:
    parsed = parse_jobs(body(job(job_id=1), bad_job), "acme")
    assert [p.external_id for p in parsed.postings] == ["1"]
    assert parsed.skipped == 1


def test_duplicate_ids_keep_first_and_count_the_rest() -> None:
    parsed = parse_jobs(body(job(job_id=7, title="A"), job(job_id=7, title="B")), "acme")
    assert [p.title for p in parsed.postings] == ["A"]
    assert parsed.skipped == 1


@pytest.mark.parametrize(
    "raw",
    [b"<html>Service Unavailable</html>", b"", b"[]", b'{"jobs": null}', b'{"status": 404}'],
)
def test_wrong_overall_shape_raises(raw: bytes) -> None:
    with pytest.raises(ParseError):
        parse_jobs(raw, "acme")


def test_empty_board_parses_to_nothing() -> None:
    # Parsing an empty list is fine; *deciding* an empty board means "unknown"
    # is the detector's job, not the parser's.
    assert parse_jobs(body(), "acme").postings == ()


# --- fetch_board --------------------------------------------------------------

Handler = Callable[[httpx.Request], httpx.Response]


def fetch(handler: Handler, etag: str | None = None) -> tuple[FetchResult, list[httpx.Request]]:
    """Run fetch_board against a fake server; return the result and requests made."""
    requests: list[httpx.Request] = []

    def recording(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return handler(request)

    async def go() -> FetchResult:
        async with httpx.AsyncClient(transport=httpx.MockTransport(recording)) as client:
            return await fetch_board(client, "acme", etag)

    return asyncio.run(go()), requests


def test_ok_returns_postings_and_etag() -> None:
    result, reqs = fetch(
        lambda r: httpx.Response(200, content=body(job()), headers={"ETag": 'W/"v1"'})
    )
    assert result.status is FetchStatus.OK
    assert len(result.postings) == 1
    assert result.etag == 'W/"v1"'
    assert str(reqs[0].url) == "https://boards-api.greenhouse.io/v1/boards/acme/jobs"
    assert "if-none-match" not in reqs[0].headers  # no ETag yet, so none sent


def test_sends_etag_and_handles_304() -> None:
    result, reqs = fetch(lambda r: httpx.Response(304), etag='W/"v1"')
    assert reqs[0].headers["if-none-match"] == 'W/"v1"'
    assert result.status is FetchStatus.NOT_MODIFIED
    assert result.etag == 'W/"v1"'  # keep using the same tag
    assert result.postings == ()


def test_404_is_not_found() -> None:
    result, _ = fetch(lambda r: httpx.Response(404, json={"status": 404, "error": "Job not found"}))
    assert result.status is FetchStatus.NOT_FOUND


@pytest.mark.parametrize("code", [429, 500, 502, 503])
def test_other_statuses_are_failures(code: int) -> None:
    result, _ = fetch(lambda r: httpx.Response(code))
    assert result.status is FetchStatus.FAILED
    assert result.error == f"HTTP {code}"


def test_garbage_200_body_is_a_failure_not_a_crash() -> None:
    result, _ = fetch(lambda r: httpx.Response(200, content=b"<html>oops</html>"))
    assert result.status is FetchStatus.FAILED
    assert result.error is not None and "invalid JSON" in result.error


@pytest.mark.parametrize(
    "exc",
    [
        httpx.ConnectTimeout("connect timed out"),
        httpx.ReadTimeout("read timed out"),
        httpx.ConnectError("connection refused"),
    ],
)
def test_network_errors_are_failures_not_crashes(exc: httpx.HTTPError) -> None:
    def boom(request: httpx.Request) -> httpx.Response:
        raise exc

    result, _ = fetch(boom)
    assert result.status is FetchStatus.FAILED
    assert result.error is not None and type(exc).__name__ in result.error
