// Run by Job Radar before every fill: clicks only work while the job-search Chrome window is drawn on screen.
// Prints VISIBLE, or HIDDEN when Chrome draws no frames (window on another desktop, behind a full-screen app, or minimized).
async (page) => {
  const p = page.context().pages()[0];
  await Promise.race([p.bringToFront(), new Promise(r => setTimeout(r, 5000))]);
  const ok = await Promise.race([
    p.evaluate(() => new Promise(r => requestAnimationFrame(() => r(true)))),
    new Promise(r => setTimeout(() => r(false), 3000)),
  ]);
  return ok ? 'VISIBLE' : 'HIDDEN';
}
