// Transfer page polling and failure display (#382), against the rendered page
// with every API call stubbed in the browser; no backend reads or writes.
//
// Each case gets fresh fetch stubs. Fixed answers cover the collections, the
// package list and the fidelity lookup unless a case holds them; every other
// /api/ request is held until the case releases it with a status and body. The
// page's 2 s poll delay is cut to 20 ms, so 30 failed polls take well under a
// second.
const assert = require('node:assert/strict');
const { makeReporter, setValue, stubAuth, chooseFromMenu } = require('./lib');

const A = 'VfyTransferA', B = 'VfyTransferB';
const PACKAGE = 'ragpkg-vfytransfera-20261021-0000-abcd1234.tar.gz';
const collectionsBody = { collections: [A, B].map(name => ({ name, object_count: 3, index_type: 'hnsw', distance_metric: 'cosine', created_at: null })) };
// An encrypted package lists no metadata (#27).
const SEALED = 'ragpkg-vfytransfera-20261021-0000-abcd1234.tar.gz.age';
// An encrypted package whose header can't be read: listed readable: false (#27).
const BROKEN = 'ragpkg-vfytransfera-20261021-0000-ffff0000.tar.gz.age';
const packagesBody = { packages: [
  { filename: PACKAGE, size_bytes: 2048, collection: A, chunk_count: 3, fidelity: 'with-sources', created_at: null, readable: true, encrypted: false },
  { filename: SEALED, size_bytes: 2100, collection: null, chunk_count: null, fidelity: null, created_at: null, readable: true, encrypted: true, encryption: 'passphrase' },
] };
// A package encrypted to its own key (#676), its key file still on the server.
const KEYED = 'ragpkg-vfytransfera-20261021-0000-cccc1111.tar.gz.age';
const keyedPackage = { filename: KEYED, size_bytes: 2200, collection: null, chunk_count: null, fidelity: null, created_at: null, readable: true, encrypted: true, encryption: 'key', key_on_server: true };
const tuneBody = name => ({ collection: name, fidelity: 'with-sources', source_document_count: 2, can_rechunk: true, can_reembed: true, can_reindex: true, note: '' });
const json = (status, body) => ({ status, body: JSON.stringify(body) });
const apiError = (status, code, msg) => json(status, { error: { code, message: msg, detail: null } });
// nginx's own page when the API is down; request() turns it into this message.
const BAD_GATEWAY = { status: 502, body: '<html><head><title>502 Bad Gateway</title></head><body><center><h1>502 Bad Gateway</h1></center><hr><center>nginx</center></body></html>' };
const DOWN = 'The API is not responding. It may still be starting; try again in a minute.';
const exportJob = (status, extra = {}) => json(200, { job_id: 'vfyexp', status, collection: A, chunks_written: 1, filename: null, size_bytes: null, source_document_count: null, fidelity: null, models_bundled: null, retrieve_script: null, warnings: [], error: null, error_code: null, error_detail: null, ...extra });
const importJob = (status, extra = {}) => json(200, { job_id: 'vfyimp', status, filename: PACKAGE, on_conflict: 'abort', collection: null, original_collection: null, chunks_written: 1, fidelity: null, renamed: false, notes: [], error: null, error_code: null, error_detail: null, ...extra });

const HALVES = {
  export: { start: '/api/export', started: json(202, { job_id: 'vfyexp', status: 'queued', collection: A }), poll: '/api/export/job/vfyexp', job: exportJob, idle: 'Export', busy: 'Exporting…', progress: "the export's progress",
            done: exportJob('completed', { chunks_written: 3, filename: 'ragpkg-vfy.tar.gz', size_bytes: 4096, fidelity: 'with-sources', models_bundled: false, retrieve_script: true }) },
  import: { start: '/api/import', started: json(202, { job_id: 'vfyimp', status: 'queued', filename: PACKAGE }), poll: '/api/import/job/vfyimp', job: importJob, idle: 'Import', busy: 'Importing…', progress: "the import's progress",
            done: importJob('completed', { collection: A, chunks_written: 3, fidelity: 'with-sources' }) },
};

async function fixture(browser, base, { hold = [], lanMode = false } = {}) {
  const ctx = await browser.createBrowserContext();
  const page = await ctx.newPage();
  const errors = [];
  page.on('pageerror', e => errors.push(e.message));
  const fixed = {
    'GET /api/collections': json(200, collectionsBody),
    'GET /api/packages': json(200, packagesBody),
    ['GET /api/tune/' + A]: json(200, tuneBody(A)),
    ['GET /api/tune/' + B]: json(200, tuneBody(B)),
    // No running or recent jobs (#617), so neither panel opens one by itself.
    'GET /api/export/jobs': json(200, { jobs: [] }),
    'GET /api/import/jobs': json(200, { jobs: [] }),
  };
  for (const key of hold) delete fixed[key];
  await page.evaluateOnNewDocument(answers => {
    const realTimeout = window.setTimeout.bind(window);
    window.setTimeout = (fn, ms, ...args) => realTimeout(fn, ms === 2000 ? 20 : ms, ...args);
    const realFetch = window.fetch.bind(window);
    window.transferFixture = { requests: [] };
    window.fetch = (input, init = {}) => {
      const path = new URL(input, location.href).pathname;
      if (!path.startsWith('/api/')) return realFetch(input, init);
      const method = init.method || 'GET';
      const entry = { path, method, done: false, body: init.body || null };
      window.transferFixture.requests.push(entry);
      const answer = answers[method + ' ' + path];
      if (answer) { entry.done = true; return Promise.resolve(new Response(answer.body, { status: answer.status })); }
      return new Promise(resolve => { entry.resolve = (status, body) => { entry.done = true; resolve(new Response(body, { status })); }; });
    };
  }, fixed);
  // The engineer, as GET /api/auth/me answers it, without a backend (#25).
  await stubAuth(page, 'engineer');
  if (lanMode) {
    // The same engineer, with LAN mode on in the user answer (#676).
    await page.evaluateOnNewDocument(() => {
      const inner = window.fetch.bind(window);
      window.fetch = (input, init) => {
        const path = new URL(typeof input === 'string' ? input : input.url, location.href).pathname;
        if (path === '/api/auth/me') return Promise.resolve(new Response(JSON.stringify({ username: 'vfy-engineer', role: 'engineer', must_change_password: false, lan_mode: true }), { status: 200, headers: { 'Content-Type': 'application/json' } }));
        return inner(input, init);
      };
    });
  }
  await page.goto(base + '/transfer', { waitUntil: 'domcontentloaded' });
  await page.waitForFunction(() => document.querySelectorAll('select option').length > 0 && document.body.innerText.includes('Export a collection'));
  return { ctx, page, errors };
}

const count = (page, path, method = 'GET') => page.evaluate((p, m) => window.transferFixture.requests.filter(r => r.path === p && r.method === m).length, path, method);
async function pending(page, path, method = 'GET') {
  await page.waitForFunction((p, m) => window.transferFixture.requests.some(r => r.path === p && r.method === m && !r.done), { timeout: 5000 }, path, method)
    .catch(() => { throw new Error(`no ${method} ${path} request arrived`); });
}
async function release(page, path, answer, method = 'GET') {
  await pending(page, path, method);
  await page.evaluate((p, m, status, body) => {
    window.transferFixture.requests.find(r => r.path === p && r.method === m && !r.done).resolve(status, body);
  }, path, method, answer.status, answer.body);
}
const text = page => page.evaluate(() => document.body.innerText.replace(/\s+/g, ' '));
async function waitText(page, want, gone = false) {
  await page.waitForFunction((w, g) => document.body.innerText.replace(/\s+/g, ' ').includes(w) !== g, { timeout: 5000 }, want, gone)
    .catch(async () => { throw new Error(`${gone ? 'still shows' : 'never showed'} "${want}"; page: ${(await text(page)).slice(0, 600)}`); });
}
// The half's start button, found by either of its labels.
const button = (page, half) => page.evaluate((idle, busy) => {
  const b = [...document.querySelectorAll('button')].find(x => [idle, busy].includes(x.textContent.trim()));
  return b ? { label: b.textContent.trim(), disabled: b.disabled } : null;
}, HALVES[half].idle, HALVES[half].busy);
async function expectButton(page, half, busy) {
  const want = busy ? HALVES[half].busy : HALVES[half].idle;
  await page.waitForFunction((label, disabled) => [...document.querySelectorAll('button')].some(x => x.textContent.trim() === label && x.disabled === disabled), { timeout: 5000 }, want, busy)
    .catch(async () => { throw new Error(`expected "${want}" ${busy ? 'disabled' : 'enabled'}, got ${JSON.stringify(await button(page, half))}`); });
}
// Buttons only by default: the nav's Data menu has an "Import" link as well as the Import button.
const click = (page, label, selector = 'button') => page.evaluate((t, sel) => {
  const b = [...document.querySelectorAll(sel)].find(x => x.textContent.trim() === t);
  if (!b) return false; b.click(); return true;
}, label, selector);

// Selects the package (import) and starts the half; leaves its start request held.
async function start(page, half) {
  if (half === 'import') {
    await setValue(page, `() => [...document.querySelectorAll('select')].find(s => [...s.options].some(o => o.value === ${JSON.stringify(PACKAGE)}))`, PACKAGE);
  }
  await expectButton(page, half, false);
  assert.equal(await click(page, HALVES[half].idle), true, `no ${HALVES[half].idle} button`);
  await pending(page, HALVES[half].start, 'POST');
}
async function startAndPoll(page, half) {
  await start(page, half);
  await release(page, HALVES[half].start, HALVES[half].started, 'POST');
  await pending(page, HALVES[half].poll);
}
// What the export panel shows while its job waits `queued` for the collection's writer guard (#520).
const QUEUED_EXPORT = 'Queued: waiting for another job on this collection to finish.';
const retrying = half => `Could not check ${HALVES[half].progress}, retrying: ${DOWN}`;
const stopped = half => `Stopped checking ${HALVES[half].progress} after 30 failed attempts; the ${half} may still be running. Last error: ${DOWN}`;

async function sleep(ms) { await new Promise(r => setTimeout(r, ms)); }

// Encryption controls (#27, #676): three radio choices.
const passwords = page => page.evaluate(() => [...document.querySelectorAll('input[type=password]')].map(i => i.value));
const typePassword = (page, index, value) => setValue(page, `() => document.querySelectorAll('input[type=password]')[${index}]`, value);
const encryptionChoices = page => page.evaluate(() => [...document.querySelectorAll('input[type=radio][name=encryption]')]
  .map(i => ({ value: i.value, checked: i.checked, label: i.closest('label').querySelector(':scope > div > div').textContent })));
const chooseEncryption = (page, value) => page.evaluate(v => {
  const input = document.querySelector(`input[type=radio][name=encryption][value=${v}]`);
  if (!input) return false; input.click(); return true;
}, value);
const checkEncrypt = page => chooseEncryption(page, 'passphrase');
const sentBody = (page, path) => page.evaluate(p => {
  const r = window.transferFixture.requests.filter(x => x.path === p && x.method === 'POST').pop();
  return r && r.body ? JSON.parse(r.body) : null;
}, path);
const selectPackage = (page, name) => setValue(page, `() => [...document.querySelectorAll('select')].find(s => [...s.options].some(o => o.value === ${JSON.stringify(name)}))`, name);

async function runTransferPageTests(browser, base, reporter) {
  reporter.section('Transfer page polling and failures (#382)');
  const cases = [];
  for (const half of ['export', 'import']) {
    const h = HALVES[half];
    cases.push([`${half}: a failed poll is shown and retried with the button still busy, and the next good poll clears it`, async page => {
      await startAndPoll(page, half);
      await release(page, h.poll, BAD_GATEWAY);
      await waitText(page, retrying(half));
      await expectButton(page, half, true);
      await release(page, h.poll, h.job('running'));
      await waitText(page, retrying(half), true);
      await release(page, h.poll, BAD_GATEWAY);
      await waitText(page, retrying(half));
      await expectButton(page, half, true);
      await release(page, h.poll, h.done);
      await waitText(page, 'Done.');
      await waitText(page, retrying(half), true);
      await expectButton(page, half, false);
    }]);
    cases.push([`${half}: after 30 failed polls in a row polling stops, the reason is shown and the button is usable; a new start clears it`, async page => {
      await startAndPoll(page, half);
      await release(page, h.poll, h.job('running'));
      for (let i = 0; i < 30; i++) await release(page, h.poll, BAD_GATEWAY);
      await waitText(page, stopped(half));
      await expectButton(page, half, false);
      const polls = await count(page, h.poll);
      await sleep(400);
      assert.equal(await count(page, h.poll), polls, 'polling went on after the limit');
      assert.equal(polls, 31, 'one good poll and 30 failed ones');
      await start(page, half);
      await waitText(page, stopped(half), true);
    }]);
    cases.push([`${half}: a poll in flight when the page is left schedules no further poll`, async page => {
      await startAndPoll(page, half);
      await release(page, h.poll, h.job('running'));
      await pending(page, h.poll);
      await chooseFromMenu(page, 'admin', 'Collections');  // throws if there is no such entry
      await page.waitForFunction(() => location.pathname === '/collections');
      await release(page, h.poll, h.job('running'));
      await sleep(400);
      assert.equal(await count(page, h.poll), 2, 'a poll was sent after the page was left');
    }]);
  }
  cases.push(['export: a start answered after the page was left starts no polling', async page => {
    await start(page, 'export');
    await chooseFromMenu(page, 'admin', 'Collections');  // throws if there is no such entry
    await page.waitForFunction(() => location.pathname === '/collections');
    await release(page, HALVES.export.start, HALVES.export.started, 'POST');
    await sleep(400);
    assert.equal(await count(page, HALVES.export.poll), 0, 'a poll was sent after the page was left');
  }]);
  cases.push(['export: a queued export says it is waiting, not writing, until it runs (#520)', async page => {
    await startAndPoll(page, 'export');
    await release(page, HALVES.export.poll, exportJob('queued', { chunks_written: 0 }));
    await waitText(page, QUEUED_EXPORT);
    await waitText(page, 'Writing…', true);
    await release(page, HALVES.export.poll, exportJob('running'));
    await waitText(page, 'Writing… 1 chunks so far.');
    await waitText(page, QUEUED_EXPORT, true);
  }]);
  for (const [status, code, msg] of [[409, 'SOURCE_INDEX_INVALID', 'Retained source index is invalid.'],
                                     [503, 'SERVICE_UNAVAILABLE', 'Synthetic tune options failure.'],
                                     [404, 'COLLECTION_NOT_FOUND', `Collection '${A}' not found.`]]) {
    cases.push([`fidelity: a ${status} ${code} from /tune is labelled, not hidden`, async page => {
      await pending(page, '/api/tune/' + A);
      await waitText(page, 'Fidelity: reading…');
      await release(page, '/api/tune/' + A, apiError(status, code, msg));
      await waitText(page, `Fidelity: unknown — it could not be read: ${msg}`);
    }, { hold: ['GET /api/tune/' + A] }]);
  }
  cases.push(['fidelity: a good lookup shows the fidelity line', async page => {
    await waitText(page, 'Fidelity: with-sources — 2 original document(s) will travel with the package.');
  }]);
  cases.push(['fidelity: a late answer for a collection no longer selected is ignored', async page => {
    await pending(page, '/api/tune/' + A);
    await setValue(page, `() => [...document.querySelectorAll('select')].find(s => [...s.options].some(o => o.value === ${JSON.stringify(B)}))`, B);
    await release(page, '/api/tune/' + B, json(200, { ...tuneBody(B), fidelity: 'chunks-only', source_document_count: 0 }));
    await waitText(page, 'Fidelity: chunks-only');
    await release(page, '/api/tune/' + A, apiError(503, 'SERVICE_UNAVAILABLE', 'Synthetic late failure.'));
    await sleep(300);
    const shown = await text(page);
    assert.ok(shown.includes('Fidelity: chunks-only') && !shown.includes('Synthetic late failure.'), shown.slice(0, 600));
  }, { hold: ['GET /api/tune/' + A, 'GET /api/tune/' + B] }]);
  cases.push(['export: a refused export shows its error_code', async page => {
    await startAndPoll(page, 'export');
    await release(page, HALVES.export.poll, exportJob('failed', { error: 'PackageError: Synthetic embedding model mismatch.', error_code: 'EMBEDDING_MISMATCH', error_detail: { expected: 'a', found: 'b' } }));
    await waitText(page, 'Failed.');
    const codes = await page.evaluate(() => [...document.querySelectorAll('code')].map(c => c.textContent.trim()));
    assert.ok(codes.includes('EMBEDDING_MISMATCH'), `codes shown: ${JSON.stringify(codes)}`);
    await waitText(page, 'Synthetic embedding model mismatch.');
    await expectButton(page, 'export', false);
  }]);
  cases.push(['packages: a failed list is shown without the empty-folder hint, and a good Refresh clears it', async page => {
    await release(page, '/api/packages', apiError(500, 'INTERNAL_ERROR', 'Synthetic package list failure.'));
    await waitText(page, 'Could not list the packages in ./exports: Synthetic package list failure.');
    assert.ok(!(await text(page)).includes('Nothing in ./exports yet'), 'the empty-folder hint is shown for a failed list');
    assert.equal(await click(page, 'Refresh'), true, 'no Refresh button');
    await release(page, '/api/packages', json(200, packagesBody));
    await waitText(page, 'Could not list the packages', true);
    const listed = await page.evaluate(p => [...document.querySelectorAll('option')].some(o => o.value === p), PACKAGE);
    assert.equal(listed, true, 'the package is not listed after a good Refresh');
  }, { hold: ['GET /api/packages'] }]);

  cases.push(['export encryption: the passphrase rules hold Export, a valid matching pair sends it, and the fields clear (#27)', async page => {
    assert.deepEqual(await passwords(page), [], 'passphrase fields shown before "Passphrase" is chosen');
    assert.equal(await checkEncrypt(page), true, 'no "Passphrase" choice');
    await page.waitForFunction(() => document.querySelectorAll('input[type=password]').length === 2);
    await waitText(page, 'The passphrase needs at least 12 characters.');
    assert.equal((await button(page, 'export')).disabled, true, 'Export enabled with no passphrase');
    await typePassword(page, 0, 'eleven-char');
    await typePassword(page, 1, 'eleven-char');
    await waitText(page, 'The passphrase needs at least 12 characters.');
    assert.equal((await button(page, 'export')).disabled, true, 'Export enabled with 11 characters');
    await typePassword(page, 0, 'correct horse battery');
    await waitText(page, 'The two passphrases do not match.');
    assert.equal((await button(page, 'export')).disabled, true, 'Export enabled with a mismatch');
    await typePassword(page, 1, 'correct horse battery');
    await waitText(page, 'The two passphrases do not match.', true);
    await expectButton(page, 'export', false);
    assert.equal(await click(page, 'Export'), true, 'no Export button');
    await pending(page, '/api/export', 'POST');
    assert.deepEqual(await sentBody(page, '/api/export'), { collection: A, include_models: false, encryption: 'passphrase', passphrase: 'correct horse battery' });
    await release(page, '/api/export', HALVES.export.started, 'POST');
    await page.waitForFunction(() => [...document.querySelectorAll('input[type=password]')].every(i => i.value === ''));
    await release(page, HALVES.export.poll, exportJob('completed', { chunks_written: 3, filename: 'ragpkg-vfy.tar.gz.age', size_bytes: 4096, fidelity: 'with-sources', models_bundled: false, retrieve_script: true, encrypted: true }));
    await waitText(page, 'with-sources, encrypted');
  }]);
  cases.push(['export encryption: the bounds count characters, 1025 holds Export, and a refused start keeps the fields (#27)', async page => {
    assert.equal(await checkEncrypt(page), true, 'no "Passphrase" choice');
    await page.waitForFunction(() => document.querySelectorAll('input[type=password]').length === 2);
    const lock = '\u{1F512}';   // one character, two UTF-16 units
    await typePassword(page, 0, lock.repeat(11));
    await typePassword(page, 1, lock.repeat(11));
    await waitText(page, 'The passphrase needs at least 12 characters.');
    assert.equal((await button(page, 'export')).disabled, true, 'Export enabled with 11 characters (22 UTF-16 units)');
    await typePassword(page, 0, lock.repeat(12));
    await typePassword(page, 1, lock.repeat(12));
    await waitText(page, 'The passphrase needs at least 12 characters.', true);
    await expectButton(page, 'export', false);
    await typePassword(page, 0, 'a'.repeat(1025));
    await typePassword(page, 1, 'a'.repeat(1025));
    await waitText(page, 'The passphrase can have at most 1024 characters.');
    assert.equal((await button(page, 'export')).disabled, true, 'Export enabled with 1025 characters');
    await typePassword(page, 0, 'a'.repeat(1024));
    await typePassword(page, 1, 'a'.repeat(1024));
    await waitText(page, 'The passphrase can have at most 1024 characters.', true);
    await expectButton(page, 'export', false);
    assert.equal(await click(page, 'Export'), true, 'no Export button');
    await release(page, '/api/export', apiError(409, 'EXPORT_IN_PROGRESS', 'An export of this collection is already running.'), 'POST');
    await waitText(page, 'An export of this collection is already running.');
    assert.deepEqual(await passwords(page), ['a'.repeat(1024), 'a'.repeat(1024)], 'the fields were cleared after a refused start');
    await expectButton(page, 'export', false);
  }]);
  cases.push(['export encryption: three choices, a new key first and chosen; the default sends encryption "key" and no passphrase (#676)', async page => {
    const choices = await encryptionChoices(page);
    assert.deepEqual(choices.map(c => [c.value, c.label, c.checked]),
      [['key', 'New key (recommended)', true], ['passphrase', 'Passphrase', false], ['none', 'Not encrypted', false]]);
    assert.deepEqual(await passwords(page), [], 'passphrase fields shown for a key export');
    await start(page, 'export');
    assert.deepEqual(await sentBody(page, '/api/export'), { collection: A, include_models: false, encryption: 'key' });
  }]);
  cases.push(['export encryption: "Not encrypted" sends encryption "none" (#676)', async page => {
    assert.equal(await chooseEncryption(page, 'none'), true, 'no "Not encrypted" choice');
    await start(page, 'export');
    assert.deepEqual(await sentBody(page, '/api/export'), { collection: A, include_models: false, encryption: 'none' });
  }]);
  cases.push(['export encryption: in LAN mode "Not encrypted" isn\'t offered (#676)', async page => {
    await waitText(page, 'In LAN mode every export is encrypted.');
    const choices = await encryptionChoices(page);
    assert.deepEqual(choices.map(c => c.value), ['key', 'passphrase']);
    await start(page, 'export');
    assert.deepEqual(await sentBody(page, '/api/export'), { collection: A, include_models: false, encryption: 'key' });
  }, { lanMode: true }]);
  cases.push(['export: a finished key export offers Download key first, with the store-apart warning, and never shows the key (#676)', async page => {
    await startAndPoll(page, 'export');
    const done = JSON.parse(HALVES.export.done.body);
    const name = 'ragpkg-vfy.tar.gz.age';
    await release(page, HALVES.export.poll, json(200, { ...done, filename: name, encrypted: true, encryption: 'key', key_filename: name + '.key', key_on_server: true }));
    await waitText(page, 'Download the key first.');
    await waitText(page, 'Store it apart from the package');
    await waitText(page, 'encrypted with a key');
    const links = await page.evaluate(() => [...document.querySelectorAll('a')].filter(a => ['Download key', 'Download'].includes(a.textContent.trim()))
      .map(a => ({ label: a.textContent.trim(), href: a.getAttribute('href'), download: a.hasAttribute('download') })));
    assert.deepEqual(links.slice(0, 2), [
      { label: 'Download key', href: '/api/packages/' + name + '.key', download: true },
      { label: 'Download', href: '/api/packages/' + name, download: true }]);
    assert.ok(!(await text(page)).includes('AGE-SECRET-KEY'), 'the key is shown as text');
    // After the click the page re-reads the export until the key is gone.
    await page.evaluate(() => document.addEventListener('click', e => e.preventDefault(), true));
    await page.evaluate(() => [...document.querySelectorAll('a')].find(a => a.textContent.trim() === 'Download key').click());
    await release(page, HALVES.export.poll, json(200, { ...done, filename: name, encrypted: true, encryption: 'key', key_filename: name + '.key', key_on_server: false }));
    await waitText(page, 'Key downloaded and deleted from ./exports.');
    assert.ok(!(await page.evaluate(() => [...document.querySelectorAll('a')].some(a => a.textContent.trim() === 'Download key'))), 'Download key still offered');
  }]);
  cases.push(['import encryption: a key package is labelled, asks for its key file, reads it in the browser and sends its text as key (#676)', async page => {
    await release(page, '/api/packages', json(200, { packages: [...packagesBody.packages, keyedPackage] }));
    await page.waitForFunction(n => [...document.querySelectorAll('option')].some(o => o.value === n), { timeout: 5000 }, KEYED);
    const label = await page.evaluate(n => [...document.querySelectorAll('option')].find(o => o.value === n).textContent, KEYED);
    assert.ok(label.includes('encrypted with a key, key on the server'), label);
    await selectPackage(page, KEYED);
    await waitText(page, 'This package is encrypted with its own key.');
    assert.deepEqual(await passwords(page), [], 'a passphrase field for a key package');
    assert.equal((await button(page, 'import')).disabled, true, 'Import enabled without a key file');
    const keyText = '# public key: age1vfy\nAGE-SECRET-KEY-1VFYNOTAREALKEY\n';
    await page.evaluate(t => {
      const input = document.querySelector('input[type=file]');
      const files = new DataTransfer();
      files.items.add(new File([t], 'vfy.key'));
      input.files = files.files;
      input.dispatchEvent(new Event('change', { bubbles: true }));
    }, keyText);
    await waitText(page, 'Chosen: vfy.key.');
    await expectButton(page, 'import', false);
    assert.equal(await click(page, 'Import'), true, 'no Import button');
    await pending(page, '/api/import', 'POST');
    assert.deepEqual(await sentBody(page, '/api/import'), { filename: KEYED, on_conflict: 'abort', key: keyText });
    assert.ok(!(await text(page)).includes('AGE-SECRET-KEY'), 'the key is shown as text');
  }, { hold: ['GET /api/packages'] }]);
  cases.push(['import encryption: a key file read that ends after another package was chosen is dropped (#860)', async page => {
    // The #860 fix was already in develop when this case was added, so it
    // passes without new code: it guards that fix, through the held-read
    // harness below (#864), rather than testing a new one.
    // A second package encrypted with its own key, so the key file input stays
    // shown when the selection moves to it.
    const OTHER = 'ragpkg-vfytransfera-20261021-0000-dddd2222.tar.gz.age';
    await release(page, '/api/packages', json(200, { packages: [...packagesBody.packages, keyedPackage, { ...keyedPackage, filename: OTHER }] }));
    await page.waitForFunction(n => [...document.querySelectorAll('option')].some(o => o.value === n), { timeout: 5000 }, OTHER);
    // The page's file reads are held until the case releases them (#864), so
    // "the read ends after another package was chosen" is true by construction.
    await page.evaluate(() => {
      const read = Blob.prototype.text;
      window.heldReads = [];
      Blob.prototype.text = function () { return new Promise(resolve => window.heldReads.push(() => resolve(read.call(this)))); };
    });
    const chooseKeyFile = () => page.evaluate(() => {
      const input = document.querySelector('input[type=file]');
      const files = new DataTransfer();
      files.items.add(new File(['# public key: age1vfy\nAGE-SECRET-KEY-1VFYNOTAREALKEY\n'], 'vfy.key'));
      input.files = files.files;
      input.dispatchEvent(new Event('change', { bubbles: true }));
    });
    const heldReads = n => page.waitForFunction(k => window.heldReads.length === k, { timeout: 5000 }, n);
    const releaseRead = () => page.evaluate(() => window.heldReads.shift()());
    const selected = name => page.waitForFunction(n => [...document.querySelectorAll('select')].some(s => s.value === n), { timeout: 5000 }, name);
    await selectPackage(page, KEYED);
    await waitText(page, 'This package is encrypted with its own key.');
    await chooseKeyFile();
    await heldReads(1);
    await selectPackage(page, OTHER);
    await selected(OTHER);
    await releaseRead();
    await sleep(300);
    assert.ok(!(await text(page)).includes('Chosen: vfy.key.'), 'a key file read for the previous package was kept');
    assert.deepEqual(await button(page, 'import'), { label: 'Import', disabled: true }, 'Import enabled with the previous package\'s key');
    // The held read does reach the page: the same file chosen for this package is kept.
    await chooseKeyFile();
    await heldReads(1);
    await releaseRead();
    await waitText(page, 'Chosen: vfy.key.');
    await expectButton(page, 'import', false);
  }, { hold: ['GET /api/packages'] }]);
  cases.push(['import encryption: an encrypted package is labelled, asks for its passphrase and sends it; a plain one sends none (#27)', async page => {
    const label = await page.evaluate(n => [...document.querySelectorAll('option')].find(o => o.value === n)?.textContent || '', SEALED);
    assert.ok(label.includes('encrypted') && !label.includes('null'), label);
    await selectPackage(page, SEALED);
    await waitText(page, 'This package is encrypted. Enter the passphrase it was exported with.');
    assert.equal((await button(page, 'import')).disabled, true, 'Import enabled without a passphrase');
    await typePassword(page, 0, 'correct horse battery');
    await expectButton(page, 'import', false);
    assert.equal(await click(page, 'Import'), true, 'no Import button');
    await pending(page, '/api/import', 'POST');
    assert.deepEqual(await sentBody(page, '/api/import'), { filename: SEALED, on_conflict: 'abort', passphrase: 'correct horse battery' });
    await release(page, '/api/import', json(202, { job_id: 'vfyimp', status: 'queued', filename: SEALED }), 'POST');
    await page.waitForFunction(() => [...document.querySelectorAll('input[type=password]')].every(i => i.value === ''));
    await release(page, HALVES.import.poll, importJob('failed', { filename: SEALED, error_code: 'DECRYPT_FAILED', error: "'x' could not be decrypted: the passphrase is wrong or the file is damaged." }));
    await waitText(page, 'the passphrase is wrong or the file is damaged');
    await selectPackage(page, PACKAGE);
    await waitText(page, 'This package is encrypted.', true);
    assert.deepEqual(await passwords(page), [], 'a passphrase field for a plain package');
    await expectButton(page, 'import', false);
    assert.equal(await click(page, 'Import'), true, 'no Import button');
    await pending(page, '/api/import', 'POST');
    assert.deepEqual(await sentBody(page, '/api/import'), { filename: PACKAGE, on_conflict: 'abort' });
  }]);
  cases.push(['import encryption: an encrypted package that can\'t be read is labelled "encrypted, unreadable" (#27)', async page => {
    const broken = { filename: BROKEN, size_bytes: 512, collection: null, chunk_count: null, fidelity: null, created_at: null, readable: false, encrypted: true };
    await release(page, '/api/packages', json(200, { packages: [...packagesBody.packages, broken] }));
    await page.waitForFunction(n => [...document.querySelectorAll('option')].some(o => o.value === n), { timeout: 5000 }, BROKEN)
      .catch(() => { throw new Error('the unreadable encrypted package is not listed'); });
    const label = await page.evaluate(n => [...document.querySelectorAll('option')].find(o => o.value === n).textContent, BROKEN);
    assert.equal(label, BROKEN + ' — encrypted, unreadable');
  }, { hold: ['GET /api/packages'] }]);

  // Downloading a package and its expiry (#677, RD-145 Q4).
  const EXPIRES = '2026-11-18T12:00:00Z';
  const downloadLinks = page => page.evaluate(() => [...document.querySelectorAll('a')]
    .filter(a => a.textContent.trim() === 'Download').map(a => ({ href: a.getAttribute('href'), download: a.hasAttribute('download') })));
  const localTime = (page, iso) => page.evaluate(i => new Date(i).toLocaleString(), iso);
  cases.push(['export: a finished export shows a Download link to its package and "Expires <local time>" (#677)', async page => {
    await startAndPoll(page, 'export');
    const done = JSON.parse(HALVES.export.done.body);
    await release(page, HALVES.export.poll, json(200, { ...done, expires_at: EXPIRES }));
    await waitText(page, 'Done.');
    await waitText(page, 'Expires ' + await localTime(page, EXPIRES));
    const links = await downloadLinks(page);
    assert.ok(links.some(l => l.href === '/api/packages/ragpkg-vfy.tar.gz' && l.download), JSON.stringify(links));
  }]);
  cases.push(['export: a finished export with no expiry still offers Download and shows no "Expires" (#677)', async page => {
    await startAndPoll(page, 'export');
    await release(page, HALVES.export.poll, HALVES.export.done);
    await waitText(page, 'Done.');
    const links = await downloadLinks(page);
    assert.ok(links.some(l => l.href === '/api/packages/ragpkg-vfy.tar.gz'), JSON.stringify(links));
    assert.ok(!(await text(page)).includes('Expires '), 'an "Expires" label for a package with no expiry');
  }]);
  cases.push(['import: the package list says when a package exported here expires, and Download follows the selected package (#677)', async page => {
    await release(page, '/api/packages', json(200, { packages: [{ ...packagesBody.packages[0], expires_at: EXPIRES }, packagesBody.packages[1]] }));
    await page.waitForFunction(n => [...document.querySelectorAll('option')].some(o => o.value === n), { timeout: 5000 }, PACKAGE);
    const labels = await page.evaluate(() => Object.fromEntries([...document.querySelectorAll('option')].map(o => [o.value, o.textContent])));
    assert.ok(labels[PACKAGE].endsWith(', expires ' + await localTime(page, EXPIRES)), labels[PACKAGE]);
    assert.ok(!labels[SEALED].includes('expires'), labels[SEALED]);
    const disabled = await page.evaluate(() => [...document.querySelectorAll('button')].some(b => b.textContent.trim() === 'Download' && b.disabled));
    assert.equal(disabled, true, 'no disabled Download while no package is selected');
    assert.deepEqual(await downloadLinks(page), [], 'a Download link with no package selected');
    await selectPackage(page, PACKAGE);
    await page.waitForFunction(() => [...document.querySelectorAll('a')].some(a => a.textContent.trim() === 'Download'), { timeout: 5000 });
    assert.deepEqual(await downloadLinks(page), [{ href: '/api/packages/' + encodeURIComponent(PACKAGE), download: true }]);
    await selectPackage(page, SEALED);
    await page.waitForFunction(h => [...document.querySelectorAll('a')].some(a => a.getAttribute('href') === h), { timeout: 5000 }, '/api/packages/' + encodeURIComponent(SEALED))
      .catch(async () => { throw new Error('Download did not follow the selection: ' + JSON.stringify(await downloadLinks(page))); });
  }, { hold: ['GET /api/packages'] }]);
  cases.push(['export: after Download, the page sees the package go and stops offering it (#677)', async page => {
    await startAndPoll(page, 'export');
    const done = JSON.parse(HALVES.export.done.body);
    await release(page, HALVES.export.poll, json(200, { ...done, expires_at: EXPIRES }));
    await waitText(page, 'Done.');
    // The click must not navigate; React's onClick still runs.
    await page.evaluate(() => document.addEventListener('click', e => e.preventDefault(), true));
    await page.evaluate(() => [...document.querySelectorAll('a')].find(a => a.textContent.trim() === 'Download'
      && a.getAttribute('href') === '/api/packages/ragpkg-vfy.tar.gz').click());
    // The fixed package list doesn't name ragpkg-vfy.tar.gz: the first re-read (3 s) finds it gone.
    await waitText(page, 'Downloaded and deleted from ./exports.');
    assert.deepEqual((await downloadLinks(page)).filter(l => l.href === '/api/packages/ragpkg-vfy.tar.gz'), []);
    assert.ok(!(await text(page)).includes('Expires '), 'still shows when the deleted package expires');
  }]);

  for (const [name, test, opts] of cases) {
    let s;
    try { s = await fixture(browser, base, opts); await test(s.page); assert.deepEqual(s.errors, []); reporter.check(name, true); }
    catch (e) { reporter.check(name, false, e.message); }
    finally { if (s) await s.ctx.close(); }
  }
}

module.exports = { runTransferPageTests };
if (require.main === module) (async () => {
  const browser = await require('puppeteer-core').launch({ executablePath: process.env.RAG_CHROMIUM_PATH || '/usr/bin/chromium-browser', headless: true, args: ['--no-sandbox', '--disable-dev-shm-usage'] });
  const reporter = makeReporter();
  try { await runTransferPageTests(browser, process.env.RAG_UI_BASE || 'http://127.0.0.1:3000', reporter); }
  finally { await browser.close(); }
  process.exitCode = reporter.summary() ? 0 : 1;
})().catch(e => { console.error(e); process.exitCode = 2; });
