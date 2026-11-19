"""companies.csv validation (pure) and syncing it into the database (real Postgres)."""

import random
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import Engine, select, update

from fledge import db
from fledge.companies import (
    Company,
    CompanyListError,
    load_companies,
    parse_companies,
    sync_companies,
)

T0 = datetime(2026, 8, 3, 18, 0, tzinfo=UTC)
HEADER = "name,ats,token,priority\n"


def test_parses_valid_rows_and_skips_blank_lines() -> None:
    rows = parse_companies(
        HEADER + "Figma,greenhouse,figma,yes\n\nScale AI,greenhouse,scaleai,no\n"
    )
    assert rows == [
        Company("Figma", "greenhouse", "figma", True),
        Company("Scale AI", "greenhouse", "scaleai", False),
    ]


def test_reports_every_problem_with_its_line_number() -> None:
    text = HEADER + (
        "Figma,greenhouse,figma,yes\n"  # line 2: fine
        ",greenhouse,acme,yes\n"  # line 3: no name
        "Acme,workday,acme2,yes\n"  # line 4: unsupported ATS
        "Acme,greenhouse,Acme Corp,yes\n"  # line 5: bad token
        "Acme,greenhouse,acme3,maybe\n"  # line 6: bad priority
        "Figma again,greenhouse,figma,no\n"  # line 7: duplicate of line 2
        "Short,greenhouse\n"  # line 8: wrong column count
    )
    with pytest.raises(CompanyListError) as info:
        parse_companies(text)
    message = str(info.value)
    for expected in (
        "line 3: name is empty",
        "line 4: unknown ats 'workday'",
        "line 5: token 'Acme Corp'",
        "line 6: priority must be yes or no",
        "line 7: duplicate of line 2",
        "line 8: expected 4 columns",
    ):
        assert expected in message
    assert "line 2" not in message.replace("duplicate of line 2", "")


def test_wrong_header_is_rejected() -> None:
    with pytest.raises(CompanyListError, match="line 1: header"):
        parse_companies("company,token\nFigma,figma\n")


def test_the_real_company_list_is_valid() -> None:
    listed = load_companies()
    assert len(listed) >= 25
    assert any(c.priority for c in listed)


# --- sync (database) ----------------------------------------------------------


def boards(engine: Engine) -> dict[str, tuple[str | None, bool, bool, datetime | None]]:
    b = db.boards
    with engine.connect() as conn:
        rows = conn.execute(
            select(b.c.token, b.c.name, b.c.priority, b.c.active, b.c.next_check_at)
        )
        return {r.token: (r.name, r.priority, r.active, r.next_check_at) for r in rows}


def sync(engine: Engine, companies: list[Company], now: datetime = T0) -> tuple[int, int, int]:
    with engine.begin() as conn:
        r = sync_companies(conn, companies, now, random.Random(1))
    return r.added, r.updated, r.deactivated


FIGMA = Company("Figma", "greenhouse", "figma", True)
STRIPE = Company("Stripe", "greenhouse", "stripe", False)


def test_sync_adds_boards_with_a_first_check_inside_one_interval(engine: Engine) -> None:
    assert sync(engine, [FIGMA, STRIPE]) == (2, 0, 0)
    state = boards(engine)
    figma_next, stripe_next = state["figma"][3], state["stripe"][3]
    assert figma_next is not None and T0 <= figma_next < T0 + timedelta(minutes=5)
    assert stripe_next is not None and T0 <= stripe_next < T0 + timedelta(minutes=30)


def test_sync_updates_and_keeps_the_existing_schedule(engine: Engine) -> None:
    sync(engine, [FIGMA])
    before = boards(engine)["figma"][3]
    renamed = Company("Figma Inc", "greenhouse", "figma", False)
    assert sync(engine, [renamed], now=T0 + timedelta(hours=1)) == (0, 1, 0)
    name, priority, active, next_at = boards(engine)["figma"]
    assert (name, priority, active) == ("Figma Inc", False, True)
    assert next_at == before  # not rescheduled


def test_resync_with_no_changes_reports_nothing(engine: Engine) -> None:
    sync(engine, [FIGMA, STRIPE])
    assert sync(engine, [FIGMA, STRIPE]) == (0, 0, 0)


def test_removed_company_is_deactivated_not_deleted_and_can_come_back(engine: Engine) -> None:
    sync(engine, [FIGMA, STRIPE])
    assert sync(engine, [FIGMA]) == (0, 0, 1)
    assert boards(engine)["stripe"][2] is False  # row (and its history) still there
    assert sync(engine, [FIGMA, STRIPE]) == (0, 1, 0)
    assert boards(engine)["stripe"][2] is True


def test_board_from_the_debug_cli_gets_a_schedule_when_listed(engine: Engine) -> None:
    with engine.begin() as conn:
        db.ensure_board(conn, "figma")  # created by fledge.crawl: no name, no schedule
    assert sync(engine, [FIGMA]) == (0, 1, 0)
    assert boards(engine)["figma"][3] is not None
    with engine.begin() as conn:  # and a later sync doesn't touch that schedule
        conn.execute(update(db.boards).values(next_check_at=T0 + timedelta(days=1)))
    sync(engine, [FIGMA])
    assert boards(engine)["figma"][3] == T0 + timedelta(days=1)
