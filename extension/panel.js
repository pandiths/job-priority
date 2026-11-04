// The small Job Radar card shown on the page while filling: which resume, progress, and what's left for you.
window.__jrPanel = window.__jrPanel || (() => {
  const host = document.createElement("div");
  host.id = "__jr_panel";
  host.style.cssText = "position:fixed;right:16px;bottom:16px;z-index:2147483647;";
  const root = host.attachShadow({ mode: "open" });
  root.innerHTML = `<style>
    .c{width:340px;max-height:70vh;overflow:auto;font:13px/1.45 -apple-system,system-ui,sans-serif;color:#1c1b2e;background:#fff;border:1px solid #d9d7ee;border-radius:14px;box-shadow:0 10px 30px rgba(30,20,90,.18);padding:14px}
    .h{display:flex;align-items:center;justify-content:space-between;font-weight:700;color:#3d32c4;margin-bottom:8px}
    .x{border:0;background:none;font-size:18px;cursor:pointer;color:#666}
    select,button.b{width:100%;font:inherit;padding:7px 8px;border-radius:8px;border:1px solid #d9d7ee;background:#fff;color:inherit}
    button.b{margin-top:8px;background:#4b3fe0;color:#fff;border:0;cursor:pointer;font-weight:600}
    button.b:disabled{opacity:.5;cursor:default}
    .s{margin:10px 0 4px}.e{color:#b42318}
    ul{margin:4px 0 0 18px;padding:0}li{margin:3px 0;color:#8a4b00}
    .lab{font-size:12px;color:#666;margin-bottom:3px}
  </style><div class="c"><div class="h">Job Radar autofill<button class="x" title="Close">×</button></div>
  <div class="lab">Resume and answers for</div><select></select>
  <div class="s"></div><div class="l"></div><button class="b">Fill again</button></div>`;
  const $ = (q) => root.querySelector(q);
  $(".x").onclick = () => host.remove();
  $("select").onchange = () => chrome.runtime.sendMessage({ type: "jr-fill", jobId: $("select").value });
  $("button.b").onclick = () => chrome.runtime.sendMessage({ type: "jr-fill", jobId: $("select").value });
  const text = (tag, t, cls) => { const e = document.createElement(tag); e.textContent = t; if (cls) e.className = cls; return e; };

  function set(s) {
    if (!host.isConnected) document.documentElement.appendChild(host);
    const sel = $("select");
    if (s.jobs) {
      sel.replaceChildren(...s.jobs.map((j) => { const o = text("option", j.label); o.value = j.id; return o; }));
      sel.value = s.jobId ?? "";
    }
    $(".s").replaceChildren(text("div", s.error || s.status || "", s.error ? "e" : ""));
    const l = $(".l");
    l.replaceChildren();
    if (s.left && s.left.length) {
      l.append(text("div", `Left for you (${s.left.length}):`));
      const ul = document.createElement("ul");
      for (const t of s.left) ul.append(text("li", t));
      l.append(ul);
    }
    sel.disabled = $("button.b").disabled = !(s.done || s.error);
  }
  return { set };
})();
