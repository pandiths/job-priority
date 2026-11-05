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
| Live check | `radar.py` (`liveness`) | Before Prepare and Fill, one request checks the posting is still open (Workday CXS, Greenhouse boards API, Ashby board, or closure wording on other pages); closed jobs are hidden |
| Fill | `radar.py`, `apply/fill_any.js`, `apply/answer_form.mjs` | Greenhouse (including company sites with `?gh_jid=`), Ashby, Lever and Workday use local fillers; every other site uses the general filler, which reads the form, asks Claude for answers from your profile and tailored resume, and fills them. Sponsorship questions stay blank; what's left is listed for you |
| Extension | `extension/` | Chrome extension: click its icon on any application page and it fills the tab you're looking at, using the same form code and the local server |
| Outreach | `outreach.py`, `apply/outreach_draft.mjs` | In a job's panel: find recruiters with Apollo (search is free; revealing an email costs a credit), Claude drafts a short truthful note, and it is sent from your job-search Gmail only when you press Send (at most 20 a day; saved to Gmail Drafts if the network blocks sending). Keys in Keychain: `apollo-api-key`, `job-radar-gmail` |
| Follow up | `radar.py` (`followups`) | Applications with no reply after 7 days (then every 7 days, at most twice) and referral asks quiet for 5 days, with a drafted note you send yourself |
| Home | `web/index.html` | One screen: the next job to apply to, forms that need an answer, fresh matches, referral-note drafts (you send them) and the pipeline; flips to a board view |
| Companies | `us_companies.json`, `bay_area.json`, `tools/bay_area.py` | US H-1B sponsors by metro and worksite city on a map, with which ones the app reads and their open roles |

The score is a ranking heuristic, not a probability. It hasn't been calibrated against real interview outcomes.

## Run

```bash
npm install
python3 radar.py scan      # about 3 minutes
python3 radar.py serve     # http://localhost:8765
npm test                   # Node tests
python3 -m unittest discover tests
```

Python 3.9+ (standard library, plus `pypdf` for page counts), Node 24 and Google Chrome.

## Claude API key

Without a key the app still runs, using keyword tailoring only. To turn on Claude:

```bash
security add-generic-password -a "$USER" -s anthropic-api-key -w
```

## Chrome extension

1. Start the server (`python3 radar.py serve`). It writes `extension/config.js` with a random token that only the extension knows.
2. Open `chrome://extensions`, turn on **Developer mode**, click **Load unpacked** and choose the `extension` folder. Pin its icon.
3. On an application page, click the icon. A panel shows which tailored resume it matched (you can switch, or use the master resume), fills the form, uploads the resume and cover letter, and lists what's left for you.

It never presses Next or Submit. Click the icon again on each page of a multi-page form. On sign-in pages it fills the login saved in Keychain:

```bash
security add-generic-password -a YOUR_EMAIL -s jobsearch-apply -w
```

The app's own Fill button needs the job-search Chrome running with `--remote-debugging-port=9333`. The general filler works even when that window is hidden; the Greenhouse, Ashby, Lever and Workday fillers need the window on screen, and the app says so when it isn't.

## Private files (not in this repo)

Set `JOBSEARCH_HOME` if these aren't in the parent folder:

- `resume_A_v2.html`, `applications.csv`, `tracker.py`, `tailor.py`, `cover_letter.py`
- Form fillers: `gh.sh`, `ashby.sh`, `lever.sh`, their templates, `wdg/fill_all.js` (Workday) and `cdp_run.js`
- `gen/profile.md`: your contact details, education, standard answers and what to leave blank. Copy `profile.example.md` to start, or point `JR_PROFILE` at it. `JR_MASTER_PDF` and `JR_MASTER_HTML` set the untailored resume.

Two files in `data/` are gitignored, as are `output/` and `radar.db`, because they contain personal details:

- `data/fact_bank.md`: the verified facts Claude may use.
- `data/profile.json`: companies to deprioritize, terms the resume must never claim, and terms you can add. Copy `profile.example.json` to start.
