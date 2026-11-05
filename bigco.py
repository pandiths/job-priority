"""Big-company career-site fetchers. Same dict shape as sources.py. Python 3.9 stdlib only.
Each site: a few queries, US only, <= 50 results per query, deduped, >= 1s between requests.
Microsoft and Uber are left out: both refused automated requests when tested on 2026-11-02."""
import json, re, time
from urllib.parse import quote, urlencode
from sources import get, clean, wanted_title, is_us

QUERIES = ("machine learning engineer", "software engineer", "ai engineer")
PAUSE = 1.2
SALARY = re.compile(r"\$\s?\d[\d,]*(?:\.\d+)?\s*(?:k|K)?(?:\s*(?:-|–|to)\s*\$?\s?\d[\d,]*(?:\.\d+)?\s*(?:k|K)?)?(?:\s*(?:/|per)\s*(?:year|yr|hour|hr))?")


def _salary(text):
    m = SALARY.search(text or "")
    return m.group(0).strip() if m else ""


def _pause():
    time.sleep(PAUSE)


def amazon():
    seen, out = set(), []
    for q in QUERIES:
        _pause()
        u = "https://www.amazon.jobs/en/search.json?" + urlencode({"base_query": q, "country": "USA", "result_limit": 50, "offset": 0, "sort": "relevant"})
        for j in json.loads(get(u)).get("jobs", []):
            key = j.get("id_icims") or j.get("id")
            if key in seen or not wanted_title(j.get("title")) or not is_us(j.get("normalized_location") or j.get("location")):
                continue
            seen.add(key)
            text = clean("\n".join(j.get(k) or "" for k in ("description", "basic_qualifications", "preferred_qualifications")))
            out.append(dict(company="Amazon", title=j["title"], location=j.get("normalized_location") or j.get("location", ""), url="https://www.amazon.jobs" + j["job_path"], posted=j.get("posted_date"), text=text, salary=_salary(j.get("preferred_qualifications")), ats=f"amz:{key}"))
    return out


def _google_page(q, page):
    u = "https://www.google.com/about/careers/applications/jobs/results/?" + urlencode([("q", q), ("location", "United States"), ("target_level", "EARLY"), ("target_level", "MID")])
    s = get(u)
    i = s.find("key: 'ds:1'")
    if i < 0:
        raise RuntimeError("Google page layout changed (no ds:1 block)")
    i = s.find("data:", i) + 5
    return json.loads(s[i:s.find(", sideChannel:", i)])[0] or []


def google():
    seen, out = set(), []
    for q in QUERIES:
        for page in (1,):  # first page only: Google's robots.txt disallows the page= parameter
            _pause()
            rows = _google_page(q, page)
            for r in rows:
                jid, title = r[0], r[1]
                locs = "; ".join(l[0] for l in (r[9] or []) if l)
                if jid in seen or not wanted_title(title) or not is_us(locs):
                    continue
                seen.add(jid)
                text = clean("\n".join((x or [None, ""])[1] or "" for x in (r[10], r[3], r[4], r[19]) if isinstance(x, list)))
                slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
                posted = (r[12] or [None])[0]
                out.append(dict(company="Google", title=title, location=locs[:120], url=f"https://www.google.com/about/careers/applications/jobs/results/{jid}-{slug}", posted=posted, text=text, salary=_salary(text), ats=f"goog:{jid}"))
            if len(rows) < 20:
                break
    return out


def apple():
    seen, out = set(), []
    for q in ('"machine learning" engineer', '"software engineer"', '"ai engineer"'):
        for page in (1, 2):  # 20 per page
            _pause()
            u = "https://jobs.apple.com/en-us/search?" + urlencode({"search": q, "location": "united-states-USA", "page": page})
            m = re.search(r'__staticRouterHydrationData = JSON\.parse\(("(?:[^"\\]|\\.)*")\);', get(u))
            if not m:
                raise RuntimeError("Apple page layout changed (no hydration data)")
            rows = json.loads(json.loads(m.group(1)))["loaderData"]["search"]["searchResults"] or []
            for r in rows:
                pid, title = r.get("positionId"), (r.get("postingTitle") or "").strip()
                locs = "; ".join(", ".join(x for x in (l.get("city"), l.get("stateProvince"), l.get("countryName")) if x) for l in r.get("locations") or [])
                if pid in seen or not wanted_title(title) or not is_us(locs.replace("United States of America", "United States")):
                    continue
                seen.add(pid)
                out.append(dict(company="Apple", title=title, location=locs[:120], url=f"https://jobs.apple.com/en-us/details/{pid}/{r.get('transformedPostingTitle', '')}", posted=r.get("postDateInGMT"), text=r.get("jobSummary") or "", ats=f"apl:{pid}"))
            if len(rows) < 20:
                break
    return out


# Relay persisted-query id from Meta's JS bundle (CareersJobSearchResultsDataQuery). Changes when Meta redeploys.
META_DOC_ID = "27506805582236862"


def meta():
    page = get("https://www.metacareers.com/jobs")
    m = re.search(r'"LSD",\[\],\{"token":"([^"]+)', page)
    if not m:
        raise RuntimeError("Meta page has no LSD token")
    lsd, seen, out = m.group(1), set(), []
    for q in QUERIES:
        _pause()
        v = {"search_input": {"q": q, "divisions": [], "offices": [], "roles": [], "leadership_levels": [], "saved_jobs": [], "saved_searches": [], "sub_teams": [], "teams": [], "is_leadership": False, "is_remote_only": False, "sort_by_new": True, "results_per_page": None}, "isLoggedIn": False}
        body = urlencode({"lsd": lsd, "doc_id": META_DOC_ID, "variables": json.dumps(v), "fb_api_req_friendly_name": "CareersJobSearchResultsDataQuery"}).encode()
        d = json.loads(get("https://www.metacareers.com/graphql", body, {"Content-Type": "application/x-www-form-urlencoded", "X-FB-LSD": lsd}))
        if "errors" in d and not d.get("data"):
            raise RuntimeError("Meta GraphQL error (doc_id may be stale): " + str(d["errors"])[:150])
        res = (d.get("data") or {}).get("job_search_with_featured_jobs") or {}
        rows = (res.get("all_jobs") or [])[:50]
        for r in rows:
            us = [l for l in r.get("locations") or [] if is_us(l)]
            if r["id"] in seen or not wanted_title(r.get("title")) or not us:
                continue
            seen.add(r["id"])
            out.append(dict(company="Meta", title=r["title"], location="; ".join(us)[:120], url=f"https://www.metacareers.com/jobs/{r['id']}/", posted=None, text="", ats=f"meta:{r['id']}"))
    return out


def _oracle_hcm(company, host, site, prefix, location_id=None):
    seen, out = set(), []
    for q in QUERIES:
        _pause()
        f = f'findReqs;siteNumber={site},limit=50,keyword="{q}",sortBy=RELEVANCY' + (f",locationId={location_id}" if location_id else "")
        u = f"https://{host}/hcmRestApi/resources/latest/recruitingCEJobRequisitions?onlyData=true&expand=requisitionList.secondaryLocations&finder=" + quote(f, safe="=;,")
        items = json.loads(get(u)).get("items") or [{}]
        rows = items[0].get("requisitionList") or []
        for r in rows:
            locs = [r.get("PrimaryLocation") or ""] + [s.get("Name", "") for s in r.get("secondaryLocations") or []]
            us = [l for l in locs if is_us(l)]
            if r["Id"] in seen or not wanted_title(r.get("Title")) or not (r.get("PrimaryLocationCountry") == "US" or us):
                continue
            seen.add(r["Id"])
            out.append(dict(company=company, title=r["Title"], location=("; ".join(us) or r.get("PrimaryLocation", ""))[:120], url=f"https://{host}/hcmUI/CandidateExperience/en/sites/{site}/job/{r['Id']}", posted=r.get("PostedDate"), text=clean(r.get("ShortDescriptionStr")), ats=f"{prefix}:{r['Id']}"))
    return out


def oracle():
    return _oracle_hcm("Oracle", "eeho.fa.us2.oraclecloud.com", "CX_45001", "orcl", 300000000149325)


def jpmorgan():
    return _oracle_hcm("JPMorgan Chase", "jpmc.fa.oraclecloud.com", "CX_1001", "jpmc", 300000000289738)


def ford():
    return _oracle_hcm("Ford", "efds.fa.em5.oraclecloud.com", "CX_1", "ford")


def nokia():
    return _oracle_hcm("Nokia", "fa-evmr-saasfaprod1.fa.ocs.oraclecloud.com", "CX_1", "nokia")


def _eightfold(company, host, domain, prefix):
    """Eightfold careers sites. robots.txt allows /api/pcsx. No description in search results."""
    seen, out = set(), []
    for q in ("machine learning engineer", "software engineer"):
        _pause()
        u = f"https://{host}/api/pcsx/search?" + urlencode({"domain": domain, "query": q, "location": "United States", "start": 0, "sort_by": "relevance"})
        rows = json.loads(get(u))["data"].get("positions") or []
        for r in rows:
            us = [l for l in r.get("standardizedLocations") or r.get("locations") or [] if is_us(l)]
            if r["id"] in seen or not wanted_title(r.get("name")) or not us:
                continue
            seen.add(r["id"])
            url = r.get("positionUrl") or f"/careers/job/{r['id']}"
            out.append(dict(company=company, title=r["name"], location="; ".join(us)[:120], url=url if url.startswith("http") else f"https://{host}{url}", posted=r.get("postedTs"), text="", ats=f"{prefix}:{r.get('displayJobId') or r['id']}"))
    return out


def slb():
    return _eightfold("SLB", "apply.slb.com", "slb.com", "slb")


def _jibe(company, host, prefix):
    """Jibe careers sites serve their search results as JSON at /api/jobs (robots.txt allows it)."""
    seen, out = set(), []
    for q in ("machine learning engineer", "software engineer"):
        _pause()
        for x in json.loads(get(f"https://{host}/api/jobs?" + urlencode({"keywords": q, "page": 1, "limit": 50}))).get("jobs") or []:
            j = x.get("data", x)
            loc = j.get("full_location") or j.get("location_name") or ""
            if j.get("req_id") in seen or not wanted_title(j.get("title")) or j.get("country_code") not in (None, "US") or not is_us(loc):
                continue
            seen.add(j.get("req_id"))
            text = clean("\n".join(j.get(k) or "" for k in ("description", "qualifications", "responsibilities")))
            out.append(dict(company=company, title=j["title"], location=loc[:120], url=(j.get("apply_url") or "").replace("/login", ""), posted=j.get("posted_date"), text=text, salary=_salary(text), ats=f"{prefix}:{j.get('req_id')}"))
    return out


def docusign():
    return _jibe("Docusign", "careers.docusign.com", "docu")


def lowes():
    """Lowe's (Phenom). Keyword search ignores the keywords, so read the Technology category page,
    whose results are embedded in the page as JSON. robots.txt allows these pages."""
    out = []
    for frm in (0, 10, 20):
        _pause()
        s = get("https://talent.lowes.com/us/en/c/technology-jobs" + (f"?from={frm}&s=1" if frm else ""))
        i = s.find('"eagerLoadRefineSearch":')
        if i < 0:
            raise RuntimeError("Lowe's page layout changed (no eagerLoadRefineSearch)")
        d, _ = json.JSONDecoder().raw_decode(s[i + len('"eagerLoadRefineSearch":'):])
        rows = d.get("data", {}).get("jobs") or []
        for j in rows:
            loc = j.get("cityStateCountry") or j.get("location") or ""
            if wanted_title(j.get("title")) and is_us(loc.replace("United States of America", "United States")):
                out.append(dict(company="Lowe's", title=j["title"], location=loc[:120], url=f"https://talent.lowes.com/us/en/job/{j['jobId']}", posted=j.get("postedDate"), text=clean(j.get("descriptionTeaser")), ats=f"lowes:{j['jobId']}"))
        if frm + 10 >= d.get("totalHits", 0):
            break
    return out


BIGCO = {"Amazon": amazon, "Google": google, "Apple": apple, "Meta": meta, "Oracle": oracle, "JPMorgan Chase": jpmorgan, "Ford": ford, "Nokia": nokia, "SLB": slb, "Docusign": docusign, "Lowe's": lowes}
