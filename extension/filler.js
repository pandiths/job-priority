// Injected into every frame of the tab on each click (replacing any older copy). window.__jr.scan() lists the form fields (tagging each with
// data-jr), window.__jr.apply() fills answers with the events React/Vue forms listen for, window.__jr.login() fills
// a sign-in form. No function here clicks Submit or Next.
window.__jr = (() => {
  const NEVER = /sponsor|visa|immigration|work permit|citizen|export control|clearance|salary|compensation/i;
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const vis = (e) => { const r = e.getBoundingClientRect(), s = getComputedStyle(e); return r.width > 0 && r.height > 0 && s.visibility !== "hidden" && s.display !== "none"; };
  const clean = (s) => (s || "").replace(/\s+/g, " ").trim().slice(0, 300);
  const norm = (s) => clean(s).toLowerCase();
  // Shadow-DOM aware helpers: forms built from web components (SmartRecruiters and others) hide their inputs in shadow roots.
  const all = (sel, root = document) => { const out = [...root.querySelectorAll(sel)]; for (const e of root.querySelectorAll("*")) if (e.shadowRoot) out.push(...all(sel, e.shadowRoot)); return out; };
  const one = (sel) => all(sel)[0];
  const up = (e) => e.parentElement || (e.getRootNode() instanceof ShadowRoot ? e.getRootNode().host : null);
  const closest = (e, sel) => { for (let c = e; c; c = up(c)) if (c.matches && c.matches(sel)) return c; return null; };
  const byId = (e, id) => e.getRootNode().getElementById?.(id) || document.getElementById(id);
  const outside = (e) => closest(e, "nav, header, footer, [role=navigation], [role=search], #__jr_panel");
  // innerText skips text rendered inside shadow roots, so fall back to walking them (and their slots).
  const deepText = (n) => {
    if (n.nodeType === 3) return n.textContent;
    if (n.nodeType !== 1 && n.nodeType !== 11) return "";
    if (/^(STYLE|SCRIPT)$/.test(n.tagName)) return "";
    if (n.tagName === "SLOT") return n.assignedNodes({ flatten: true }).map(deepText).join(" ");
    return [...(n.shadowRoot || n).childNodes].map(deepText).join(" ");
  };
  const text = (e) => clean(e.innerText) || clean(deepText(e));
  // Ancestor names like "resume-upload" or "apply-with-resume" tell upload boxes apart when their text doesn't.
  const hints = (e) => {
    const h = [], re = /resume|cover|letter|\bcv\b|transcript|apply-with|portfolio/i;
    for (let c = up(e), i = 0; c && i < 8; i++, c = up(c)) {
      if (re.test(c.tagName)) h.push(c.tagName.toLowerCase());
      for (const a of c.getAttributeNames ? c.getAttributeNames() : []) { if (re.test(a)) h.push(a); else if (/^(id|name|data-test|data-testid|data-qa)$/.test(a) && re.test(c.getAttribute(a))) h.push(c.getAttribute(a)); }
    }
    return [...new Set(h)].join(", ");
  };

  const labelOf = (e) => {
    let t = e.labels && e.labels.length ? [...e.labels].map((l) => l.innerText).join(" ") : "";
    if (!t && e.getAttribute("aria-labelledby")) t = e.getAttribute("aria-labelledby").split(/\s+/).map((i) => byId(e, i)?.innerText || "").join(" ");
    if (!t) t = e.getAttribute("aria-label") || "";
    for (let c = e, i = 0; !t && c && i < 8; i++) {
      c = up(c);
      if (!c) break;
      if (c.shadowRoot && (c.getAttribute("label") || c.getAttribute("aria-label"))) { t = c.getAttribute("label") || c.getAttribute("aria-label"); break; }
      const pool = [...c.querySelectorAll("label, legend, [class*='label' i], [class*='question' i]"), ...(c.shadowRoot ? c.shadowRoot.querySelectorAll("label, legend, [class*='label' i]") : [])];
      const l = pool.find((x) => !x.contains(e) && text(x));
      if (l) t = text(l);
    }
    return clean(t || e.placeholder || e.name || "");
  };
  const optText = (e) => clean((e.labels && e.labels[0] && text(e.labels[0])) || e.getAttribute("aria-label") || (e.parentElement && text(e.parentElement)) || e.value);
  const groupQ = (els) => {
    const lg = els[0].closest("fieldset")?.querySelector("legend")?.innerText;
    if (lg) return clean(lg);
    const rg = els[0].closest("[role=radiogroup], [role=group]");
    if (rg?.getAttribute("aria-label")) return clean(rg.getAttribute("aria-label"));
    if (rg?.getAttribute("aria-labelledby")) return clean(byId(rg, rg.getAttribute("aria-labelledby"))?.innerText);
    const opts = els.map(optText);
    for (let c = els[0].parentElement, i = 0; c && i < 6; i++, c = c.parentElement) {
      if (els.every((x) => c.contains(x)) && c.innerText.length > opts.join(" ").length + 3) { let t = c.innerText; for (const o of opts) t = t.replace(o, ""); return clean(t); }
    }
    return "";
  };
  const req = (e, label) => !!(e.required || e.getAttribute("aria-required") === "true" || /\*\s*$/.test(label));

  function scan() {
    all("[data-jr], [data-jr-opt]").forEach((e) => { e.removeAttribute("data-jr"); e.removeAttribute("data-jr-opt"); });
    const fields = [], seen = new Set();
    let id = 0;
    const els = all("input, textarea, select, [role=combobox], [role=radio]");
    const password = els.some((e) => e.type === "password" && vis(e));
    for (const e of els) {
      if (seen.has(e) || outside(e)) continue;
      const tag = e.tagName.toLowerCase(), type = (e.getAttribute("type") || "").toLowerCase(), role = e.getAttribute("role");
      if (tag === "input" && /^(hidden|submit|button|reset|image|search|password)$/.test(type)) continue;
      if (tag === "input" && !/^(file|radio|checkbox)$/.test(type) && role !== "combobox" && (e.tabIndex === -1 || e.getAttribute("aria-hidden") === "true" || getComputedStyle(e).opacity === "0")) continue;
      if (type === "file") { const c = closest(up(e) || e, "div, label, section, [class*='drop' i]") || up(e); if (!vis(e) && !(c && vis(c))) continue; }
      else if (!vis(e) && type !== "radio" && type !== "checkbox") continue;
      if (type === "radio" || role === "radio" || type === "checkbox") {
        const group = type === "checkbox" && !e.name ? [e] : role === "radio" ? [...(e.closest("[role=radiogroup]") || e.parentElement).querySelectorAll("[role=radio]")] : [...e.getRootNode().querySelectorAll(`input[type="${type}"][name="${CSS.escape(e.name)}"]`)];
        const shown = group.filter((x) => vis(x) || vis(x.parentElement) || (x.labels && x.labels[0] && vis(x.labels[0])));
        group.forEach((x) => seen.add(x));
        if (!shown.length) continue;
        const n = id++;
        shown.forEach((x, k) => x.setAttribute("data-jr-opt", n + "|" + k));
        const on = (x) => x.checked || x.getAttribute("aria-checked") === "true";
        if (type === "checkbox" && shown.length === 1) {
          const l = optText(shown[0]);
          fields.push({ id: n, type: "checkbox", label: clean(l.length > 15 ? l : groupQ(shown) + " " + l), required: req(shown[0], l), value: on(shown[0]) ? "checked" : "" });
        } else {
          const label = groupQ(shown);
          fields.push({ id: n, type: type === "checkbox" ? "checkbox group" : "radio", label, required: shown.some((x) => req(x, label)), options: shown.map(optText), value: shown.filter(on).map(optText).join("; ") });
        }
        continue;
      }
      seen.add(e);
      const n = id++, label = labelOf(e);
      e.setAttribute("data-jr", n);
      if (tag === "select") {
        fields.push({ id: n, type: "select", label, required: req(e, label), options: [...e.options].filter((x) => x.value !== "" && !/^(select|choose|--)/i.test(x.text.trim())).map((x) => clean(x.text)).slice(0, 250), value: e.selectedIndex > 0 && e.value ? clean(e.options[e.selectedIndex].text) : "" });
      } else if (role === "combobox" || e.getAttribute("aria-autocomplete") === "list") {
        const box = e.closest("[class*='control' i]") || e.closest("[class*='container' i], [class*='select' i]") || e.parentElement;
        fields.push({ id: n, type: "combobox", label, required: req(e, label), value: clean(e.value || box?.querySelector("[class*='single-value' i], [class*='singleValue' i], [class*='multi-value' i], [class*='multiValue' i]")?.innerText || "") });
      } else if (type === "file") {
        // Upload buttons often say only "Attach"; the surrounding block names the document (Resume/CV, Cover Letter).
        let fl = label;
        for (let c = up(e), i = 0; c && i < 8 && (!fl || fl.length < 12 || (/^(attach|upload|browse|choose)/i.test(fl) && fl.length < 30)); i++, c = up(c)) if (text(c).length > 6) fl = text(c).slice(0, 120);
        const h = hints(e);
        fields.push({ id: n, type: "file", label: (h ? `[section: ${h}] ` : "") + fl, required: req(e, fl), value: e.files && e.files.length ? e.files[0].name : "" });
      } else {
        fields.push({ id: n, type: tag === "textarea" ? "textarea" : type || "text", label, required: req(e, label), value: clean(e.value) });
      }
    }
    return { fields, password };
  }

  // Lists a custom dropdown's options by opening it, so Claude can pick an exact one.
  async function comboOptions(fields) {
    for (const f of fields) {
      if (f.type !== "combobox" || f.value) continue;
      const el = one(`[data-jr="${f.id}"]`);
      press(el.closest("[class*='control' i]") || el); el.focus(); await sleep(500);
      f.options = [...new Set(all("[role=option]").filter(vis).map(text).filter(Boolean))].slice(0, 120);
      key(el, "Escape"); el.blur(); await sleep(150);
      if (!f.options.length) delete f.options;
      else if (f.options.length >= 100) f.options_partial = true; // long lists load more as you type
    }
    return fields;
  }

  const press = (el) => {
    el.scrollIntoView({ block: "center" });
    const r = el.getBoundingClientRect(), o = { bubbles: true, cancelable: true, composed: true, view: window, clientX: r.x + r.width / 2, clientY: r.y + r.height / 2, button: 0 };
    for (const t of ["pointerdown", "mousedown", "pointerup", "mouseup", "click"]) el.dispatchEvent(new (t.startsWith("pointer") ? PointerEvent : MouseEvent)(t, o));
  };
  const key = (el, k) => { el.dispatchEvent(new KeyboardEvent("keydown", { key: k, bubbles: true, composed: true })); el.dispatchEvent(new KeyboardEvent("keyup", { key: k, bubbles: true, composed: true })); };
  const setValue = (el, v) => {
    const proto = el.tagName === "TEXTAREA" ? HTMLTextAreaElement.prototype : el.tagName === "SELECT" ? HTMLSelectElement.prototype : HTMLInputElement.prototype;
    Object.getOwnPropertyDescriptor(proto, "value").set.call(el, v);
    el.dispatchEvent(new Event("input", { bubbles: true, composed: true }));
  };
  const typeText = (el, v) => { el.focus(); setValue(el, v); el.dispatchEvent(new Event("change", { bubbles: true, composed: true })); el.dispatchEvent(new FocusEvent("blur")); el.blur(); };
  const findOption = (want) => {
    const os = all("[role=option]").filter(vis);
    return os.find((o) => norm(text(o)) === norm(want)) || os.find((o) => norm(text(o)).startsWith(norm(want)));
  };
  const fileFrom = (f) => { const b = atob(f.b64), u = new Uint8Array(b.length); for (let i = 0; i < b.length; i++) u[i] = b.charCodeAt(i); return new File([u], f.name, { type: "application/pdf" }); };

  async function fillOne(a, files) {
    const el = one(`[data-jr="${a.id}"]`);
    const opt = (k) => one(`[data-jr-opt="${a.id}|${k}"]`);
    const optIndex = (want) => { for (let k = 0; opt(k); k++) if (norm(optText(opt(k))) === norm(want)) return k; return -1; };
    const tick = (x) => { if (!(x.checked || x.getAttribute("aria-checked") === "true")) { x.scrollIntoView({ block: "center" }); x.click(); } };
    if (a.type === "radio") { const k = optIndex(a.answer); if (k < 0) return false; tick(opt(k)); return true; }
    if (a.type === "checkbox group") { let ok = false; for (const c of a.choices || []) { const k = optIndex(c); if (k >= 0) { tick(opt(k)); ok = true; } } return ok; }
    if (a.type === "checkbox") { if (a.answer !== "check") return false; tick(opt(0)); return true; }
    if (!el) return false;
    if (a.type === "select") {
      const o = [...el.options].find((x) => norm(x.text) === norm(a.answer));
      if (!o) return false;
      el.focus(); setValue(el, o.value); el.dispatchEvent(new Event("change", { bubbles: true, composed: true })); el.blur(); return true;
    }
    if (a.type === "file") {
      const f = a.answer === "RESUME" ? files.resume : a.answer === "COVER" ? files.cover : null;
      if (!f || !f.b64) return false;
      const dt = new DataTransfer(); dt.items.add(fileFrom(f)); el.files = dt.files;
      el.dispatchEvent(new Event("input", { bubbles: true, composed: true })); el.dispatchEvent(new Event("change", { bubbles: true, composed: true }));
      await sleep(1500); return true;
    }
    if (a.type === "combobox") {
      press(el.closest("[class*='control' i]") || el); el.focus(); await sleep(400);
      let o = findOption(a.answer);
      if (!o && el.tagName === "INPUT") { setValue(el, a.answer); await sleep(1300); o = findOption(a.answer); }
      if (!o) { if (el.tagName === "INPUT") setValue(el, ""); key(el, "Escape"); el.blur(); return false; }
      press(o); await sleep(300); el.blur(); return true;
    }
    typeText(el, a.answer);
    return !("value" in el) || el.value.trim() !== ""; // sites may reformat, e.g. phone numbers
  }

  async function apply(list, files) {
    const scanned = new Map(scan().fields.map((f) => [f.id, f]));
    let filled = 0;
    const failed = [];
    for (const a of list) {
      const f = scanned.get(a.id);
      if (!f || f.value || NEVER.test(f.label)) continue;
      let ok = false;
      try { ok = await fillOne({ ...a, type: f.type }, files); } catch { ok = false; }
      if (ok) filled++;
      else failed.push(`${f.label || f.type}: couldn't set "${a.answer || (a.choices || []).join(", ")}"`);
      await sleep(120);
    }
    return { filled, failed };
  }

  function login(c) {
    const shown = all("input").filter(vis);
    const pw = shown.filter((e) => e.type === "password");
    const user = shown.find((e) => /email|user|login/i.test(e.type + " " + e.name + " " + e.id + " " + e.autocomplete + " " + labelOf(e)) && e.type !== "password");
    if (user && !user.value) typeText(user, c.email);
    for (const p of pw) if (!p.value) typeText(p, c.password);
    const creating = pw.length > 1 || /create (an )?account|sign up|register/i.test(document.body.innerText.slice(0, 3000)) && pw.length > 1;
    if (creating) return "Filled your email and password to create an account. Tick any terms box, press Create account, then click the icon again on the form.";
    const btn = all("button, input[type=submit], [role=button]").filter(vis).find((b) => /^(sign in|log in|login|sign-in)$/i.test(clean(b.innerText || b.value)));
    if (btn) { btn.click(); return "Signed in with your saved login. When the form opens, click the icon again."; }
    return "Filled your saved login. Press the sign-in button, then click the icon again on the form.";
  }

  return { scan, apply, login, comboOptions };
})();
