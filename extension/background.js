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
// The resume you picked in the panel, per tab and posting. Moving to another posting starts from the best match again.
const picked = new Map();
const postingKey = (url) => { try { const u = new URL(url); return u.host + "/" + u.pathname.split("/").filter(Boolean).slice(0, 2).join("/"); } catch { return url; } };

chrome.action.onClicked.addListener((tab) => run(tab.id, null));
chrome.runtime.onMessage.addListener((m, sender) => {
  if (m.type === "jr-fill") run(sender.tab.id, m.jobId);
});

// Date boxes that ignore script input get real keystrokes through the debugger protocol. Chrome shows a
// "started debugging this browser" bar while attached; it detaches right after.
async function typeKeys(tabId, frameId, keys) {
  const target = { tabId }, send = (type, k) => chrome.debugger.sendCommand(target, "Input.dispatchKeyEvent", { type, ...k });
  const pause = (ms) => new Promise((r) => setTimeout(r, ms));
  await chrome.debugger.attach(target, "1.3");
  let typed = 0;
  try {
    for (const k of keys) {
      const [{ result: ok }] = await inFrames(tabId, [frameId], (id) => { const e = document.getElementById(id); if (!e) return false; e.scrollIntoView({ block: "center" }); e.focus(); return true; }, [k.id]);
      if (!ok) continue;
      for (const ch of k.text) { await send("keyDown", { key: ch, text: ch, unmodifiedText: ch }); await send("keyUp", { key: ch }); await pause(70); }
      const tab = { key: "Tab", code: "Tab", windowsVirtualKeyCode: 9 };
      await send("keyDown", tab); await send("keyUp", tab); await pause(150);
      typed++;
    }
  } finally {
    await chrome.debugger.detach(target).catch(() => {});
  }
  return typed;
}

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
    const key = `${tabId} ${postingKey(page.url)}`;
    if (jobId != null) picked.set(key, jobId);
    else jobId = picked.has(key) ? picked.get(key) : jobs[0].match >= 2 ? jobs[0].id : "";
    const want = (jobs.find((j) => j.id === jobId) || {}).files || {};
    const state = { jobs, jobId, status: "Reading the form…", left: [], done: false };
    await panel(tabId, state);

    // Make room for every job and degree on the resume (Workday and others start these sections empty).
    const entries = await api("entries", { job_id: jobId }).catch(() => null);
    if (entries && !entries.error) await inFrames(tabId, null, (c) => window.__jr && window.__jr.addEntries(c), [entries]).catch(() => {});
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
    // A resume or cover letter from another job (e.g. picked earlier in this tab) gets replaced.
    const ours = /_(Resume|CoverLetter)_.*\.pdf$/i;
    for (const f of fields) if (f.type === "file" && ours.test(f.value || "") && f.value !== want.resume && f.value !== want.cover) { f.value = ""; f.replace = true; }
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
      perFrame.get(w.frameId).push({ ...a, id: w.id, replace: !!f.replace });
    }
    let filled = 0;
    for (const [frameId, list] of perFrame) {
      await panel(tabId, { ...state, status: "Filling…" });
      const [{ result: r }] = await inFrames(tabId, [frameId], (l, fs) => window.__jr.apply(l, fs), [list, files]);
      filled += r.filled;
      left.push(...r.failed);
      if (r.keys && r.keys.length) {
        try { filled += await typeKeys(tabId, frameId, r.keys); }
        catch (e) { left.push(...r.keys.map((k) => `${k.label}: type ${k.text} yourself (${e.message || e})`)); }
      }
    }
    if (filled && jobId) await api("filled", { job_id: jobId });
    await panel(tabId, { ...state, done: true, left, status: `Filled ${filled} fields. Check them, press Next or Submit yourself, and click the icon again on the next page.` });
  } catch (e) {
    await panel(tabId, { error: "Stopped: " + (e.message || e) });
  } finally {
    busy.delete(tabId);
  }
}
