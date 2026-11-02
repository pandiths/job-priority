"""Interview-chance score: a transparent 0-100 heuristic, not a measured probability.
No outcome data exists yet, so the weights are judgement calls; every factor is shown in the UI."""
import csv, datetime as dt, json, os, re, sys

HERE = os.path.dirname(os.path.abspath(__file__))
# Personal files (resume, tracker, form fillers) stay outside this repo, in the parent folder by default.
JOBSEARCH = os.environ.get("JOBSEARCH_HOME", os.path.dirname(HERE))
sys.path.insert(0, HERE)
from ats_score import score as ats, text_of  # noqa: E402

WEIGHTS = dict(fit=30, timing=20, level=15, sponsor=15, brand=15, location=5)
APPLY_NOW, STRONG = 82, 62

TIER1 = "google meta apple microsoft amazon nvidia netflix openai anthropic tiktok bytedance databricks stripe uber airbnb linkedin snowflake xai waymo".split()
TIER2 = "salesforce adobe snap snapchat pinterest coinbase figma doordash lyft robinhood reddit instacart cloudflare datadog discord dropbox roblox scale scaleai cursor cognition perplexity notion ramp plaid rippling rubrik mongodb elastic cisco intel paypal qualcomm walmart capitalone hudsonrivertrading janestreet jumptrading cerebras togetherai anyscale glean harvey sierra palantir block samsara twilio atlassian workday expedia".split()
# Companies with a recent rejection, from the private profile; applying again now rarely reaches a recruiter.
_PROFILE = os.path.join(HERE, "data", "profile.json")
COOLDOWN = json.load(open(_PROFILE)).get("cooldown", {}) if os.path.exists(_PROFILE) else {}

BLOCKS = [
    ("US citizens only", r"u\.?s\.? citizen(ship)? (is )?required|must be a u\.?s\.? citizen|only u\.?s\.? citizens"),
    ("Security clearance", r"security clearance|ts/sci|secret clearance|\bpolygraph\b"),
        ("Export control", r"\bitar\b|export control.{0,40}u\.?s\.? person"),
    ("PhD required", r"requires? a ph\.?d|must have a ph\.?d|ph\.?d\.? (is |degree )?(required|only)"),
    ("Looks like a PERM ad", r"\(\d+\) years in the job offered|any suitable combination"),
]
SPONSOR_WORD = re.compile(r"sponsor|h-?1b", re.I)
NEGATION = re.compile(r"\b(not|no|unable|cannot|can't|won't|without|aren't|isn't|doesn't|don't|never)\b", re.I)
VISA_QUESTION = re.compile(r"\?|\bwhether\b|\bwill you\b|\bdo you\b|\bif you\b", re.I)
GRAD_ONLY = re.compile(r"graduat\w*\s+(in|by|between|from)?[^.\n]{0,40}(december 2026|winter 2026|spring 2027|2027)|expected graduation[^.\n]{0,40}2027", re.I)
# Domains the resume has no real depth in; keyword overlap alone overstates the fit for these.
SPECIALIST = re.compile(r"recommend|vlsi|neuromorphic|elixir|telephony|onboard|perception|robot|autonomy|\bgpu\b|compiler|kernel|driver|graphics|\bux\b|test frameworks|storage|network|security|device|\bc\+\+|rust\b|sensing|middleware|embedded|vision|chromeos|game|engine scalab|payments? network", re.I)
CORE = re.compile(r"machine learning|\bml\b|\bmle\b|\bai\b|\bllm|agent|forward deployed|genai|applied", re.I)
YEARS = re.compile(r"(\d{1,2})\s*(?:\+|-\s*\d+|to\s+\d+|or more)?\s*\+?\s*years?(?:\s+of)?[^.\n;]{0,70}?(?:experience|engineering|software|industry|professional|building|development|developing)", re.I)
YEARS_MS = re.compile(r"(\d{1,2})\s*\+?\s*years?[^.\n;]{0,60}?(?:master|m\.s\.|\bms\b|graduate degree|advanced degree)|(?:master|m\.s\.|\bms\b|graduate degree|advanced degree)[^.\n;]{0,60}?(\d{1,2})\s*\+?\s*years?", re.I)
PREFERRED = re.compile(r"preferred|nice to have|bonus|pluses|benefits|compensation|pay range|equal opportunity", re.I)
REQ_HEAD = re.compile(r"minimum qualifications|basic qualifications|required qualifications|requirements|what you.ll need|what you need|what you.ll bring|what we.re looking for|you have|who you are|about you|qualifications", re.I)
ABBR = {"mle": "machine learning engineer", "swe": "software engineer", "sde": "software engineer", "fde": "forward deployed engineer", "fdse": "forward deployed software engineer", "ml": "machine learning", "eng": "engineer", "sre": "site reliability engineer"}


def norm(s):
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def company_key(s):
    return re.sub(r"(inc|llc|corp|corporation|technologies|technology|labs|ai|careers|usa)$", "", norm(s)) or norm(s)


def tokens(title):
    words = re.findall(r"[a-z0-9]+", re.sub(r"\(.*?\)", " ", (title or "").lower()))
    out = set()
    for w in words:
        out |= set(ABBR.get(w, w).split())
    return out - {"and", "the", "of", "at", "i", "a"}


def load_tracker():
    path = os.path.join(JOBSEARCH, "applications.csv")
    rows = [r for r in csv.DictReader(open(path)) if r.get("status") != "SKIPPED"]
    by = {}
    for r in rows:
        by.setdefault(company_key(r["company"]), []).append(tokens(r["role"]))
    return by


def tracker_rows():
    """Every tracker row with its position, grouped by company, for looking up a job's current stage."""
    by = {}
    with open(os.path.join(JOBSEARCH, "applications.csv")) as f:
        for i, r in enumerate(csv.DictReader(f)):
            by.setdefault(company_key(r["company"]), []).append(dict(r, i=i, tokens=tokens(r["role"])))
    return by


def tracker_row(company, title, by):
    key = company_key(company)
    rows = by.get(key)
    if rows is None:
        rows = [r for k, v in by.items() if len(k) >= 4 and len(key) >= 4 and (k.startswith(key) or key.startswith(k)) for r in v]
    t = tokens(title)
    best = [r for r in rows if t and r["tokens"] and len(t & r["tokens"]) / max(len(t), len(r["tokens"])) >= 0.75]
    return best[-1] if best else None


def tracker_match(company, title, tracker):
    """(already applied to this role, number of tracked roles at this company)."""
    key = company_key(company)
    roles = tracker.get(key)
    if roles is None:
        hits = [k for k in tracker if len(k) >= 4 and len(key) >= 4 and (k.startswith(key) or key.startswith(k))]
        roles = [r for k in hits for r in tracker[k]]
    t = tokens(title)
    same = any(r and len(t & r) / max(len(t), len(r)) >= 0.75 for r in roles if t)
    return same, len(roles)


def required_years(text):
    body = text
    m = PREFERRED.search(text, 400) if len(text) > 400 else None
    if m and REQ_HEAD.search(text[:m.start()]):
        body = text[:m.start()]
    yrs = [int(x.group(1)) for x in YEARS.finditer(body) if int(x.group(1)) < 15]
    if not yrs:
        return None
    alt = [int(g) for x in YEARS_MS.finditer(body) for g in x.groups() if g and int(g) < 15]
    return min(alt) if alt else max(yrs)


def sponsor_signal(text):
    """Sentence-level, because a bare "H-1B" often sits inside a refusal (Roblox) and a refusal can follow a yes (Anthropic)."""
    yes = no = False
    for sent in re.split(r"(?<=[.!?])\s+|\n+", text or ""):
        if not SPONSOR_WORD.search(sent) or VISA_QUESTION.search(sent):
            continue
        if NEGATION.search(sent):
            no = True
        elif re.search(r"\b(we|will|can|do|does|offer|provide|available|support)\b", sent, re.I):
            yes = True
    return "yes" if yes else "no" if no else ""


def blocked(job, years):
    text = job.get("text") or ""
    for reason, pat in BLOCKS:
        if re.search(pat, text, re.I):
            return reason
    if sponsor_signal(text) == "no":
        return "No visa sponsorship"
    if re.search(r"public sector|federal|government|\bdod\b", job.get("title") or "", re.I):
        return "Public sector role, usually citizens only"
    if GRAD_ONLY.search(text) and not re.search(r"recent graduate", text, re.I):
        return "For students graduating later"
    return ""


def to_dt(v):
    if v is None or v == "":
        return None
    try:
        if isinstance(v, (int, float)):
            return dt.datetime.fromtimestamp(v / 1000 if v > 1e11 else v, dt.timezone.utc)
        s = str(v).replace("Z", "+00:00")
        d = dt.datetime.fromisoformat(s)
        return d if d.tzinfo else d.replace(tzinfo=dt.timezone.utc)
    except ValueError:
        return None


def timing_score(posted, applicants, tags):
    if posted:
        days = max(0, (dt.datetime.now(dt.timezone.utc) - posted).total_seconds() / 86400)
        s = 100 if days < 1 else 90 if days < 3 else 70 if days < 7 else 45 if days < 14 else 25 if days < 30 else 8
    else:
        s = 35
    if applicants is not None:
        s += 10 if applicants < 25 else 0 if applicants < 100 else -15 if applicants < 300 else -30
    if any("early applicant" in str(t).lower() for t in tags or []):
        s += 10
    return max(0, min(100, s))


def level_score(years, title, seniority):
    if re.search(r"2027", title or ""):
        return 40
    if re.search(r"\bII\b|\b2\b|mid[- ]level", title or "") or (seniority or "").lower().startswith("mid"):
        return 100
    if years is None:
        return 65
    return {0: 70, 1: 80, 2: 100, 3: 90, 4: 70, 5: 50, 6: 35}.get(years, 20)


def brand_score(company):
    k = company_key(company)
    if k in TIER1 or any(k.startswith(t) for t in TIER1 if len(t) > 4):
        return 100, 1
    if k in TIER2:
        return 75, 2
    return 40, 3


def sponsor_score(job, tier):
    if sponsor_signal(job.get("text")) == "yes":
        return 100, "Job text says it sponsors"
    if any("h1b" in str(t).lower() for t in job.get("tags") or []):
        return 85, "Jobright: H-1B sponsor likely"
    if tier == 1:
        return 80, "Large company known to sponsor"
    if tier == 2:
        return 70, "Established company that commonly sponsors"
    if job.get("jobright"):
        return 70, "Came through Jobright's H-1B filter"
    return 45, "Not stated; ask before investing time"


def fit_score(job, resume_text):
    text = job.get("text") or ""
    if len(text) < 150:
        return 45, dict(score=None, max_honest_score=None, missing_claimable=[], missing_not_claimable=[], thin=True)
    r = ats(resume_text, text)
    required_missing = [t for t in r["missing_not_claimable"] if t in r["required_terms"]]
    s = r["max_honest_score"] - 8 * len(required_missing)
    if r["jd_terms"] < 5:
        s = min(s, 65)
    if job.get("skill") is not None:
        s = 0.7 * s + 30 * float(job["skill"])
    title = job.get("title") or ""
    s *= 0.55 if SPECIALIST.search(title) else 1.0 if CORE.search(title) else 0.9
    r["required_missing"] = required_missing
    return max(0, min(100, s)), r


def salary_text(job):
    if job.get("salary"):
        return str(job["salary"])[:60]
    vals = [int(a) * 1000 + int(b) for a, b in re.findall(r"\$\s?(\d{2,3}),(\d{3})", job.get("text") or "")]
    vals = [v for v in vals if v >= 50000]
    if not vals:
        return ""
    lo, hi = min(vals), max(vals)
    return f"${lo // 1000}k" if lo == hi else f"${lo // 1000}k–${hi // 1000}k"


def requirements_excerpt(text):
    m = REQ_HEAD.search(text or "")
    if not m:
        return (text or "")[:900]
    tail = text[m.start():]
    end = PREFERRED.search(tail, 60)
    return tail[: end.start() if end else 1200][:1200]


def evaluate(job, resume_text, tracker):
    text = job.get("text") or ""
    years = required_years(text) if len(text) >= 150 else None
    if years is None and job.get("years") is not None:
        years = int(job["years"])
    posted = to_dt(job.get("posted"))
    brand, tier = brand_score(job["company"])
    sponsor, sponsor_why = sponsor_score(job, tier)
    fit, ats_r = fit_score(job, resume_text)
    parts = dict(fit=fit, timing=timing_score(posted, job.get("applicants"), job.get("tags")), level=level_score(years, job["title"], job.get("seniority")), sponsor=sponsor, brand=brand, location=100 if re.search(r"remote|california|, ca\b|san francisco|bay area|sunnyvale|san jose|palo alto|mountain view|los angeles", job.get("location") or "", re.I) else 70)
    total = sum(WEIGHTS[k] * parts[k] / 100 for k in WEIGHTS)
    applied, at_company = tracker_match(job["company"], job["title"], tracker)
    cooldown = next((why for k, why in COOLDOWN.items() if company_key(job["company"]) == k), "")
    if cooldown:
        total -= 15
    if at_company >= 3:
        total -= 5
    return dict(score=round(max(0, total), 1), parts={k: round(v) for k, v in parts.items()}, years=years, tier=tier, sponsor_why=sponsor_why, ats=ats_r.get("score"), ats_max=ats_r.get("max_honest_score"), missing=ats_r.get("missing_claimable", [])[:10], cannot_claim=ats_r.get("missing_not_claimable", [])[:10], required_missing=ats_r.get("required_missing", []), thin_jd=bool(ats_r.get("thin")), summary_only=bool(job.get("jobright")) and not any(x in job.get("sources", []) for x in ("Greenhouse", "Ashby", "Lever", "Workday")), block=blocked(job, years), applied=applied or bool(job.get("applied")), at_company=at_company, cooldown=cooldown, salary=salary_text(job), posted=posted.isoformat() if posted else None, requirements=requirements_excerpt(text))
