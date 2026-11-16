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

## Open items
- [x] Install Python 3.13 and Docker Desktop (2026-11-15). Docker Desktop must be opened once to start its engine.
- [ ] Before writing each crawler, check that ATS's job-board API terms.
- [ ] Check the licenses of the GitHub internship lists before any backfill.
- [ ] Pick an email provider after checking current free-tier limits.
- [ ] Create the Oracle Cloud account (founder; needs a card for identity verification).
- [ ] Domain decision at launch prep.
