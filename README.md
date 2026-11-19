# Fledge

> The early bird gets the offer.

Fledge watches tech companies' public hiring feeds and tells you the moment an internship or new-grad role drops. It also estimates how long the role is likely to stay open and helps you reach someone at the company in one click.

**Status:** early build. A worker watches 48 companies' Greenhouse boards (priority companies every 5 minutes, the rest every 30), detects new, missing, closed and reopened postings, and saves everything to Postgres, so a restart picks up where it left off. Email alerts are next. See [DECISIONS.md](DECISIONS.md).

## Development
Requires Python 3.12+ and Docker.

```bash
cp .env.example .env            # then replace change-me with your own password
docker compose up -d            # Postgres 18 on localhost:5432 (also creates fledge_test)

cd backend
python -m venv .venv
.venv/Scripts/python -m pip install -e ".[dev]"   # macOS/Linux: .venv/bin/python
.venv/Scripts/alembic upgrade head                 # create/update the tables
.venv/Scripts/python -m pytest                     # tests (database tests use fledge_test)
.venv/Scripts/ruff check . && .venv/Scripts/ruff format --check .   # lint + format
.venv/Scripts/mypy                                 # strict type check
```

Try it. The company list is [backend/data/companies.csv](backend/data/companies.csv) (`priority=yes` means every 5 minutes):

```bash
.venv/Scripts/python -m fledge.worker sync     # load companies.csv into the database
.venv/Scripts/python -m fledge.worker run      # check every company on its schedule (Ctrl+C to stop)
.venv/Scripts/python -m fledge.worker status   # one line per company: last check, failures, next check
```

`run` prints new, missing, closed and reopened postings, plus an `ALERT` line for each new early-career role. Stop it and start it again: it continues from the database. Only one worker can run per database. For debugging a single board: `python -m fledge.crawl figma --interval 300`.

CI runs the same checks, against a throwaway Postgres, on every push and pull request.

## Planned features
- **Drop alerts.** Email (and later browser push) within minutes of a posting appearing on a company's own hiring feed.
- **Closing Clock.** "Roles like this here usually close in ~9 days," estimated from each posting's observed history.
- **Connect.** One-click alumni/company search links and a ready-to-edit outreach message.
- **Fit Radar** (later). Ranks open roles against your resume, entirely in your browser.

## Architecture
```
ATS feeds ──> worker (crawler + change detector + alert sender) ──> Postgres
                                                                     │
React app (static host) <── FastAPI (auth, watches, postings API) <──┘
                     └── email / web push
```

## License
MIT. See [LICENSE](LICENSE).
