# Fledge

> The early bird gets the offer.

Fledge watches tech companies' public hiring feeds and tells you the moment an internship or new-grad role drops. It also estimates how long the role is likely to stay open and helps you reach someone at the company in one click.

**Status:** early build. The posting change detector (closed-rule state machine) is done and tested; the crawler is next. See [DECISIONS.md](DECISIONS.md).

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
