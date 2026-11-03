"""Extra job sources: Jobright's public GitHub lists, HN Who is hiring, SmartRecruiters, Workable, Adzuna, JSearch.

Each fetcher returns dicts shaped like sources.py: company, title, location, url, posted, text, ats (+ optional salary).
Tested on 2026-11-02: jobright_github, hn_hiring, smartrecruiters, workable.
UNTESTED (written from public docs only, never called): adzuna, jsearch.
"""
import datetime, json, os, re, subprocess, time, urllib.parse

from sources import get, clean, wanted_title, is_us, ats_key, FOREIGN, US_CODES, US_NAMES

PAUSE = 1.0  # seconds between requests to the same site
# Recruiting agencies repost other companies' jobs without naming them.
AGENCIES = {"raydar"}


def secret(service):
    """An API key from the environment or the macOS Keychain; "" when not set up."""
    env = os.environ.get(service.upper().replace("-", "_"))
    if env:
        return env
    try:
        return subprocess.run(["security", "find-generic-password", "-s", service, "-w"], capture_output=True, text=True, timeout=10).stdout.strip()
    except Exception:
        return ""


# ---------------------------------------------------------------- 1. Jobright GitHub lists
# Only github.com raw files are fetched. The apply links inside point at jobright.ai/jobs/info/<id>;
# this code never opens them (the account is restricted). Each README keeps only the last 7 days.
JOBRIGHT_REPOS = (
    "2026-Software-Engineer-New-Grad",
    "2026-Engineering-New-Grad",   # big (~2 MB, ~8k rows), mostly non-software; filters drop most of it
    "2026-Data-Analysis-New-Grad",  # on 2026-11-03 no row passed wanted_title; kept in case engineering roles appear
    "2026-Public-Sector-New-Grad",  # same; government roles often need citizenship, which the table doesn't show
)
# Different table: Company | Title | Level | Location | H1B status | Link | Date (YYYY-MM-DD).
# Stopped updating on 2026-05-06; the date cutoff drops every row until it resumes.
JOBRIGHT_H1B_REPO = "Daily-H1B-Jobs-In-Tech"
JR_RAW = "https://raw.githubusercontent.com/jobright-ai/{repo}/master/README.md"
JR_ROW = re.compile(
    r"^\|\s*(?P<company>.+?)\s*\|\s*\*\*\[(?P<title>[^\]]+)\]\((?P<url>[^)]+)\)\*\*\s*\|"
    r"\s*(?P<loc>[^|]*?)\s*\|\s*(?P<model>[^|]*?)\s*\|\s*(?P<date>[A-Z][a-z]{2} \d{1,2})\s*\|\s*$")


def _md_date(s, now=None):
    """'Nov 02' -> epoch seconds. No year in the table, so pick the year that is not in the future."""
    now = now or datetime.datetime.now()
    d = datetime.datetime.strptime(f"{s} {now.year}", "%b %d %Y")
    if d > now + datetime.timedelta(days=2):
        d = d.replace(year=now.year - 1)
    return d.timestamp()


def jobright_github(repos=JOBRIGHT_REPOS, days=21):
    cutoff, out, seen = time.time() - days * 86400, [], set()
    for i, repo in enumerate(repos):
        if i:
            time.sleep(PAUSE)
        company = ""
        for line in get(JR_RAW.format(repo=repo), timeout=60).splitlines():
            m = JR_ROW.match(line)
            if not m:
                continue
            c = m.group("company")
            if c != "↳":  # '↳' rows repeat the company above
                cm = re.match(r"\*\*\[(.+?)\]\(.*\)\*\*$|\*\*(.+?)\*\*$", c)
                company = (cm.group(1) or cm.group(2)) if cm else c
            title, loc, url = m.group("title").strip(), m.group("loc"), m.group("url")
            jid = re.search(r"/jobs/info/([0-9a-f]+)", url)
            jid = jid.group(1) if jid else url
            posted = _md_date(m.group("date"))
            if jid in seen or posted < cutoff or not wanted_title(title) or not (is_us(loc) or m.group("model") == "Remote" and "United States" in loc):
                continue
            seen.add(jid)
            out.append(dict(company=company, title=title, location=f"{loc} ({m.group('model')})"[:80],
                            url=url.split("?")[0], posted=posted, text="", ats="",
                            jobright_id=jid, jobright_link=True, repo=repo))
    time.sleep(PAUSE)
    out += _jobright_h1b(cutoff, seen)
    return out


def _jobright_h1b(cutoff, seen):
    """Jobright's H-1B list. A medal (🏅/🥈) in the status column means Jobright saw H-1B filings."""
    out, company = [], ""
    for line in get(JR_RAW.format(repo=JOBRIGHT_H1B_REPO), timeout=60).splitlines():
        m = re.match(r"^\|\s*(\*\*\[.+?\]\(.*?\)\*\*|\*\*.+?\*\*|↳)\s*\|(.*)\|\s*$", line)
        if not m:
            continue
        cells = [c.strip() for c in m.group(2).split("|")]
        if len(cells) < 6 or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", cells[-1]):
            continue
        if m.group(1) != "↳":
            company = re.match(r"\*\*\[?(.+?)\]?(?:\(.*\))?\*\*$", m.group(1)).group(1)
        title, loc, status = cells[0], " / ".join(cells[2:-3]), cells[-3]
        link = re.search(r"\((https?://[^)]+)\)", cells[-2])
        url = link.group(1).split("?")[0] if link else ""
        jid = re.search(r"/jobs/info/([0-9a-f]+)", url)
        jid = jid.group(1) if jid else url
        posted = datetime.datetime.strptime(cells[-1], "%Y-%m-%d").timestamp()
        if not url or jid in seen or posted < cutoff or not wanted_title(title) or not is_us(loc):
            continue
        seen.add(jid)
        out.append(dict(company=company, title=title, location=loc[:80], url=url, posted=posted, text="", ats="",
                        seniority=cells[1], tags=["H1B sponsor"] if status else [],
                        jobright_id=jid, jobright_link=True, repo=JOBRIGHT_H1B_REPO))
    return out


# ---------------------------------------------------------------- 2. HN Who is hiring
HN = "https://hn.algolia.com/api/v1"
NO_VISA = re.compile(r"no visa|visa:?\s*no\b|no (?:visa )?sponsorship|not (?:able to |currently )?(?:offer(?:ing)? |provide |providing )?(?:visa )?sponsor|"
                     r"(?:unable|cannot|can't|can not|won't|will not|do not|don't|does not) (?:to )?(?:offer |provide )?(?:visa )?sponsor|"
                     r"without (?:visa )?sponsorship|no h-?1b|u\.?s\.? citizens? (?:only|required)|citizenship (?:is )?required|"
                     r"must be (?:a )?u\.?s\.? citizen|security clearance|green card (?:holders? )?(?:only|required)|"
                     r"must (?:already )?(?:be )?(?:authori[sz]ed|eligible) to work in the u", re.I)
VISA = re.compile(r"\bvisa\b|sponsor|h-?1b|\bo-?1\b", re.I)
ROLE_WORDS = (r"junior|founding|new|grad|early|career|entry|level|full|stack|fullstack|back|end|backend|software|platform|infrastructure|"
              r"applied|machine|learning|ml|ai|llm|genai|product|forward|deployed|research|data|mlops|devops|cloud|site|reliability|"
              r"security|operations|agent|agentic|solutions|generalist|python|distributed|systems|search|inference|training")
ROLE = re.compile(r"(?:\b(?:" + ROLE_WORDS + r")[ /-]+){0,4}(?:engineers?|developers?|swes?|mles?)\b", re.I)
HN_FOREIGN_ONLY = re.compile(r"\bEU\b|\bCET\b|GMT\+|europe only|india only", re.I)


def _hn_title(head, body):
    """First role in the header that passes wanted_title, else first one in the body."""
    for chunk in [head, body[:3000]]:
        for seg in re.split(r"\s\|\s|,|;|\(|\)|\n| and | or |•|\*", chunk):
            for m in ROLE.finditer(seg):
                t = re.sub(r"engineers\b", "Engineer", re.sub(r"developers\b", "Developer", m.group(0).strip(" /-"), flags=re.I), flags=re.I)
                t = t[:1].upper() + t[1:]
                if wanted_title(t):
                    return t
    return ""


def hn_hiring(months=1):
    """Top-level comments of the latest `months` 'Who is hiring?' threads that look like US/remote-US ML/SWE roles."""
    hits = json.loads(get(f"{HN}/search_by_date?tags=story,author_whoishiring&hitsPerPage=10"))["hits"]
    threads = [h for h in hits if (h.get("title") or "").lower().startswith("ask hn: who is hiring")][:months]
    out = []
    for th in threads:
        time.sleep(PAUSE)
        story = json.loads(get(f"{HN}/items/{th['objectID']}", timeout=60))
        for c in story.get("children") or []:
            raw = c.get("text") or ""
            if not raw or c.get("type") != "comment":
                continue
            text = clean(raw)
            head = clean(re.split(r"<p>", raw, 1)[0])[:300]
            parts = [p.strip() for p in head.split("|")]
            # Location: any header segment that is US or remote (is_us already ignores foreign parts).
            piped = len(parts) > 1  # some posts have no 'Company | Role | Location' header at all
            us_parts = [p for p in (parts[1:] if piped else parts) if is_us(p) and not HN_FOREIGN_ONLY.search(p)]
            # A header that names a foreign city and only says "remote" loosely (e.g. "Cologne | ONSITE (part remote)") is not US.
            if FOREIGN.search(head) and not any(US_CODES.search(p) or US_NAMES.search(p) or re.search(r"remote\W{0,3}(?:\(|-)?\s*(?:us|usa|u\.s|north america|americas|worldwide|global|anywhere)", p, re.I) for p in us_parts):
                us_parts = []
            if not us_parts or NO_VISA.search(text):
                continue
            title = _hn_title(head, text)
            if not title:
                continue
            company = re.sub(r"\s*\(.*?\)\s*", " ", parts[0]).strip() if piped else re.split(r"\s[-:–—]\s|[,.(]", head)[0][:40]
            out.append(dict(company=company[:60], title=title[:100], location="; ".join(us_parts)[:80] if piped else "(see post)",
                            url=f"https://news.ycombinator.com/item?id={c['id']}", posted=c.get("created_at_i"),
                            text=text, ats=f"hn:{c['id']}", visa_ok=True if VISA.search(text) else None,
                            hn_header=head))
    return out


# ---------------------------------------------------------------- 3a. SmartRecruiters
SR = "https://api.smartrecruiters.com/v1/companies/{cid}/postings"


def smartrecruiters(company_id, details=True, max_pages=10):
    """Public postings API. List pages are 100 each (country=us); one detail call per job that survives the filters."""
    out, offset = [], 0
    for page in range(max_pages):
        if page:
            time.sleep(PAUSE)
        d = json.loads(get(SR.format(cid=company_id) + f"?limit=100&offset={offset}&country=us"))
        for p in d.get("content", []):
            loc = p.get("location") or {}
            full = loc.get("fullLocation") or ", ".join(x for x in (loc.get("city"), loc.get("region"), loc.get("country")) if x)
            if loc.get("remote"):
                full += " (Remote)"
            if not (wanted_title(p.get("name")) and (loc.get("country") == "us" or is_us(full))):
                continue
            if (p.get("typeOfEmployment") or {}).get("id") in ("intern", "temporary", "contract"):
                continue
            job = dict(company=(p.get("company") or {}).get("name") or company_id, title=p["name"].strip(), location=full[:80],
                       url=f"https://jobs.smartrecruiters.com/{company_id}/{p['id']}", posted=p.get("releasedDate"),
                       text="", ats=f"sr:{p['id']}")
            if details:
                time.sleep(PAUSE)
                det = json.loads(get(SR.format(cid=company_id) + f"/{p['id']}"))
                secs = (det.get("jobAd") or {}).get("sections") or {}
                job["text"] = "\n".join(clean(secs[k].get("text")) for k in ("jobDescription", "qualifications", "additionalInformation") if k in secs)
                job["url"] = det.get("postingUrl") or job["url"]
            out.append(job)
        offset += 100
        if offset >= d.get("totalFound", 0):
            break
    return out


# ---------------------------------------------------------------- 3b. Workable
WK_WIDGET = "https://apply.workable.com/api/v1/widget/accounts/{slug}?details=true"


def workable(slug):
    """Widget endpoint: one GET returns every published job with its HTML description. Returns 404 for unknown slugs."""
    d = json.loads(get(WK_WIDGET.format(slug=slug), timeout=60))
    out = []
    for j in d.get("jobs", []):
        locs = j.get("locations") or [{"city": j.get("city"), "region": j.get("state"), "country": j.get("country")}]
        names = [", ".join(x for x in (l.get("city"), l.get("region"), l.get("country")) if x) for l in locs]
        if str(j.get("telecommuting")).lower() == "true":
            names = [n + " (Remote)" for n in names]
        us = [n for n in names if is_us(n) or "United States" in n]
        if not (us and wanted_title(j.get("title"))) or j.get("employment_type") in ("Internship", "Contract", "Temporary"):
            continue
        out.append(dict(company=d.get("name") or slug, title=j["title"].strip(), location="; ".join(us)[:80],
                        url=j.get("url") or j.get("shortlink"), posted=j.get("published_on") or j.get("created_at"),
                        text=clean(j.get("description")), ats=f"wk:{j.get('shortcode')}"))
    return out


def discover_more(jobs, boards):
    """Like sources.discover: pick SmartRecruiters / Workable boards out of aggregator URLs (e.g. SimplifyJobs)."""
    added = 0
    for j in jobs:
        u = j.get("url") or ""
        for kind, pat in (("smartrecruiters", r"(?:jobs|careers)\.smartrecruiters\.com/([A-Za-z0-9_-]+)/"),
                          ("workable", r"apply\.workable\.com/([a-z0-9-]+)/")):
            m = re.search(pat, u)
            if m and m.group(1) not in ("j", "api") and m.group(1).lower() not in AGENCIES and m.group(1) not in boards.setdefault(kind, []):
                boards[kind].append(m.group(1))
                added += 1
    return added


# ---------------------------------------------------------------- 4a. Adzuna  (UNTESTED)
def adzuna(app_id=None, app_key=None, queries=("machine learning engineer", "software engineer"), days=21, pages=2):
    """UNTESTED. Written from developer.adzuna.com/docs/search; never called.
    GET https://api.adzuna.com/v1/api/jobs/us/search/{page}?app_id&app_key&what&max_days_old&sort_by=date&results_per_page=50
    results[]: id, title, description (snippet only), redirect_url, created, company.display_name,
    location.display_name / location.area[], salary_min, salary_max, salary_is_predicted, contract_time."""
    app_id = app_id or secret("adzuna-app-id")
    app_key = app_key or secret("adzuna-app-key")
    if not (app_id and app_key):
        return []
    out, seen = [], set()
    for q in queries:
        for page in range(1, pages + 1):
            time.sleep(PAUSE)
            qs = urllib.parse.urlencode(dict(app_id=app_id, app_key=app_key, what=q, max_days_old=days, sort_by="date",
                                             results_per_page=50, what_exclude="senior staff principal intern",
                                             **{"content-type": "application/json"}))
            res = json.loads(get(f"https://api.adzuna.com/v1/api/jobs/us/search/{page}?{qs}")).get("results", [])
            for r in res:
                loc = (r.get("location") or {}).get("display_name", "")
                if r.get("id") in seen or not wanted_title(r.get("title")) or r.get("contract_time") == "part_time":
                    continue
                seen.add(r.get("id"))
                sal = ""
                if r.get("salary_min") and not str(r.get("salary_is_predicted")) in ("1", "True", "true"):
                    sal = f"${int(r['salary_min']):,}-${int(r.get('salary_max') or r['salary_min']):,}"
                out.append(dict(company=(r.get("company") or {}).get("display_name", ""), title=clean(r["title"]), location=loc or "US",
                                url=r.get("redirect_url"), posted=r.get("created"), text=clean(r.get("description")),
                                salary=sal, ats=ats_key(r.get("redirect_url") or "") or f"az:{r.get('id')}"))
            if len(res) < 50:
                break
    return out


# ---------------------------------------------------------------- 4b. JSearch on RapidAPI  (UNTESTED)
def jsearch(api_key=None, queries=("machine learning engineer", "software engineer new grad"), date_posted="week"):
    """UNTESTED. Shapes from the RapidAPI JSearch docs (field names not re-verified; the docs page is JS-rendered); never called.
    GET https://jsearch.p.rapidapi.com/search?query&page=1&num_pages=1&country=us&date_posted=all|today|3days|week|month
        &employment_types=FULLTIME&job_requirements=under_3_years_experience,no_experience
    headers X-RapidAPI-Key, X-RapidAPI-Host: jsearch.p.rapidapi.com.  Each num_page costs one request of quota.
    data[]: job_id, employer_name, job_title, job_apply_link, job_description, job_city, job_state, job_country,
    job_is_remote, job_posted_at_datetime_utc, job_min_salary, job_max_salary, job_salary_period."""
    api_key = api_key or secret("rapidapi-key")
    if not api_key:
        return []
    hdr = {"X-RapidAPI-Key": api_key, "X-RapidAPI-Host": "jsearch.p.rapidapi.com"}
    out, seen = [], set()
    for q in queries:
        time.sleep(PAUSE)
        qs = urllib.parse.urlencode(dict(query=q + " in USA", page=1, num_pages=1, country="us", date_posted=date_posted,
                                         employment_types="FULLTIME", job_requirements="under_3_years_experience,no_experience"))
        for r in json.loads(get(f"https://jsearch.p.rapidapi.com/search?{qs}", headers=hdr)).get("data") or []:
            loc = ", ".join(x for x in (r.get("job_city"), r.get("job_state"), r.get("job_country")) if x)
            if r.get("job_is_remote"):
                loc = (loc + " (Remote)").strip()
            if r.get("job_id") in seen or not wanted_title(r.get("job_title")) or not (is_us(loc) or r.get("job_country") == "US"):
                continue
            seen.add(r.get("job_id"))
            sal = ""
            if r.get("job_min_salary"):
                sal = f"${int(r['job_min_salary']):,}-${int(r.get('job_max_salary') or r['job_min_salary']):,} {r.get('job_salary_period') or ''}".strip()
            url = r.get("job_apply_link") or ""
            out.append(dict(company=r.get("employer_name", ""), title=r["job_title"], location=loc[:80], url=url,
                            posted=r.get("job_posted_at_datetime_utc"), text=r.get("job_description") or "",
                            salary=sal, ats=ats_key(url) or f"js:{r.get('job_id')}"))
    return out


