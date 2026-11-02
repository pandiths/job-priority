import { existsSync, readFileSync } from "node:fs";

// Pure resume logic: find bullets, apply edits, and reject edits that fail the deterministic truth checks.
const LI = /<li>([\s\S]*?)<\/li>/g;

export const bullets = (html) => [...html.matchAll(LI)].map((m, i) => ({ index: i, html: m[1] }));

export const plain = (s) => s.replace(/<[^>]+>/g, " ").replace(/&amp;/g, "&").replace(/\s+/g, " ").trim();

// Things the resume must never start claiming, from the private profile. An existing mention is fine; a new one is rejected.
const PROFILE = new URL("../data/profile.json", import.meta.url);
export const NEVER = existsSync(PROFILE) ? JSON.parse(readFileSync(PROFILE, "utf8")).never_claim.map((p) => new RegExp(p, "i")) : [];

export const numbersIn = (s) => plain(s).match(/\d[\d,.]*\d|\d/g) || [];

export function checkEdit(edit, original, factBank, resumeText, never = NEVER) {
  const text = plain(edit.new_html);
  const reasons = [];
  if (!original) return ["unknown bullet"];
  for (const re of never) if (re.test(text) && !re.test(resumeText)) reasons.push(`new never-claim term: ${re.source}`);
  for (const n of numbersIn(edit.new_html)) if (!factBank.includes(n)) reasons.push(`number not in fact bank: ${n}`);
  if (/<(?!\/?b>)/i.test(edit.new_html)) reasons.push("only <b> markup is allowed");
  if (text.length > plain(original.html).length * 1.25) reasons.push("too long for a one-page resume");
  if (!factBank.includes(edit.source_fact.trim()) || edit.source_fact.trim().length < 15) reasons.push("source_fact is not an exact quote from the fact bank");
  return reasons;
}

export function applyEdits(html, edits) {
  const byIndex = new Map(edits.map((e) => [e.index, e.new_html]));
  let i = 0;
  return html.replace(LI, (whole, inner) => {
    const next = byIndex.get(i++);
    return next === undefined ? whole : `<li>${next}</li>`;
  });
}
