"""Greenhouse job-board source: fetch one company's public board and parse it.

API: GET https://boards-api.greenhouse.io/v1/boards/{board}/jobs (public, no auth).
See DECISIONS.md (2026-11-16) for what we verified about it.

`parse_jobs` is pure. `fetch_board` does the HTTP call but takes the client as a
parameter (dependency injection) so tests can hand it a fake one.
"""

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from enum import Enum
from typing import Any

import httpx

from fledge.models import Posting

SOURCE = "greenhouse"
BASE_URL = "https://boards-api.greenhouse.io/v1/boards"


class ParseError(Exception):
    """The response body isn't the shape we expect, so we can't trust it."""


@dataclass(frozen=True)
class ParsedBoard:
    postings: tuple[Posting, ...]
    skipped: int  # individual jobs we dropped because they were malformed


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _parse_job(job: Any, board: str) -> Posting | None:
    """Return a Posting, or None if this job lacks what we need to identify it."""
    if not isinstance(job, dict):
        return None
    job_id, title, url = job.get("id"), job.get("title"), job.get("absolute_url")
    # bool is a subclass of int in Python, so exclude it explicitly.
    if not isinstance(job_id, int) or isinstance(job_id, bool):
        return None
    if not isinstance(title, str) or not title.strip():
        return None
    if not isinstance(url, str):
        return None
    location = job.get("location")
    location_name = location.get("name") if isinstance(location, dict) else None
    return Posting(
        source=SOURCE,
        board=board,
        external_id=str(job_id),
        title=title.strip(),
        location=location_name.strip() if isinstance(location_name, str) else "",
        url=url,
        first_published=_parse_time(job.get("first_published")),  # undocumented; optional
        updated_at=_parse_time(job.get("updated_at")),
    )


def parse_jobs(body: bytes | str, board: str) -> ParsedBoard:
    """Parse a /jobs response body.

    Raises ParseError if the overall structure is wrong (the whole check is then
    untrustworthy). A single malformed job is skipped and counted instead, so one
    bad record can't freeze a company's board forever.
    """
    try:
        data = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ParseError(f"invalid JSON: {exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("jobs"), list):
        raise ParseError("expected an object with a 'jobs' list")

    postings: list[Posting] = []
    seen_ids: set[str] = set()
    skipped = 0
    for job in data["jobs"]:
        posting = _parse_job(job, board)
        if posting is None or posting.external_id in seen_ids:
            skipped += 1
            continue
        seen_ids.add(posting.external_id)
        postings.append(posting)
    return ParsedBoard(postings=tuple(postings), skipped=skipped)


class FetchStatus(Enum):
    OK = "ok"  # fresh data parsed
    NOT_MODIFIED = "not_modified"  # 304: same data as our last successful fetch
    NOT_FOUND = "not_found"  # 404: board token is wrong or the board was removed
    FAILED = "failed"  # network error, timeout, 5xx, or unparseable body


@dataclass(frozen=True)
class FetchResult:
    status: FetchStatus
    postings: tuple[Posting, ...] = ()
    etag: str | None = None
    skipped: int = 0
    error: str | None = None
    retry_after: timedelta | None = None  # the server asked us to wait (429/503)


def parse_retry_after(value: str | None, now: datetime) -> timedelta | None:
    """Read a Retry-After header: either seconds ("120") or an HTTP date.

    Returns None for a missing or unreadable value, and never a negative wait.
    """
    if not value:
        return None
    value = value.strip()
    if value.isdigit():
        return timedelta(seconds=int(value))
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:  # HTTP dates are GMT by definition
        when = when.replace(tzinfo=UTC)
    return max(when - now, timedelta(0))


async def fetch_board(
    client: httpx.AsyncClient, board: str, etag: str | None = None
) -> FetchResult:
    """Fetch one board. Never raises for network/HTTP problems; reports them instead.

    Sends `If-None-Match` when we have an ETag so an unchanged board costs a
    bodyless 304. Timeouts come from the client's configuration.
    """
    headers = {"If-None-Match": etag} if etag else {}
    try:
        response = await client.get(f"{BASE_URL}/{board}/jobs", headers=headers)
    except httpx.HTTPError as exc:  # includes timeouts and connection errors
        return FetchResult(FetchStatus.FAILED, error=f"{type(exc).__name__}: {exc}")

    if response.status_code == 304:
        return FetchResult(FetchStatus.NOT_MODIFIED, etag=etag)
    if response.status_code == 404:
        return FetchResult(FetchStatus.NOT_FOUND, error="board not found (404)")
    if response.status_code != 200:
        return FetchResult(
            FetchStatus.FAILED,
            error=f"HTTP {response.status_code}",
            retry_after=parse_retry_after(response.headers.get("retry-after"), datetime.now(UTC)),
        )

    try:
        parsed = parse_jobs(response.content, board)
    except ParseError as exc:
        return FetchResult(FetchStatus.FAILED, error=str(exc))
    return FetchResult(
        FetchStatus.OK,
        postings=parsed.postings,
        etag=response.headers.get("etag"),
        skipped=parsed.skipped,
    )
