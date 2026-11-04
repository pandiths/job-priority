// Click the toolbar icon: read the form in every frame, ask the local Job Radar server for answers, fill them.
// The server holds the profile, resumes and Claude key; this file only moves fields and answers between them.
// Nothing here presses Submit or Next. You review the page, move on, and click the icon again.
import { SERVER, TOKEN } from "./config.js";

const NEVER = /sponsor|visa|immigration|work permit|citizen|export control|clearance|salary|compensation/i;
const api = (route, body) =>
  fetch(`${SERVER}/api/ext/${route}`, { method: "POST", headers: { "Content-Type": "application/json", "X-JR-Token": TOKEN }, body: JSON.stringify(body || {}) }).then((r) => r.json());
const inFrames = (tabId, frameIds, func, args = []) => chrome.scripting.executeScript({ target: frameIds ? { tabId, frameIds } : { tabId, allFrames: true }, func, args });
const panel = (tabId, state) => inFrames(tabId, [0], (s) => window.__jrPanel && window.__jrPanel.set(s), [state]).catch(() => {});

const busy = new Set();
const lastJob = new Map();

chrome.action.onClicked.addListener((tab) => run(tab.id, null));
chrome.runtime.onMessage.addListener((m, sender) => {
  if (m.type === "jr-fill") run(sender.tab.id, m.jobId);
});

async function run(tabId, jobId) {
  if (busy.has(tabId)) return;
  busy.add(tabId);
  try {
    await chrome.scripting.executeScript({ target: { tabId, allFrames: true }, files: ["filler.js"] });
    await chrome.scripting.executeScript({ target: { tabId, frameIds: [0] }, files: ["panel.js"] });
    const [{ result: page }] = await inFrames(tabId, [0], () => ({ url: location.href, title: document.title }));
    let jobs;
    try {
      jobs = await api("jobs", page);
    } catch {
      return panel(tabId, { error: "Job Radar isn't running. In Terminal: cd ~/Documents/JobSearch/radar && python3 radar.py serve" });
    }
    if (jobs.error) return panel(tabId, { error: "Job Radar refused the extension. Reload it in chrome://extensions after restarting the server." });
    if (jobId == null) jobId = lastJob.has(tabId) ? lastJob.get(tabId) : jobs[0].match >= 2 ? jobs[0].id : "";
    lastJob.set(tabId, jobId);
    const state = { jobs, jobId, status: "Reading the form…", left: [], done: false };
    await panel(tabId, state);

    const scans = (await inFrames(tabId, null, async () => {
      if (!window.__jr) return null;
      const s = window.__jr.scan();
      if (!s.password) s.fields = await window.__jr.comboOptions(s.fields);
      return s;
    })).filter((s) => s.result);
    // Sign-in or create-account page: fill the login saved in Keychain and stop.
    const login = scans.find((s) => s.result.password);
    const formFields = scans.reduce((n, s) => n + s.result.fields.length, 0);
    if (login && formFields < 6) {
      const cred = await api("login");
      if (cred.error) return panel(tabId, { ...state, status: cred.error, done: true });
      const [{ result: r }] = await inFrames(tabId, [login.frameId], (c) => window.__jr.login(c), [cred]);
      return panel(tabId, { ...state, done: true, status: r });
    }
    const fields = [], where = [];
    for (const s of scans) for (const f of s.result.fields) { where.push({ frameId: s.frameId, id: f.id }); fields.push({ ...f, id: fields.length }); }
    if (!fields.length) return panel(tabId, { ...state, done: true, status: "No form fields on this page. Open the application form, then click the icon again." });
    const empty = fields.filter((f) => !f.value).length;
    await panel(tabId, { ...state, status: `Answering ${empty} empty fields with Claude… (about a minute)` });

    const ans = await api("answer", { job_id: jobId, fields, url: page.url, title: page.title });
    if (ans.error) return panel(tabId, { ...state, done: true, status: "Claude couldn't answer: " + ans.error });
    const files = {};
    for (const kind of ["resume", "cover"]) if (ans.answers.some((a) => a.answer === kind.toUpperCase())) files[kind] = await api("file", { job_id: jobId, kind });

    const left = [], perFrame = new Map();
    for (const a of ans.answers) {
      const f = fields[a.id];
      if (!f || f.value) continue;
      const blank = NEVER.test(f.label) || a.leave || (!a.answer && !(a.choices || []).length);
      if (blank) { if (f.required || NEVER.test(f.label)) left.push(`${f.label || f.type}${a.reason ? " — " + a.reason : ""}`); continue; }
      const w = where[a.id];
      if (!perFrame.has(w.frameId)) perFrame.set(w.frameId, []);
      perFrame.get(w.frameId).push({ ...a, id: w.id });
    }
    let filled = 0;
    for (const [frameId, list] of perFrame) {
      await panel(tabId, { ...state, status: "Filling…" });
      const [{ result: r }] = await inFrames(tabId, [frameId], (l, fs) => window.__jr.apply(l, fs), [list, files]);
      filled += r.filled;
      left.push(...r.failed);
    }
    if (filled && jobId) await api("filled", { job_id: jobId });
    await panel(tabId, { ...state, done: true, left, status: `Filled ${filled} fields. Check them, press Next or Submit yourself, and click the icon again on the next page.` });
  } catch (e) {
    await panel(tabId, { error: "Stopped: " + (e.message || e) });
  } finally {
    busy.delete(tabId);
  }
}
