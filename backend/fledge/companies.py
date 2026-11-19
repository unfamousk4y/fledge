"""The list of companies Fledge watches: data/companies.csv, synced into `boards`.

The CSV is the source of truth for *which* companies are watched; the database
holds everything learned about them. `sync` makes the database match the CSV:
- new rows become boards, with a first check at a random point within one
  interval (so they don't all fire at once);
- changed names/priorities are updated;
- boards missing from the CSV are deactivated, never deleted (history is kept).
"""

import csv
import io
import random
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from sqlalchemy import Connection, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from fledge import db, schedule
from fledge.sources import greenhouse

CSV_PATH = Path(__file__).resolve().parents[1] / "data" / "companies.csv"
HEADER = ["name", "ats", "token", "priority"]
SUPPORTED_ATS = {greenhouse.SOURCE}
_TOKEN = re.compile(r"[a-z0-9][a-z0-9_-]*")  # what Greenhouse board tokens look like


class CompanyListError(ValueError):
    """The CSV has problems. The message lists every one, with line numbers."""


@dataclass(frozen=True)
class Company:
    name: str
    ats: str
    token: str
    priority: bool


def parse_companies(text: str) -> list[Company]:
    """Validate the whole file and report all problems at once, not just the first."""
    reader = csv.reader(io.StringIO(text))
    rows = list(reader)
    if not rows or [h.strip() for h in rows[0]] != HEADER:
        raise CompanyListError(f"line 1: header must be exactly {','.join(HEADER)}")

    errors: list[str] = []
    companies: list[Company] = []
    seen: dict[tuple[str, str], int] = {}
    for line, row in enumerate(rows[1:], start=2):
        if not any(cell.strip() for cell in row):
            continue  # blank line
        if len(row) != len(HEADER):
            errors.append(f"line {line}: expected {len(HEADER)} columns, got {len(row)}")
            continue
        name, ats, token, priority = (cell.strip() for cell in row)
        problems = []
        if not name:
            problems.append("name is empty")
        if ats not in SUPPORTED_ATS:
            problems.append(f"unknown ats {ats!r} (supported: {', '.join(sorted(SUPPORTED_ATS))})")
        if not _TOKEN.fullmatch(token):
            problems.append(f"token {token!r} must be lowercase letters, digits, - or _")
        if priority not in ("yes", "no"):
            problems.append(f"priority must be yes or no, got {priority!r}")
        if (ats, token) in seen:
            problems.append(f"duplicate of line {seen[(ats, token)]}")
        if problems:
            errors.append(f"line {line}: " + "; ".join(problems))
            continue
        seen[(ats, token)] = line
        companies.append(Company(name, ats, token, priority == "yes"))

    if errors:
        raise CompanyListError("\n".join(errors))
    return companies


def load_companies(path: Path = CSV_PATH) -> list[Company]:
    return parse_companies(path.read_text(encoding="utf-8"))


@dataclass(frozen=True)
class SyncResult:
    added: int
    updated: int
    deactivated: int


def sync_companies(
    conn: Connection, companies: list[Company], now: datetime, rng: random.Random
) -> SyncResult:
    b = db.boards
    existing = {
        (row.source, row.token): row
        for row in conn.execute(select(b.c.source, b.c.token, b.c.name, b.c.priority, b.c.active))
    }
    added = updated = 0
    for c in companies:
        old = existing.get((c.ats, c.token))
        if old is None:
            added += 1
        elif (old.name, old.priority, old.active) != (c.name, c.priority, True):
            updated += 1
        stmt = pg_insert(b).values(
            source=c.ats,
            token=c.token,
            name=c.name,
            priority=c.priority,
            active=True,
            next_check_at=now + schedule.first_delay(c.priority, rng),
        )
        conn.execute(
            stmt.on_conflict_do_update(
                index_elements=[b.c.source, b.c.token],
                set_={
                    "name": stmt.excluded.name,
                    "priority": stmt.excluded.priority,
                    "active": True,
                    # Keep an existing schedule; only fill one in if it was never set
                    # (e.g. a board first created by the one-board debug CLI).
                    "next_check_at": func.coalesce(b.c.next_check_at, stmt.excluded.next_check_at),
                },
            )
        )

    listed = {(c.ats, c.token) for c in companies}
    gone = [key for key, row in existing.items() if row.active and key not in listed]
    for source, token in gone:
        conn.execute(update(b).where(b.c.source == source, b.c.token == token).values(active=False))
    return SyncResult(added, updated, len(gone))
