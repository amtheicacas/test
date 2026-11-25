// Run on a scratch copy only (see TESTING.md, 'Browser checks'): after seed_demo, put script-like text in the records (owner names, descriptions...), then: node script_injection_check.js
// Real-browser XSS canary: records such as owner names and descriptions hold script-like text; no script may ever run on any page, popup or picker.
const { chromium } = require('playwright');
const BASE = 'http://127.0.0.1:8020';
const SKIP = /^\/(static|media|accounts\/logout|admin|api|healthz)|\.(zip|pdf|csv|png|jpg|xlsx)(\?|$)|\/(photo|notice|letters?\/\d+|notices\/\d+\/pdf|case-file|template)\b/;
(async () => {
  const browser = await chromium.launch();
  const hits = [];
  for (const user of ['manager', 'board', 'committee', 'homeowner']) {
    const ctx = await browser.newContext({ viewport: { width: 1100, height: 900 } });
    const page = await ctx.newPage();
    page.on('dialog', d => { hits.push('[' + user + '] a dialog opened: ' + d.message()); d.dismiss(); });
    await page.goto(BASE + '/accounts/login/');
    await page.click('summary');
    await page.fill('input[name=username]', user);
    await page.fill('input[name=password]', 'demo-password-123');
    await Promise.all([page.waitForNavigation(), page.click('details[open] button')]);
    const seen = new Set();
    const queue = ['/', '/violations/new/', '/map/'];
    let n = 0;
    while (queue.length && n < 60) {
      const path = queue.shift();
      if (seen.has(path) || SKIP.test(path)) continue;
      seen.add(path);
      let resp;
      try { resp = await page.goto(BASE + path, { waitUntil: 'load', timeout: 20000 }); } catch (e) { continue; }
      if (!resp || !(resp.headers()['content-type'] || '').includes('html')) continue;
      n++;
      if (path.startsWith('/map/')) {
        for (const el of (await page.$$('.leaflet-interactive')).slice(0, 8)) { try { await el.click({ timeout: 1500, force: true }); } catch (e) {} }
        for (const cb of await page.$$('#filters input[type=checkbox]')) { try { await cb.check({ timeout: 800 }); } catch (e) {} }
        for (const r of await page.$$('#filters input[name=view]')) { try { await r.check({ timeout: 800 }); } catch (e) {} }
      }
      if (path.startsWith('/violations/new/')) {
        for (const inp of (await page.$$('input[type=search], input[type=text]')).slice(0, 4)) {
          try { await inp.fill('Pat', { timeout: 800 }); await inp.fill('<img', { timeout: 800 }); } catch (e) {}
        }
      }
      const x = await page.evaluate(() => window.__xss);
      if (x) hits.push('[' + user + '] script ran on ' + path + ' (window.__xss=' + x + ')');
      const links = await page.$$eval('a[href^="/"]', as => as.map(a => a.getAttribute('href').split('#')[0]).filter(Boolean));
      for (const l of links) if (!seen.has(l) && !SKIP.test(l)) queue.push(l);
    }
    console.log(user + ': ' + n + ' pages, popups and pickers exercised');
    await ctx.close();
  }
  await browser.close();
  console.log(hits.length ? 'SCRIPT RAN (XSS):\n  ' + hits.join('\n  ') : 'NO SCRIPT RAN ANYWHERE');
})();
