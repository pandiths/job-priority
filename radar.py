#!/usr/bin/env python3
"""Job Radar. usage: radar.py scan [--no-notify] | radar.py serve [port] | radar.py top [n]"""
import base64, csv, datetime as dt, fcntl, functools, hashlib, hmac, itertools, json, os, re, sqlite3, subprocess, sys, threading, urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

D = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, D)
import outreach, scoring, sources  # noqa: E402

# Sources that carry the full job description; their row wins when the same job comes from several places.
FULL_TEXT_SOURCES = ("Greenhouse", "Ashby", "Lever", "Workday", "SmartRecruiters", "Workable", "Career sites")

DB = os.path.join(D, "radar.db")
BOARDS = os.path.join(D, "boards.json")
LOCK = os.path.join(D, ".scan.lock")
TRACKER = os.path.join(scoring.JOBSEARCH, "tracker.py")
RESUME = os.path.join(scoring.JOBSEARCH, "resume_A_v2.html")
OUTPUT = os.path.join(D, "output")
FILL_LOCK = os.path.join(D, ".fill.lock")
CLOSED_AFTER_DAYS = 3
PORT = 8765
MIN_HOURS_BETWEEN_SCANS = 6


NODE = os.path.expanduser("~/.local/node/bin/node")
PREPARING = {}
SUBMIT_CLICK = re.compile(r"""name:\s*/?['"]?submit[^)]*\)\s*\.click|\.submit\(\)""", re.I)


def db():
    c = sqlite3.connect(DB, timeout=30)
    c.row_factory = sqlite3.Row
    c.executescript("""
    CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY, company TEXT, title TEXT, location TEXT, url TEXT, sources TEXT,
      applicants INT, tags TEXT, text TEXT, eval TEXT, score REAL, first_seen TEXT, last_seen TEXT,
      state TEXT DEFAULT 'new', notified INT DEFAULT 0);
    CREATE TABLE IF NOT EXISTS scans(at TEXT, status TEXT, total INT, new INT);
    CREATE INDEX IF NOT EXISTS jobs_score ON jobs(score);
    CREATE TABLE IF NOT EXISTS study(id TEXT PRIMARY KEY);
    CREATE TABLE IF NOT EXISTS referrals(company TEXT, role TEXT, at TEXT, PRIMARY KEY(company, role));
    CREATE TABLE IF NOT EXISTS followups(company TEXT, role TEXT, at TEXT);
    CREATE TABLE IF NOT EXISTS outcomes(company TEXT, role TEXT, outcome TEXT, at TEXT, PRIMARY KEY(company, role));
    CREATE TABLE IF NOT EXISTS job_urls(id TEXT, url TEXT, host TEXT, first_seen TEXT, PRIMARY KEY(id, url));
    """)
    c.executescript(outreach.SCHEMA)
    outreach.migrate(c)
    if "raw" not in [r["name"] for r in c.execute("PRAGMA table_info(jobs)")]:
        c.execute("ALTER TABLE jobs ADD COLUMN raw TEXT")
    have = [r["name"] for r in c.execute("PRAGMA table_info(referrals)")]
    for col in ("person", "link", "status", "note"):
        if col not in have:
            c.execute(f"ALTER TABLE referrals ADD COLUMN {col} TEXT DEFAULT ''")
    return c


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def merge(raw):
    """Same job from several sources becomes one row: ATS text wins, Jobright adds competition signals."""
    by_ats, merged = {}, {}
    for j in sorted(raw, key=lambda x: 0 if x["source"] in FULL_TEXT_SOURCES else 1):
        key = (scoring.company_key(j["company"]), scoring.norm(re.sub(r"\(.*?\)", "", j["title"])))
        hit = by_ats.get(j.get("ats")) if j.get("ats") else None
        hit = hit or merged.get(key)
        if not hit:
            hit = dict(j, sources=[j["source"]], id="|".join(key))
            merged[key] = hit
        else:
            if j["source"] not in hit["sources"]:
                hit["sources"].append(j["source"])
            if j.get("location") and j["location"] not in (hit.get("location") or ""):
                hit["location"] = (hit.get("location", "") + "; " + j["location"]).strip("; ")[:80]
            if len(j.get("text") or "") > len(hit.get("text") or ""):
                hit["text"] = j["text"]
            for k in ("applicants", "tags", "skill", "seniority", "years", "salary", "jobright", "applied", "jobright_id", "simplify_id"):
                if j.get(k) not in (None, "", []) and hit.get(k) in (None, "", []):
                    hit[k] = j[k]
        if j.get("ats"):
            by_ats[j["ats"]] = hit
    return list(merged.values())


def notify(rows):
    if not rows:
        return
    head = f"{len(rows)} new high-chance opening{'s' if len(rows) > 1 else ''}"
    body = " · ".join(f"{r['company']}: {r['title']}" for r in rows[:3])[:220]
    script = f'display notification {json.dumps(body)} with title "Job Radar" subtitle {json.dumps(head)} sound name "Glass"'
    subprocess.run(["osascript", "-e", script], capture_output=True, timeout=20)


def hours_until_next_scan():
    row = db().execute("SELECT max(at) AS at FROM scans").fetchone()
    if not row["at"]:
        return 0
    age = (dt.datetime.now(dt.timezone.utc) - dt.datetime.fromisoformat(row["at"])).total_seconds() / 3600
    return max(0, MIN_HOURS_BETWEEN_SCANS - age)


def scan(send_notification=True):
    wait = hours_until_next_scan()
    if wait and "--force" not in sys.argv:
        print(f"last scan was recent; next one allowed in {wait:.1f} hours")
        return
    lock = open(LOCK, "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print("scan already running")
        return
    boards = json.load(open(BOARDS))
    raw, status = sources.scan_all(boards)
    json.dump(boards, open(BOARDS, "w"), indent=0)
    resume, tracker = scoring.text_of(RESUME), scoring.load_tracker()
    jobs, ts, c, new = merge(raw), now(), db(), 0
    for j in jobs:
        e = scoring.evaluate(j, resume, tracker)
        old = c.execute("SELECT state,first_seen FROM jobs WHERE id=?", (j["id"],)).fetchone()
        new += old is None
        c.execute("""INSERT INTO jobs(id,company,title,location,url,sources,applicants,tags,text,eval,score,first_seen,last_seen,raw)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET company=excluded.company,title=excluded.title,
            location=excluded.location,url=excluded.url,sources=excluded.sources,applicants=excluded.applicants,tags=excluded.tags,
            text=excluded.text,eval=excluded.eval,score=excluded.score,last_seen=excluded.last_seen,raw=excluded.raw""",
            (j["id"], j["company"], j["title"], j.get("location"), j["url"], json.dumps(j["sources"]), j.get("applicants"), json.dumps(j.get("tags") or []), j.get("text") or "", json.dumps(e), e["score"], ts, ts, json.dumps({k: v for k, v in j.items() if k != "text"})))
        c.execute("INSERT OR IGNORE INTO job_urls VALUES(?,?,?,?)", (j["id"], j["url"], urlparse(j["url"]).hostname, ts))
    # Rows missing from this scan stay visible for a few days; rescore them so aging and rule changes reach them too.
    for r in c.execute("SELECT id,text,raw FROM jobs WHERE last_seen<>? AND raw IS NOT NULL", (ts,)).fetchall():
        e = scoring.evaluate(dict(json.loads(r["raw"]), text=r["text"]), resume, tracker)
        c.execute("UPDATE jobs SET eval=?,score=? WHERE id=?", (json.dumps(e), e["score"], r["id"]))
    c.execute("INSERT INTO scans VALUES(?,?,?,?)", (ts, json.dumps(status), len(jobs), new))
    c.commit()
    first_scan = c.execute("SELECT count(*) FROM scans").fetchone()[0] == 1
    picks = [r for r in visible(c) if r["bucket"] == "now" and not r["notified"] and r["state"] == "new"]
    c.executemany("UPDATE jobs SET notified=1 WHERE id=?", [(r["id"],) for r in picks])
    c.commit()
    if send_notification and not first_scan:
        notify(picks)
    print(f"{len(jobs)} roles, {new} new, {len(picks)} top picks; sources: " + ", ".join(f"{k}={v['jobs']}" + ("" if v["ok"] else " FAILED") for k, v in status.items() if k[0] != "_"))


FILLED_STATUSES = ("FILLED - TAB OPEN", "NEEDS YOU")
APPLIED_STATUSES = ("SUBMITTED", "SUBMITTED?", "LIKELY SUBMITTED")


def visible(c):
    rows = scoring.tracker_rows()
    cutoff = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=CLOSED_AFTER_DAYS)).isoformat()
    out, reposts = [], reposted(c)
    for r in c.execute("SELECT id,company,title,location,url,sources,applicants,tags,eval,score,first_seen,last_seen,state,notified,json_extract(raw,'$.jobright_id') AS jobright_id,json_extract(raw,'$.simplify_id') AS simplify_id FROM jobs WHERE last_seen>=? AND state<>'closed' ORDER BY score DESC", (cutoff,)):
        d = dict(r)
        d["reposted"] = reposts.get(d["id"], 0)
        d.update(eval=json.loads(d["eval"]), sources=json.loads(d["sources"]), tags=json.loads(d["tags"]))
        e = d["eval"]
        row = scoring.tracker_row(d["company"], d["title"], rows)
        status = row["status"] if row else ""
        d["tracker_i"] = row["i"] if row else None
        d["prepared"] = os.path.exists(prep_paths(d)[2])
        d["fill_kind"] = (fill_target_for(d) or (None,))[0]
        d["fillable"] = bool(d["fill_kind"])
        d["bucket"] = "filled" if status in FILLED_STATUSES else "applied" if status in APPLIED_STATUSES or e["applied"] or d["state"] == "applied" else "skipped" if status == "SKIPPED" else "blocked" if e["block"] else "now" if d["score"] >= scoring.APPLY_NOW and e["parts"]["timing"] >= 45 and e["parts"]["fit"] >= 70 and e["parts"]["level"] >= 60 and not e["thin_jd"] else "strong" if d["score"] >= scoring.STRONG else "other"
        out.append(d)
    return out


REPOST_MIN_DAYS = 7


def reposted(c):
    """Times each role came back under a new URL on the same site, a week or more after an earlier listing.
    URLs first seen in one scan are parallel openings, not reposts (rule from career-ops' detect-reposts)."""
    out = {}
    rows = c.execute("SELECT id,host,first_seen FROM job_urls WHERE id IN (SELECT id FROM job_urls GROUP BY id HAVING count(*)>1) ORDER BY first_seen").fetchall()
    for (jid, host), grp in itertools.groupby(sorted(rows, key=lambda r: (r["id"], r["host"] or "", r["first_seen"])), key=lambda r: (r["id"], r["host"])):
        seen = [dt.datetime.fromisoformat(r["first_seen"]) for r in grp]
        n = sum((b - a).days >= REPOST_MIN_DAYS for a, b in zip(seen, seen[1:]))
        if n:
            out[jid] = out.get(jid, 0) + n
    return out


# Closure wording on posting pages, the English part of career-ops' liveness-core patterns.
CLOSED_TEXT = re.compile(
    r"job (is )?no longer available|job.{0,40}no longer open|this job has expired|job posting has expired"
    r"|no longer accepting applications|this (position|role|job) (is )?no longer|this (job|role|position)( listing)? is closed\b(?!-)"
    r"|job (listing )?not found|the page you are looking for doesn.t exist|applications? (have |are |is )?closed"
    r"|\b(job|position|role|posting|opening|requisition)\b.{0,60}?(?<!application )(?<!form )has been filled\b(?! out)", re.I)


def liveness(job):
    """One request to the posting before spending a resume or a form fill on it.
    Returns 'closed', 'open' or 'unknown'; a blocked or unclear answer never counts as closed."""
    url = job["url"]
    try:
        m = re.match(r"https://([\w-]+)\.(wd\d+)\.myworkdayjobs\.com/(?:[a-z]{2}-[A-Z]{2}/)?([^/]+)/job/([^?#]+)", url)
        if m:
            t, wd, site, path = m.groups()
            sources.get(f"https://{t}.{wd}.myworkdayjobs.com/wday/cxs/{t}/{site}/job/{path}")
            return "open"
        m = re.match(r"https://jobs\.ashbyhq\.com/([^/]+)/([0-9a-f-]{36})", url)
        if m:
            ids = {j["id"] for j in json.loads(sources.get(f"https://api.ashbyhq.com/posting-api/job-board/{m[1]}"))["jobs"]}
            return "open" if m[2] in ids else "closed"
        m = re.match(r"https://(?:job-)?boards(?:\.eu)?\.greenhouse\.io/(?:embed/job_app\?for=)?([\w-]+)(?:/jobs/|&token=)(\d+)", url)
        if m:
            sources.get(f"https://boards-api.greenhouse.io/v1/boards/{m[1]}/jobs/{m[2]}")
            return "open"
        final, text = sources.get(url, final_url=True)
        # A closed Greenhouse job on a company site redirects to the general careers page, dropping its gh_jid.
        jid = re.search(r"gh_jid=(\d+)", url)
        if re.search(r"[?&]error=true", final) or (jid and jid[1] not in final) or CLOSED_TEXT.search(sources.clean(text)[:20000]):
            return "closed"
        return "open"
    except urllib.error.HTTPError as e:
        return "closed" if e.code in (404, 410) else "unknown"
    except Exception:
        return "unknown"


def close_if_gone(job):
    """Marks the job closed and returns an error message when the posting is gone."""
    if liveness(job) != "closed":
        return None
    c = db()
    c.execute("UPDATE jobs SET state='closed' WHERE id=?", (job["id"],))
    c.commit()
    return "This posting has closed, so it was taken off your list. Pick another job."


FOLLOWUP_FIRST_DAYS, FOLLOWUP_EVERY_DAYS, FOLLOWUP_MAX = 7, 7, 2  # career-ops' default cadence for applied roles
REFERRAL_NUDGE_DAYS = 5


def followups():
    """Applications with no reply that are due a follow-up, and referral asks that have gone quiet."""
    c, today = db(), dt.date.today()
    done = {}
    for r in c.execute("SELECT company,role,at FROM followups ORDER BY at"):
        done.setdefault((r["company"], r["role"]), []).append(r["at"][:10])
    due = []
    for r in tracker_list():
        if r["status"] not in APPLIED_STATUSES or not re.match(r"\d{4}-\d{2}-\d{2}$", r["date"] or ""):
            continue
        past = done.get((r["company"], r["role"]), [])
        if len(past) >= FOLLOWUP_MAX:
            continue
        last = dt.date.fromisoformat(past[-1] if past else r["date"])
        wait = FOLLOWUP_EVERY_DAYS if past else FOLLOWUP_FIRST_DAYS
        if (today - last).days >= wait:
            due.append(dict(kind="application", company=r["company"], role=r["role"], applied=r["date"], days=(today - dt.date.fromisoformat(r["date"])).days, count=len(past)))
    for r in c.execute("SELECT company,role,at FROM referrals WHERE status IN ('', 'asked')"):
        if (r["company"], r["role"]) in done or not r["at"]:
            continue
        days = (today - dt.date.fromisoformat(r["at"][:10])).days
        if days >= REFERRAL_NUDGE_DAYS:
            due.append(dict(kind="referral", company=r["company"], role=r["role"], applied=r["at"][:10], days=days, count=0))
    due += [dict(f, applied=f["at"][:10], count=0) for f in outreach.due_followups(c, today)]
    return sorted(due, key=lambda x: -x["days"])


def sponsor_companies(jobs):
    """US H-1B sponsors for software/ML roles (us_companies.json, by worksite city) with the roles the
    last scans found for each, and whether the app reads the company's careers site.
    Employer variants that match the same job-board company (Amazon.com Services, Amazon Web Services) are merged."""
    path = os.path.join(D, "us_companies.json")
    if not os.path.exists(path):
        return []
    by_co = {}
    for j in jobs:
        if j["state"] != "dismissed" and j["bucket"] != "blocked":
            by_co.setdefault(scoring.norm_company(j["company"]), []).append(j)
    names = sorted((n for n in by_co if len(n) >= 4), key=len, reverse=True)
    # Boards the app scans: ATS slugs, Workday tenants, custom career sites, plus the Bay Area probe's matches.
    b = json.load(open(os.path.join(D, "boards.json")))
    slugs = {str(x).lower().split(".")[0] for v in b.values() for x in v}
    import bigco
    slugs |= {scoring.norm_company(n).replace(" ", "") for n in bigco.BIGCO}
    probed = {}
    if os.path.exists(os.path.join(D, "bay_area.json")):
        probed = {e["employer"]: e["board"] for e in json.load(open(os.path.join(D, "bay_area.json"))) if e.get("board")}
    fix = {"San J Ose": "San Jose", "San Francsico": "San Francisco", "San Franciso": "San Francisco", "South  San Francisco": "South San Francisco"}
    groups = {}
    for e in json.load(open(path)):
        k = scoring.norm_company(e["employer"])
        k = next((n for n in names if k == n or k.startswith(n + " ")), k)
        g = groups.setdefault(k, dict(name=e["employer"], filings=0, wage=e.get("median_wage"), cities={}, board=None, roles=[]))
        g["filings"] += e["count"]
        first = k.split(" ")[0]
        g["board"] = g["board"] or probed.get(e["employer"]) or (k.replace(" ", "") in slugs or (len(first) >= 4 and first in slugs)) or None
        for city, n in e["cities"].items():
            c, _, st = city.rpartition(", ")
            city = f"{fix.get(c, c)}, {st}"
            g["cities"][city] = g["cities"].get(city, 0) + n
    for k, g in groups.items():
        js = by_co.get(k, [])
        if js:
            g["name"] = js[0]["company"]
        g["open"] = len(js)
        g["roles"] = [dict(id=j["id"], title=j["title"], score=round(j["score"]), bucket=j["bucket"]) for j in sorted(js, key=lambda j: -j["score"])[:5]]
    return sorted(groups.values(), key=lambda g: -g["filings"])


OUTCOMES = ("interview", "rejected", "offer")
REFERRAL_STATUSES = ("asked", "replied", "referred", "declined")


def report(today=None):
    """Totals behind the Report page, from the tracker plus what the app records (referrals, cold emails, outcomes)."""
    c, today = db(), today or dt.date.today()
    rows = tracker_list()
    out = {(r["company"], r["role"]): r["outcome"] for r in c.execute("SELECT company,role,outcome FROM outcomes")}
    refs = {(r["company"], r["role"]): r["status"] or "asked" for r in c.execute("SELECT company,role,status FROM referrals")}
    mailed = {outreach.company_key(r["company"]) for r in c.execute("SELECT company FROM outreach")}
    dated = lambda r: re.match(r"\d{4}-\d{2}-\d{2}$", r["date"] or "")
    applied = [r for r in rows if r["status"] in APPLIED_STATUSES]
    days = [(today - dt.timedelta(days=i)).isoformat() for i in range(27, -1, -1)]
    per_day = {d: dict(day=d, submitted=0, filled=0) for d in days}
    for r in rows:
        if dated(r) and r["date"] in per_day:
            k = "submitted" if r["status"] in APPLIED_STATUSES else "filled" if r["status"] in ("FILLED - TAB OPEN", "NEEDS YOU") else None
            if k:
                per_day[r["date"]][k] += 1
    waiting = [r for r in rows if r["status"] in ("FILLED - TAB OPEN", "NEEDS YOU") and dated(r)]
    age = lambda r: (today - dt.date.fromisoformat(r["date"])).days
    ages = sorted(age(r) for r in applied if dated(r))
    apps = [dict(company=r["company"], role=r["role"], date=r["date"], days=age(r) if dated(r) else None,
                 outcome=out.get((r["company"], r["role"]), ""), referral=refs.get((r["company"], r["role"]), ""),
                 emailed=outreach.company_key(r["company"]) in mailed) for r in applied]
    sends = c.execute("SELECT via, COUNT(*) n FROM outreach GROUP BY via").fetchall()
    return dict(
        submitted=len(applied), companies=len({scoring.company_key(r["company"]) for r in applied}),
        waiting=len(waiting), stale=sum(age(r) > 7 for r in waiting), needs_you=sum(r["status"] == "NEEDS YOU" for r in rows),
        median_age=ages[len(ages) // 2] if ages else None,
        outcomes={o: sum(a["outcome"] == o for a in apps) for o in OUTCOMES},
        referrals={s: sum(v == s for v in refs.values()) for s in REFERRAL_STATUSES},
        emails={(r["via"] or "smtp"): r["n"] for r in sends},
        per_day=list(per_day.values()), applications=sorted(apps, key=lambda a: a["date"] or "", reverse=True))


def focus_tab(url, company, port=9333, opener=None):
    """Brings a job's open form to the front in the job-search Chrome (CDP on :9333).
    Matches the posting's id (Greenhouse/Workday number, Ashby uuid) in the tab URL, then the company name."""
    import urllib.request
    opener = opener or (lambda u: urllib.request.urlopen(u, timeout=5).read())
    try:
        tabs = [t for t in json.loads(opener(f"http://127.0.0.1:{port}/json")) if t.get("type") == "page"]
    except (OSError, ValueError):
        return {"error": "The job-search Chrome isn't running, so there's no open form to show."}
    ids = set(re.findall(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}|\d{5,}", url or ""))
    key = re.sub(r"[^a-z0-9]", "", (company or "").lower())
    flat = lambda t: re.sub(r"[^a-z0-9]", "", t["url"].lower())
    hits = [t for t in tabs if ids and any(i in t["url"] for i in ids)] or [t for t in tabs if len(key) >= 4 and key in flat(t)]
    if not hits:
        return {"error": f"No open tab for {company} in the job-search Chrome. The form may have been closed."}
    opener(f"http://127.0.0.1:{port}/json/activate/{hits[0]['id']}")
    return {"ok": True, "title": hits[0].get("title", ""), "matches": len(hits)}


def tracker_list():
    with open(os.path.join(scoring.JOBSEARCH, "applications.csv")) as f:
        return [dict(r, i=i) for i, r in enumerate(csv.DictReader(f))]


def pipeline():
    rows = list(csv.DictReader(open(os.path.join(scoring.JOBSEARCH, "applications.csv"))))
    count = lambda *s: sum(r["status"] in s for r in rows)
    return dict(applied=count("SUBMITTED", "SUBMITTED?", "LIKELY SUBMITTED"), waiting=count("FILLED - TAB OPEN"), needs_you=count("NEEDS YOU"))


def slug(company, title):
    return re.sub(r"[^A-Za-z0-9]", "", company)[:14] + re.sub(r"[^A-Za-z0-9]", "", title)[:22]


def job_row(job_id):
    r = db().execute("SELECT id,company,title,url,text,raw FROM jobs WHERE id=?", (job_id,)).fetchone()
    return dict(r) if r else None


def prep_paths(job):
    name = slug(job["company"], job["title"])
    out = os.path.join(OUTPUT, name)
    # Long titles can share a slug; a folder prepared for another role gets a suffix from the job id.
    try:
        with open(os.path.join(out, name + ".report.json")) as f:
            taken = json.load(f).get("role") != job["title"]
    except (OSError, ValueError):
        taken = False
    if taken:
        name += hashlib.sha1(job["id"].encode()).hexdigest()[:6]
        out = os.path.join(OUTPUT, name)
    return name, out, os.path.join(out, name + ".report.json")


def prep_files(name, out):
    """Tailored resume HTML, resume PDF and cover letter PDF that apply/prepare.mjs writes for a job."""
    return os.path.join(out, name + ".html"), os.path.join(out, f"Skanda_Pandith_Resume_{name}.pdf"), os.path.join(out, f"Skanda_Pandith_CoverLetter_{name}.pdf")


def prep_status(job):
    name, out, report = prep_paths(job)
    if job["id"] in PREPARING:
        if PREPARING[job["id"]].poll() is None:
            return dict(state="running")
        code = PREPARING.pop(job["id"]).returncode
        if code:
            return dict(state="failed", error=f"Resume step failed (exit {code}). See {out}/prepare.log")
    if os.path.exists(report):
        return dict(json.load(open(report)), state="ready")
    return dict(state="none")


def start_prepare(job):
    name, out, _ = prep_paths(job)
    if len(job["text"] or "") < 150:
        return dict(state="failed", error="This source has no full job description. Open the posting instead.")
    gone = close_if_gone(job)
    if gone:
        return dict(state="failed", error=gone)
    os.makedirs(out, exist_ok=True)
    jd = os.path.join(out, name + ".jd.txt")
    with open(jd, "w") as f:
        f.write(job["text"])
    log = open(os.path.join(out, "prepare.log"), "w")
    PREPARING[job["id"]] = subprocess.Popen([NODE, os.path.join(D, "apply", "prepare.mjs"), name, job["company"], job["title"], jd, out], stdout=log, stderr=log, cwd=D)
    return dict(state="running")


def fill_target(url):
    """Which filler handles this posting, and the URL it expects. None means fill by hand."""
    if re.search(r"[?&]for=[^&]+&token=\d+|greenhouse\.io/[^/]+/jobs/\d+", url):
        return "greenhouse", url.split("?")[0] if "/jobs/" in url else url
    m = re.match(r"(https://jobs\.ashbyhq\.com/[^/]+/[0-9a-f-]{36})", url)
    if m:
        return "ashby", m.group(1) + "/application"
    m = re.match(r"(https://jobs\.lever\.co/[^/]+/[0-9a-f-]{36})", url)
    if m:
        return "lever", m.group(1) + "/apply"
    m = re.match(r"(https://[\w-]+\.wd\d+\.myworkdayjobs\.com/.+/job/[^?#]+)", url)
    if m:
        return "workday", m.group(1)
    # Everything else goes to the general filler, except links that only lead back to an aggregator.
    if not re.search(r"//([\w-]+\.)*(jobright\.ai|ycombinator\.com|linkedin\.com)/", url):
        return "generic", url
    return None


@functools.lru_cache(maxsize=1)
def greenhouse_boards():
    with open(os.path.join(D, "boards.json")) as f:
        return frozenset(json.load(f).get("greenhouse", []))


def fill_target_for(job):
    """fill_target, plus Greenhouse forms embedded on company sites (?gh_jid=), which go to the Greenhouse filler."""
    target = fill_target(job["url"])
    jid = re.search(r"[?&]gh_jid=(\d+)", job["url"])
    if target and target[0] == "generic" and jid:
        try:
            board = json.loads(job.get("raw") or "{}").get("board")
        except ValueError:
            board = None
        board = board or next((b for b in greenhouse_boards() if b == re.sub(r"[^a-z0-9]", "", job["company"].lower())), None)
        if board:
            return "greenhouse", f"https://job-boards.greenhouse.io/embed/job_app?for={board}&token={jid.group(1)}"
    return target


def fill(job):
    """Fills the form in the job-search Chrome and leaves the tab open. Nothing in this path can submit."""
    status = prep_status(job)
    if status.get("state") != "ready" or not status.get("passed"):
        return dict(ok=False, error="Prepare a resume that passes the ATS check first.")
    target = fill_target_for(job)
    if not target:
        return dict(ok=False, error="This site has no automatic filler yet. Use Open posting or an Apply through button.")
    kind, url = target
    gone = close_if_gone(job)
    if gone:
        return dict(ok=False, error=gone)
    name, out, _ = prep_paths(job)
    html, pdf, cover = prep_files(name, out)
    lock = open(FILL_LOCK, "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return dict(ok=False, error="Another form is being filled. Try again in a minute.")
    # Sponsorship questions stay blank on purpose: the applicant answers those personally.
    env = dict(os.environ, BLANKSP="1")
    if kind == "workday":
        # One fixed script walks the Workday steps; pressing Fill again resumes from the step the tab is on.
        gen_path = os.path.join(scoring.JOBSEARCH, "wdg", "fill_all.js")
        env.update(WD_URL=url, WD_RES=pdf, WD_HTML=html)
        timeout = 900
    elif kind == "generic":
        # Reads each page's fields, asks Claude for answers, fills them; pressing Fill again continues from the tab.
        gen_path = os.path.join(D, "apply", "fill_any.js")
        env.update(GF_URL=url, GF_RES=pdf, GF_COVER=cover, GF_HTML=html, GF_COMPANY=job["company"], GF_TITLE=job["title"], GF_PROFILE=PROFILE_MD, GF_RADAR=D)
        timeout = 900
    else:
        script, gen = {"greenhouse": ("gh.sh", "gh_run.js"), "ashby": ("ashby.sh", "ashby_gen.js"), "lever": ("lever.sh", "lever_gen.js")}[kind]
        subprocess.run(["bash", os.path.join(scoring.JOBSEARCH, script), out, f"{name}={url}"], cwd=scoring.JOBSEARCH, env=env, capture_output=True, timeout=30, check=True)
        gen_path = os.path.join(scoring.JOBSEARCH, gen)
        timeout = 300
    if SUBMIT_CLICK.search(open(gen_path).read()):
        return dict(ok=False, error="Stopped: the generated filler contains a Submit click.")
    # The dedicated fillers click like a person, which stalls while Chrome isn't drawing the window.
    # The general filler uses DOM events and doesn't need this.
    seen = subprocess.run([NODE, os.path.join(scoring.JOBSEARCH, "cdp_run.js"), os.path.join(D, "apply", "visible_check.js")], cwd=scoring.JOBSEARCH, capture_output=True, text=True, timeout=60) if kind != "generic" else None
    if seen and "VISIBLE" not in seen.stdout:
        return dict(ok=False, error="The job-search Chrome window is hidden (on another desktop, behind a full-screen window, or minimized), so it can't be clicked. Put it on screen, for example side by side with this page, and press Fill again.")
    p = subprocess.run([NODE, os.path.join(scoring.JOBSEARCH, "cdp_run.js"), gen_path], cwd=scoring.JOBSEARCH, env=env, capture_output=True, text=True, timeout=timeout)
    if p.returncode or "LEFT OPEN FOR REVIEW" not in p.stdout:
        why = next((l[2:].strip() for l in p.stdout.splitlines() if l.startswith("!!")), "The filler didn't finish. Is the job-search Chrome open?")
        return dict(ok=False, error=why, log=p.stdout[-1500:])
    # A Workday form can take several Fill presses; record it in the tracker once.
    rows = csv.DictReader(open(os.path.join(scoring.JOBSEARCH, "applications.csv")))
    if not any(r["company"] == job["company"] and r["role"] == job["title"] for r in rows):
        subprocess.run([sys.executable, TRACKER, "add", dt.date.today().isoformat(), job["company"], job["title"], "FILLED - TAB OPEN", out, f"ATS {status['ats_after']}; filled by Job Radar"], capture_output=True, timeout=20)
    todo = [l.strip() for l in p.stdout.splitlines() if l.startswith("??") or l.startswith("!!")]
    return dict(ok=True, needs_you=todo)


# ---------- browser extension (extension/): the server answers forms; the extension fills the tab you're on ----------
EXT_TOKEN_FILE = os.path.join(D, "data", "ext_token")
# Personal files live outside the repo; see profile.example.md.
PROFILE_MD = os.environ.get("JR_PROFILE", os.path.join(scoring.JOBSEARCH, "gen", "profile.md"))
MASTER_PDF = os.path.expanduser(os.environ.get("JR_MASTER_PDF", "~/Downloads/Skanda_Pandith_Resume_A_v2.pdf"))
MASTER_HTML = os.environ.get("JR_MASTER_HTML", os.path.join(scoring.JOBSEARCH, "resume_A_v2.html"))
LOGIN_SERVICE = "jobsearch-apply"


def ext_token():
    """A random secret shared only with the unpacked extension (extension/config.js, git-ignored)."""
    if not os.path.exists(EXT_TOKEN_FILE):
        os.makedirs(os.path.dirname(EXT_TOKEN_FILE), exist_ok=True)
        with open(EXT_TOKEN_FILE, "w") as f:
            f.write(os.urandom(24).hex())
    with open(EXT_TOKEN_FILE) as f:
        token = f.read().strip()
    cfg = os.path.join(D, "extension", "config.js")
    line = f'export const SERVER = "http://localhost:{PORT}";\nexport const TOKEN = "{token}";\n'
    with open(cfg, "a+") as f:
        f.seek(0)
        stale = f.read() != line
    if stale:
        with open(cfg, "w") as f:
            f.write(line)
    return token


def ext_jobs(page_url, page_title):
    """Prepared resumes, best match for the page first, then the master resume.
    The exact posting scores highest. On shared applicant-tracking hosts (jobs.ashbyhq.com, ...) the host
    alone says nothing, so the company's board in the path (/plaid/...) is compared instead."""
    base = lambda u: u.split("#")[0].split("?")[0].rstrip("/").lower()
    page, host = base(page_url), urlparse(page_url).netloc.lower()
    board = lambda u: (urlparse(u).path.strip("/").split("/") or [""])[0].lower()
    hay = re.sub(r"[^a-z0-9]", "", (page_url + " " + page_title).lower())
    words = set(re.findall(r"[a-z]{3,}", page_title.lower()))
    out = []
    for d in visible(db()):
        if not d["prepared"]:
            continue
        s = prep_status(d)
        if s.get("state") != "ready":
            continue
        ju, jh = base(d["url"]), urlparse(d["url"]).netloc.lower()
        exact = bool(ju) and (page.startswith(ju) or (urlparse(ju).path.strip("/") and ju.startswith(page)))
        same_site = jh == host and (not outreach.ATS_HOSTS.search(host) or board(d["url"]) == board(page_url))
        co = re.sub(r"[^a-z0-9]", "", d["company"].lower())
        score = 10 * exact + 3 * same_site + 2 * (bool(co) and co in hay) + len(words & set(re.findall(r"[a-z]{3,}", d["title"].lower()))) / 4
        name, out_dir, _ = prep_paths(d)
        _, pdf, cover = prep_files(name, out_dir)
        ats = "" if s.get("passed") else f" (ATS {s.get('ats_after', 0):.0f})"
        out.append(dict(id=d["id"], label=f"{d['company']} · {d['title']}{ats}", match=round(score, 2),
                        files=dict(resume=os.path.basename(pdf), cover=os.path.basename(cover))))
    out.sort(key=lambda x: -x["match"])
    return out + [dict(id="", label="Master resume (not tailored)", match=0, files=dict(resume=os.path.basename(MASTER_PDF), cover=""))]


def ext_paths(job_id):
    """Resume HTML, resume PDF and cover letter PDF for a job id, or the master resume for ''."""
    job = job_row(job_id) if job_id else None
    if not job:
        return MASTER_HTML, MASTER_PDF, None, None
    name, out, _ = prep_paths(job)
    return (*prep_files(name, out), job)


def html_text(path):
    with open(path) as f:
        t = re.sub(r"<style[\s\S]*?</style>", "", f.read(), flags=re.I)
    t = re.sub(r"</(p|li|h\d|div)>", "\n", t, flags=re.I)
    t = re.sub(r"<[^>]+>", " ", t).replace("&amp;", "&")
    return re.sub(r"\n\s*\n+", "\n", re.sub(r"[ \t]+", " ", t)).strip()


def resume_entries(path):
    """How many jobs and degrees the resume lists, so the extension can press Add that many times."""
    with open(path) as f:
        h = f.read()
    def count(heading):
        part = (re.split(rf"<h2>\s*{heading}\s*</h2>", h, flags=re.I)[1:] or [""])[0].split("<h2>")[0]
        return len(re.findall(r'<span class="l">', part))
    return dict(work=count("Work Experience"), education=count("Education"))


def ext_answer(b):
    html, _, _, job = ext_paths(b.get("job_id", ""))
    with open(PROFILE_MD) as f:
        profile = f.read()
    payload = dict(fields=b.get("fields", []), profile=profile, resume=html_text(html),
                   company=job["company"] if job else b.get("company", ""), title=job["title"] if job else b.get("title", ""))
    p = subprocess.run([NODE, os.path.join(D, "apply", "answer_form.mjs")], input=json.dumps(payload), capture_output=True, text=True, timeout=300, cwd=D)
    try:
        return json.loads(p.stdout)
    except ValueError:
        return dict(error=(p.stderr or "no answer")[-300:])


def ext_file(job_id, kind):
    _, pdf, cover, _ = ext_paths(job_id)
    path = cover if kind == "cover" else pdf
    if not path or not os.path.exists(path):
        return None
    with open(path, "rb") as f:
        return dict(name=os.path.basename(path), b64=base64.b64encode(f.read()).decode())


def ext_login():
    """The job-site login saved in macOS Keychain (service jobsearch-apply). Sent only to the extension."""
    attrs = subprocess.run(["security", "find-generic-password", "-s", LOGIN_SERVICE], capture_output=True, text=True).stdout
    email = re.search(r'"acct"<blob>="([^"]+)"', attrs)
    pw = subprocess.run(["security", "find-generic-password", "-s", LOGIN_SERVICE, "-w"], capture_output=True, text=True).stdout.rstrip("\n")
    if not (email and pw):
        return dict(error=f"No login saved in Keychain. Add one with: security add-generic-password -a YOUR_EMAIL -s {LOGIN_SERVICE} -w")
    return dict(email=email.group(1), password=pw)


def ext_filled(job_id):
    job = job_row(job_id) if job_id else None
    if not job:
        return dict(ok=True)
    status = prep_status(job)
    rows = csv.DictReader(open(os.path.join(scoring.JOBSEARCH, "applications.csv")))
    if not any(r["company"] == job["company"] and r["role"] == job["title"] for r in rows):
        subprocess.run([sys.executable, TRACKER, "add", dt.date.today().isoformat(), job["company"], job["title"], "FILLED - TAB OPEN", prep_paths(job)[1], f"ATS {status.get('ats_after')}; filled by the Job Radar extension"], capture_output=True, timeout=20)
    return dict(ok=True)


# ---------- recruiter outreach (outreach.py): Apollo lookups, Claude drafts, sending only on the user's click ----------
def sender_name():
    with open(PROFILE_MD) as f:
        m = re.search(r"^- First name:\s*(.+)$", f.read(), re.M)
    return os.environ.get("JR_SENDER_NAME") or (m.group(1).strip() if m else "")


def outreach_view(job):
    c = db()
    return dict(domain=outreach.guess_domain(job["company"], job["url"]), contacts=outreach.contacts(c, job),
                sent=[h for h in outreach.history(c) if h["job_id"] == job["id"]], max_per_day=outreach.MAX_SENDS_PER_DAY)


def outreach_draft(job, person_id, attach):
    c = db()
    contact = c.execute("SELECT * FROM contacts WHERE person_id=?", (person_id,)).fetchone()
    if not contact or not contact["email"]:
        return dict(error="Reveal the recruiter's email first.")
    html, pdf, _, _ = ext_paths(job["id"])
    if not os.path.exists(html):
        html, pdf = MASTER_HTML, MASTER_PDF
    with open(os.path.join(D, "data", "fact_bank.md")) as f:
        facts = f.read()
    with open(os.path.join(D, "data", "profile.json")) as f:
        never = json.load(f).get("never_claim", [])
    with open(PROFILE_MD) as f:
        profile = f.read()
    payload = dict(factBank=facts, neverClaim=never, profile=profile, resume=html_text(html), jd=job["text"] or "", company=job["company"], title=job["title"],
                   url=job["url"], recruiter=dict(name=contact["name"] or contact["first_name"], title=contact["title"]), attach=bool(attach and os.path.exists(pdf)))
    p = subprocess.run([NODE, os.path.join(D, "apply", "outreach_draft.mjs")], input=json.dumps(payload), capture_output=True, text=True, timeout=180, cwd=D)
    try:
        return json.loads(p.stdout)
    except ValueError:
        return dict(error=(p.stderr or "no draft")[-300:])


def outreach_send(job, b):
    c = db()
    contact = c.execute("SELECT * FROM contacts WHERE person_id=?", (b.get("person_id"),)).fetchone()
    _, pdf, _, _ = ext_paths(job["id"])
    if not os.path.exists(pdf):
        pdf = MASTER_PDF
    try:
        return outreach.send(c, job, dict(contact) if contact else None, str(b.get("subject", "")), str(b.get("body", "")),
                             attachment=pdf if b.get("attach") and os.path.exists(pdf) else None, sender_name=sender_name(), allow_unverified=bool(b.get("allow_unverified")))
    except (outreach.smtplib.SMTPAuthenticationError, outreach.imaplib.IMAP4.error) as e:
        return dict(ok=False, error=f"Gmail rejected the app password saved as job-radar-gmail ({e}). Make a new one at myaccount.google.com/apppasswords and save it again.")
    except Exception as e:  # other SMTP and IMAP errors go back to the page as text
        return dict(ok=False, error=f"Gmail refused: {e}")


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def reply(self, body, ctype="application/json", code=200):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        u = urlparse(self.path)
        if u.path == "/":
            return self.reply(open(os.path.join(D, "web", "index.html"), "rb").read(), "text/html; charset=utf-8")
        c = db()
        if u.path == "/api/jobs":
            return self.reply(visible(c))
        if u.path == "/api/study":
            return self.reply([r["id"] for r in c.execute("SELECT id FROM study")])
        if u.path == "/api/tracker":
            return self.reply(tracker_list())
        if u.path == "/api/status":
            s = c.execute("SELECT at,status,total,new FROM scans ORDER BY at DESC LIMIT 1").fetchone()
            return self.reply(dict(scan=dict(s, status=json.loads(s["status"])) if s else None, pipeline=pipeline(), scanning=scanning(), weights=scoring.WEIGHTS, resting=sources.cooldowns()))
        if u.path == "/api/outreach":
            q = parse_qs(u.query)
            job = job_row(q.get("id", [""])[0]) or (outreach.company_job(q["company"][0]) if q.get("company", [""])[0].strip() else None)
            return self.reply(outreach_view(job) if job else {"error": "unknown job"})
        if u.path == "/api/outreach/lists":
            try:
                return self.reply(dict(lists=outreach.lists(), companies=outreach.saved_companies(db())))
            except RuntimeError as e:
                return self.reply({"error": str(e), "companies": outreach.saved_companies(db())})
        if u.path == "/api/outreach/history":
            return self.reply(outreach.history(db()))
        if u.path == "/api/followups":
            return self.reply(followups())
        if u.path == "/api/referrals":
            return self.reply([dict(r) for r in c.execute("SELECT company,role,at,person,link,status,note FROM referrals ORDER BY at DESC")])
        if u.path == "/api/report":
            return self.reply(report())
        if u.path == "/api/companies":
            return self.reply(sponsor_companies(visible(c)))
        if u.path == "/api/prep":
            job = job_row(parse_qs(u.query).get("id", [""])[0])
            return self.reply(prep_status(job) if job else {"state": "none"})
        if u.path == "/api/pdf":
            job = job_row(parse_qs(u.query).get("id", [""])[0])
            status = prep_status(job) if job else {}
            if status.get("state") == "ready" and os.path.exists(status["pdf"]):
                return self.reply(open(status["pdf"], "rb").read(), "application/pdf")
        self.reply({"error": "not found"}, code=404)

    def do_POST(self):
        origin = self.headers.get("Origin", "")
        if self.path.startswith("/api/ext/"):
            # Only the unpacked extension knows the token; pages on other sites can't call these.
            if not origin.startswith("chrome-extension://") or not hmac.compare_digest(self.headers.get("X-JR-Token", ""), ext_token()):
                return self.reply({"error": "forbidden"}, code=403)
            b = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
            route = self.path[len("/api/ext/"):]
            if route == "jobs":
                return self.reply(ext_jobs(b.get("url", ""), b.get("title", "")))
            if route == "answer":
                return self.reply(ext_answer(b))
            if route == "file":
                return self.reply(ext_file(b.get("job_id", ""), b.get("kind", "resume")) or {"error": "no file"})
            if route == "entries":
                return self.reply(resume_entries(ext_paths(b.get("job_id", ""))[0]))
            if route == "login":
                return self.reply(ext_login())
            if route == "filled":
                return self.reply(ext_filled(b.get("job_id", "")))
            return self.reply({"error": "not found"}, code=404)
        if origin and not re.fullmatch(r"http://(localhost|127\.0\.0\.1):\d+", origin):
            return self.reply({"error": "forbidden"}, code=403)
        if self.path == "/api/scan":
            wait = hours_until_next_scan()
            if wait:
                return self.reply({"started": False, "error": f"Scans run at most every {MIN_HOURS_BETWEEN_SCANS} hours, to stay polite to job sites. Next one in {wait:.1f} hours."})
            subprocess.Popen([sys.executable, os.path.abspath(__file__), "scan", "--no-notify"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return self.reply({"started": True})
        if self.path == "/api/state":
            b = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
            if b.get("state") not in ("new", "saved", "dismissed", "applied"):
                return self.reply({"error": "bad state"}, code=400)
            c = db()
            r = c.execute("SELECT company,title,state FROM jobs WHERE id=?", (b.get("id"),)).fetchone()
            if not r:
                return self.reply({"error": "unknown job"}, code=404)
            if b["state"] == "applied" and r["state"] != "applied":
                row = scoring.tracker_row(r["company"], r["title"], scoring.tracker_rows())
                args = ["set", row["company"], row["role"], "SUBMITTED"] if row else ["add", dt.date.today().isoformat(), r["company"], r["title"], "SUBMITTED", "", "marked in Job Radar"]
                subprocess.run([sys.executable, TRACKER] + args, capture_output=True, timeout=20)
            c.execute("UPDATE jobs SET state=? WHERE id=?", (b["state"], b["id"]))
            c.commit()
            return self.reply({"ok": True})
        if self.path == "/api/study":
            b = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
            c = db()
            c.execute("INSERT OR IGNORE INTO study VALUES(?)" if b.get("done") else "DELETE FROM study WHERE id=?", (str(b.get("id"))[:80],))
            c.commit()
            return self.reply({"ok": True})
        if self.path == "/api/referral":
            # Records a referral ask the user sent themselves. The app never sends messages.
            b = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
            c = db()
            if b.get("sent"):
                key = (str(b.get("company"))[:120], str(b.get("role"))[:200])
                if not c.execute("SELECT 1 FROM referrals WHERE company=? AND role=?", key).fetchone():
                    c.execute("INSERT INTO referrals(company,role,at,person,link,status,note) VALUES(?,?,?,'','','asked','')", (*key, now()))
                if b.get("status") not in (None, *REFERRAL_STATUSES):
                    return self.reply({"error": "unknown status"}, code=400)
                for col, n in (("person", 200), ("link", 300), ("status", 20), ("note", 1000)):
                    if b.get(col) is not None:
                        c.execute(f"UPDATE referrals SET {col}=? WHERE company=? AND role=?", (str(b[col])[:n], *key))
            else:
                c.execute("DELETE FROM referrals WHERE company=? AND role=?", (b.get("company"), b.get("role")))
            c.commit()
            return self.reply({"ok": True})
        if self.path == "/api/focus":
            b = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
            return self.reply(focus_tab(str(b.get("url") or ""), str(b.get("company") or "")))
        if self.path == "/api/tracker_status":
            # Submit sweep: a filled form the user sent (SUBMITTED) or gave up on (SKIPPED).
            b = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
            co, role = str(b.get("company") or "").strip(), str(b.get("role") or "").strip()
            if b.get("status") not in ("SUBMITTED", "SKIPPED") or not co or not role:
                return self.reply({"error": "bad request"}, code=400)
            subprocess.run([sys.executable, TRACKER, "set", co, role, b["status"]], capture_output=True, timeout=20)
            return self.reply({"ok": True})
        if self.path == "/api/outcome":
            # What the company did with an application: interview, rejected, offer, or "" for no reply yet.
            b = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
            o, c = b.get("outcome") or "", db()
            if o and o not in OUTCOMES:
                return self.reply({"error": "unknown outcome"}, code=400)
            key = (str(b.get("company"))[:120], str(b.get("role"))[:200])
            if o:
                c.execute("INSERT OR REPLACE INTO outcomes VALUES(?,?,?,?)", (*key, o, now()))
            else:
                c.execute("DELETE FROM outcomes WHERE company=? AND role=?", key)
            c.commit()
            return self.reply({"ok": True})
        if self.path == "/api/followup":
            # Records a follow-up the user sent themselves. The app never sends messages.
            b = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
            c = db()
            c.execute("INSERT INTO followups VALUES(?,?,?)", (str(b.get("company"))[:120], str(b.get("role"))[:200], now()))
            c.commit()
            return self.reply({"ok": True})
        if self.path.startswith("/api/outreach/"):
            b = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
            if self.path == "/api/outreach/sync":
                try:
                    if not b.get("label_id"):
                        return self.reply({"error": "Choose the Apollo list you made for your job search."})
                    n = outreach.sync_contacts(db(), b["label_id"])
                except RuntimeError as e:
                    return self.reply({"error": str(e)})
                return self.reply(dict(synced=n, companies=outreach.saved_companies(db())))
            # A job from the list, or just a company name typed on the Email recruiters page.
            job = job_row(str(b.get("id", ""))) or (outreach.company_job(b["company"]) if str(b.get("company", "")).strip() else None)
            if not job:
                return self.reply({"error": "unknown job"}, code=404)
            try:
                if self.path == "/api/outreach/find":
                    domain = re.sub(r"^(https?://)?(www\.)?", "", str(b.get("domain") or "")).strip("/ ")
                    if b.get("org_id") or domain:
                        return self.reply(dict(contacts=outreach.find(db(), job, domain=domain or None, org_id=b.get("org_id"))))
                    if job["id"].startswith("co:"):
                        return self.reply(outreach.find_by_name(db(), job, job["company"]))
                    return self.reply(dict(contacts=outreach.find(db(), job, domain=outreach.guess_domain(job["company"], job["url"]))))
                if self.path == "/api/outreach/lookup":
                    return self.reply(dict(companies=outreach.lookup_company(job["company"])))
                if self.path == "/api/outreach/reveal":
                    return self.reply(dict(contact=outreach.reveal(db(), str(b.get("person_id")))))
            except RuntimeError as e:
                return self.reply({"error": str(e)})
            if self.path == "/api/outreach/draft":
                return self.reply(outreach_draft(job, b.get("person_id"), b.get("attach")))
            if self.path == "/api/outreach/send":
                return self.reply(outreach_send(job, b))
            return self.reply({"error": "not found"}, code=404)
        if self.path in ("/api/prepare", "/api/fill"):
            job = job_row(json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0)))).get("id"))
            if not job:
                return self.reply({"error": "unknown job"}, code=404)
            return self.reply(start_prepare(job) if self.path == "/api/prepare" else fill(job))
        self.reply({"error": "not found"}, code=404)


def scanning():
    try:
        with open(LOCK, "w") as f:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(f, fcntl.LOCK_UN)
        return False
    except OSError:
        return True


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "serve"
    if cmd == "scan":
        scan("--no-notify" not in sys.argv)
    elif cmd == "top":
        for r in [r for r in visible(db()) if r["bucket"] in ("now", "strong")][: int(sys.argv[2]) if len(sys.argv) > 2 else 20]:
            e = r["eval"]
            print(f"{r['score']:5.1f} {r['bucket']:6} {r['company'][:22]:22} {r['title'][:52]:52} yrs={e['years']} fit={e['parts']['fit']} t={e['parts']['timing']} sp={e['parts']['sponsor']} {','.join(r['sources'])}")
    else:
        port = int(sys.argv[2]) if len(sys.argv) > 2 else PORT
        ext_token()  # writes extension/config.js for the unpacked extension
        print(f"http://localhost:{port}")
        ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
