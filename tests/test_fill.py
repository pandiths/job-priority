"""Fill routing, output folders and the extension API. Run: python3 -m unittest discover tests"""
import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import radar  # noqa: E402


class FillTarget(unittest.TestCase):
    def test_dedicated_fillers(self):
        self.assertEqual(radar.fill_target("https://job-boards.greenhouse.io/acme/jobs/123?gh_src=x")[0], "greenhouse")
        self.assertEqual(radar.fill_target("https://jobs.ashbyhq.com/acme/0e8a7e2c-2a4b-4c1e-9f7a-1b2c3d4e5f60"), ("ashby", "https://jobs.ashbyhq.com/acme/0e8a7e2c-2a4b-4c1e-9f7a-1b2c3d4e5f60/application"))
        self.assertEqual(radar.fill_target("https://jobs.lever.co/acme/0e8a7e2c-2a4b-4c1e-9f7a-1b2c3d4e5f60")[0], "lever")
        self.assertEqual(radar.fill_target("https://acme.wd5.myworkdayjobs.com/en-US/Careers/job/Seattle/MLE_R1?src=x"), ("workday", "https://acme.wd5.myworkdayjobs.com/en-US/Careers/job/Seattle/MLE_R1"))

    def test_other_sites_use_the_general_filler(self):
        self.assertEqual(radar.fill_target("https://lifeattiktok.com/search/123")[0], "generic")

    def test_aggregator_links_are_not_fillable(self):
        for url in ("https://jobright.ai/jobs/info/abc", "https://news.ycombinator.com/item?id=1", "https://www.linkedin.com/jobs/view/1"):
            self.assertIsNone(radar.fill_target(url), url)

    def test_greenhouse_on_a_company_site_goes_to_the_greenhouse_filler(self):
        with mock.patch.object(radar, "greenhouse_boards", return_value=frozenset({"acme"})):
            job = dict(company="Acme", url="https://acme.com/jobs/search?gh_jid=42", raw="{}")
            self.assertEqual(radar.fill_target_for(job), ("greenhouse", "https://job-boards.greenhouse.io/embed/job_app?for=acme&token=42"))
            job = dict(company="Other Co", url="https://other.com/jobs?gh_jid=42", raw=json.dumps({"board": "otherco"}))
            self.assertEqual(radar.fill_target_for(job)[1], "https://job-boards.greenhouse.io/embed/job_app?for=otherco&token=42")
            self.assertEqual(radar.fill_target_for(dict(company="Nobody", url="https://nobody.com/j?gh_jid=1", raw=""))[0], "generic")


class PrepPaths(unittest.TestCase):
    def test_roles_with_the_same_slug_get_separate_folders(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(radar, "OUTPUT", tmp):
            a = dict(id="acme|a", company="Acme", title="Machine Learning Engineer, Search Ranking")
            b = dict(id="acme|b", company="Acme", title="Machine Learning Engineer, Ads Ranking")
            name, out, report = radar.prep_paths(a)
            os.makedirs(out)
            with open(report, "w") as f:
                json.dump({"role": a["title"]}, f)
            self.assertEqual(radar.prep_paths(a)[0], name)
            self.assertNotEqual(radar.prep_paths(b)[0], name)
            self.assertTrue(radar.prep_paths(b)[0].startswith(name))


class ExtensionApi(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.makedirs(os.path.join(self.tmp.name, "extension"))
        self.patches = [mock.patch.object(radar, "D", self.tmp.name), mock.patch.object(radar, "EXT_TOKEN_FILE", os.path.join(self.tmp.name, "data", "ext_token"))]
        for p in self.patches:
            p.start()
        self.token = radar.ext_token()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), radar.Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def post(self, headers):
        req = urllib.request.Request(f"http://127.0.0.1:{self.server.server_port}/api/ext/jobs", data=b'{"url":"https://acme.com/apply","title":"Apply"}', headers={"Content-Type": "application/json", **headers})
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.load(r)
        except urllib.error.HTTPError as e:
            return e.code, None

    def test_token_is_written_to_the_extension_config(self):
        with open(os.path.join(self.tmp.name, "extension", "config.js")) as f:
            self.assertIn(self.token, f.read())

    def test_requests_without_the_token_or_from_a_web_page_are_refused(self):
        self.assertEqual(self.post({"Origin": "chrome-extension://abc"})[0], 403)
        self.assertEqual(self.post({"Origin": "chrome-extension://abc", "X-JR-Token": "wrong"})[0], 403)
        self.assertEqual(self.post({"Origin": "https://evil.example", "X-JR-Token": self.token})[0], 403)

    def test_the_extension_gets_matching_resumes_first(self):
        jobs = [dict(id="x|1", company="Other", title="Data Engineer", url="https://other.com/1", prepared=True),
                dict(id="acme|2", company="Acme", title="ML Engineer", url="https://acme.com/2", prepared=True),
                dict(id="acme|3", company="Acme", title="Not prepared", url="https://acme.com/3", prepared=False)]
        with mock.patch.object(radar, "visible", return_value=jobs), mock.patch.object(radar, "db"), mock.patch.object(radar, "prep_status", return_value={"state": "ready", "passed": True}):
            code, body = self.post({"Origin": "chrome-extension://abc", "X-JR-Token": self.token})
        self.assertEqual(code, 200)
        self.assertEqual([j["id"] for j in body], ["acme|2", "x|1", ""])


if __name__ == "__main__":
    unittest.main()
