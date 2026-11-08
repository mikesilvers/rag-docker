// Deferred API responses against the rendered Chunking page; no backend writes.
const assert = require('node:assert/strict');
const { makeReporter, clickByText, setValue, stubAuth } = require('./lib');
const A = { collection: 'FixtureA', chunking_strategy: 'fixed', chunk_size: 100, chunk_overlap: 0, min_chunk_size: 0, similarity_threshold: null, is_default: false };
const B = { ...A, collection: 'FixtureB', chunk_size: 2000 };
const path = name => '/api/ingest/config/' + name;
async function fixture(browser, base, { collectionsFail = false } = {}) {
  const ctx = await browser.createBrowserContext();
  const page = await ctx.newPage();
  const errors = [];
  page.on('pageerror', e => errors.push(e.message));
  await page.evaluateOnNewDocument((a, b, failCollections) => {
    const realFetch = window.fetch.bind(window);
    window.chunkingFixture = { requests: [] };
    window.fetch = (input, init = {}) => {
      const path = new URL(input, location.href).pathname;
      if (!path.startsWith('/api/')) return realFetch(input, init);
      if (path === '/api/collections' && failCollections) return Promise.resolve(new Response(JSON.stringify({ error: { message: 'Collections list failed' } }), { status: 500 }));
      if (path === '/api/collections') return Promise.resolve(new Response(JSON.stringify({ collections: [a, b].map(c => ({ name: c.collection, object_count: 1, index_type: 'hnsw', distance_metric: 'cosine' })) }), { status: 200 }));
      if (path.startsWith('/api/ingest/config')) {
        const entry = { path, method: init.method || 'GET', body: init.body ? JSON.parse(init.body) : null, done: false };
        window.chunkingFixture.requests.push(entry);
        return new Promise(resolve => { entry.resolve = (value, status) => { entry.done = true; resolve(new Response(JSON.stringify(value), { status })); }; });
      }
      return Promise.reject(new Error('Unexpected fixture request ' + path));
    };
  }, A, B, collectionsFail);
  // The engineer, as GET /api/auth/me answers it, without a backend (#25).
  await stubAuth(page, 'engineer');
  await page.goto(base + '/chunking', { waitUntil: 'domcontentloaded' });
  if (!collectionsFail) await pending(page, path(A.collection));
  return { ctx, page, errors };
}
async function pending(page, url, method = 'GET', count = 1) {
  await page.waitForFunction((p, m, n) => window.chunkingFixture.requests.filter(r => r.path === p && r.method === m && !r.done).length >= n, {}, url, method, count);
}
async function release(page, url, value, { method = 'GET', status = 200, last = false } = {}) {
  await pending(page, url, method);
  await page.evaluate(async (p, m, v, s, latest) => {
    const entries = window.chunkingFixture.requests.filter(r => r.path === p && r.method === m && !r.done);
    (latest ? entries.at(-1) : entries[0]).resolve(v, s);
    await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
  }, url, method, value, status, last);
}
async function select(page, name) {
  const count = await page.evaluate(p => window.chunkingFixture.requests.filter(r => r.path === p && r.method === 'GET' && !r.done).length, path(name));
  await page.select('select', name);
  await pending(page, path(name), 'GET', count + 1);
}
async function assertConfig(page, config) {
  assert.deepEqual(await page.evaluate(() => ({ collection: document.querySelector('select').value, size: Number(document.querySelector('input[type=range]')?.value) })), { collection: config.collection, size: config.chunk_size });
}
async function startSave(page) { assert.equal(await clickByText(page, 'Save as Default'), true); await pending(page, '/api/ingest/config', 'POST'); }
async function runChunkingConfigTests(browser, base, reporter) {
  reporter.section('collection-bound Chunking settings (deferred API fixtures)');
  const cases = [
    ['late A load cannot overwrite B display or B save payload', async page => {
      await select(page, B.collection);
      await release(page, path(B.collection), B);
      await release(page, path(A.collection), A);
      await assertConfig(page, B);
      await startSave(page);
      const body = await page.evaluate(() => window.chunkingFixture.requests.find(r => r.method === 'POST').body);
      assert.equal(body.collection, B.collection); assert.equal(body.chunk_size, B.chunk_size);
    }],
    ['save is unavailable during a new load and after that load fails', async page => {
      await release(page, path(A.collection), A);
      await select(page, B.collection);
      const enabled = () => [...document.querySelectorAll('button')].some(b => b.textContent === 'Save as Default' && !b.matches(':disabled'));
      assert.equal(await page.evaluate(enabled), false);
      await release(page, path(B.collection), { error: { message: 'Owned B load failed' } }, { status: 500 });
      assert.equal(await page.evaluate(enabled), false);
      assert.equal(await page.evaluate(() => document.body.innerText.includes('Owned B load failed')), true);
    }],
    ['A to B to A keeps the newest A load', async page => {
      await select(page, B.collection); await select(page, A.collection);
      const current = { ...A, chunk_size: 500 };
      await release(page, path(A.collection), current, { last: true });
      await release(page, path(B.collection), B);
      await release(page, path(A.collection), A);
      await assertConfig(page, current);
    }],
    ['an old save cannot refresh or mark the newly selected collection saved', async page => {
      await release(page, path(A.collection), A); await startSave(page);
      await select(page, B.collection); await release(page, path(B.collection), B);
      await release(page, '/api/ingest/config', A, { method: 'POST', status: 201 });
      // Baseline performs an unguarded reload after saving; release it too.
      if (await page.evaluate(p => window.chunkingFixture.requests.some(r => r.path === p && !r.done), path(A.collection))) await release(page, path(A.collection), A);
      await assertConfig(page, B);
      assert.equal(await page.evaluate(() => document.body.innerText.includes('Saved!')), false);
    }],
    ['returning to A waits for its outstanding save before allowing another write', async page => {
      await release(page, path(A.collection), A); await startSave(page);
      await select(page, B.collection); await release(page, path(B.collection), B);
      await page.select('select', A.collection);
      await page.waitForFunction(() => document.querySelector('select').value === 'FixtureA');
      assert.equal(await page.evaluate(() => [...document.querySelectorAll('button')].some(b => b.textContent === 'Save as Default' && !b.matches(':disabled'))), false);
      assert.equal(await page.evaluate(() => window.chunkingFixture.requests.filter(r => r.method === 'POST').length), 1);
      await release(page, '/api/ingest/config', A, { method: 'POST', status: 201 });
      await release(page, path(A.collection), A);
      await assertConfig(page, A);
      await setValue(page, '() => document.querySelector("input[type=range]")', '500');
      await startSave(page);
      const bodies = await page.evaluate(() => window.chunkingFixture.requests.filter(r => r.method === 'POST').map(r => r.body));
      assert.deepEqual(bodies.map(b => [b.collection, b.chunk_size]), [[A.collection, 100], [A.collection, 500]]);
      const updated = { ...A, chunk_size: 500 };
      await release(page, '/api/ingest/config', updated, { method: 'POST', status: 201 });
      await assertConfig(page, updated);
    }],
    ['duplicate saves are prevented and failed writes preserve the edited draft', async page => {
      await release(page, path(A.collection), A);
      await setValue(page, '() => document.querySelector("input[type=range]")', '500');
      await page.evaluate(() => { const b = [...document.querySelectorAll('button')].find(b => b.textContent === 'Save as Default'); b.click(); b.click(); });
      await pending(page, '/api/ingest/config', 'POST');
      assert.equal(await page.evaluate(() => window.chunkingFixture.requests.filter(r => r.method === 'POST').length), 1);
      await release(page, '/api/ingest/config', { error: { message: 'Owned save failed' } }, { method: 'POST', status: 500 });
      await assertConfig(page, { ...A, chunk_size: 500 });
      assert.equal(await page.evaluate(() => document.body.innerText.includes('Owned save failed')), true);
    }],
    // Reviewer-added cases (#197 R3: old responses cannot replace current edits, errors or success state).
    ['a late load cannot replace edits made on the selected collection', async page => {
      await select(page, B.collection);
      await release(page, path(B.collection), B);
      await setValue(page, '() => document.querySelector("input[type=range]")', '3000');
      await release(page, path(A.collection), A);
      await assertConfig(page, { ...B, chunk_size: 3000 });
      await startSave(page);
      const body = await page.evaluate(() => window.chunkingFixture.requests.find(r => r.method === 'POST').body);
      assert.deepEqual([body.collection, body.chunk_size], [B.collection, 3000]);
    }],
    ['late load and save failures cannot set an error on the newly selected collection', async page => {
      await select(page, B.collection);
      await release(page, path(B.collection), B);
      await release(page, path(A.collection), { error: { message: 'Stale A load failed' } }, { status: 500 });
      assert.equal(await page.evaluate(() => document.body.innerText.includes('Stale A load failed')), false, 'stale load error shown under B');
      await assertConfig(page, B);
      await select(page, A.collection);
      await release(page, path(A.collection), A);
      await startSave(page);
      await select(page, B.collection);
      await release(page, path(B.collection), B);
      await release(page, '/api/ingest/config', { error: { message: 'Stale A save failed' } }, { method: 'POST', status: 500 });
      assert.equal(await page.evaluate(() => document.body.innerText.includes('Stale A save failed')), false, 'stale save error shown under B');
      await assertConfig(page, B);
    }],
    ['changing collection clears the saved notice, and a config for another collection is refused', async page => {
      await release(page, path(A.collection), A);
      await startSave(page);
      await release(page, '/api/ingest/config', A, { method: 'POST', status: 201 });
      assert.equal(await page.evaluate(() => document.body.innerText.includes('Saved!')), true, 'current save shows Saved!');
      await select(page, B.collection);
      assert.equal(await page.evaluate(() => document.body.innerText.includes('Saved!')), false, 'Saved! kept after switching to B');
      await release(page, path(B.collection), A);
      assert.equal(await page.evaluate(() => document.body.innerText.includes('belongs to another collection')), true, 'mismatched config not refused');
      assert.equal(await page.evaluate(() => [...document.querySelectorAll('button')].some(b => b.textContent === 'Save as Default')), false, 'save offered for a mismatched config');
    }],
    ['a failed collections list is reported', async page => {
      await page.waitForFunction(() => document.body.innerText.includes('Collections list failed'), { timeout: 5000 });
    }, { collectionsFail: true }],
  ];
  for (const [name, test, opts] of cases) {
    let s;
    try { s = await fixture(browser, base, opts); await test(s.page); assert.deepEqual(s.errors, []); reporter.check(name, true); }
    catch (e) { reporter.check(name, false, e.stack || e.message); }
    finally { if (s) await s.ctx.close(); }
  }
}
module.exports = { runChunkingConfigTests };
if (require.main === module) (async () => {
  const browser = await require('puppeteer-core').launch({ executablePath: process.env.RAG_CHROMIUM_PATH || '/usr/bin/chromium-browser', headless: true, args: ['--no-sandbox', '--disable-dev-shm-usage'] });
  const reporter = makeReporter();
  try { await runChunkingConfigTests(browser, process.env.RAG_UI_BASE || 'http://127.0.0.1:3000', reporter); }
  finally { await browser.close(); }
  process.exitCode = reporter.summary() ? 0 : 1;
})().catch(e => { console.error(e); process.exitCode = 2; });
