#!/usr/bin/env python3
"""Job Radar. usage: radar.py scan [--no-notify] | radar.py serve [port] | radar.py top [n]"""
import csv, datetime as dt, fcntl, json, os, re, sqlite3, subprocess, sys, threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

D = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, D)
import scoring, sources  # noqa: E402

DB = os.path.join(D, "radar.db")
BOARDS = os.path.join(D, "boards.json")
LOCK = os.path.join(D, ".scan.lock")
TRACKER = os.path.join(scoring.JOBSEARCH, "tracker.py")
RESUME = os.path.join(scoring.JOBSEARCH, "resume_A_v2.html")
OUTPUT = os.path.join(D, "output")
FILL_LOCK = os.path.join(D, ".fill.lock")
CLOSED_AFTER_DAYS = 3
PORT = 8765


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
    """)
    if "raw" not in [r["name"] for r in c.execute("PRAGMA table_info(jobs)")]:
        c.execute("ALTER TABLE jobs ADD COLUMN raw TEXT")
    return c


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def merge(raw):
    """Same job from several sources becomes one row: ATS text wins, Jobright adds competition signals."""
    by_ats, merged = {}, {}
    for j in sorted(raw, key=lambda x: 0 if x["source"] in ("Greenhouse", "Ashby", "Lever", "Workday") else 1):
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
            for k in ("applicants", "tags", "skill", "seniority", "years", "salary", "jobright", "applied"):
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


def scan(send_notification=True):
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
    out = []
    for r in c.execute("SELECT id,company,title,location,url,sources,applicants,tags,eval,score,first_seen,last_seen,state,notified FROM jobs WHERE last_seen>=? ORDER BY score DESC", (cutoff,)):
        d = dict(r)
        d.update(eval=json.loads(d["eval"]), sources=json.loads(d["sources"]), tags=json.loads(d["tags"]))
        e = d["eval"]
        row = scoring.tracker_row(d["company"], d["title"], rows)
        status = row["status"] if row else ""
        d["tracker_i"] = row["i"] if row else None
        d["prepared"] = os.path.exists(prep_paths(d)[2])
        d["bucket"] = "filled" if status in FILLED_STATUSES else "applied" if status in APPLIED_STATUSES or e["applied"] or d["state"] == "applied" else "skipped" if status == "SKIPPED" else "blocked" if e["block"] else "now" if d["score"] >= scoring.APPLY_NOW and e["parts"]["timing"] >= 45 and e["parts"]["fit"] >= 70 and e["parts"]["level"] >= 60 and not e["thin_jd"] else "strong" if d["score"] >= scoring.STRONG else "other"
        out.append(d)
    return out


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
    r = db().execute("SELECT id,company,title,url,text FROM jobs WHERE id=?", (job_id,)).fetchone()
    return dict(r) if r else None


def prep_paths(job):
    name = slug(job["company"], job["title"])
    out = os.path.join(OUTPUT, name)
    return name, out, os.path.join(out, name + ".report.json")


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
    return None


def fill(job):
    """Fills the form in the job-search Chrome and leaves the tab open. Nothing in this path can submit."""
    status = prep_status(job)
    if status.get("state") != "ready" or not status.get("passed"):
        return dict(ok=False, error="Prepare a resume that passes the ATS check first.")
    target = fill_target(job["url"])
    if not target:
        return dict(ok=False, error="This site has no automatic filler yet. Open the posting and apply by hand.")
    kind, url = target
    script, gen = {"greenhouse": ("gh.sh", "gh_run.js"), "ashby": ("ashby.sh", "ashby_gen.js"), "lever": ("lever.sh", "lever_gen.js")}[kind]
    name, out, _ = prep_paths(job)
    lock = open(FILL_LOCK, "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return dict(ok=False, error="Another form is being filled. Try again in a minute.")
    # Sponsorship questions stay blank on purpose: the applicant answers those personally.
    env = dict(os.environ, BLANKSP="1")
    subprocess.run(["bash", os.path.join(scoring.JOBSEARCH, script), out, f"{name}={url}"], cwd=scoring.JOBSEARCH, env=env, capture_output=True, timeout=30, check=True)
    gen_path = os.path.join(scoring.JOBSEARCH, gen)
    if SUBMIT_CLICK.search(open(gen_path).read()):
        return dict(ok=False, error="Stopped: the generated filler contains a Submit click.")
    p = subprocess.run([NODE, os.path.join(scoring.JOBSEARCH, "cdp_run.js"), gen_path], cwd=scoring.JOBSEARCH, capture_output=True, text=True, timeout=300)
    if p.returncode or "LEFT OPEN FOR REVIEW" not in p.stdout:
        return dict(ok=False, error="The filler didn't finish. Is the job-search Chrome open?", log=p.stdout[-1500:])
    subprocess.run([sys.executable, TRACKER, "add", dt.date.today().isoformat(), job["company"], job["title"], "FILLED - TAB OPEN", out, f"ATS {status['ats_after']}; filled by Job Radar"], capture_output=True, timeout=20)
    todo = [l.strip() for l in p.stdout.splitlines() if l.startswith("??") or l.startswith("!!")]
    return dict(ok=True, needs_you=todo)


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
        if u.path == "/api/tracker":
            return self.reply(tracker_list())
        if u.path == "/api/status":
            s = c.execute("SELECT at,status,total,new FROM scans ORDER BY at DESC LIMIT 1").fetchone()
            return self.reply(dict(scan=dict(s, status=json.loads(s["status"])) if s else None, pipeline=pipeline(), scanning=scanning(), weights=scoring.WEIGHTS))
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
        if origin and not re.fullmatch(r"http://(localhost|127\.0\.0\.1):\d+", origin):
            return self.reply({"error": "forbidden"}, code=403)
        if self.path == "/api/scan":
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
        print(f"http://localhost:{port}")
        ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
