// Shared browser-test helpers.
const puppeteer = require('puppeteer-core');

const sleep = ms => new Promise(r => setTimeout(r, ms));

function makeReporter() {
  let pass = 0, fail = 0, skip = 0;
  const failed = [];
  return {
    check(name, ok, detail = '') {
      if (ok) { pass++; console.log(`    PASS  ${name}`); }
      else { fail++; failed.push(name); console.log(`    FAIL  ${name}${detail ? '  — ' + detail : ''}`); }
    },
    skip(name, why = '') { skip++; console.log(`    SKIP  ${name}${why ? '  — ' + why : ''}`); },
    section(t) { console.log(`\n  ${t}`); },
    summary() {
      console.log(`\n  ${pass} passed, ${fail} failed, ${skip} skipped`);
      if (fail) { console.log('  failed:'); failed.forEach(f => console.log(`    - ${f}`)); }
      return fail === 0;
    },
  };
}

async function launch() {
  return puppeteer.launch({
    executablePath: '/usr/bin/chromium-browser', headless: 'new',
    args: ['--no-sandbox', '--disable-dev-shm-usage', '--disable-gpu'],
  });
}

// The test accounts' credentials (#25), read once from standard input:
// 06_ui.sh runs this container with the harness's credentials file as stdin
// (RAG_AUTH_STDIN=1), so no password is on a command line or in the
// environment. Lines are `role username password`.
let credentials = null;
function credentialsFor(role) {
  if (credentials === null) {
    credentials = {};
    if (process.env.RAG_AUTH_STDIN === '1') {
      for (const line of require('fs').readFileSync(0, 'utf8').split('\n')) {
        const [r, username, ...rest] = line.split(' ');
        if (r && username && rest.length) credentials[r] = { username, password: rest.join(' ') };
      }
    }
  }
  if (!credentials[role]) throw new Error(`no test credentials for ${role}; run through 06_ui.sh`);
  return credentials[role];
}

// Logs the page's browser context in as the role's test account, through the
// API, as the login page does. The session is the HttpOnly cookie the answer sets.
async function login(page, base, role) {
  const { username, password } = credentialsFor(role);
  // Any same-origin page will do; /api/health is not the UI, so nothing asks
  // GET /api/auth/me before the login and logs its 401 as a console error.
  if (!page.url().startsWith(base)) await page.goto(base + '/api/health', { waitUntil: 'domcontentloaded' });
  // nginx allows 10 logins a minute per address, a burst of 5 (#577): a login
  // it refuses (429 LOGIN_RATE_LIMITED) is sent again once a slot has refilled.
  const deadline = Date.now() + 300000;
  for (;;) {
    const { status, code } = await page.evaluate(async (u, p) => {
      const res = await fetch('/api/auth/login', {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ username: u, password: p }),
      });
      let code = null;
      try { code = (await res.json()).error.code; } catch { /* not an error envelope */ }
      return { status: res.status, code };
    }, username, password);
    if (status === 200) return;
    if (status !== 429 || code !== 'LOGIN_RATE_LIMITED' || Date.now() >= deadline) {
      throw new Error(`login as ${role} answered ${status}`);
    }
    await new Promise(resolve => setTimeout(resolve, 6500));
  }
}

// For fixtures that answer every /api/ request themselves: answers
// GET /api/auth/status and GET /api/auth/me for `role` (null: logged out) and
// passes everything else on. Registered after a fixture's own fetch stub, it
// wraps that stub.
async function stubAuth(page, role) {
  await page.evaluateOnNewDocument(r => {
    const inner = window.fetch.bind(window);
    window.fetch = (input, init) => {
      const path = new URL(typeof input === 'string' ? input : input.url, location.href).pathname;
      const reply = (status, body) => Promise.resolve(new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } }));
      if (path === '/api/auth/status') return reply(200, { setup_required: false });
      if (path === '/api/auth/me') {
        return r ? reply(200, { username: 'vfy-' + r.replace('_', '-'), role: r, must_change_password: false })
                 : reply(401, { error: { code: 'AUTH_REQUIRED', message: 'Log in to use this.', detail: null } });
      }
      return inner(input, init);
    };
  }, role);
}

// A fresh browser context per role, with console errors and API calls recorded.
// Console errors matter: a React component that throws unmounts the whole app,
// which once turned every page blank after visiting /health.
// With a role, the context is logged in as that role's test account.
async function session(browser, base, role) {
  const ctx = await browser.createBrowserContext();
  const page = await ctx.newPage();
  const errors = [], api = [];
  page.on('pageerror', e => errors.push('pageerror: ' + e.message));
  page.on('console', m => { if (m.type() === 'error') errors.push('console.error: ' + m.text()); });
  page.on('request', r => {
    const u = r.url();
    if (u.includes('/api/')) api.push({ method: r.method(), url: u.replace(/^https?:\/\/[^/]+/, ''), at: Date.now() });
  });
  if (role) await login(page, base, role);
  await page.goto(base + '/', { waitUntil: 'networkidle2' });
  return { ctx, page, errors, api };
}

const bodyText = page => page.evaluate(
  () => (document.querySelector('#root')?.innerText || '').replace(/\s+/g, ' ').trim());

// React tracks input state internally, so assigning .value is ignored. Use the
// native setter and dispatch the event React listens for.
const setValue = (page, selectorFn, value) => page.evaluate((fn, v) => {
  const el = eval(fn)();
  const proto = el instanceof HTMLSelectElement ? HTMLSelectElement.prototype
              : el instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype
              : HTMLInputElement.prototype;
  Object.getOwnPropertyDescriptor(proto, 'value').set.call(el, v);
  el.dispatchEvent(new Event(el instanceof HTMLSelectElement ? 'change' : 'input', { bubbles: true }));
}, selectorFn, value);

const clickByText = (page, text) => page.evaluate(t => {
  const el = [...document.querySelectorAll('a,button')].find(e => (e.textContent || '').trim() === t);
  if (!el) return false; el.click(); return true;
}, text);

// The nav's menus (#616). A menu button is `nav button[data-menu=<key>]` (data,
// tune, admin, account); while its menu is open, its aria-controls names the
// role="menu" element. A closed menu's entries aren't in the page.
async function openMenu(page, key) {
  const found = await page.evaluate(k => {
    const b = document.querySelector(`nav button[data-menu="${k}"]`);
    if (!b) return false;
    if (b.getAttribute('aria-expanded') !== 'true') b.click();
    return true;
  }, key);
  if (!found) throw new Error(`no ${key} menu in the nav`);
  await page.waitForFunction(k => {
    const b = document.querySelector(`nav button[data-menu="${k}"]`);
    return b && b.getAttribute('aria-expanded') === 'true' && !!document.getElementById(b.getAttribute('aria-controls') || '');
  }, { timeout: 5000 }, key);
}

async function chooseFromMenu(page, key, label) {
  await openMenu(page, key);
  const chosen = await page.evaluate((k, t) => {
    const b = document.querySelector(`nav button[data-menu="${k}"]`);
    const m = document.getElementById(b.getAttribute('aria-controls'));
    const e = [...m.querySelectorAll('[role=menuitem]')].find(x => x.textContent.trim() === t);
    if (!e) return false; e.click(); return true;
  }, key, label);
  if (!chosen) throw new Error(`no "${label}" in the ${key} menu`);
}

// The nav as the user sees it: the top-level links, then each menu in order
// with its label and the entries it shows when opened. Each menu is opened and
// closed again. `pages` is the links and every menu's entries but the account's.
async function readNav(page) {
  const { links, keys } = await page.evaluate(() => ({
    links: [...document.querySelectorAll('nav a')].filter(a => !a.closest('[role=menu]')).map(a => a.textContent.trim()),
    keys: [...document.querySelectorAll('nav button[aria-haspopup=menu]')].map(b => b.dataset.menu),
  }));
  const menus = [];
  for (const key of keys) {
    await openMenu(page, key);
    menus.push(await page.evaluate(k => {
      const b = document.querySelector(`nav button[data-menu="${k}"]`);
      const shown = b.cloneNode(true);
      shown.querySelectorAll('[aria-hidden=true]').forEach(x => x.remove());
      const m = document.getElementById(b.getAttribute('aria-controls'));
      return { key: k, label: shown.textContent.replace(/\s+/g, ' ').trim(), entries: [...m.querySelectorAll('[role=menuitem]')].map(x => x.textContent.trim()) };
    }, key));
    await page.evaluate(k => document.querySelector(`nav button[data-menu="${k}"]`).click(), key);
    await page.waitForFunction(k => document.querySelector(`nav button[data-menu="${k}"]`)?.getAttribute('aria-expanded') === 'false', { timeout: 5000 }, key);
  }
  return { links, menus, pages: [...links, ...menus.filter(m => m.key !== 'account').flatMap(m => m.entries)] };
}

module.exports = { sleep, makeReporter, launch, session, login, stubAuth, credentialsFor, bodyText, setValue, clickByText, openMenu, chooseFromMenu, readNav };
