#!/usr/bin/env node
// usage: node apply/prepare.mjs <name> <company> <role> <jd.txt> <out_dir>
// Resume pipeline: keyword tailoring -> Claude bullet edits -> truth checks -> ATS gate -> report.json. Never touches a form.
import { execFileSync } from "node:child_process";
import { existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { apiKey, auditEdits, proposeEdits, realLlm } from "./claude.mjs";
import { applyEdits, bullets, checkEdit, plain } from "./resume.mjs";

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const JOBSEARCH = process.env.JOBSEARCH_HOME || resolve(ROOT, "..");
const CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome";
export const ATS_GATE = 90;

const python = (args, env = {}) => execFileSync("python3", args, { encoding: "utf8", env: { ...process.env, ...env }, stdio: ["ignore", "pipe", "ignore"] });
const atsScore = (html, jd) => JSON.parse(python([join(ROOT, "ats_score.py"), html, jd, "--json"]));

function render(html, pdf) {
  execFileSync(CHROME, ["--headless", "--disable-gpu", "--no-pdf-header-footer", `--print-to-pdf=${pdf}`, `file://${html}`], { stdio: "ignore" });
  return Number(python(["-c", "import sys,pypdf;print(len(pypdf.PdfReader(sys.argv[1]).pages))", pdf]).trim());
}

// Runs Claude and both checkers; returns the edits that survive and those rejected, each with its reason.
export async function reviewedEdits(llm, { factBank, html, jd, missing }) {
  const list = bullets(html);
  const resumeText = plain(html);
  const proposed = await proposeEdits(llm, { factBank, bullets: list, jd, missing });
  const rejected = [];
  const passed = proposed.filter((e) => {
    const reasons = checkEdit(e, list[e.index], factBank, resumeText);
    if (reasons.length) rejected.push({ ...e, reasons });
    return !reasons.length;
  });
  if (!passed.length) return { accepted: [], rejected };
  const audit = await auditEdits(llm, { factBank, edits: passed, bullets: list });
  const verdict = new Map(audit.map((a) => [a.index, a]));
  const accepted = passed.filter((e) => {
    const v = verdict.get(e.index);
    if (!v?.supported) rejected.push({ ...e, reasons: [v?.problem || "fact checker gave no verdict"] });
    return v?.supported;
  });
  return { accepted, rejected };
}

async function main([name, company, role, jdPath, out]) {
  mkdirSync(out, { recursive: true });
  const env = { TAILOR_OUT: out };
  const base = JSON.parse(python([join(JOBSEARCH, "tailor.py"), jdPath, name], env));
  const htmlPath = join(out, `${name}.html`), pdfPath = join(out, `Skanda_Pandith_Resume_${name}.pdf`);
  const report = { name, company, role, ats_before: base.before, ats_keywords: base.after, skills_added: base.added, changes: [], rejected: [], claude: "skipped: no API key" };
  let pages = base.pages;
  const factPath = join(ROOT, "data", "fact_bank.md");
  if (apiKey() && existsSync(factPath)) {
    const html = readFileSync(htmlPath, "utf8");
    const factBank = `${readFileSync(factPath, "utf8")}\n\nCurrent resume:\n${plain(html)}`;
    const { accepted, rejected } = await reviewedEdits(realLlm(), { factBank, html, jd: readFileSync(jdPath, "utf8"), missing: base.still_missing_claimable || [] });
    const list = bullets(html);
    // Largest growth goes first if the page overflows.
    const kept = [...accepted].sort((a, b) => plain(a.new_html).length - plain(b.new_html).length);
    while (true) {
      writeFileSync(htmlPath, applyEdits(html, kept));
      pages = render(htmlPath, pdfPath);
      if (pages <= 1 || !kept.length) break;
      rejected.push({ ...kept.pop(), reasons: ["removed to keep the resume on one page"] });
    }
    report.claude = "used";
    report.rejected = rejected;
    report.changes = kept.map((e) => ({ before: plain(list[e.index].html), after: plain(e.new_html), source_fact: e.source_fact, why: e.why }));
  } else if (apiKey()) {
    report.claude = "skipped: data/fact_bank.md is missing";
  }
  const ats = atsScore(htmlPath, jdPath);
  python([join(JOBSEARCH, "cover_letter.py"), company, role, jdPath, name], env);
  Object.assign(report, { ats_after: ats.score, not_claimable: ats.missing_not_claimable, pages, pdf: pdfPath, cover_letter: join(out, `Skanda_Pandith_CoverLetter_${name}.pdf`), out_dir: out, passed: ats.score >= ATS_GATE && pages === 1 });
  writeFileSync(join(out, `${name}.report.json`), JSON.stringify(report, null, 1));
  console.log(JSON.stringify({ passed: report.passed, ats_after: report.ats_after, claude: report.claude }));
}

if (process.argv[1] === fileURLToPath(import.meta.url)) {
  main(process.argv.slice(2)).catch((e) => { console.error(e.message); process.exit(1); });
}
