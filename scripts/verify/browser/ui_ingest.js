// Ingest through the web UI, end to end (#641), run by 22_ui_ingest.sh.
//
// A real browser, logged in as the harness Engineer through the real API, uses
// the Import and Prepare pages as a user does: a new collection per case with
// "+ New", the files through the page's file input, Start Ingest (or Upload,
// Prepare selected and Import selected), and the page's own polling until the
// job ends. Nothing is stubbed or intercepted. The script only observes: what
// the pages show, the answers to the page's own requests, and the API's job and
// prepared-document records, read with fetch in the logged-in page. The cases
// come from the fixtures' ui_ingest/manifest.json, mounted at /fixtures.
//
// It prints one line `UI_INGEST_RESULT <json>` with the observations, which
// ui_ingest_check.py turns into checks. A case that can't go on records the step
// and why, and the run moves to the next case. Exit 1 only when the run itself
// can't start (browser, login, manifest).
const fs = require('fs');
const { launch, session, setValue, clickByText, sleep } = require('./lib');

const BASE = process.env.RAG_UI_BASE || 'http://localhost';
const SKIP_SLOW = process.env.RAG_SKIP_SLOW === '1';
const FIXTURES = '/fixtures';
const TERMINAL = ['completed', 'failed', 'partial', 'cancelled', 'interrupted'];
const MINUTE = 60000;

class Step extends Error {
  constructor(step, message) { super(message); this.step = step; }
}

// Runs `fn`; a failure is rethrown as the named step.
async function step(name, fn) {
  try { return await fn(); } catch (e) {
    if (e instanceof Step) throw e;
    throw new Step(name, e && e.message ? e.message.replace(/\s+/g, ' ').slice(0, 300) : String(e));
  }
}

// A request the logged-in page sends itself: { status, body }.
const apiGet = (page, path) => page.evaluate(async p => {
  const res = await fetch(p);
  let body = null;
  try { body = await res.json(); } catch { /* not JSON */ }
  return { status: res.status, body };
}, path);

// What the page shows now. `scope` is a CSS selector the progress panel and the
// Job ID line are looked for in; red messages are read page-wide.
const observe = (page, scope) => page.evaluate(sel => {
  const root = document.querySelector(sel) || document;
  const text = e => (e.textContent || '').replace(/\s+/g, ' ').trim();
  const idLine = [...root.querySelectorAll('p')].find(p => text(p).startsWith('Job ID:'));
  const panel = [...root.querySelectorAll('div.mt-4.p-4.border.rounded.bg-gray-50')]
    .find(d => text(d).startsWith('Files:'));
  let status = null, errors = [], split = [], skipped = [];
  if (panel) {
    const spans = panel.querySelectorAll(':scope > div:first-child > span');
    status = spans.length >= 3 ? text(spans[2]) : null;
    errors = [...panel.querySelectorAll('ul.text-red-600 li')].map(text);
    split = [...panel.querySelectorAll('[data-testid=split-files] li')].map(text);
    skipped = [...panel.querySelectorAll('div.text-amber-700 li')].map(text);
  }
  const red = [...document.querySelectorAll('p.text-red-600, [role=alert]')].map(text).filter(Boolean);
  return {
    job_id: idLine ? text(idLine.querySelector('.font-mono') || idLine).replace(/^Job ID:\s*/, '') : null,
    status, errors, split, skipped, red: [...new Set(red)],
  };
}, scope);

// waitForFunction for up to `minutes`, in rounds of two minutes: one round is a
// single DevTools call, which puppeteer ends after 180 s (protocolTimeout).
async function waitLong(page, minutes, fn, ...args) {
  const deadline = Date.now() + minutes * MINUTE;
  for (;;) {
    const round = Math.min(2 * MINUTE, deadline - Date.now());
    try {
      return await page.waitForFunction(fn, { timeout: Math.max(round, 1000), polling: 1000 }, ...args);
    } catch (e) {
      if (!(e && e.name === 'TimeoutError') || Date.now() >= deadline) throw e;
    }
  }
}

// The progress panel in `scope` shows job `id` with a status it never leaves.
async function waitForJob(page, scope, id, minutes) {
  await step('wait for the job to end', () => waitLong(page, minutes, (sel, jobId, terminal) => {
    const root = document.querySelector(sel) || document;
    const text = e => (e.textContent || '').replace(/\s+/g, ' ').trim();
    const idLine = [...root.querySelectorAll('p')].find(p => text(p).startsWith('Job ID:'));
    if (!idLine || !text(idLine).endsWith(jobId)) return false;
    const panel = [...root.querySelectorAll('div.mt-4.p-4.border.rounded.bg-gray-50')]
      .find(d => text(d).startsWith('Files:'));
    const spans = panel ? panel.querySelectorAll(':scope > div:first-child > span') : [];
    return spans.length >= 3 && terminal.includes(text(spans[2]));
  }, scope, id, TERMINAL));
}

// "+ New" in `scope`, the name typed in the modal, Create; the scope's
// collection selector then shows the new collection.
async function newCollection(page, scope, name) {
  await step('new collection', async () => {
    const opened = await page.evaluate(sel => {
      const b = [...document.querySelector(sel).querySelectorAll('button')].find(x => x.textContent.trim() === '+ New');
      if (!b) return false; b.click(); return true;
    }, scope);
    if (!opened) throw new Error('no "+ New" button');
    await page.waitForSelector('input[placeholder="Collection name"]', { timeout: 10000 });
    await setValue(page, `() => document.querySelector('input[placeholder="Collection name"]')`, name);
    if (!await clickByText(page, 'Create')) throw new Error('no Create button');
    await page.waitForFunction((sel, n) => document.querySelector(sel)?.querySelector('select')?.value === n,
      { timeout: 15000 }, scope, name);
  });
}

// The page's file input gets the case's files; the page then lists each.
async function chooseFiles(page, inputId, files) {
  await step('choose files', async () => {
    const input = await page.$('#' + inputId);
    if (!input) throw new Error('no #' + inputId);
    await input.uploadFile(...files.map(f => `${FIXTURES}/${f}`));
    await page.waitForFunction(names => {
      const shown = [...document.querySelectorAll('li span')].map(s => s.textContent.trim());
      return names.every(n => shown.includes(n));
    }, { timeout: 10000 }, files);
  });
}

// Clicks `label` and returns the answer to the page's own POST to `path`.
async function send(page, label, path) {
  return step('send: ' + label, async () => {
    const answer = page.waitForResponse(r => r.request().method() === 'POST' && new URL(r.url()).pathname === path,
      { timeout: 5 * MINUTE });
    if (!await clickByText(page, label)) throw new Error(`no "${label}" button`);
    const res = await answer;
    let body = null;
    try { body = await res.json(); } catch { /* not JSON */ }
    return { status: res.status(), body };
  });
}

// The API's record of job `id`, and the jobs of `collection` in the job list.
async function jobRecords(page, id, collection) {
  const record = id ? await apiGet(page, `/api/ingest/job/${encodeURIComponent(id)}`) : null;
  const list = await apiGet(page, '/api/ingest/jobs');
  const jobs = (list.body && Array.isArray(list.body.jobs) ? list.body.jobs : [])
    .filter(j => j.collection === collection).map(j => ({ job_id: j.job_id, status: j.status }));
  return { record, jobs, jobs_status: list.status };
}

// Sets a slider by its label's text and returns what the label then shows.
async function slider(page, label, value) {
  return page.evaluate((l, v) => {
    const lab = [...document.querySelectorAll('label')].find(x => x.textContent.trim().startsWith(l + ':'));
    const input = lab?.parentElement?.querySelector('input[type=range]');
    if (!input) return null;
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set.call(input, String(v));
    input.dispatchEvent(new Event('input', { bubbles: true }));
    return true;
  }, label, value);
}

const sliderLabels = page => page.evaluate(() => [...document.querySelectorAll('label')]
  .map(l => l.textContent.trim()).filter(t => /^(Chunk Size|Overlap|Min Chunk Size):/.test(t)));

// Import page: a new collection, the files, Start Ingest, the job to its end.
async function ingestCase(page, c, out) {
  await step('open the Import page', () => page.goto(BASE + '/import', { waitUntil: 'networkidle2' }));
  await newCollection(page, 'main', c.collection);
  if (c.settings.chunk_size !== undefined) {
    await step('set the strategy and the sliders', async () => {
      const found = await page.evaluate(v => !![...document.querySelectorAll('main select')]
        .find(x => [...x.options].some(o => o.value === v)), c.settings.strategy);
      if (!found) throw new Error('no Chunking Strategy selector');
      await setValue(page, `() => [...document.querySelectorAll('main select')].find(x => [...x.options].some(o => o.value === ${JSON.stringify(c.settings.strategy)}))`, c.settings.strategy);
      await sleep(300);
      if (!await slider(page, 'Chunk Size', c.settings.chunk_size)) throw new Error('no Chunk Size slider');
      if (!await slider(page, 'Min Chunk Size', c.settings.min_chunk_size)) throw new Error('no Min Chunk Size slider');
      await sleep(300);
    });
    out.sliders = await sliderLabels(page);
    out.strategy = await page.evaluate(v => [...document.querySelectorAll('main select')]
      .find(x => [...x.options].some(o => o.value === v))?.value, c.settings.strategy);
  }
  await chooseFiles(page, 'file-input', c.files);
  out.upload = await send(page, 'Start Ingest', '/api/ingest/upload');
  const id = out.upload.status === 202 && out.upload.body ? out.upload.body.job_id : null;
  if (id) {
    await waitForJob(page, 'main', id, c.settings.chunk_size !== undefined ? 20 : 10);
  } else {
    await step('wait for the message', () => page.waitForFunction(
      () => [...document.querySelectorAll('p.text-red-600, [role=alert]')].some(e => e.textContent.trim()),
      { timeout: 15000 }));
  }
  out.page = await observe(page, 'main');
  Object.assign(out, await jobRecords(page, id, c.collection));
}

// Prepare page: Upload, Prepare selected, then Import selected into a new collection.
async function prepareCase(page, c, out) {
  await step('open the Prepare page', () => page.goto(BASE + '/prepare', { waitUntil: 'networkidle2' }));
  await chooseFiles(page, 'prepare-file-input', c.files);
  out.prepare_upload = await send(page, 'Upload', '/api/prepare/upload');
  const ids = await step('find the uploaded documents', async () => {
    await page.waitForFunction(names => names.every(n => [...document.querySelectorAll('tr[data-document]')]
      .some(r => r.cells[1] && r.cells[1].firstChild && r.cells[1].firstChild.textContent === n)),
    { timeout: 30000 }, c.files);
    return page.evaluate(names => Object.fromEntries(names.map(n => [n,
      [...document.querySelectorAll('tr[data-document]')].filter(r => r.cells[1].firstChild.textContent === n)
        .map(r => r.dataset.document).pop()])), c.files);
  });
  out.document_ids = ids;
  await step('tick the documents', async () => {
    for (const name of c.files) {
      const box = await page.$(`input[aria-label="Select ${name}"]`);
      if (!box) throw new Error('no checkbox for ' + name);
      await box.click();
    }
  });
  out.prepare_start = await send(page, 'Prepare selected', '/api/prepare/jobs');
  const prepareId = out.prepare_start.body ? out.prepare_start.body.job_id : null;
  if (out.prepare_start.status !== 202 || !prepareId) throw new Step('prepare', `the start answered ${out.prepare_start.status}`);
  await step('wait for the preparation to end', () => waitLong(page, 20, (jobId, terminal) => {
    const s = document.querySelector('section[aria-label="Preparation progress"]');
    if (!s || !s.textContent.includes(jobId)) return false;
    const status = s.querySelector(':scope > div:first-child > span.text-blue-600');
    return !!status && terminal.includes(status.textContent.trim());
  }, prepareId, TERMINAL));
  await sleep(1000);      // the list is read again when the job ends
  out.preparation = await page.evaluate((jobId, names) => {
    const text = e => (e.textContent || '').replace(/\s+/g, ' ').trim();
    const s = document.querySelector('section[aria-label="Preparation progress"]');
    const heads = [...document.querySelectorAll('section[aria-label="Prepared documents"] thead th')].map(text);
    const col = heads.indexOf('Status');
    const rows = {};
    for (const r of document.querySelectorAll('tr[data-document]')) {
      const name = r.cells[1].firstChild.textContent;
      if (!names.includes(name)) continue;
      const err = r.cells[1].querySelector('div.text-red-600');
      rows[name] = { status: col >= 0 ? text(r.cells[col]) : null, last_error: err ? text(err) : null };
    }
    return {
      job_id: jobId, shows_job: !!s && s.textContent.includes(jobId),
      status: s ? text(s.querySelector(':scope > div:first-child > span.text-blue-600')) : null,
      errors: s ? [...s.querySelectorAll('ul.text-red-600 li')].map(text) : [], rows,
    };
  }, prepareId, c.files);
  out.records = {};
  for (const [name, id] of Object.entries(ids)) {
    const r = await apiGet(page, `/api/prepare/documents/${encodeURIComponent(id)}`);
    out.records[name] = { status: r.status, parts: r.body && Array.isArray(r.body.parts) ? r.body.parts.map(p => p.name) : null };
  }
  const scope = 'section[aria-label="Import prepared documents"]';
  await newCollection(page, scope, c.collection);
  out.upload = await send(page, 'Import selected', '/api/prepare/import');
  const id = out.upload.status === 202 && out.upload.body ? out.upload.body.job_id : null;
  if (!id) throw new Step('import', `the import answered ${out.upload.status}`);
  await waitForJob(page, scope, id, 10);
  out.page = await observe(page, scope);
  Object.assign(out, await jobRecords(page, id, c.collection));
}

(async () => {
  const manifest = JSON.parse(fs.readFileSync(`${FIXTURES}/manifest.json`, 'utf8'));
  const browser = await launch();
  const result = { skip_slow: SKIP_SLOW, cases: {}, collections: null };
  let exit = 0;
  try {
    const s = await session(browser, BASE, 'engineer');
    s.page.setDefaultTimeout(60000);
    for (const c of manifest.cases) {
      const out = { collection: c.collection };
      result.cases[c.name] = out;
      if (c.slow && SKIP_SLOW) { out.skipped = true; console.log(`    case ${c.name}: skipped (RAG_SKIP_SLOW=1)`); continue; }
      const started = Date.now();
      try {
        await (c.kind === 'prepare' ? prepareCase : ingestCase)(s.page, c, out);
      } catch (e) {
        out.failed_step = e instanceof Step ? e.step : 'unexpected';
        out.error = e && e.message ? e.message.replace(/\s+/g, ' ').slice(0, 300) : String(e);
        try { out.page = out.page || await observe(s.page, 'main'); } catch { /* the page is gone */ }
      }
      out.seconds = Math.round((Date.now() - started) / 1000);
      const ended = out.upload && out.upload.status !== 202 ? `upload answered ${out.upload.status}`
        : 'job ' + (out.page && out.page.status);
      console.log(`    case ${c.name}: ${out.failed_step ? 'stopped at ' + out.failed_step + ': ' + out.error : ended} (${out.seconds} s)`);
    }
    await s.page.goto(BASE + '/collections', { waitUntil: 'networkidle2' });
    await sleep(500);
    result.collections = await s.page.evaluate(() => Object.fromEntries([...document.querySelectorAll('tbody tr')]
      .map(r => [r.cells[0].textContent.trim(), Number(r.cells[1].textContent.trim())])));
    await s.ctx.close();
  } catch (e) {
    console.log(`    the run could not go on: ${e && e.message ? e.message.replace(/\s+/g, ' ').slice(0, 300) : e}`);
    exit = 1;
  }
  await browser.close();
  console.log('UI_INGEST_RESULT ' + JSON.stringify(result));
  process.exit(exit);
})().catch(e => { console.log(`    the run could not start: ${e && e.message ? e.message : e}`); process.exit(1); });
