"""Job sources. Each returns normalised dicts: company,title,location,url,posted,text,salary,source + optional Jobright signals."""
import html, json, os, re, subprocess, urllib.request
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import unquote

D = os.path.dirname(os.path.abspath(__file__))
SIMPLIFY = "https://raw.githubusercontent.com/SimplifyJobs/New-Grad-Positions/dev/.github/scripts/listings.json"
NODE = os.path.expanduser("~/.local/node/bin/node")

ENGINEER = re.compile(r"engineer|developer|\bsde\b|\bswe\b|\bmle\b|member of technical staff", re.I)
TARGET = re.compile(r"software|machine learning|\bml\b|\bai\b|back-?end|platform|infrastructure|forward deployed|full[- ]?stack|applied|\bllm|mlops|reliability|\bsre\b|product engineer|\bsde\b|\bswe\b|\bmle\b|member of technical staff", re.I)
EXCLUDE = re.compile(r"\bsenior\b|\bsr\b|\bstaff\b|principal|\blead\b|manager|director|\bhead\b|\bvp\b|\bintern\b|internship|co-?op\b|front-?end|\bios\b|android|embedded|firmware|hardware|\bqa\b|\btest engineer|verification|support|sales|recruit|architect|\bphd\b|clearance|polygraph|silicon|\bcad\b|mechanical|electrical|summer 20|\biv\b|\bv\b|apprentice|physical design|designer|contractor|data center|\bit systems|in test\b|\bsdet\b|analyst|gameplay", re.I)

STATES = "AL AK AZ AR CA CO CT DE FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV NH NJ NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY DC".split()
US_CODES = re.compile(r"\b(?:" + "|".join(STATES) + r"|US|USA|U\.S\.)\b")
US_NAMES = re.compile(r"united states|san francisco|new york|\bnyc\b|seattle|bay area|palo alto|mountain view|sunnyvale|san jose|los angeles|boston|chicago|austin|bellevue|redmond|menlo park|santa clara|cupertino|denver|atlanta|san diego|washington", re.I)
FOREIGN = re.compile(r"canada|toronto|vancouver|montreal|london|\buk\b|united kingdom|ireland|dublin|germany|berlin|munich|france|paris|india|bangalore|bengaluru|hyderabad|singapore|japan|tokyo|china|beijing|shanghai|australia|sydney|netherlands|amsterdam|poland|spain|israel|mexico|brazil|korea|seoul|pune|mumbai|delhi|chennai|noida|gurgaon|emea|apac|europe|latam|philippines|argentina|colombia|romania|portugal|sweden", re.I)


def wanted_title(t):
    t = t or ""
    probe = re.sub(r"member of technical staff", "mts", t, flags=re.I)
    return bool(ENGINEER.search(t) and TARGET.search(t) and not EXCLUDE.search(probe))


def is_us(loc):
    for part in re.split(r"[;|/]", loc or ""):
        if FOREIGN.search(part):
            continue
        if US_CODES.search(part) or US_NAMES.search(part) or re.search(r"remote", part, re.I):
            return True
    return False


def get(url, data=None, headers=None, timeout=30):
    h = {"User-Agent": "Mozilla/5.0", **(headers or {})}
    return urllib.request.urlopen(urllib.request.Request(url, data=data, headers=h), timeout=timeout).read().decode("utf-8", "ignore")


def clean(t):
    t = html.unescape(html.unescape(t or ""))
    t = re.sub(r"<(style|script).*?</\1>", " ", t, flags=re.S)
    t = re.sub(r"</(p|li|h\d|div)>|<br\s*/?>", "\n", t)
    t = re.sub(r"<[^>]+>", " ", t)
    return re.sub(r"[ \t\xa0]+", " ", t).strip()


def pretty(slug):
    return re.sub(r"[-_]+|%20", " ", unquote(slug)).strip().title()


def greenhouse(board):
    out = []
    for j in json.loads(get(f"https://boards-api.greenhouse.io/v1/boards/{board}/jobs?content=true"))["jobs"]:
        loc = (j.get("location") or {}).get("name", "")
        if wanted_title(j.get("title")) and is_us(loc):
            out.append(dict(company=j.get("company_name") or pretty(board), title=j["title"], location=loc, url=j["absolute_url"], posted=j.get("first_published") or j.get("updated_at"), text=clean(j.get("content")), ats=f"gh:{j['id']}"))
    return out


def ashby(board):
    out = []
    for j in json.loads(get(f"https://api.ashbyhq.com/posting-api/job-board/{board}?includeCompensation=true")).get("jobs", []):
        locs = [j.get("location") or ""] + [s.get("location", "") for s in j.get("secondaryLocations") or []]
        us = [l for l in locs if is_us(l)]
        if j.get("isListed", True) and j.get("employmentType") in (None, "FullTime") and wanted_title(j.get("title")) and us:
            comp = (j.get("compensation") or {}).get("compensationTierSummary") or ""
            out.append(dict(company=pretty(board), title=j["title"].strip(), location="; ".join(us)[:80], url=j["jobUrl"], posted=j.get("publishedAt"), text=j.get("descriptionPlain") or clean(j.get("descriptionHtml")), salary=comp, ats=f"as:{j['id']}"))
    return out


def lever(board):
    out = []
    for j in json.loads(get(f"https://api.lever.co/v0/postings/{board}?mode=json")):
        c = j.get("categories") or {}
        loc = c.get("location") or ""
        if j.get("country") == "US" or is_us(loc):
            if wanted_title(j.get("text")):
                txt = (j.get("descriptionPlain") or "") + " " + " ".join(l.get("text", "") + " " + clean(l.get("content")) for l in j.get("lists", [])) + " " + (j.get("additionalPlain") or "")
                out.append(dict(company=pretty(board), title=j["text"], location=loc, url=j["hostedUrl"], posted=j.get("createdAt"), text=txt, ats=f"lv:{j['id']}"))
    return out


def workday(spec):
    tenant_wd, site = spec.split("/")
    tenant = tenant_wd.split(".")[0]
    base = f"https://{tenant_wd}.myworkdayjobs.com/wday/cxs/{tenant}/{site}"
    seen, out = set(), []
    for q in ("machine learning engineer", "software engineer"):
        body = json.dumps({"appliedFacets": {}, "limit": 20, "offset": 0, "searchText": q}).encode()
        for p in json.loads(get(base + "/jobs", body, {"Content-Type": "application/json"})).get("jobPostings", []):
            path, loc = p.get("externalPath"), p.get("locationsText", "")
            if path in seen or "30+" in p.get("postedOn", "") or not wanted_title(p.get("title")):
                continue
            seen.add(path)
            if not (is_us(loc) or re.search(r"\d+ Locations", loc)):
                continue
            d = json.loads(get(base + path, headers={"Accept": "application/json"}))["jobPostingInfo"]
            full = f"{d.get('location', '')} {(d.get('country') or {}).get('descriptor', '')}"
            if is_us(full):
                out.append(dict(company=pretty(tenant), title=d["title"], location=d.get("location", loc), url=f"https://{tenant_wd}.myworkdayjobs.com/en-US/{site}{path}", posted=d.get("startDate"), text=clean(d.get("jobDescription")), ats="wd:" + path.rsplit("_", 1)[-1]))
    return out


def simplify():
    import time
    cutoff, out = time.time() - 21 * 86400, []
    for x in json.loads(get(SIMPLIFY, timeout=60)):
        locs = "; ".join(x.get("locations") or [])
        if x.get("active") and x.get("is_visible", True) and x.get("date_posted", 0) > cutoff and wanted_title(x.get("title")) and is_us(locs):
            out.append(dict(company=x["company_name"], title=x["title"], location=locs[:80], url=x["url"], posted=x["date_posted"], text="", ats=ats_key(x["url"]), simplify_id=x["id"]))
    return out


def jobright():
    # Written when Jobright restricts the account. No request is sent until this file is deleted by hand.
    pause_file = os.path.join(D, "data", "jobright_paused")
    if os.path.exists(pause_file):
        raise RuntimeError("Paused: Jobright restricted the account. Delete data/jobright_paused only after access is restored.")
    out_file = os.path.join(D, ".jobright.json")
    p = subprocess.run([NODE, os.path.join(D, "jobright_fetch.cjs"), out_file], capture_output=True, text=True, timeout=240)
    if p.returncode == 3:
        open(pause_file, "w").close()
        raise RuntimeError("Paused: Jobright restricted the account. Delete data/jobright_paused only after access is restored.")
    if p.returncode:
        raise RuntimeError(p.stderr.strip()[:200] or "fetch failed")
    data = json.load(open(out_file))
    if data["queries"] and data["failed"] == data["queries"]:
        raise RuntimeError("every Jobright query failed; the session may have expired")
    out = []
    for j in data["jobs"]:
        if wanted_title(j.get("title")) and is_us(j.get("location")):
            j["posted_utc"] = j.pop("posted", None)
            out.append(dict(j, posted=j["posted_utc"], ats=ats_key(j.get("url") or ""), jobright=True, jobright_id=j["id"]))
    return out


def ats_key(url):
    m = re.search(r"gh_jid=(\d+)|[?&]token=(\d+)|greenhouse\.io/[^/]+/jobs/(\d+)", url)
    if m:
        return "gh:" + next(g for g in m.groups() if g)
    m = re.search(r"jobs\.ashbyhq\.com/[^/]+/([0-9a-f-]{36})", url)
    if m:
        return "as:" + m.group(1)
    m = re.search(r"jobs\.lever\.co/[^/]+/([0-9a-f-]{36})", url)
    if m:
        return "lv:" + m.group(1)
    m = re.search(r"myworkdayjobs\.com/.*_([A-Za-z0-9-]+?)(?:\?|$)", url)
    return "wd:" + m.group(1) if m else ""


def discover(jobs, boards):
    """Boards seen in aggregator links get scanned too, so the list grows on its own."""
    added = 0
    for j in jobs:
        u = j.get("url") or ""
        for kind, pat in (("greenhouse", r"[?&]for=([a-z0-9_-]+)|greenhouse\.io/([a-z0-9_-]+)/jobs"), ("ashby", r"jobs\.ashbyhq\.com/([A-Za-z0-9_.%-]+)/"), ("lever", r"jobs\.lever\.co/([A-Za-z0-9_.-]+)/")):
            m = re.search(pat, u)
            if m:
                b = next(g for g in m.groups() if g)
                if b != "embed" and b not in boards[kind]:
                    boards[kind].append(b)
                    added += 1
    return added


def scan_all(boards):
    """Returns (jobs, status). A failing board never stops the scan; it is reported in status."""
    status, jobs = {}, []
    for name, fn in (("Jobright", jobright), ("SimplifyJobs", simplify)):
        try:
            got = fn()
            jobs += [dict(j, source=name) for j in got]
            status[name] = dict(ok=True, jobs=len(got))
        except Exception as e:
            status[name] = dict(ok=False, jobs=0, error=str(e)[:200])
    discovered = discover(jobs, boards)
    for label, fn, key in (("Greenhouse", greenhouse, "greenhouse"), ("Ashby", ashby, "ashby"), ("Lever", lever, "lever"), ("Workday", workday, "workday")):
        ok, bad, count = 0, [], 0
        def run(b):
            try:
                return b, fn(b), None
            except Exception as e:
                return b, [], str(e)[:80]
        with ThreadPoolExecutor(6 if key == "workday" else 12) as ex:
            for b, got, err in ex.map(run, boards[key]):
                if err:
                    bad.append(b)
                else:
                    ok += 1
                    count += len(got)
                    jobs += [dict(j, source=label) for j in got]
        status[label] = dict(ok=ok > 0, jobs=count, boards=ok, failed=bad)
    status["_discovered"] = discovered
    return jobs, status
