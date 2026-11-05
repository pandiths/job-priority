import assert from "node:assert/strict";
import { test } from "node:test";
import { draftEmail, SCHEMA, SYSTEM } from "../outreach_draft.mjs";

const D = { factBank: "FACTS", neverClaim: ["Twilio"], profile: "PROFILE", resume: "RESUME", jd: "JD-TEXT", company: "Acme", title: "MLE", url: "https://acme.com/j/1", recruiter: { name: "Ana Diaz", title: "Technical Recruiter" }, attach: true };

test("facts, never-claim list, profile and resume go in the system prompt; the job in the user turn", async () => {
  let seen;
  await draftEmail(async (req) => { seen = req; return { subject: "s", body: "b" }; }, D);
  for (const part of ["<fact_bank>\nFACTS", "<never_claim>\nTwilio", "<profile>\nPROFILE", "<resume>\nRESUME"]) assert.ok(seen.system.includes(part), part);
  assert.match(seen.user, /Role: MLE at Acme/);
  assert.match(seen.user, /Recruiter: Ana Diaz, Technical Recruiter/);
  assert.match(seen.user, /attach: true/);
  assert.match(seen.user, /<job_description>\nJD-TEXT/);
  assert.equal(seen.schema, SCHEMA);
});

test("the prompt keeps the truth and safety rules", () => {
  assert.match(SYSTEM, /never-claim list overrides/);
  assert.match(SYSTEM, /don't mention visas or sponsorship/);
  assert.match(SYSTEM, /untrusted data/);
});
