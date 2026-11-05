"""Recruiter outreach: find recruiters with Apollo, draft a note with Claude, send it from the job-search Gmail.

Nothing is sent without the user pressing Send on that one email. Apollo's people search is free; revealing a
person's email and LinkedIn costs Apollo credits, so it happens only per person, on request.
Keys live in macOS Keychain: apollo-api-key (Apollo API key), job-radar-gmail (account = Gmail address,
password = a Google app password).
"""
import datetime as dt
import imaplib
import json
import re
import smtplib
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from email.message import EmailMessage
from email.utils import make_msgid

APOLLO = "https://api.apollo.io/api/v1"
RECRUITER_TITLES = ["recruiter", "technical recruiter", "talent acquisition", "talent partner", "sourcer", "university recruiter", "recruiting"]
# Engineering-focused recruiters first.
TITLE_RANK = re.compile(r"technical|engineering|software|machine learning|\bml\b|\bai\b|tech", re.I)
ATS_HOSTS = re.compile(r"(greenhouse\.io|ashbyhq\.com|lever\.co|myworkdayjobs\.com|smartrecruiters\.com|workable\.com|icims\.com|"
                       r"jobvite\.com|rippling\.com|applytojob\.com|oraclecloud\.com|jobright\.ai|linkedin\.com|ycombinator\.com|"
                       r"bamboohr\.com|recruitee\.com|teamtailor\.com|dover\.com|pinpointhq\.com|avature\.net|eightfold\.ai)$")
MAX_SENDS_PER_DAY = 20
RESEND_AFTER_DAYS = 30
FOLLOW_UP_DAYS = 5

SCHEMA = """
CREATE TABLE IF NOT EXISTS contacts(person_id TEXT PRIMARY KEY, job_id TEXT, company TEXT, first_name TEXT, name TEXT,
  title TEXT, has_email INT, linkedin TEXT, email TEXT, email_status TEXT, revealed_at TEXT);
CREATE TABLE IF NOT EXISTS outreach(id INTEGER PRIMARY KEY, person_id TEXT, job_id TEXT, company TEXT, role TEXT,
  email TEXT, subject TEXT, body TEXT, sent_at TEXT, message_id TEXT, via TEXT);
"""


def migrate(c):
    if "via" not in [r[1] for r in c.execute("PRAGMA table_info(outreach)")]:
        c.execute("ALTER TABLE outreach ADD COLUMN via TEXT")


def keychain(service):
    """(account, secret) for a Keychain item, or (None, None)."""
    attrs = subprocess.run(["security", "find-generic-password", "-s", service], capture_output=True, text=True).stdout
    acct = re.search(r'"acct"<blob>="([^"]*)"', attrs)
    secret = subprocess.run(["security", "find-generic-password", "-s", service, "-w"], capture_output=True, text=True).stdout.rstrip("\n")
    return (acct.group(1) if acct else None), (secret or None)


def guess_domain(company, url):
    """The company's web domain: the posting's own site when it isn't an applicant-tracking host, else company.com."""
    host = urllib.parse.urlparse(url).netloc.lower().split(":")[0]
    if host and not ATS_HOSTS.search(host):
        parts = host.split(".")
        return ".".join(parts[-3:]) if len(parts) > 2 and len(parts[-1]) == 2 and parts[-2] in ("co", "com") else ".".join(parts[-2:])
    return re.sub(r"[^a-z0-9]", "", company.lower()) + ".com"


def apollo(path, params, opener=urllib.request.urlopen):
    _, key = keychain("apollo-api-key")
    if not key:
        raise RuntimeError("No Apollo API key. Create one in Apollo (Settings > Integrations > API) and run: security add-generic-password -a apollo -s apollo-api-key -w")
    req = urllib.request.Request(f"{APOLLO}/{path}?{urllib.parse.urlencode(params, doseq=True)}", data=b"{}", method="POST",
                                 headers={"x-api-key": key, "Content-Type": "application/json", "Cache-Control": "no-cache", "Accept": "application/json"})
    try:
        with opener(req, timeout=30) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Apollo said {e.code}: {e.read().decode(errors='replace')[:200]}")


def find(c, job, domain, opener=urllib.request.urlopen):
    """Free Apollo search for recruiters at the domain. Saves and returns them, engineering recruiters first."""
    r = apollo("mixed_people/api_search", {"person_titles[]": RECRUITER_TITLES, "q_organization_domains_list[]": [domain], "per_page": 25, "page": 1}, opener)
    people = r.get("people") or []
    for p in people:
        c.execute("INSERT INTO contacts(person_id, job_id, company, first_name, title, has_email) VALUES(?,?,?,?,?,?) "
                  "ON CONFLICT(person_id) DO UPDATE SET title=excluded.title, has_email=excluded.has_email",
                  (p["id"], job["id"], job["company"], p.get("first_name") or "", p.get("title") or "", int(bool(p.get("has_email")))))
    c.commit()
    return contacts(c, job, [p["id"] for p in people])


def contacts(c, job, ids=None):
    rows = [dict(r) for r in c.execute("SELECT * FROM contacts WHERE company=?", (job["company"],))]
    if ids is not None:
        rows = [r for r in rows if r["person_id"] in ids or r["email"]]
    sent = {r["person_id"]: r["sent_at"] for r in c.execute("SELECT person_id, max(sent_at) sent_at FROM outreach GROUP BY person_id")}
    for r in rows:
        r["sent_at"] = sent.get(r["person_id"])
    rows.sort(key=lambda r: (not r["email"], not TITLE_RANK.search(r["title"] or ""), not r["has_email"], r["first_name"]))
    return rows


def reveal(c, person_id, opener=urllib.request.urlopen):
    """Apollo enrichment for one person: full name, LinkedIn, work email. Costs Apollo credits (about 1 for an email)."""
    p = apollo("people/match", {"id": person_id, "reveal_personal_emails": "false"}, opener).get("person") or {}
    c.execute("UPDATE contacts SET name=?, linkedin=?, email=?, email_status=?, title=coalesce(?, title), revealed_at=? WHERE person_id=?",
              (p.get("name") or "", p.get("linkedin_url") or "", p.get("email") or "", p.get("email_status") or "", p.get("title"),
               dt.datetime.now().isoformat(timespec="seconds"), person_id))
    c.commit()
    return dict(c.execute("SELECT * FROM contacts WHERE person_id=?", (person_id,)).fetchone())


def check_send(c, contact, subject, body, allow_unverified=False, today=None):
    """Why this email must not be sent, or None."""
    today = today or dt.date.today()
    if not contact or not contact["email"]:
        return "Reveal the recruiter's email first."
    if not subject.strip() or not body.strip():
        return "The subject and message can't be empty."
    if contact["email_status"] != "verified" and not allow_unverified:
        return f"Apollo marks this email as {contact['email_status'] or 'unknown'}, not verified. Tick 'send anyway' if you're sure."
    sent_today = c.execute("SELECT count(*) FROM outreach WHERE substr(sent_at,1,10)=?", (today.isoformat(),)).fetchone()[0]
    if sent_today >= MAX_SENDS_PER_DAY:
        return f"Already sent {MAX_SENDS_PER_DAY} cold emails today. More can hurt the address's reputation; send tomorrow."
    last = c.execute("SELECT max(sent_at) FROM outreach WHERE lower(email)=lower(?)", (contact["email"],)).fetchone()[0]
    if last and (today - dt.date.fromisoformat(last[:10])).days < RESEND_AFTER_DAYS:
        return f"You emailed {contact['email']} on {last[:10]}. Use the Follow up panel instead of a new cold email."
    return None


def save_draft(msg, account, password, imap=imaplib.IMAP4_SSL):
    """Puts the message in the Gmail Drafts folder, for networks that block sending mail directly."""
    m = imap("imap.gmail.com", 993)
    try:
        m.login(account, password)
        typ, data = m.append('"[Gmail]/Drafts"', "\\Draft", None, msg.as_bytes())
        if typ != "OK":
            raise RuntimeError(f"Gmail didn't save the draft: {data}")
    finally:
        m.logout()


def send(c, job, contact, subject, body, attachment=None, sender_name="", allow_unverified=False, smtp=smtplib.SMTP_SSL, imap=imaplib.IMAP4_SSL):
    """Sends through Gmail SMTP. If the network blocks SMTP, saves the email to Gmail Drafts instead."""
    why = check_send(c, contact, subject, body, allow_unverified)
    if why:
        return dict(ok=False, error=why)
    account, app_password = keychain("job-radar-gmail")
    app_password = (app_password or "").replace(" ", "")  # Google shows app passwords in groups of four
    if not (account and app_password):
        return dict(ok=False, error="No Gmail app password saved. Create one for your job-search Gmail and run: security add-generic-password -a YOUR_GMAIL -s job-radar-gmail -w")
    msg = EmailMessage()
    msg["From"] = f"{sender_name} <{account}>" if sender_name else account
    msg["To"] = contact["email"]
    msg["Subject"] = subject.strip()
    msg["Message-ID"] = make_msgid(domain=account.split("@")[-1])
    msg.set_content(body.strip())
    if attachment:
        with open(attachment, "rb") as f:
            msg.add_attachment(f.read(), maintype="application", subtype="pdf", filename=attachment.rsplit("/", 1)[-1])
    via = "sent"
    try:
        with smtp("smtp.gmail.com", 465, timeout=20) as s:
            s.login(account, app_password)
            s.send_message(msg)
    except (smtplib.SMTPServerDisconnected, smtplib.SMTPConnectError, TimeoutError, ConnectionError, OSError) as e:
        if isinstance(e, smtplib.SMTPAuthenticationError):
            raise
        save_draft(msg, account, app_password, imap)
        via = "draft"
    c.execute("INSERT INTO outreach(person_id, job_id, company, role, email, subject, body, sent_at, message_id, via) VALUES(?,?,?,?,?,?,?,?,?,?)",
              (contact["person_id"], job["id"], job["company"], job["title"], contact["email"], subject.strip(), body.strip(),
               dt.datetime.now().isoformat(timespec="seconds"), msg["Message-ID"], via))
    c.commit()
    if via == "draft":
        return dict(ok=True, via=via, sent_from=account, note=f"This network blocks sending mail, so it's saved in {account} Drafts. Open Gmail and press Send.")
    return dict(ok=True, via=via, sent_from=account)


def history(c):
    return [dict(r) for r in c.execute("SELECT o.*, k.name, k.title, k.linkedin FROM outreach o LEFT JOIN contacts k USING(person_id) ORDER BY sent_at DESC")]


def due_followups(c, today=None):
    """Cold emails with no follow-up recorded, FOLLOW_UP_DAYS or more after sending."""
    today = today or dt.date.today()
    out = []
    for r in history(c):
        days = (today - dt.date.fromisoformat(r["sent_at"][:10])).days
        followed = c.execute("SELECT 1 FROM followups WHERE company=? AND role=? AND at>?", (r["company"], f"cold email: {r['email']}", r["sent_at"])).fetchone()
        if days >= FOLLOW_UP_DAYS and not followed:
            out.append(dict(kind="cold email", company=r["company"], role=f"cold email: {r['email']}", job=r["role"], name=r["name"], days=days, at=r["sent_at"]))
    return out
