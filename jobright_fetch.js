// Reads Jobright search results through the already logged-in tab on the CDP Chrome (:9333).
// Read-only: it never navigates, reloads or touches any other tab, because that browser holds filled application forms.
const { chromium } = require(process.env.HOME + '/.local/node/lib/node_modules/@playwright/mcp/node_modules/playwright-core');
const TITLES = ['Machine Learning Engineer', 'Software Engineer, Machine Learning', 'AI Engineer', 'Applied AI Engineer', 'Software Engineer II', 'Software Engineer', 'Backend Engineer', 'ML Infrastructure Engineer', 'Forward Deployed Engineer', 'Software Engineer AI'];
const body = (title, position) => ({ searchType: 'job_title', value: title, jobTaxonomyList: [{ taxonomyId: '00-00-00', title }], country: 'US', jobTypes: [], seniority: [], workModel: [], locations: [], companies: [], isH1BOnly: true, companyCategory: null, annualSalaryMinimum: null, roleType: null, companyStages: null, skills: [], excludedCompanies: [], excludedSkills: null, excludeStaffingAgency: true, minYearsOfExperienceRange: null, excludeCompanyCategory: [], excludeSecurityClearance: true, excludeUsCitizen: true, refresh: false, position, sortCondition: 1 });
(async () => {
  const browser = await chromium.connectOverCDP('http://localhost:9333', { timeout: 8000 });
  const jp = browser.contexts()[0].pages().find(p => /jobright\.ai/.test(p.url()));
  if (!jp) throw new Error('no Jobright tab open in the job-search Chrome');
  const queries = [];
  for (const t of TITLES) for (const pos of [0, 10, 20, 30]) queries.push([t, pos]);
  const out = new Map(); let failed = 0;
  for (const [t, pos] of queries) {
    const r = await jp.evaluate(async ([b, pos]) => {
      const res = await fetch(`/swan/recommend/search?searchType=job_title&refresh=false&count=10&position=${pos}&sortCondition=1`, { method: 'POST', headers: { 'content-type': 'application/json', 'x-client-type': 'web' }, body: JSON.stringify(b) });
      return res.ok ? res.text() : '';
    }, [body(t, pos), pos]);
    let list = [];
    try { const j = JSON.parse(r); list = j.result.jobList || []; } catch (e) { failed++; }
    for (const it of list) {
      const jr = it.jobResult || {};
      out.set(jr.jobId, { id: jr.jobId, title: jr.jobTitle, company: (it.companyResult || {}).companyName || '', location: jr.jobLocation, url: jr.applyLink || jr.originalUrl, posted: jr.publishTime, seniority: jr.jobSeniority, years: jr.minYearsOfExperience, salary: jr.salaryDesc, applicants: jr.applicantsCount, tags: [...(jr.jobTags || []), ...(jr.recommendationTags || [])], skill: ((jr.recommendationScores || []).find(s => s.featureName === 'q_job_skill_match') || {}).score, applied: !!(it.isApplied || jr.isApplied), text: [jr.jobSummary, ...(jr.coreResponsibilities || []), ...(jr.skillSummaries || [])].join(' ') });
    }
    await jp.waitForTimeout(1100);
  }
  // A file, not stdout: process.exit would cut a large pipe write short.
  require('fs').writeFileSync(process.argv[2], JSON.stringify({ jobs: [...out.values()], queries: queries.length, failed }));
  process.exit(0);
})().catch(e => { console.error(e.message); process.exit(1); });
