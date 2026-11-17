# Fledge

> The early bird gets the offer.

Fledge watches tech companies' public hiring feeds and tells you the moment an internship or new-grad role drops. It also estimates how long the role is likely to stay open and helps you reach someone at the company in one click.

**Status:** early build. A Greenhouse crawler, change detector and role classifier work end to end for one company at a time, with state kept in memory. The database is next. See [DECISIONS.md](DECISIONS.md).

Try it (prints new, missing, closed and reopened postings, plus which ones would alert):

```bash
cd backend
.venv/Scripts/python -m fledge.crawl figma --interval 300
```

## Development
Requires Python 3.12+.

```bash
cd backend
python -m venv .venv
.venv/Scripts/python -m pip install -e ".[dev]"   # macOS/Linux: .venv/bin/python
.venv/Scripts/python -m pytest                     # tests
.venv/Scripts/ruff check . && .venv/Scripts/ruff format --check .   # lint + format
.venv/Scripts/mypy                                 # strict type check
```

CI runs the same checks on every push and pull request.

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
