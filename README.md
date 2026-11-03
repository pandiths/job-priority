# Job Radar

A local app that finds job openings, ranks them by how worthwhile they are to apply to, prepares a truthful tailored resume, and fills the application form. **You always press Submit yourself.** The app has no code path that submits.

## How it works

```
Scan sources -> filter and rank -> tailor resume -> truth checks + ATS check -> fill form -> you review and submit
```

| Step | Code | Notes |
|---|---|---|
| Scan | `sources.py`, `bigco.py`, `feeds.py`, `jobright_fetch.cjs` | Greenhouse, Ashby, Lever, Workday, SmartRecruiters and Workable public APIs; Amazon, Google, Apple, Meta, Oracle and JPMorgan career sites; SimplifyJobs and Jobright GitHub lists; Hacker News Who is hiring; Adzuna and JSearch when an API key is set; SLB, Docusign, Lowe's, Ford and Nokia career sites; Jobright through a logged-in browser tab (off by default) |
| Sponsors | `sponsors.json`, `tools/build_sponsors.py` | Certified H-1B filings per employer from public Labor Department data; raises the sponsorship score and found new boards to scan |
| Rank | `scoring.py` | Weighted score for fit, timing, level, sponsorship, brand and location, plus hard blocks such as clearance or no sponsorship |
| Tailor | `apply/prepare.mjs`, `apply/claude.mjs` | Keyword tailoring, then Claude rewords bullets using only the fact bank |
| Truth checks | `apply/resume.mjs` | Rejects new never-claim terms, numbers missing from the fact bank, and quotes that aren't exact, then a second Claude pass audits each edit |
| Gate | `ats_score.py` | Needs ATS 90 or higher on one page, otherwise the Fill button doesn't appear |
| Fill | `radar.py` | Runs the local fillers, leaves sponsorship questions blank, and lists what's left for you |
| Home | `web/index.html` | One screen: the next job to apply to, forms that need an answer, fresh matches, referral-note drafts (you send them) and the pipeline; flips to a board view |
| Bay Area | `bay_area.json`, `tools/bay_area.py` | Bay Area H-1B sponsors by worksite city on a map, with which ones the app reads and their open roles |

The score is a ranking heuristic, not a probability. It hasn't been calibrated against real interview outcomes.

## Run

```bash
npm install
python3 radar.py scan      # about 3 minutes
python3 radar.py serve     # http://localhost:8765
npm test
```

Python 3.9+ (standard library, plus `pypdf` for page counts), Node 24 and Google Chrome.

## Claude API key

Without a key the app still runs, using keyword tailoring only. To turn on Claude:

```bash
security add-generic-password -a "$USER" -s anthropic-api-key -w
```

## Private files (not in this repo)

Set `JOBSEARCH_HOME` if these aren't in the parent folder:

- `resume_A_v2.html`, `applications.csv`, `tracker.py`, `tailor.py`, `cover_letter.py`
- Form fillers: `gh.sh`, `ashby.sh`, `lever.sh`, their templates, and `cdp_run.js`

Two files in `data/` are gitignored, as are `output/` and `radar.db`, because they contain personal details:

- `data/fact_bank.md`: the verified facts Claude may use.
- `data/profile.json`: companies to deprioritize, terms the resume must never claim, and terms you can add. Copy `profile.example.json` to start.
