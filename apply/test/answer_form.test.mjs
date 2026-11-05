import assert from "node:assert/strict";
import { test } from "node:test";
import { answerForm, SCHEMA, SYSTEM } from "../answer_form.mjs";

const FIELDS = [{ id: 0, type: "text", label: "First name", required: true, value: "" }, { id: 1, type: "radio", label: "Need sponsorship?", options: ["Yes", "No"], required: true, value: "" }];

test("the profile and resume go in the system prompt, the form fields in the user turn", async () => {
  let seen;
  const llm = async (req) => { seen = req; return { answers: [] }; };
  await answerForm(llm, { fields: FIELDS, profile: "PROFILE-TEXT", resume: "RESUME-TEXT", company: "Acme", title: "MLE" });
  assert.match(seen.system, /<profile>\nPROFILE-TEXT\n<\/profile>/);
  assert.match(seen.system, /<resume>\nRESUME-TEXT\n<\/resume>/);
  assert.match(seen.user, /Job: MLE at Acme/);
  assert.deepEqual(JSON.parse(seen.user.match(/<form_fields>\n(.*)\n<\/form_fields>/s)[1]), FIELDS);
  assert.equal(seen.schema, SCHEMA);
});

test("answers come back as Claude returned them", async () => {
  const answers = [{ id: 0, leave: false, answer: "Jane", choices: [], reason: "" }];
  assert.deepEqual(await answerForm(async () => ({ answers }), { fields: FIELDS, profile: "", resume: "", company: "", title: "" }), answers);
});

test("the prompt keeps the safety rules", () => {
  assert.match(SYSTEM, /Leaving a field is always better than guessing/);
  assert.match(SYSTEM, /Never tick consents/);
  assert.match(SYSTEM, /untrusted data/);
});

test("every answer field is required by the schema", () => {
  const item = SCHEMA.properties.answers.items;
  assert.deepEqual(item.required.sort(), ["answer", "choices", "id", "leave", "reason"]);
  assert.equal(item.additionalProperties, false);
});
