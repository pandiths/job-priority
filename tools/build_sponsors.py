#!/usr/bin/env python3
"""Build a list of real H-1B sponsors for ML / software roles from DOL OFLC LCA disclosure data,
then map them to public Greenhouse / Ashby / Lever job boards.

Usage (run with python3 -I; stdlib only, no openpyxl needed):
  python3 -I build_sponsors.py parse  LCA_Disclosure_Data_FY2026_Q3.xlsx  [--out sponsors.json] [--top 400]
  python3 -I build_sponsors.py probe  sponsors.json  --boards /path/to/radar/boards.json  [--cap 1500]

Get the xlsx from https://www.dol.gov/agencies/eta/foreign-labor/performance (Disclosure Data -> LCA).
The quarterly file is cumulative for the fiscal year (Q4 = full FY). A plain HTTPS GET of the file
URL works even though the HTML page itself blocks curl.

Stage 'parse' streams the 1.5 GB sheet XML with regex (fast, low memory) and writes:
  sponsors.json            top N employers {employer, count, median_wage, top_titles, ...}
  excluded_staffing.json   large employers dropped by the staffing/outsourcing filter (for review)
Stage 'probe' writes new_boards.json, unmatched.json, probe_log.json.
"""
import json, re, sys, os, time, zipfile, statistics, argparse, collections
import xml.etree.ElementTree as ET
import urllib.request, urllib.error

# ---------------------------------------------------------------- filters
TITLE_INC = re.compile(
    r"machine learning|\bml\b|\bmle\b|mlops|\bai\b|artificial intelligence|deep learning|"
    r"computer vision|\bnlp\b|data scien|applied scien|"
    r"software (?:development )?(?:engineer|developer|dev\b)|software engineering|\bswe\b|"
    r"(?:backend|back[- ]end|frontend|front[- ]end|full[- ]?stack) (?:software )?(?:engineer|developer)",
    re.I)
TITLE_EXC = re.compile(
    r"\bsenior\b|\bsr\b|\bstaff\b|principal|manager|\bmgr\b|director|\blead\b|\bhead\b|\bvp\b|"
    r"vice president|architect|chief|distinguished|fellow|\bintern\b|professor|\btest|\bqa\b|"
    r"\biii\b|\biv\b|\bv\b|\bvi\b|\b[3-9]\b|level [3-9]|\bprinc\b|\bsmts\b|\blmts\b|\bpmts\b|advisor|"
    r"\bvp\d|\bdir\b|\bavp\b|postdoc", re.I)

# Known IT-services / staffing / outsourcing / consulting firms (match on normalized name).
DENY = [
    "cognizant", "infosys", "tata consultancy", "tcs", "wipro", "hcl america", "hcl technologies", "hcltech",
    "accenture", "capgemini", "tech mahindra", "ltimindtree", "larsen toubro", "lt technology", "mindtree",
    "deloitte", "ernst young", "ey global", "kpmg", "pricewaterhouse", "pwc", "mphasis", "hexaware",
    "persistent systems", "zensar", "mastech", "igate", "syntel", "virtusa", "ust global", "ust ",
    "ibm india", "cgi technologies", "cgi inc", "cgi federal", "ntt data", "nttdata", "dxc", "atos", "birlasoft",
    "cyient", "coforge", "niit", "kpit", "sonata software", "happiest minds", "intellect design",
    "photon infotech", "photon interactive", "collabera", "apexon", "infovision", "compunnel",
    "randstad", "kforce", "insight global", "teksystems", "robert half", "apex systems", "diverse lynx",
    "yash technologies", "yash ", "itc infotech", "sutherland", "conduent", "genpact", "exl service",
    "wns ", "firstsource", "quinnox", "nagarro", "globant", "epam", "endava", "luxoft", "sigmaways",
    "ciber", "igate", "tavant", "innova solutions", "akraya", "sriven", "kanini", "rapid it", "srs consulting",
    "e pro", "ampcus", "softsol", "intelliswift", "american cybersystems", "cybersystems", "xoriant",
    "ust inc", "valuelabs", "value labs", "3i infotech", "hitachi vantara", "hitachi digital services",
    "fractal analytics", "tiger analytics", "mu sigma", "latentview", "sigmoid", "quantiphi",
    "slalom", "booz allen", "mckinsey", "boston consulting", "bain ", "publicis sapient", "sapient",
    "thoughtworks", "perficient", "ness digital", "grid dynamics", "softserve", "emids", "infocepts",
    "cloudious", "beacon hill", "genesis corp", "tecaholic",
    # small consultancies spotted by manual review of the FY2026 Q3 top-400 (generic titles, low wages)
    "horizon international trd", "infocons", "anand pag", "ar infotek", "iresh", "netprime", "s v tek",
    "ultimus", "banking soft", "skygys", "digitronix", "neuronet", "mclaryn", "wingskyline", "sumeru",
    "sloka", "cognit ai", "datara", "axitem", "neon it", "novamegha", "dataflake", "neo techstaff",
    "softech technology", "missouri edp", "quadrant technologies", "nb alpha omega", "axzora", "immense brains",
    "ai datasphere", "teczone", "nest technologies", "orbisiq", "visionary innovative", "brillio", "experis", "manpower", "aditi consulting", "hirequest",
]
STAFFING_WORDS = re.compile(
    r"consult|staffing|outsourc|manpower|recruit|talent|workforce|infotech|info tech|infosoft|"
    r"it services|it solutions|software solutions|tech solutions|technology solutions|global services|"
    r"systems integrat|infotek|\btek\b|\bsolution\b|techstaff|\bstaff\b|\bedp\b|\bIT\b (?:LLC|INC|CORP)", re.I)

SUFFIX = re.compile(
    r"\b(inc|incorporated|llc|l l c|corp|corporation|co|company|ltd|limited|lp|llp|plc|pllc|pc|na|n a|"
    r"the|usa|us|u s|l p|america|americas|north america|holdings|holding|group|services)\b")


def norm_name(n):
    n = n.upper().replace("&", " AND ")
    n = re.sub(r"\(.*?\)", " ", n)
    n = re.sub(r"[^A-Z0-9 ]", " ", n).lower()
    n = re.sub(r"\band\b", " ", n)
    n = SUFFIX.sub(" ", n)
    return re.sub(r"\s+", " ", n).strip()


def annual(wage, unit):
    try:
        w = float(str(wage).replace(",", "").replace("$", ""))
    except ValueError:
        return None
    mult = {"year": 1, "hour": 2080, "week": 52, "bi-weekly": 26, "month": 12}.get((unit or "").strip().lower())
    if not mult:
        return None
    v = w * mult
    return v if 20000 <= v <= 2000000 else None


# ---------------------------------------------------------------- xlsx streaming
def col_idx(letters):
    n = 0
    for ch in letters:
        n = n * 26 + ord(ch) - 64
    return n - 1


def load_shared(z):
    out = []
    ns = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
    for ev, el in ET.iterparse(z.open("xl/sharedStrings.xml"), events=("end",)):
        if el.tag == ns + "si":
            out.append("".join(t.text or "" for t in el.iter(ns + "t")))
            el.clear()
    return out


CELL = re.compile(r'<c r="([A-Z]+)\d+"([^>]*?)(?:/>|>(.*?)</c>)', re.S)
VAL = re.compile(r"<v>(.*?)</v>|<t[^>]*>(.*?)</t>", re.S)


def unesc(s):
    return s.replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"').replace("&apos;", "'").replace("&amp;", "&")


def iter_rows(path):
    z = zipfile.ZipFile(path)
    ss = load_shared(z)
    print(f"shared strings: {len(ss):,}", file=sys.stderr)
    f = z.open("xl/worksheets/sheet1.xml")
    buf = ""
    while True:
        chunk = f.read(1 << 24)
        if not chunk:
            break
        buf += chunk.decode("utf-8", "replace")
        parts = buf.split("</row>")
        buf = parts.pop()
        for p in parts:
            i = p.find("<row")
            if i < 0:
                continue
            row = {}
            for col, attrs, inner in CELL.findall(p[i:]):
                if not inner:
                    continue
                m = VAL.search(inner)
                if not m:
                    continue
                v = m.group(1) if m.group(1) is not None else unesc(m.group(2) or "")
                if 't="s"' in attrs:
                    v = ss[int(v)]
                elif m.group(1) is not None:
                    v = unesc(v)
                row[col_idx(col)] = v
            yield row


# ---------------------------------------------------------------- stage 1
US_STATES = set("AL AK AZ AR CA CO CT DE DC FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV NH NJ NM NY NC "
                "ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY PR GU VI MP AS".split())


def parse(args):
    header = None
    H = {}
    stats = collections.Counter()
    emp = {}
    t0 = time.time()
    for n, row in enumerate(iter_rows(args.xlsx)):
        if header is None:
            header = [row.get(i, "") for i in range(max(row) + 1)]
            H = {h: i for i, h in enumerate(header)}
            continue
        g = lambda k: row.get(H[k], "")
        stats["rows"] += 1
        if n % 200000 == 0:
            print(f"  {n:,} rows {time.time()-t0:.0f}s", file=sys.stderr)
        if g("VISA_CLASS") != "H-1B":
            continue
        stats["h1b"] += 1
        if g("CASE_STATUS").strip().lower() != "certified":
            continue
        stats["h1b_certified"] += 1
        title = g("JOB_TITLE").strip()
        if not TITLE_INC.search(title) or TITLE_EXC.search(title):
            continue
        st = g("WORKSITE_STATE").strip().upper()
        if st not in US_STATES:
            continue
        stats["title_match_us"] += 1
        name = g("EMPLOYER_NAME").strip()
        key = norm_name(name)
        if not key:
            continue
        e = emp.setdefault(key, {"names": collections.Counter(), "titles": collections.Counter(), "wages": [],
                                 "n": 0, "sec": 0, "dep": 0, "states": collections.Counter()})
        e["n"] += 1
        e["names"][name] += 1
        e["titles"][re.sub(r"\s+", " ", title.title())] += 1
        e["states"][st] += 1
        if g("SECONDARY_ENTITY").strip().lower().startswith("y"):
            e["sec"] += 1
        if g("H_1B_DEPENDENT").strip().lower().startswith("y"):
            e["dep"] += 1
        w = annual(g("WAGE_RATE_OF_PAY_FROM"), g("WAGE_UNIT_OF_PAY"))
        if w:
            e["wages"].append(w)
    print(json.dumps(stats), file=sys.stderr)

    # staffing heuristic
    keep, dropped = [], []
    for key, e in emp.items():
        disp = e["names"].most_common(1)[0][0]
        sec_share, dep_share = e["sec"] / e["n"], e["dep"] / e["n"]
        reason = None
        light = " " + re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]", " ", disp.lower().replace("&", " "))).strip() + " "
        med = statistics.median(e["wages"]) if e["wages"] else 0
        if any((" " + d.strip() + " ") in f" {key} " or (" " + d.strip() + " ") in light for d in DENY):
            reason = "denylist"
        elif STAFFING_WORDS.search(disp):
            reason = "staffing-word in name"
        elif sec_share >= 0.05:
            reason = f"places workers at secondary/client entity ({sec_share:.0%})"
        elif dep_share >= 0.5 and len(e["states"]) >= 4 and len(e["states"]) / e["n"] >= 0.2:
            # H-1B dependency alone is NOT a staffing signal (Meta/NVIDIA/eBay are dependent);
            # combined with workers scattered across many states it usually is.
            reason = f"H-1B dependent + scattered worksites ({dep_share:.0%}, {len(e['states'])} states)"
        elif dep_share >= 0.5 and med < 115000:
            # dependent + wage near the prevailing-wage floor: typical of small body shops, rare for product cos
            reason = f"H-1B dependent + low median wage ({dep_share:.0%}, ${med:,.0f})"
        rec = {"employer": disp, "count": e["n"],
               "median_wage": round(statistics.median(e["wages"])) if e["wages"] else None,
               "top_titles": [t for t, _ in e["titles"].most_common(5)],
               "top_states": [s for s, _ in e["states"].most_common(3)], "n_states": len(e["states"]),
               "secondary_entity_share": round(sec_share, 3), "h1b_dependent_share": round(dep_share, 3),
               "name_variants": [k for k, _ in e["names"].most_common(4)], "key": key}
        (dropped if reason else keep).append(rec | ({"reason": reason} if reason else {}))
    keep.sort(key=lambda r: -r["count"])
    dropped.sort(key=lambda r: -r["count"])
    out = keep[: args.top]
    d = os.path.dirname(os.path.abspath(args.out))
    json.dump(out, open(args.out, "w"), indent=1)
    json.dump(dropped[:300], open(os.path.join(d, "excluded_staffing.json"), "w"), indent=1)
    json.dump(dict(stats, employers_total=len(emp), employers_kept=len(keep), employers_dropped=len(dropped),
                   lcas_kept=sum(r["count"] for r in keep), lcas_dropped=sum(r["count"] for r in dropped)),
              open(os.path.join(d, "parse_stats.json"), "w"), indent=1)
    print(f"kept {len(keep)} employers, dropped {len(dropped)}; wrote {len(out)} to {args.out}", file=sys.stderr)


# ---------------------------------------------------------------- stage 2
STRIP_SLUG = re.compile(r"\b(technologies|technology|tech|software|systems|labs|lab|platforms|international|global|"
                        r"enterprises|solutions|com|ai|io|management|operations|communications|markets|corporate|"
                        r"development|center|data|digital|wealth|financial)\b")


GENERIC_FIRST = set("national clear polar american united general first global international applied advanced "
                    "digital data cloud smart north south east west new open bank capital health medical "
                    "research science scientific people team effective perennial particle state best credit".split())


def slug_guesses(rec):
    """Up to 3 guesses, best first: joined core name, first word (if distinctive), hyphenated core name."""
    key = rec["key"]                      # already suffix-stripped, lowercase, spaces
    m = re.search(r"\b(?:d/?b/?a|dba)\b[\s.:]*(.+?)\)?$", rec["employer"], re.I)
    if m:                                 # "People Center, Inc. d/b/a Rippling" -> rippling
        key = norm_name(m.group(1)) or key
    words = [w for w in key.split() if len(w) > 1 or w.isdigit()]
    base = [w for w in STRIP_SLUG.sub(" ", " ".join(words)).split()] or words
    g = []
    if base:
        g.append("".join(base))
        if len(base) > 1 and len(base[0]) >= 4 and base[0] not in GENERIC_FIRST:
            g.append(base[0])             # first-word guess: riskier, sanity-checked / flagged
        if len(base) > 1:
            g.append("-".join(base))
    if words != base:
        g.append("".join(words))
    out = []
    for x in g:
        if x and x not in out:
            out.append(x)
    return out[:3]


class RateLimited(Exception):
    pass


def get_json(url):
    req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "job-radar-sponsor-probe/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, json.loads(r.read().decode("utf-8", "replace") or "null")
    except urllib.error.HTTPError as e:
        if e.code == 429:
            raise RateLimited(url)
        return e.code, None
    except Exception:
        return 0, None


ATS = {
    "greenhouse": "https://boards-api.greenhouse.io/v1/boards/{}/jobs",
    "ashby": "https://api.ashbyhq.com/posting-api/job-board/{}",
    "lever": "https://api.lever.co/v0/postings/{}?mode=json&limit=1",
}


def tokens(s):
    return {t for t in norm_name(s).split() if len(t) > 2}


def probe(args):
    sponsors = json.load(open(args.sponsors))
    boards = json.load(open(args.boards))
    have = {s.lower() for k in ("greenhouse", "ashby", "lever") for s in boards.get(k, [])}
    have_wd = " ".join(boards.get("workday", [])).lower()
    d = os.path.dirname(os.path.abspath(args.sponsors))
    found = {"greenhouse": [], "ashby": [], "lever": []}
    already, log = [], []
    todo = []                              # (round, idx, slug)
    for i, rec in enumerate(sponsors):
        gs = slug_guesses(rec)
        rec["_slugs"] = gs
        if any(s in have for s in gs) or (gs and gs[0] in have_wd):
            already.append(rec["employer"]); rec["_done"] = True
            continue
        for r, s in enumerate(gs):
            todo.append((r, i, s))
    todo.sort()
    nreq, stopped = 0, None
    try:
        for r, i, s in todo:
            rec = sponsors[i]
            if rec.get("_done"):
                continue
            for ats, tpl in ATS.items():
                if nreq >= args.cap:
                    raise StopIteration
                time.sleep(args.delay)
                code, data = get_json(tpl.format(s)); nreq += 1
                jobs = (data or {}).get("jobs") if ats != "lever" else data
                ok = code == 200 and isinstance(jobs, list) and len(jobs) > 0
                log.append([ats, s, code, len(jobs) if isinstance(jobs, list) else None])
                if not ok:
                    continue
                n = len(jobs)
                board_name = ""
                if ats == "greenhouse":
                    board_name = jobs[0].get("company_name") or ""
                    if not board_name:
                        time.sleep(args.delay)
                        c2, meta = get_json(f"https://boards-api.greenhouse.io/v1/boards/{s}"); nreq += 1
                        board_name = (meta or {}).get("name", "") if c2 == 200 else ""
                elif ats == "lever":
                    time.sleep(args.delay)
                    c2, full = get_json(f"https://api.lever.co/v0/postings/{s}?mode=json"); nreq += 1
                    n = len(full) if isinstance(full, list) else n
                locs = []
                for j in jobs[:50]:
                    if ats == "greenhouse":
                        locs.append(((j.get("location") or {}).get("name")) or "")
                    elif ats == "ashby":
                        locs.append(j.get("location") or "")
                    else:
                        locs.append(((j.get("categories") or {}).get("location")) or "")
                us = sum(1 for l in locs if re.search(r"\b(US|USA|United States|Remote|[A-Z]{2})\b|, (CA|NY|WA|TX|MA)", l or ""))
                et = tokens(rec["employer"])
                bt = tokens(board_name) if board_name else set()
                if board_name:
                    conf = "high" if et & bt else "low"
                else:
                    conf = "medium" if r == 0 else "low"   # ashby/lever expose no company name
                first_word = r > 0 and "-" not in s and len(rec["_slugs"]) > 1 and rec["_slugs"][0].startswith(s)
                if first_word and conf != "high":
                    conf = "low"
                if len(s) <= 4 and conf != "high":
                    conf = "low"
                found[ats].append({"slug": s, "employer": rec["employer"], "lca_count": rec["count"], "jobs": n,
                                   "board_name": board_name, "confidence": conf,
                                   "sample_locations": sorted(set(locs))[:5], "us_like_share": round(us / max(1, len(locs)), 2)})
                rec["_done"] = True
                print(f"MATCH {ats:10s} {s:30s} {n:4d} jobs  <- {rec['employer']} [{conf}] {board_name}", file=sys.stderr)
                break
    except StopIteration:
        stopped = f"request cap {args.cap} reached"
    except RateLimited as e:
        stopped = f"429 from {e}; stopped"
    unmatched = [{"employer": r["employer"], "count": r["count"], "median_wage": r["median_wage"],
                  "slugs_tried": r["_slugs"]} for r in sponsors if not r.get("_done")]
    json.dump(found, open(os.path.join(d, "new_boards.json"), "w"), indent=1)
    json.dump(unmatched, open(os.path.join(d, "unmatched.json"), "w"), indent=1)
    json.dump({"requests": nreq, "stopped": stopped, "already_in_boards": already, "log": log},
              open(os.path.join(d, "probe_log.json"), "w"), indent=1)
    print(f"requests={nreq} stopped={stopped} found=" + str({k: len(v) for k, v in found.items()}) +
          f" already={len(already)} unmatched={len(unmatched)}", file=sys.stderr)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    sp = ap.add_subparsers(dest="cmd", required=True)
    p = sp.add_parser("parse"); p.add_argument("xlsx"); p.add_argument("--out", default="sponsors.json")
    p.add_argument("--top", type=int, default=400)
    q = sp.add_parser("probe"); q.add_argument("sponsors"); q.add_argument("--boards", required=True)
    q.add_argument("--cap", type=int, default=1500); q.add_argument("--delay", type=float, default=0.6)
    a = ap.parse_args()
    parse(a) if a.cmd == "parse" else probe(a)
