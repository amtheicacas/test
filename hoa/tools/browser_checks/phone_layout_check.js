// Run on a scratch copy only (see TESTING.md, 'Browser checks'): python manage.py seed_demo, then runserver 127.0.0.1:8020, then: node phone_layout_check.js
// Crawl the site in a real browser as each kind of login at phone width: no sideways scrolling, no script errors, no error pages.
const { chromium } = require('playwright');
const BASE = 'http://127.0.0.1:8020';
const ROLES = [['manager', 'manager'], ['board', 'board'], ['committee', 'committee'], ['homeowner', 'homeowner']];
const SKIP = /^\/(static|media|accounts\/logout|admin|api|healthz)|\.(zip|pdf|csv|png|jpg|xlsx)(\?|$)|\/(photo|notice|letters?\/\d+\/pdf|case-file|template)\b/;
(async () => {
  const browser = await chromium.launch();
  const report = [];
  for (const [user, role] of ROLES) {
    for (const width of [375]) {
      const ctx = await browser.newContext({ viewport: { width, height: 812 }, deviceScaleFactor: 1 });
      const page = await ctx.newPage();
      const errors = [];
      page.on('pageerror', e => errors.push('pageerror: ' + String(e).slice(0, 160)));
      page.on('console', m => { if (m.type() === 'error') errors.push('console: ' + m.text().slice(0, 160)); });
      await page.goto(BASE + '/accounts/login/');
      await page.click('summary'); await page.fill('input[name=username]', user);
      await page.fill('input[name=password]', 'demo-password-123');
      await Promise.all([page.waitForNavigation(), page.click('details[open] button')]);
      const seen = new Set(), queue = ['/'];
      let n = 0;
      while (queue.length && n < 45) {
        const path = queue.shift();
        if (seen.has(path) || SKIP.test(path)) continue;
        seen.add(path);
        errors.length = 0;
        let resp;
        try { resp = await page.goto(BASE + path, { waitUntil: 'load', timeout: 20000 }); } catch (e) { if (!/Download is starting/.test(String(e))) report.push({ role, width, path, problem: 'goto failed ' + String(e).slice(0, 100) }); continue; }
        if (!resp) continue;
        const ct = resp.headers()['content-type'] || '';
        if (!ct.includes('html')) continue;
        n++;
        const info = await page.evaluate(() => {
          const w = window.innerWidth, over = [];
          for (const el of document.querySelectorAll('body *')) {
            const r = el.getBoundingClientRect();
            if (r.width > 0 && r.right > w + 1 && getComputedStyle(el).position !== 'fixed') {
              let p = el.parentElement, clipped = false;
              while (p && p !== document.body) { const s = getComputedStyle(p); if (/(auto|scroll|hidden)/.test(s.overflowX)) { clipped = true; break; } p = p.parentElement; }
              if (!clipped) over.push((el.tagName.toLowerCase() + (el.className ? '.' + String(el.className).split(' ')[0] : '')) + ':' + Math.round(r.right));
            }
          }
          const txt = document.body.innerText;
          return { scrollW: document.documentElement.scrollWidth, w, over: over.slice(0, 3), error: /Traceback|Server Error \(500\)|OperationalError|Exception Value/.test(txt) };
        });
        const problems = [];
        if (info.scrollW > info.w + 1) problems.push(`sideways scroll ${info.scrollW}>${info.w} (${info.over.join(', ')})`);
        if (info.error) problems.push('error text on page');
        if (resp.status() >= 500) problems.push('HTTP ' + resp.status());
        if (errors.length) problems.push(errors.slice(0, 2).join(' | '));
        if (problems.length) report.push({ role, width, path, problem: problems.join('; ') });
        const links = await page.$$eval('a[href^="/"]', as => as.map(a => a.getAttribute('href').split('#')[0]).filter(Boolean));
        for (const l of links) if (!seen.has(l) && !SKIP.test(l)) queue.push(l);
      }
      console.log(`${role} @${width}px: ${n} pages checked`);
      await ctx.close();
    }
  }
  await browser.close();
  console.log(report.length ? 'PROBLEMS:\n' + report.map(r => `  [${r.role}@${r.width}] ${r.path}: ${r.problem}`).join('\n') : 'NO PROBLEMS FOUND');
})();
