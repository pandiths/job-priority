"""Job sources. Each returns normalised dicts: company,title,location,url,posted,text,salary,source + optional Jobright signals."""
import html, json, os, re, subprocess, threading, time, urllib.error, urllib.request
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import unquote, urlparse

D = os.path.dirname(os.path.abspath(__file__))
# Scan politely: two requests at once at most, a pause per board, and a full stop when a site asks us to slow down.
WORKERS = 2
PAUSE_SECONDS = 1.0
SIMPLIFY = "https://raw.githubusercontent.com/SimplifyJobs/New-Grad-Positions/dev/.github/scripts/listings.json"
NODE = os.path.expanduser("~/.local/node/bin/node")

ENGINEER = re.compile(r"engineer|developer|\bsde\b|\bswe\b|\bmle\b|member of technical staff", re.I)
TARGET = re.compile(r"software|machine learning|\bml\b|\bai\b|back-?end|platform|infrastructure|forward deployed|full[- ]?stack|applied|\bllm|mlops|reliability|\bsre\b|product engineer|\bsde\b|\bswe\b|\bmle\b|member of technical staff", re.I)
EXCLUDE = re.compile(r"\bsenior\b|\bsr\b|\bstaff\b|principal|distinguished|\bfellow\b|vice president|\blead\b|manager|director|\bhead\b|\bvp\b|\bintern\b|internship|co-?op\b|front-?end|\bios\b|android|embedded|firmware|hardware|\bqa\b|\btest engineer|verification|support|sales|recruit|architect|\bphd\b|clearance|polygraph|ts/sci|top secret|silicon|\bcad\b|mechanical|electrical|summer 20|\biv\b|\bv\b|apprentice|physical design|designer|contractor|data center|\bit systems|in test\b|\bsdet\b|analyst|gameplay", re.I)

STATES = "AL AK AZ AR CA CO CT DE FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV NH NJ NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY DC".split()
US_CODES = re.compile(r"\b(?:" + "|".join(STATES) + r"|US|USA|U\.S\.)\b")
US_NAMES = re.compile(r"united states|san francisco|new york|\bnyc\b|seattle|bay area|palo alto|mountain view|sunnyvale|san jose|los angeles|boston|chicago|austin|bellevue|redmond|menlo park|santa clara|cupertino|denver|atlanta|san diego|washington", re.I)
US_STATE_NAMES = re.compile(r"\b(?:alabama|alaska|arizona|arkansas|california|colorado|connecticut|delaware|florida|georgia|hawaii|idaho|illinois|indiana|iowa|kansas|kentucky|louisiana|maine|maryland|massachusetts|michigan|minnesota|mississippi|missouri|montana|nebraska|nevada|new hampshire|new jersey|new mexico|north carolina|north dakota|ohio|oklahoma|oregon|pennsylvania|rhode island|south carolina|south dakota|tennessee|texas|utah|vermont|virginia|west virginia|wisconsin|wyoming)\b", re.I)
# US places whose names also match FOREIGN (Dublin CA/OH, New Mexico). Only codes that aren't also country codes.
US_OVERRIDE = re.compile(r",\s*(?:CA|OH|NM)\b")
FOREIGN = re.compile(r"new zealand|\bnz\b|canada|toronto|vancouver|montreal|london|\buk\b|united kingdom|ireland|dublin|germany|berlin|munich|france|paris|india|bangalore|bengaluru|hyderabad|singapore|japan|tokyo|china|beijing|shanghai|australia|sydney|netherlands|amsterdam|poland|spain|israel|mexico|brazil|korea|seoul|pune|mumbai|delhi|chennai|noida|gurgaon|emea|apac|europe|latam|philippines|argentina|colombia|romania|portugal|sweden", re.I)


def wanted_title(t):
    t = t or ""
    probe = re.sub(r"member of technical staff", "mts", t, flags=re.I)
    return bool(ENGINEER.search(t) and TARGET.search(t) and not EXCLUDE.search(probe))


def is_us(loc):
    for part in re.split(r"[;|/]", loc or ""):
        if FOREIGN.search(part) and not (US_STATE_NAMES.search(part) or US_OVERRIDE.search(part)):
            continue
        if US_CODES.search(part) or US_NAMES.search(part) or US_STATE_NAMES.search(part) or re.search(r"remote", part, re.I):
            return True
    return False


# A site that says "too many requests" (429), "unavailable" (503) or "forbidden" (403) is left alone
# for at least a day, across scans, and longer if it sends Retry-After. Shared job-board APIs are only
# rested on 429/503, since a 403 there is about one board, not about us.
COOLDOWN_FILE = os.path.join(D, "data", "cooldown.json")
COOLDOWN_HOURS = 24
SHARED_HOSTS = {"boards-api.greenhouse.io", "api.ashbyhq.com", "api.lever.co", "api.smartrecruiters.com",
                "apply.workable.com", "raw.githubusercontent.com", "hn.algolia.com"}
USER_AGENT = "JobRadar/1.0 (personal job search tool; reads public job listings slowly)"
_cooldown_lock = threading.Lock()


def cooldowns():
    try:
        return {h: t for h, t in json.load(open(COOLDOWN_FILE)).items() if t > time.time()}
    except Exception:
        return {}


def _rest(host, retry_after):
    wait = COOLDOWN_HOURS * 3600
    if str(retry_after or "").isdigit():
        wait = max(wait, int(retry_after))
    with _cooldown_lock:
        c = cooldowns()
        c[host] = max(c.get(host, 0), time.time() + wait)
        os.makedirs(os.path.dirname(COOLDOWN_FILE), exist_ok=True)
        with open(COOLDOWN_FILE + ".tmp", "w") as f:
            json.dump(c, f)
        os.replace(COOLDOWN_FILE + ".tmp", COOLDOWN_FILE)


def get(url, data=None, headers=None, timeout=30, final_url=False):
    """final_url=True returns (url after redirects, text)."""
    host = urlparse(url).hostname or ""
    until = cooldowns().get(host)
    if until:
        raise RuntimeError(f"{host} asked us to slow down; resting until {time.strftime('%b %d %H:%M', time.localtime(until))}")
    h = {"User-Agent": USER_AGENT, **(headers or {})}
    try:
        r = urllib.request.urlopen(urllib.request.Request(url, data=data, headers=h), timeout=timeout)
        text = r.read().decode("utf-8", "ignore")
        return (r.geturl(), text) if final_url else text
    except urllib.error.HTTPError as e:
        if e.code in (429, 503) or (e.code == 403 and host not in SHARED_HOSTS):
            _rest(host, e.headers.get("Retry-After"))
        raise


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
            out.append(dict(company=j.get("company_name") or pretty(board), title=j["title"], location=loc, url=j["absolute_url"], posted=j.get("first_published") or j.get("updated_at"), text=clean(j.get("content")), ats=f"gh:{j['id']}", board=board))
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
    # Global boards (eBay, Barclays) list many non-US roles first, so read up to 3 pages of 20 per query.
    pages = []
    for q in ("machine learning engineer", "software engineer"):
        for offset in (0, 20, 40):
            if offset:
                time.sleep(0.5)
            body = json.dumps({"appliedFacets": {}, "limit": 20, "offset": offset, "searchText": q}).encode()
            got = json.loads(get(base + "/jobs", body, {"Content-Type": "application/json"})).get("jobPostings", [])
            pages += got
            if len(got) < 20:
                break
    for p in pages:
        path, loc = p.get("externalPath"), p.get("locationsText", "")
        if path in seen or "30+" in p.get("postedOn", "") or not wanted_title(p.get("title")):
            continue
        seen.add(path)
        # Skip only clearly foreign roles here; campus names like "Whippany Campus" are checked on the job page.
        if FOREIGN.search(loc) and not is_us(loc):
            continue
        time.sleep(0.5)
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
        # Most rows say "Other"; skip only the ones Simplify marked as closed to H-1B candidates.
        if x.get("sponsorship") in ("U.S. Citizenship is Required", "Does Not Offer Sponsorship"):
            continue
        if x.get("active") and x.get("is_visible", True) and x.get("date_posted", 0) > cutoff and wanted_title(x.get("title")) and is_us(locs):
            out.append(dict(company=x["company_name"], title=x["title"], location=locs[:80], url=x["url"], posted=x["date_posted"], text="", ats=ats_key(x["url"]), simplify_id=x["id"],
                            tags=["H1B sponsor"] if x.get("sponsorship") == "Offers Sponsorship" else []))
    return out


JOBRIGHT_NOT_BEFORE = 1794211200  # 2026-11-09 00:00 Pacific
JOBRIGHT_EVERY_HOURS = 24


def jobright():
    # Written when Jobright restricts the account. No request is sent until this file is deleted by hand.
    pause_file = os.path.join(D, "data", "jobright_paused")
    if os.path.exists(pause_file):
        raise RuntimeError("Paused: Jobright restricted the account. Delete data/jobright_paused only after access is restored.")
    if time.time() < JOBRIGHT_NOT_BEFORE:
        raise RuntimeError("Off until Nov 9, 2026, after the 7-day suspension ends.")
    # Once a day at most, even with --force. The fetch takes about an hour, so it runs on its own
    # and each scan uses the last finished result. Stamped at launch so a crash still counts.
    out_file = os.path.join(D, ".jobright.json")
    stamp = os.path.join(D, "data", "jobright_last_scan")
    if not os.path.exists(stamp) or time.time() - os.path.getmtime(stamp) >= JOBRIGHT_EVERY_HOURS * 3600:
        open(stamp, "w").close()
        log = open(os.path.join(D, "data", "jobright_fetch.log"), "w")
        subprocess.Popen([NODE, os.path.join(D, "jobright_fetch.cjs"), out_file, pause_file], stdout=log, stderr=log, start_new_session=True)
    if not os.path.exists(out_file) or time.time() - os.path.getmtime(out_file) > 2 * 86400:
        raise RuntimeError("Running slowly in the background (about an hour); results show on the next scan.")
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


# Free API plans have small quotas. JSearch's free plan allows 25 calls a month and a run costs 2,
# so it runs at most every 72 hours (about 20 calls a month). Adzuna costs 4 a run, fine at the 6-hour scan limit.
KEYED_EVERY_HOURS = {"JSearch": 72}


def rationed(name, fn):
    """Calls fn at most every KEYED_EVERY_HOURS[name]; between runs, returns the last saved result."""
    cache = os.path.join(D, "data", name.lower() + "_last.json")
    if os.path.exists(cache) and time.time() - os.path.getmtime(cache) < KEYED_EVERY_HOURS[name] * 3600:
        return json.load(open(cache))
    got, err = [], None
    try:
        got = fn()
    except Exception as e:
        err = e  # a failed run still uses up its turn, so a broken call isn't repeated every scan
    with open(cache + ".tmp", "w") as f:
        json.dump(got, f)
    os.replace(cache + ".tmp", cache)
    if err:
        raise err
    return got


def scan_all(boards):
    """Returns (jobs, status). A failing board never stops the scan; it is reported in status."""
    status, jobs = {}, []
    import bigco, feeds  # here, not at the top: both import helpers from this module
    keyed = {"Adzuna": feeds.secret("adzuna-app-id") and feeds.secret("adzuna-app-key"), "JSearch": feeds.secret("rapidapi-key")}
    for name, fn in (("Jobright", jobright), ("SimplifyJobs", simplify), ("Jobright GitHub", feeds.jobright_github), ("Hacker News", feeds.hn_hiring), ("Adzuna", feeds.adzuna), ("JSearch", feeds.jsearch)):
        if name in keyed and not keyed[name]:
            status[name] = dict(ok=False, jobs=0, error="Not set up: needs an API key")
            continue
        try:
            got = rationed(name, fn) if name in KEYED_EVERY_HOURS else fn()
            jobs += [dict(j, source=name) for j in got]
            status[name] = dict(ok=True, jobs=len(got))
        except Exception as e:
            status[name] = dict(ok=False, jobs=0, error=str(e)[:200])
    # Big companies' own career sites, one at a time.
    got_all, bad = [], []
    for company, fn in bigco.BIGCO.items():
        try:
            got_all += fn()
        except Exception as e:
            bad.append(f"{company} ({str(e)[:60]})")
    jobs += [dict(j, source="Career sites") for j in got_all]
    status["Career sites"] = dict(ok=len(bad) < len(bigco.BIGCO), jobs=len(got_all), boards=len(bigco.BIGCO) - len(bad), failed=bad)
    discovered = discover(jobs, boards) + feeds.discover_more(jobs, boards)
    for label, fn, key in (("Greenhouse", greenhouse, "greenhouse"), ("Ashby", ashby, "ashby"), ("Lever", lever, "lever"), ("Workday", workday, "workday"), ("SmartRecruiters", feeds.smartrecruiters, "smartrecruiters"), ("Workable", feeds.workable, "workable")):
        ok, bad, count = 0, [], 0
        slow_down = threading.Event()
        def run(b):
            if slow_down.is_set():
                return b, [], "skipped"
            time.sleep(PAUSE_SECONDS)
            try:
                return b, fn(b), None
            except urllib.error.HTTPError as e:
                if e.code in (429, 503):
                    slow_down.set()
                return b, [], str(e)[:80]
            except Exception as e:
                return b, [], str(e)[:80]
        with ThreadPoolExecutor(1 if key == "workday" else WORKERS) as ex:
            for b, got, err in ex.map(run, boards.get(key, [])):
                if err:
                    bad.append(b)
                else:
                    ok += 1
                    count += len(got)
                    jobs += [dict(j, source=label) for j in got]
        status[label] = dict(ok=ok > 0 and not slow_down.is_set(), jobs=count, boards=ok, failed=bad)
        if slow_down.is_set():
            status[label]["error"] = "the site asked us to slow down, so this scan stopped early"
    status["_discovered"] = discovered
    return jobs, status
