"""Recruiter outreach: Apollo calls, send rules and follow-ups, with Apollo and Gmail replaced by fakes."""
import datetime as dt
import io
import json
import os
import sqlite3
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import outreach  # noqa: E402

JOB = dict(id="acme|mle", company="Acme", title="ML Engineer", url="https://job-boards.greenhouse.io/acme/jobs/1")


def database():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript(outreach.SCHEMA + "CREATE TABLE followups(company TEXT, role TEXT, at TEXT);")
    return c


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


def opener(payload, seen):
    def open_(req, timeout=None):
        seen.append(req)
        return FakeResponse(json.dumps(payload).encode())
    return open_


class FakeSMTP:
    sent = []

    def __init__(self, host, port, timeout=None):
        self.host = host

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass

    def login(self, user, password):
        self.user = user

    def send_message(self, msg):
        FakeSMTP.sent.append(msg)


@mock.patch.object(outreach, "keychain", lambda service: ("me@example.com", "secret"))
class Outreach(unittest.TestCase):
    def setUp(self):
        self.c = database()
        FakeSMTP.sent = []

    def add_contact(self, **over):
        row = dict(person_id="p1", job_id=JOB["id"], company="Acme", first_name="Ana", name="Ana Diaz", title="Technical Recruiter",
                   has_email=1, linkedin="https://linkedin.com/in/ana", email="ana@acme.com", email_status="verified", revealed_at="x")
        row.update(over)
        self.c.execute(f"INSERT INTO contacts({','.join(row)}) VALUES({','.join('?' * len(row))})", list(row.values()))
        return dict(self.c.execute("SELECT * FROM contacts WHERE person_id=?", (row["person_id"],)).fetchone())

    def test_domain_guess(self):
        self.assertEqual(outreach.guess_domain("Acme", "https://job-boards.greenhouse.io/acme/jobs/1"), "acme.com")
        self.assertEqual(outreach.guess_domain("Airbnb", "https://careers.airbnb.com/positions/1"), "airbnb.com")
        self.assertEqual(outreach.guess_domain("Monzo", "https://jobs.monzo.co.uk/x"), "monzo.co.uk")

    def test_find_is_a_free_search_and_ranks_technical_recruiters_first(self):
        seen = []
        people = [dict(id="a", first_name="Bo", title="Recruiter", has_email=True), dict(id="b", first_name="Cy", title="Technical Recruiter", has_email=True)]
        rows = outreach.find(self.c, JOB, domain="acme.com", opener=opener(dict(people=people), seen))
        self.assertIn("mixed_people/api_search", seen[0].full_url)
        self.assertIn("q_organization_domains_list%5B%5D=acme.com", seen[0].full_url)
        self.assertEqual(seen[0].headers["X-api-key"], "secret")
        self.assertEqual([r["person_id"] for r in rows], ["b", "a"])

    def test_company_name_search_is_free_when_name_dot_com_matches(self):
        seen = []
        people = [dict(id="a", first_name="Bo", title="Recruiter", has_email=True, organization=dict(name="Stripe, Inc."))]
        r = outreach.find_by_name(self.c, outreach.company_job("Stripe"), "Stripe", opener(dict(people=people), seen))
        self.assertEqual(r["searched"], "stripe.com")
        self.assertEqual([x["person_id"] for x in r["contacts"]], ["a"])
        self.assertEqual(len(seen), 1)

    def test_a_different_company_at_the_guessed_domain_is_not_used(self):
        people = [dict(id="z", first_name="Zed", title="Recruiter", organization=dict(name="Scale Fitness"))]
        r = outreach.find_by_name(self.c, outreach.company_job("Scale AI"), "Scale AI", opener(dict(people=people), []))
        self.assertTrue(r["need_lookup"])
        self.assertEqual(self.c.execute("SELECT count(*) FROM contacts").fetchone()[0], 0)

    def test_a_typed_website_is_searched_directly(self):
        seen = []
        outreach.find_by_name(self.c, outreach.company_job("scale.com"), "https://www.scale.com/", opener(dict(people=[]), seen))
        self.assertIn("q_organization_domains_list%5B%5D=scale.com", seen[0].full_url)

    def test_sync_copies_saved_apollo_contacts_without_credits(self):
        seen = []
        saved = [dict(id="k1", person_id="p9", first_name="Lee", name="Lee Park", title="Technical Recruiter", email="lee@acme.com", email_status="verified",
                      linkedin_url="https://linkedin.com/in/lee", organization_name="Acme", created_at="2026-11-04T10:00:00Z")]
        self.assertEqual(outreach.sync_contacts(self.c, "L1", opener(dict(contacts=saved), seen)), 1)
        self.assertIn("contacts/search", seen[0].full_url)
        self.assertEqual(json.loads(seen[0].data)["contact_label_ids"], ["L1"])
        self.assertEqual(outreach.saved_companies(self.c)[0]["company"], "Acme")
        row = outreach.contacts(self.c, dict(company="ACME Inc."))[0]
        self.assertEqual((row["email"], row["linkedin"]), ("lee@acme.com", "https://linkedin.com/in/lee"))

    def test_reveal_saves_email_and_linkedin(self):
        self.add_contact(email="", email_status="", linkedin="", name="")
        person = dict(name="Ana Diaz", linkedin_url="https://linkedin.com/in/ana", email="ana@acme.com", email_status="verified", title="Recruiter")
        seen = []
        r = outreach.reveal(self.c, "p1", opener(dict(person=person), seen))
        self.assertIn("people/match", seen[0].full_url)
        self.assertIn("reveal_personal_emails=false", seen[0].full_url)
        self.assertEqual((r["email"], r["linkedin"]), ("ana@acme.com", "https://linkedin.com/in/ana"))

    def test_send_records_and_uses_the_job_search_account(self):
        contact = self.add_contact()
        r = outreach.send(self.c, JOB, contact, "ML Engineer role", "Hi Ana", sender_name="Jane Doe", smtp=FakeSMTP)
        self.assertTrue(r["ok"])
        msg = FakeSMTP.sent[0]
        self.assertEqual((msg["From"], msg["To"]), ("Jane Doe <me@example.com>", "ana@acme.com"))
        self.assertEqual(len(outreach.history(self.c)), 1)

    def test_blocked_network_saves_to_gmail_drafts(self):
        class Blocked(FakeSMTP):
            def __enter__(self):
                raise outreach.smtplib.SMTPServerDisconnected("Connection unexpectedly closed")
        saved = []

        class FakeIMAP:
            def __init__(self, host, port):
                pass

            def login(self, user, password):
                pass

            def append(self, box, flags, when, data):
                saved.append((box, flags, data))
                return "OK", [b""]

            def logout(self):
                pass
        r = outreach.send(self.c, JOB, self.add_contact(), "ML Engineer role", "Hi Ana", smtp=Blocked, imap=FakeIMAP)
        self.assertEqual(r["via"], "draft")
        self.assertIn("Drafts", r["note"])
        self.assertEqual(saved[0][0], '"[Gmail]/Drafts"')
        self.assertIn(b"To: ana@acme.com", saved[0][2])
        self.assertEqual(outreach.history(self.c)[0]["via"], "draft")

    def test_send_rules(self):
        contact = self.add_contact()
        self.assertIn("empty", outreach.check_send(self.c, contact, "", "body"))
        self.assertIn("not verified", outreach.check_send(self.c, dict(contact, email_status="guessed"), "s", "b"))
        self.assertIsNone(outreach.check_send(self.c, dict(contact, email_status="guessed"), "s", "b", allow_unverified=True))
        outreach.send(self.c, JOB, contact, "s", "b", smtp=FakeSMTP)
        self.assertIn("Follow up", outreach.check_send(self.c, contact, "s", "b"))
        self.assertEqual(len(FakeSMTP.sent), 1)

    def test_daily_limit(self):
        today = dt.date.today().isoformat()
        for i in range(outreach.MAX_SENDS_PER_DAY):
            self.c.execute("INSERT INTO outreach(person_id, email, sent_at) VALUES(?,?,?)", (f"x{i}", f"x{i}@a.com", today + "T09:00:00"))
        self.assertIn("Already sent", outreach.check_send(self.c, self.add_contact(), "s", "b"))

    def test_follow_up_after_five_days_until_marked(self):
        contact = self.add_contact()
        outreach.send(self.c, JOB, contact, "s", "b", smtp=FakeSMTP)
        later = dt.date.today() + dt.timedelta(days=outreach.FOLLOW_UP_DAYS)
        due = outreach.due_followups(self.c, later)
        self.assertEqual([(d["kind"], d["name"]) for d in due], [("cold email", "Ana Diaz")])
        self.c.execute("INSERT INTO followups VALUES(?,?,?)", ("Acme", due[0]["role"], later.isoformat()))
        self.assertEqual(outreach.due_followups(self.c, later), [])

    def test_lists_use_the_search_key_until_a_lists_key_is_saved(self):
        used = []
        with mock.patch.object(outreach, "keychain", lambda s: (used.append(s), (None, "k") if s == outreach.SEARCH_KEY else (None, None))[1]):
            outreach.apollo("labels", opener=opener([], []), method="GET", key_service=outreach.LISTS_KEY)
        self.assertEqual(used, [outreach.LISTS_KEY, outreach.SEARCH_KEY])

    def test_missing_keys_explain_what_to_do(self):
        with mock.patch.object(outreach, "keychain", lambda s: (None, None)):
            with self.assertRaisesRegex(RuntimeError, "apollo-api-key"):
                outreach.apollo("mixed_people/api_search", {})
            with self.assertRaisesRegex(RuntimeError, "apollo-lists-key"):
                outreach.lists()  # no lists key and no search key to fall back to
            r = outreach.send(self.c, JOB, self.add_contact(), "s", "b", smtp=FakeSMTP)
            self.assertIn("job-radar-gmail", r["error"])


if __name__ == "__main__":
    unittest.main()
