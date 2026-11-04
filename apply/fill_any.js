// General form filler behind the app's Fill button, for sites without a dedicated filler. Run through cdp_run.js.
// It drives the job-search Chrome: opens the posting, presses its Apply button, signs in with the Keychain login,
// then on each page injects extension/filler.js (the same form code as the extension), asks Claude for answers
// (apply/answer_form.mjs) and fills them. DOM events are used throughout, so it works while the window is hidden.
// It never presses Submit. Env: GF_URL, GF_RES, GF_COVER, GF_HTML, GF_COMPANY, GF_TITLE, GF_PROFILE.
async (page) => {
  const fs = require('fs'), cp = require('child_process'), path = require('path');
  const RADAR = path.resolve(process.env.GF_RADAR || process.cwd());
  const URL0 = process.env.GF_URL, STATE = path.join(RADAR, 'data', 'fill_tabs.json');
  const FILLER = fs.readFileSync(path.join(RADAR, 'extension', 'filler.js'), 'utf8');
  const NEVER = /sponsor|visa|immigration|work permit|citizen|export control|clearance|salary|compensation/i;
  const NEXT = /^(next|next step|continue|save (and|&) continue|save (and|&) next|proceed)$/i;
  const APPLY = /^(apply|apply now|apply for this (job|position|role)|apply to this (job|position)|start (your )?application|apply online|i'?m interested|apply manually)$/i;
  const LOGIN_URL = /\/(login|signin|sign-in|sign_in|auth)\b/i;
  const out = [], say = (s) => out.push(s);
  const ctx = page.context();
  const W = (ms) => new Promise((r) => setTimeout(r, ms));
  const within = (pr, ms, fallback) => Promise.race([pr, W(ms).then(() => fallback)]);

  // Tabs are remembered by Chrome's target id, which survives sign-in redirects.
  const tid = (x) => within((async () => { try { const c = await ctx.newCDPSession(x); const r = await c.send('Target.getTargetInfo'); await c.detach(); return r.targetInfo.targetId; } catch { return ''; } })(), 3000, '');
  let tabs = {};
  try { tabs = JSON.parse(fs.readFileSync(STATE, 'utf8')); } catch {}
  let p = null;
  if (tabs[URL0]) for (const x of ctx.pages()) if ((await tid(x)) === tabs[URL0]) { p = x; break; }
  p = p || ctx.pages().find((x) => x.url().split('#')[0] === URL0.split('#')[0]);
  if (p) say('Reusing the open application tab.');
  else { p = await ctx.newPage(); await p.goto(URL0, { waitUntil: 'domcontentloaded', timeout: 60000 }); await W(6000); }

  const profile = fs.readFileSync(process.env.GF_PROFILE, 'utf8');
  const resume = fs.readFileSync(process.env.GF_HTML, 'utf8').replace(/<style[\s\S]*?<\/style>/gi, '').replace(/<\/(p|li|h\d|div)>/gi, '\n').replace(/<[^>]+>/g, ' ').replace(/&amp;/g, '&').replace(/[ \t]+/g, ' ').replace(/\n\s*\n+/g, '\n').trim();
  const pdf = (f) => (f && fs.existsSync(f) ? { name: path.basename(f), b64: fs.readFileSync(f).toString('base64') } : null);
  const files = { resume: pdf(process.env.GF_RES), cover: pdf(process.env.GF_COVER) };

  // Reads every frame with the extension's form code. Fields get page-wide ids; `where` maps them back.
  const scan = async () => {
    const fields = [], where = [];
    let password = false, embedded = null, loginFrame = null;
    for (const f of p.frames()) {
      // Injected as a CDP expression, which page Content-Security-Policies don't block.
      if (!(await within(f.evaluate(FILLER).then(() => true).catch(() => false), 10000, false))) continue;
      const r = await within(f.evaluate(async () => {
        const s = window.__jr.scan();
        if (!s.password) s.fields = await window.__jr.comboOptions(s.fields);
        return { ...s, href: location.href };
      }).catch(() => null), 30000, null);
      if (!r) continue;
      if (r.password) { password = true; loginFrame = loginFrame || f; }
      if (f !== p.mainFrame() && r.fields.length > 2 && /^https?:/.test(r.href)) embedded = r.href;
      for (const x of r.fields) { where.push({ f, id: x.id }); fields.push({ ...x, id: fields.length }); }
    }
    const captcha = await p.evaluate(() => [...document.querySelectorAll('iframe')].some((f) => /recaptcha|hcaptcha|turnstile|challenges\.cloudflare/.test(f.src))).catch(() => false);
    return { fields, where, password, embedded, loginFrame, captcha };
  };

  // Presses the most specific Apply link or button on a posting page, never menu links or a filled form's button.
  const openForm = async () => {
    const before = p.url(), had = new Set(ctx.pages());
    const label = await p.evaluate((src) => {
      const re = new RegExp(src, 'i'), t = (e) => e.innerText.replace(/\s+/g, ' ').trim();
      const c = [...document.querySelectorAll('a, button, [role=button]')].filter((e) => {
        if (!e.getBoundingClientRect().height || !re.test(t(e)) || e.closest('nav, header, footer, [role=navigation], [role=menu]')) return false;
        const form = e.closest('form');
        return !(form && form.querySelectorAll('input:not([type=hidden]), textarea').length > 2);
      });
      c.sort((a, b) => t(b).length - t(a).length);
      if (!c[0]) return null;
      c[0].click();
      return t(c[0]);
    }, APPLY.source).catch(() => null);
    if (!label) return false;
    // The form may open in a new tab, sometimes several seconds later.
    for (let i = 0; i < 12; i++) { await W(1000); const np = ctx.pages().find((x) => !had.has(x)); if (np) { p = np; break; } if (p.url() !== before) break; }
    await p.waitForLoadState('domcontentloaded').catch(() => {}); await W(4000);
    say(`Opened the application from "${label}"${p.url() !== before ? ' (' + p.url().slice(0, 90) + ')' : ''}.`);
    return true;
  };

  const login = async (f) => {
    const raw = cp.execFileSync('security', ['find-generic-password', '-s', 'jobsearch-apply', '-g'], { encoding: 'utf8', stdio: ['ignore', 'pipe', 'pipe'] });
    const email = (raw.match(/"acct"<blob>="([^"]+)"/) || [])[1];
    const password = cp.execFileSync('security', ['find-generic-password', '-s', 'jobsearch-apply', '-w'], { encoding: 'utf8' }).replace(/\n$/, '');
    return f.evaluate((c) => window.__jr.login(c), { email, password });
  };

  const pressNext = () => p.evaluate((src) => {
    const re = new RegExp(src, 'i');
    const b = [...document.querySelectorAll('button, [role=button], input[type=button]')].reverse().find((e) => e.getBoundingClientRect().height > 0 && !e.disabled && re.test((e.innerText || e.value || '').replace(/\s+/g, ' ').trim()));
    if (b) b.click();
    return !!b;
  }, NEXT.source).catch(() => false);
  const hasNext = () => p.evaluate((src) => { const re = new RegExp(src, 'i'); return [...document.querySelectorAll('button, [role=button], input[type=button]')].some((e) => e.getBoundingClientRect().height > 0 && re.test((e.innerText || e.value || '').replace(/\s+/g, ' ').trim())); }, NEXT.source).catch(() => false);

  let filledTotal = 0, applyClicks = 0, logins = 0, lastSig = '', embeddedOpened = false;
  for (let pg = 0; pg < 10; pg++) {
    const s = await scan();
    // A page counts as an application form when it asks for a resume, name, email or phone.
    const isForm = s.fields.some((x) => x.type === 'file' || /e-?mail|first name|last name|full name|legal name|phone/i.test(x.label));
    const onLogin = s.password || LOGIN_URL.test(p.url());
    if (onLogin && s.fields.length < 6) {
      if (logins++ || !s.loginFrame) { say('!! This site wants you to sign in or create an account. Do that in the open tab, then press Continue filling.'); break; }
      const msg = await login(s.loginFrame).catch((e) => 'Could not use the saved login: ' + e.message.split('\n')[0]);
      say(msg);
      if (!/^Signed in/.test(msg)) { say('!! Finish signing in or creating the account in the open tab, then press Continue filling.'); break; }
      await W(8000); continue;
    }
    if (!isForm && applyClicks < 2 && (await openForm())) { applyClicks++; continue; }
    if (!isForm) { say('!! No application form found on the page. Open the form in the tab, then press Continue filling.'); break; }
    // Forms inside another site's frame are opened in this tab directly, so later pages and Next work normally.
    if (s.embedded && !embeddedOpened) { embeddedOpened = true; say('The form is embedded from ' + new URL(s.embedded).host + '; opened it directly.'); await p.goto(s.embedded, { waitUntil: 'domcontentloaded', timeout: 60000 }); await W(5000); continue; }
    const sig = s.fields.map((x) => x.label).join('|');
    if (sig === lastSig) { say('?? The page did not move on after Next. Check the messages on the form.'); break; }
    lastSig = sig;

    const res = cp.spawnSync(process.execPath, [path.join(RADAR, 'apply', 'answer_form.mjs')], { input: JSON.stringify({ fields: s.fields, profile, resume, company: process.env.GF_COMPANY, title: process.env.GF_TITLE }), encoding: 'utf8', timeout: 300000, cwd: RADAR });
    let ans;
    try { ans = JSON.parse(res.stdout); } catch { ans = { error: (res.stderr || '').slice(-300) || 'no answer' }; }
    if (ans.error) { say('!! Claude could not answer the form: ' + ans.error); break; }

    const left = [], perFrame = new Map();
    for (const a of ans.answers) {
      const fld = s.fields[a.id];
      if (!fld || fld.value) continue;
      if (NEVER.test(fld.label) || a.leave || (!a.answer && !(a.choices || []).length)) { if (fld.required || NEVER.test(fld.label)) left.push(`${fld.label || fld.type}${a.reason ? ' (' + a.reason + ')' : ''}`); continue; }
      const w = s.where[a.id];
      if (!perFrame.has(w.f)) perFrame.set(w.f, []);
      perFrame.get(w.f).push({ ...a, id: w.id });
    }
    let filled = 0;
    for (const [f, list] of perFrame) {
      const r = await within(f.evaluate(([l, fl]) => window.__jr.apply(l, fl), [list, files]), 120000, { filled: 0, failed: ['The page stopped responding while filling.'] });
      filled += r.filled; left.push(...r.failed);
    }
    filledTotal += filled;
    say(`Page ${pg + 1}: filled ${filled} of ${s.fields.filter((x) => !x.value).length} empty fields.`);
    for (const l of left) say('?? ' + l.slice(0, 220));
    if (s.captcha) say('?? The page has a CAPTCHA. Solve it before you submit.');
    if (!(await hasNext())) break;
    if (left.length) { say('Stopped before Next: answer the fields above, then press Continue filling.'); break; }
    await pressNext(); await p.waitForLoadState('domcontentloaded').catch(() => {}); await W(4000);
  }
  tabs[URL0] = await tid(p);
  try { fs.writeFileSync(STATE, JSON.stringify(tabs, null, 1)); } catch {}
  if (!filledTotal && !out.some((l) => l.startsWith('!!'))) say('!! Nothing was filled. Check the open tab.');
  say(`Filled ${filledTotal} fields in total. Nothing was submitted.`);
  // With nothing filled, the first "!!" line becomes the app's error message.
  if (filledTotal || !out.some((l) => l.startsWith('!!'))) say('LEFT OPEN FOR REVIEW');
  return out.join('\n');
}
