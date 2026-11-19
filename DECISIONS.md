# Decisions

Settled choices for Fledge and why we made them. Add a dated entry when a decision changes instead of silently editing the old one.

## 2026-11-15: Project kickoff

**Product.** Fledge ("The early bird gets the offer.") watches companies' public hiring feeds. It alerts students the moment a US tech internship or new-grad role drops, estimates how long the role will stay open (the Closing Clock), helps them reach someone at the company in one click, and later shows how well they fit (the Fit Radar).

**Why this problem.** The founder is an internship seeker and feels this pain right now. We rejected these alternatives: gaming/Discord (not a pain the founder has lived, and no admin access), tutoring queues (no group at school to adopt it), money tools (saturated, and they overlap SpendSense).

| Area | Decision | Reason |
|---|---|---|
| Users | Students and early-career job seekers, 13+, US tech internships and new-grad roles | The founder's own search; a focused set of a few hundred companies |
| Budget / time | $0/month. ~10 weeks × 10 hrs for v1; Fit Radar in weeks 11–14 | Student budget; realistic scope |
| Stack | FastAPI (Python) backend, React + TypeScript (Vite) frontend, Postgres | The hard work happens in the founder's strongest language; React/TS is the most in-demand frontend skill |
| Hosting | Oracle Cloud Always Free VM running Docker Compose (API, worker, Postgres, Caddy for HTTPS); frontend on a free static host | Minute-level alerts need a process that never sleeps; free app hosts sleep |
| Repo | Public from day 1, MIT license, secrets only in env vars | A visible build history; forces good secret handling |
| Domain | None for now. **Ask again at launch prep (week 10).** | Keeps the budget at $0 until it matters |
| Data sources | Public ATS job-board feeds only (e.g. Greenhouse, Lever, Ashby). No LinkedIn or Indeed scraping. Check each feed's terms before using it | Legal and ethical; reliable structured data |
| Backfill | Use the commit history of GitHub community internship lists **only if the license permits**, with credit | Cold start for predictions, without legal risk |
| Poll rate | Every 5 min if anyone watches the company, otherwise every 30 min | Fast where it matters, light load on companies' servers |
| Closed rule | A posting is closed when it is missing from **≥2 successful checks ≥30 min apart**. Errors and empty responses count as "unknown", never as "closed" | A flaky feed must not create fake closures or fake "new" alerts |
| Closing Clock | Hidden for a company until it has ≥10 past closures, with the reason shown. Until then, fall back to a clearly labeled industry-wide estimate | Honest predictions build credibility |
| Reposts | Alert again, labeled "Repost of a role from <date>", using near-duplicate detection | A reposted role is still a real chance to apply, and the label is useful intel |
| Alerts | Email first (weeks 4–5), then browser push. The first alert sends instantly; a burst within 10 min is grouped into one message | Speed without spam; stays inside free email limits |
| Sign-in | Google sign-in + email magic link through an established library (Authlib). No passwords stored | Security handled by well-tested code |
| Age gate | Ask birth year, block under 13, store only a `13+` flag | Avoids COPPA (US law requiring parental consent under 13); minimal data |
| Deletion | Hard-delete a user's data immediately; backups age out within 7 days | A simple, honest privacy promise |
| Connect | 1-click pre-filtered LinkedIn alumni/company search links (opened in the user's own browser) + template message drafts. No contact uploads, no stored third-party data | Low friction, legal, no privacy liability |
| Fit Radar | Runs in the browser (transformers.js). The resume is never uploaded | No sensitive data on our server |
| Name | **Fledge**. No "worm" in the name (engineers read it as malware). Check name and handle availability before launch | A fledgling is a young bird leaving the nest, like a student going after a first job |

## 2026-11-16: Role scope and Greenhouse crawler

| Area | Decision | Reason |
|---|---|---|
| Role scope | Early-career roles in software, data, ML/AI, security, infra/IT, hardware. Not PM, design, sales | Clear technical roles that are classifiable accurately in v1 |
| Degree level | Include PhD/Master's-only internships, tagged with a badge users can filter on | Undergrads aren't spammed and no data is thrown away |
| Greenhouse API | Public `boards-api.greenhouse.io/v1/boards/{token}/jobs`, no auth. Docs (checked 2026-11-16) state no rate limits, caching rules or third-party terms | Documented public data |
| Polite polling | Conditional requests with `ETag` / `If-None-Match` (verified: unchanged board → `304`, no body). Poll the list *without* `content`; fetch a job's full description only when it first appears | The full list can be 1.6 MB per poll; a 304 costs almost nothing |
| Detector input | Pass **every** posting ID on the board to the detector, not just early-career ones. Filter only when alerting or displaying | Otherwise a company's last intern role closing makes the filtered list empty, which reads as "unknown" forever |
| 304 handling | A `304` is a successful check whose IDs equal the last successful check's | Time keeps passing, so a missing posting can still be confirmed closed |
| Fixtures | Tests use synthetic JSON shaped like the real API, not copies of companies' postings | No third-party content republished in a public repo |

## 2026-11-17: Persistence

| Area | Decision | Reason |
|---|---|---|
| DB access | SQLAlchemy 2.x **Core** (not the ORM) + Alembic migrations | SQL stays visible and explainable; migrations are versioned |
| Driver | psycopg 3 (`psycopg[binary]`; no compiling on Windows) | Standard driver; works sync and async; speed isn't the bottleneck |
| Dependencies | Approved: `sqlalchemy`, `alembic`, `psycopg[binary]` | Nothing installed already covers DB access |
| Detector state | Stored as columns on `postings` (`status`, `missing_since`); history in `posting_events` | Loading state is one query; events feed the Closing Clock |
| Crash safety | One transaction per check: run record, postings, events and states commit together or not at all | No half-saved state or duplicate events; the next check redoes the work |
| Downtime | After a restart, postings that appeared while the worker was down **do** alert, with honest latency | Better late than missed; the latency metric stays truthful |
| Retention | Check records (`crawl_runs`) kept 30 days; postings and events kept forever | Recent success-rate metric without ~20M rows/year; the Clock needs full history |
| Test database | Real Postgres in Docker (docker-compose locally, service container in CI). No SQLite | Tests run against the same engine as production |
| Scope of this piece | DB + one-board crawler saving/loading state, with a restart test. Multi-board worker is the next piece | One reviewable step |

## 2026-11-18: Multi-board worker

| Area | Decision | Reason |
|---|---|---|
| Company list | `companies.csv` in the repo (name, ATS, board token, priority), loaded into `boards` by a `sync` command | Reviewable in PRs; board tokens are public |
| Scheduling | `next_check_at` per board in Postgres; the loop checks boards that are due | Survives restarts, can be queried, supports tiers |
| Poll tiers (pre-users) | 30 min by default; ~25 `priority` companies every 5 min. Watches will also set 5 min later | ~17k requests/day, mostly cheap 304s; speed where it matters |
| Concurrency | One asyncio process, at most ~10 checks in flight (semaphore) | Simple, fits a free VM, polite to Greenhouse |
| Jitter | Random offset on each board's schedule | No burst of 200 requests after a restart |
| Sync DB in async worker | Saves run in a thread (`asyncio.to_thread`); `db.py` stays sync | Fetches keep going during saves; no churn in tested code |
| Dead boards | 404 for 24 h straight → `broken`, checked daily, shown in a status command; recovers by itself | No wasted requests; history is kept |
| Failures / 429 | Per-board exponential backoff (2× per consecutive failure, max 1 h), honor `Retry-After`, reset on success | Ignoring "slow down" gets crawlers blocked |
| Bugs in one board | Caught per board: logged with traceback, recorded as a failed run, backed off; other boards continue | One bad company can't stop alerts for everyone |
| Single worker | Postgres advisory lock; a second worker exits with a message | Two workers could send every alert twice |
| Scope of this piece | Worker, CSV + `sync`, scheduling, failure handling, lock, status command, **48** verified Greenhouse companies (all live boards from 50 probed on 2026-11-18; the 24 with current early-career roles are `priority`). `fledge.crawl` stays as a one-board debug tool. Growing to 200 is a separate step | One reviewable step |

## Open items
- [x] Install Python 3.13 and Docker Desktop (2026-11-15). Docker Desktop must be opened once to start its engine.
- [x] Enable the Windows "Virtual Machine Platform" feature so Docker's engine can start (done 2026-11-17).
- [ ] Before writing each crawler, check that ATS's job-board API terms.
- [ ] Check the licenses of the GitHub internship lists before any backfill.
- [ ] Pick an email provider after checking current free-tier limits.
- [ ] Create the Oracle Cloud account (founder; needs a card for identity verification).
- [ ] Domain decision at launch prep.
