// UI controls that had no browser check (#400): Collections, Import, navigation
// and roles, Q&A, Retrieval, and the client's handling of the proxy's own error
// pages. Every API call is answered by the check (see stub.js), so nothing here
// touches the backend or a model. Non-ASCII UI text is written as \u escapes.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const S = require('./stub');
const { json, apiError, html, hold, callsTo, text, waitFor, waitText, press, fill, fillNth, forms, buttonState, nav, waitPath } = S;

const A = 'VfyUiA', B = 'VfyUiB';
const DASH = '\u2014', DOT = '\u00b7', ELLIPSIS = '\u2026', CROSS = '\u2715';
const COLS = [
  { name: A, object_count: 12, index_type: 'hnsw', distance_metric: 'cosine', created_at: '2026-01-02T12:00:00Z', hnsw_config: { ef: 64, efConstruction: 128, maxConnections: 64 } },
  { name: B, object_count: 0, index_type: 'flat', distance_metric: 'dot', created_at: null, hnsw_config: null },
];
const retrievalConfig = (name, over = {}) => ({ collection: name, retrieval_mode: 'hnsw', top_k: 5, alpha: 0.75, ef: null, response_format: 'engineer', is_default: true, ...over });
const queryReply = (over = {}) => ({ answer: 'A plain answer.', citations: null, retrieval_latency_ms: 12, llm_latency_ms: 34, chunks_retrieved: 2, ...over });
const CITATIONS = [
  { source_file: 'alpha.txt', chunk_index: 2, score: 0.12345, excerpt: 'First excerpt.' },
  { source_file: 'beta.txt', chunk_index: 0, score: null, excerpt: 'Second excerpt.' },
];
// Answers for the pages a case may pass through; a case adds or overrides routes.
const base = (extra = {}) => ({
  'GET /api/collections': json(200, { collections: COLS }),
  'GET /api/retrieval/config/*': call => json(200, retrievalConfig(call.path.split('/').pop())),
  'GET /api/packages': json(200, { packages: [] }),
  'GET /api/tune/*': call => json(200, { collection: call.path.split('/').pop(), fidelity: 'with-sources', source_document_count: 2, can_rechunk: true, can_reembed: true, can_reindex: true, note: '' }),
  'GET /api/help/transfer': json(200, { topic: 'transfer', markdown: '# Help\n\nSome text.' }),
  ...extra,
});

const bodyHas = (page, want) => page.evaluate(w => document.body.innerText.replace(/\s+/g, ' ').includes(w), want);
const labels = page => page.evaluate(() => [...document.querySelectorAll('label')].map(l => l.textContent.trim()));
const rangeAttrs = (page, i) => page.evaluate(n => { const r = document.querySelectorAll('input[type=range]')[n]; return r ? { min: r.min, max: r.max, step: r.step, value: r.value } : null; }, i);
const selectOptions = (page, i) => page.evaluate(n => [...(document.querySelectorAll('select')[n]?.options || [])].map(o => o.value), i);
const selected = (page, i) => page.evaluate(n => document.querySelectorAll('select')[n]?.value, i);
const modalOpen = page => page.evaluate(() => !!document.querySelector('.fixed.inset-0'));
const waitSelected = (page, i, why) => waitFor(page, n => !!document.querySelectorAll('select')[n]?.value, why || 'the collection select to be filled', i);

function tempFiles(names) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'vfyui-'));
  return names.map(n => { const f = path.join(dir, n); fs.writeFileSync(f, 'inert test file\n'); return f; });
}
async function queueFiles(page, names) {
  const input = await page.$('#file-input');
  await input.uploadFile(...tempFiles(names));
  await waitText(page, names[names.length - 1]);
}

// Ctrl+Enter in the question box.
async function ctrlEnter(page) {
  await page.focus('textarea');
  await page.keyboard.down('Control'); await page.keyboard.press('Enter'); await page.keyboard.up('Control');
}
async function askQuestion(page, question = 'What is inert?') {
  await waitSelected(page, 0);
  await fill(page, 'textarea', question);
  await ctrlEnter(page);
}
const qaOpen = (role, routes = {}) => ({ role, path: '/qa', routes: base({ 'POST /api/query': json(200, queryReply()), ...routes }) });

function collectionsCases() {
  const open = (routes = {}) => ({ role: 'engineer', path: '/collections', routes: base(routes) });
  return [
    { name: 'Collections table shows its columns, each collection and a dash for a null created date', open: open(), async run(s) {
      await waitText(s.page, A);
      const heads = await s.page.evaluate(() => [...document.querySelectorAll('thead th')].map(t => t.textContent.trim()));
      assert.deepEqual(heads, ['Name', 'Objects', 'Index', 'Distance', 'Created', '']);
      const rows = await s.page.evaluate(() => [...document.querySelectorAll('tbody tr')].map(r => [...r.querySelectorAll('td')].map(c => c.textContent.trim())));
      const date = await s.page.evaluate(v => new Date(v).toLocaleDateString(), COLS[0].created_at);
      assert.deepEqual(rows[0].slice(0, 5), [A, '12', 'hnsw', 'cosine', date]);
      assert.notEqual(date, DASH);
      assert.deepEqual(rows[1].slice(0, 5), [B, '0', 'flat', 'dot', DASH]);
    } },
    { name: 'New Collection modal starts with HNSW, Cosine and the three HNSW sliders at their defaults and bounds', open: open(), async run(s) {
      await waitText(s.page, A);
      await press(s.page, '+ New Collection');
      await waitFor(s.page, () => !!document.querySelector('.fixed.inset-0'), 'the modal');
      assert.equal(await s.page.evaluate(() => document.querySelector('.fixed input:not([type=range])').value), '');
      assert.equal(await s.page.evaluate(() => document.querySelectorAll('.fixed select')[0].value), 'hnsw');
      assert.equal(await s.page.evaluate(() => document.querySelectorAll('.fixed select')[1].value), 'cosine');
      assert.deepEqual(await rangeAttrs(s.page, 0), { min: '64', max: '512', step: '8', value: '128' });
      assert.deepEqual(await rangeAttrs(s.page, 1), { min: '16', max: '128', step: '4', value: '64' });
      assert.deepEqual(await rangeAttrs(s.page, 2), { min: '16', max: '512', step: '8', value: '64' });
      const l = await labels(s.page);
      for (const want of ['efConstruction: 128', 'maxConnections: 64', 'ef: 64']) assert.ok(l.includes(want), `label ${want} in ${JSON.stringify(l)}`);
      await fillNth(s.page, 'input[type=range]', 0, 256);
      assert.ok((await labels(s.page)).includes('efConstruction: 256'));
    } },
    { name: 'Choosing Flat removes the HNSW sliders and choosing HNSW brings them back', open: open(), async run(s) {
      await waitText(s.page, A);
      await press(s.page, '+ New Collection');
      await fillNth(s.page, '.fixed select', 0, 'flat');
      await waitFor(s.page, () => document.querySelectorAll('input[type=range]').length === 0, 'the sliders to go');
      await fillNth(s.page, '.fixed select', 0, 'hnsw');
      await waitFor(s.page, () => document.querySelectorAll('input[type=range]').length === 3, 'the sliders to return');
    } },
    { name: 'Create sends the typed name, the chosen index, distance and slider values, then closes and reloads the list', open: (() => {
      let lists = 0;
      return open({ 'GET /api/collections': () => { lists++; return json(200, { collections: lists === 1 ? COLS : [...COLS, { name: 'VfyUiNew', object_count: 0, index_type: 'hnsw', distance_metric: 'dot', created_at: null, hnsw_config: null }] }); },
                    'POST /api/collections': json(201, { name: 'VfyUiNew' }) });
    })(), async run(s) {
      await waitText(s.page, A);
      await press(s.page, '+ New Collection');
      await fill(s.page, '.fixed input:not([type=range])', 'VfyUiNew');
      await fillNth(s.page, '.fixed select', 1, 'dot');
      await fillNth(s.page, 'input[type=range]', 0, 256);
      await fillNth(s.page, 'input[type=range]', 1, 32);
      await fillNth(s.page, 'input[type=range]', 2, 96);
      await press(s.page, 'Create');
      await waitText(s.page, 'VfyUiNew');
      const posts = callsTo(s, 'POST', '/api/collections');
      assert.equal(posts.length, 1);
      assert.deepEqual(posts[0].body, { name: 'VfyUiNew', index_type: 'hnsw', distance_metric: 'dot', hnsw_config: { efConstruction: 256, maxConnections: 32, ef: 96 } });
      assert.equal(await modalOpen(s.page), false);
      assert.equal(callsTo(s, 'GET', '/api/collections').length, 2);
    } },
  ];
}

function importCases() {
  const open = (role = 'engineer', routes = {}) => ({ role, path: '/import', routes: base(routes) });
  const strategyIs = (page, value) => fillNth(page, 'select', 1, value);
  const ready = async page => { await waitSelected(page, 0); };
  return [
    { name: 'Import queued list shows each file, removes only the one chosen, and Start Ingest follows the queue', open: open(), async run(s) {
      await ready(s.page);
      assert.equal((await buttonState(s.page, 'Start Ingest')).disabled, true);
      await queueFiles(s.page, ['one.txt', 'two.txt']);
      const names = () => s.page.evaluate(() => [...document.querySelectorAll('ul li span')].map(x => x.textContent.trim()));
      assert.deepEqual(await names(), ['one.txt', 'two.txt']);
      assert.equal((await buttonState(s.page, 'Start Ingest')).disabled, false);
      await press(s.page, CROSS);
      await waitFor(s.page, () => document.querySelectorAll('ul li').length === 1, 'one file removed');
      assert.deepEqual(await names(), ['two.txt']);
      await press(s.page, CROSS);
      await waitFor(s.page, () => document.querySelectorAll('ul li').length === 0, 'no files left');
      assert.equal((await buttonState(s.page, 'Start Ingest')).disabled, true);
    } },
    { name: 'Import page lists the collections, selects the first and shows the default sliders', open: open(), async run(s) {
      await ready(s.page);
      assert.deepEqual(await selectOptions(s.page, 0), [A, B]);
      assert.equal(await selected(s.page, 0), A);
      const l = await labels(s.page);
      for (const want of ['Chunk Size: 1000', 'Overlap: 200', 'Min Chunk Size: 100']) assert.ok(l.includes(want), `${want} in ${JSON.stringify(l)}`);
      assert.ok(!l.some(x => x.startsWith('Similarity Threshold')));
    } },
    { name: 'Engineer sees Overlap for fixed, overlap and language, and Similarity Threshold only for semantic', open: open(), async run(s) {
      await ready(s.page);
      const has = async prefix => (await labels(s.page)).some(x => x.startsWith(prefix));
      for (const strategy of ['fixed', 'overlap', 'language']) {
        await strategyIs(s.page, strategy);
        await waitText(s.page, strategy === 'language' ? 'natural sentence' : strategy === 'fixed' ? 'equal-sized' : 'shares some text');
        assert.equal(await has('Overlap:'), true, strategy + ' shows Overlap');
        assert.equal(await has('Similarity Threshold'), false, strategy + ' hides Similarity');
      }
      await strategyIs(s.page, 'context_aware');
      await waitText(s.page, 'headings, paragraphs');
      assert.equal(await has('Overlap:'), false);
      assert.equal(await has('Similarity Threshold'), false);
      await strategyIs(s.page, 'semantic');
      await waitText(s.page, 'same topic');
      assert.equal(await has('Overlap:'), false);
      assert.equal(await has('Similarity Threshold: 0.85'), true);
    } },
    { name: 'Developer never sees Similarity Threshold, not even for semantic', open: open('developer'), async run(s) {
      await ready(s.page);
      await strategyIs(s.page, 'semantic');
      await waitText(s.page, 'same topic');
      assert.equal((await labels(s.page)).some(x => x.startsWith('Similarity Threshold')), false);
      assert.equal((await labels(s.page)).some(x => x.startsWith('Overlap:')), false);
    } },
    { name: 'Lowering Chunk Size pulls Overlap down to Chunk Size minus 50 and caps the Overlap slider', open: open(), async run(s) {
      await ready(s.page);
      await fillNth(s.page, 'input[type=range]', 0, 150);
      await waitFor(s.page, () => [...document.querySelectorAll('label')].some(l => l.textContent.trim() === 'Overlap: 100'), 'Overlap 100');
      assert.ok((await labels(s.page)).includes('Chunk Size: 150'));
      assert.equal((await rangeAttrs(s.page, 1)).max, '100');
      await fillNth(s.page, 'input[type=range]', 0, 3000);
      await waitFor(s.page, () => [...document.querySelectorAll('label')].some(l => l.textContent.trim() === 'Chunk Size: 3000'), 'Chunk Size 3000');
      assert.equal((await rangeAttrs(s.page, 1)).max, '2000');
      assert.ok((await labels(s.page)).includes('Overlap: 100'), 'overlap is not raised again');
    } },
    { name: 'Start Ingest sends the collection, strategy and slider values as the form fields', open: open('engineer', {
      'POST /api/ingest/upload': json(202, { job_id: 'vfyjob', status: 'queued', files_queued: 1, collection: B }),
      'GET /api/ingest/job/*': json(200, { job_id: 'vfyjob', status: 'completed', files_total: 1, files_completed: 1, files_failed: 0, chunks_stored: 1, errors: [] }) }), async run(s) {
      await ready(s.page);
      await queueFiles(s.page, ['doc.txt']);
      await fillNth(s.page, 'select', 0, B);
      await strategyIs(s.page, 'semantic');
      await waitText(s.page, 'same topic');
      await fillNth(s.page, 'input[type=range]', 0, 2000);
      await fillNth(s.page, 'input[type=range]', 1, 40);
      await fillNth(s.page, 'input[type=range]', 2, 0.5);
      await press(s.page, 'Start Ingest');
      await waitFor(s.page, () => window.__forms.length === 1, 'the upload');
      const [form] = await forms(s.page);
      const get = k => form.filter(([n]) => n === k).map(([, v]) => v);
      assert.deepEqual(get('files'), ['file:doc.txt']);
      assert.deepEqual(get('collection'), [B]);
      assert.deepEqual(get('strategy'), ['semantic']);
      assert.deepEqual(get('chunk_size'), ['2000']);
      assert.deepEqual(get('min_chunk_size'), ['40']);
      assert.deepEqual(get('similarity_threshold'), ['0.5']);
      assert.deepEqual(get('chunk_overlap'), ['200']);
    } },
    { name: 'Import "+ New" modal ignores a blank name, creates with cosine and the default HNSW values, selects the new collection, and Cancel sends nothing', open: (() => {
      let lists = 0;
      return open('engineer', { 'GET /api/collections': () => { lists++; return json(200, { collections: lists === 1 ? COLS : [...COLS, { name: 'VfyUiNew', object_count: 0, index_type: 'flat', distance_metric: 'cosine', created_at: null, hnsw_config: null }] }); },
                                'POST /api/collections': json(201, { name: 'VfyUiNew' }) });
    })(), async run(s) {
      await ready(s.page);
      await press(s.page, '+ New');
      await waitFor(s.page, () => !!document.querySelector('.fixed.inset-0'), 'the modal');
      await press(s.page, 'Create');
      await fill(s.page, '.fixed input', '   ');
      await press(s.page, 'Create');
      assert.equal(callsTo(s, 'POST', '/api/collections').length, 0);
      assert.equal(await modalOpen(s.page), true);
      await press(s.page, 'Cancel');
      assert.equal(await modalOpen(s.page), false);
      assert.equal(callsTo(s, 'POST', '/api/collections').length, 0);
      await press(s.page, '+ New');
      await fill(s.page, '.fixed input', 'VfyUiNew');
      await fill(s.page, '.fixed select', 'flat');
      await press(s.page, 'Create');
      await waitFor(s.page, () => document.querySelectorAll('select')[0].value === 'VfyUiNew', 'the new collection selected');
      const posts = callsTo(s, 'POST', '/api/collections');
      assert.equal(posts.length, 1);
      assert.deepEqual(posts[0].body, { name: 'VfyUiNew', index_type: 'flat', distance_metric: 'cosine', hnsw_config: { efConstruction: 128, maxConnections: 64, ef: 64 } });
      assert.equal(await modalOpen(s.page), false);
    } },
  ];
}

function navigationCases() {
  const gated = ['/import', '/chunking', '/retrieval', '/goldstandard', '/transfer', '/help/transfer'];
  const goto = async (page, p) => { await page.goto(S.BASE + p, { waitUntil: 'domcontentloaded' }); };
  return [
    { name: 'Developer nav has Q&A, Import, Chunking, Retrieval, Gold Standard and Transfer, and no Collections or Health', open: { role: 'developer', path: '/qa', routes: base() }, async run(s) {
      await waitFor(s.page, () => document.querySelectorAll('nav a').length > 0, 'the nav');
      assert.deepEqual(await nav(s.page), ['Q&A', 'Import', 'Chunking', 'Retrieval', 'Gold Standard', 'Transfer']);
    } },
    { name: 'Developer is sent back to Q&A from /collections and /health, but can open /transfer and /help/transfer', open: { role: 'developer', path: '/qa', routes: base() }, async run(s) {
      for (const p of ['/collections', '/health']) {
        await goto(s.page, p);
        await waitText(s.page, 'Ask a Question');
        await waitPath(s.page, '/qa');
      }
      await goto(s.page, '/transfer');
      await waitText(s.page, 'Export a collection');
      await waitPath(s.page, '/transfer');
      await goto(s.page, '/help/transfer');
      await waitText(s.page, 'Export and Import');
      await waitPath(s.page, '/help/transfer');
    } },
    { name: 'End User opening a Developer page directly is sent to Q&A (the API refuses those requests too, #25)', open: { role: 'end_user', path: '/qa', routes: base() }, async run(s) {
      for (const p of [...gated, '/collections', '/health']) {
        await goto(s.page, p);
        await waitText(s.page, 'Ask a Question');
        await waitPath(s.page, '/qa');
      }
    } },
  ];
}

// Login, logout, setup, password changes (#25). The API is stubbed; the real
// login and the API's own refusals are checked in ui_criteria.js and 17_auth.sh.
function authCases() {
  const badge = { engineer: 'AI Engineer', developer: 'Developer', end_user: 'End User' };
  const user = (role, over = {}) => ({ username: 'vfy-' + role.replace('_', '-'), role, must_change_password: false, ...over });
  const loginForm = async (page, username, password) => {
    await fill(page, '#login-username', username);
    await fill(page, '#login-password', password);
    await press(page, 'Log in');
  };
  const submit = page => press(page, 'Change password', 'form button[type=submit]');
  return [
    { name: 'Logged out, any page shows the login page at /login with no nav and no role picker', open: { role: null, path: '/collections', routes: base() }, async run(s) {
      await waitText(s.page, 'Log in to continue');
      await waitPath(s.page, '/login');
      assert.equal(await s.page.evaluate(() => document.querySelectorAll('nav').length), 0);
      assert.equal(await bodyHas(s.page, 'Select your role'), false);
    } },
    ...['engineer', 'developer', 'end_user'].map(role => ({ name: `Logging in as ${badge[role]} opens /qa with the role badge and username from the API, stored nowhere in the browser`, open: { role: null, path: '/login', routes: base({ 'POST /api/auth/login': json(200, user(role)) }) }, async run(s) {
      await waitText(s.page, 'Log in to continue');
      await loginForm(s.page, 'VFY-User', 'a password of twelve');
      await waitPath(s.page, '/qa');
      await waitText(s.page, 'Ask a Question');
      assert.equal(await s.page.evaluate(() => document.querySelector('nav span.bg-blue-100')?.textContent.trim()), badge[role]);
      assert.equal(await s.page.evaluate(() => document.querySelector('[data-testid=nav-username]')?.textContent.trim()), user(role).username);
      assert.deepEqual(callsTo(s, 'POST', '/api/auth/login').map(c => c.body), [{ username: 'VFY-User', password: 'a password of twelve' }]);
      assert.deepEqual(await s.page.evaluate(() => [Object.keys(sessionStorage), Object.keys(localStorage)]), [[], []]);
    } })),
    { name: 'A failed login shows the API\'s generic message, clears the password and stays on /login', open: { role: null, path: '/login', routes: base({ 'POST /api/auth/login': apiError(401, 'LOGIN_FAILED', 'The username or password is incorrect.') }) }, async run(s) {
      await waitText(s.page, 'Log in to continue');
      await loginForm(s.page, 'someone', 'wrong password!');
      await waitText(s.page, 'The username or password is incorrect.');
      assert.equal(await s.page.evaluate(() => document.querySelector('#login-password').value), '');
      await waitPath(s.page, '/login');
    } },
    { name: 'A throttled login says to wait', open: { role: null, path: '/login', routes: base({ 'POST /api/auth/login': apiError(429, 'LOGIN_THROTTLED', 'Too many failed logins. Wait 15 minutes and try again.') }) }, async run(s) {
      await waitText(s.page, 'Log in to continue');
      await loginForm(s.page, 'someone', 'wrong password!');
      await waitText(s.page, 'Wait 15 minutes');
    } },
    { name: 'Log out calls the API and returns to the login page', open: { role: 'developer', path: '/qa', routes: base({ 'POST /api/auth/logout': json(200, { status: 'logged_out' }) }) }, async run(s) {
      await waitText(s.page, 'Ask a Question');
      await press(s.page, 'Log out');
      await waitText(s.page, 'Log in to continue');
      await waitPath(s.page, '/login');
      assert.equal(callsTo(s, 'POST', '/api/auth/logout').length, 1);
      assert.equal(await s.page.evaluate(() => document.querySelectorAll('nav').length), 0);
    } },
    { name: 'Before setup, every page shows how to create the first Engineer and nothing else', open: { role: null, path: '/qa', routes: base({ 'GET /api/auth/status': json(200, { setup_required: true }) }) }, async run(s) {
      await waitText(s.page, 'Not set up yet');
      await waitText(s.page, 'docker compose exec api python -m auth bootstrap <username>');
      assert.equal(await s.page.evaluate(() => document.querySelectorAll('nav, form').length), 0);
      assert.equal(callsTo(s, 'GET', '/api/auth/me').length, 0);
    } },
    { name: 'A due password change shows only the password page; a mismatch is caught before sending; the change opens /qa', open: { role: null, path: '/collections', routes: base({
      'GET /api/auth/me': json(200, user('developer', { must_change_password: true })),
      'POST /api/auth/password': json(200, { status: 'changed', ...user('developer') }),
    }) }, async run(s) {
      await waitPath(s.page, '/account/password');
      await waitText(s.page, 'password was generated for you');
      assert.equal(await s.page.evaluate(() => document.querySelectorAll('nav').length), 0);
      await fill(s.page, '#current-password', 'generated-one');
      await fill(s.page, '#new-password', 'my new password');
      await fill(s.page, '#confirm-password', 'my new passwor');
      await submit(s.page);
      await waitText(s.page, 'The new passwords do not match.');
      assert.equal(callsTo(s, 'POST', '/api/auth/password').length, 0);
      await fill(s.page, '#confirm-password', 'my new password');
      await submit(s.page);
      await waitPath(s.page, '/qa');
      await waitText(s.page, 'Ask a Question');
      assert.deepEqual(callsTo(s, 'POST', '/api/auth/password').map(c => c.body), [{ current_password: 'generated-one', new_password: 'my new password' }]);
    } },
    { name: 'Change password in the nav opens the form; the API\'s rule message is shown; success says other sessions ended', open: { role: 'end_user', path: '/qa', routes: base({
      'POST /api/auth/password': call => call.body.new_password === 'short' ? apiError(422, 'INVALID_PASSWORD', 'The password must be at least 12 characters.') : json(200, { status: 'changed', ...user('end_user') }),
    }) }, async run(s) {
      await waitText(s.page, 'Ask a Question');
      await press(s.page, 'Change password');
      await waitPath(s.page, '/account/password');
      for (const [id, v] of [['#current-password', 'the old one!'], ['#new-password', 'short'], ['#confirm-password', 'short']]) await fill(s.page, id, v);
      await submit(s.page);
      await waitText(s.page, 'at least 12 characters');
      for (const [id, v] of [['#new-password', 'long enough now'], ['#confirm-password', 'long enough now']]) await fill(s.page, id, v);
      await submit(s.page);
      await waitText(s.page, 'Your other sessions were logged out.');
      assert.equal(await s.page.evaluate(() => document.querySelectorAll('nav').length), 1);
    } },
    { name: 'An API answer 401 AUTH_REQUIRED on any page returns to the login page', open: { role: 'engineer', path: '/collections', routes: base({ 'GET /api/collections': apiError(401, 'AUTH_REQUIRED', 'Log in to use this.') }) }, async run(s) {
      await waitText(s.page, 'Log in to continue');
      await waitPath(s.page, '/login');
    } },
    { name: 'An API answer 403 PASSWORD_CHANGE_REQUIRED returns to the password page', open: { role: 'engineer', path: '/collections', routes: base({ 'GET /api/collections': apiError(403, 'PASSWORD_CHANGE_REQUIRED', 'Change your password before doing anything else.') }) }, async run(s) {
      await waitPath(s.page, '/account/password');
      await waitText(s.page, 'password was generated for you');
    } },
  ];
}

// The Users page (PR 2 of #25): Engineers manage accounts. The API is stubbed
// with a small in-memory list; the real routes are checked in 17_auth.sh.
function usersCases() {
  const ME = 'vfy-engineer';
  const row = (username, role, over = {}) => ({ username, role, enabled: true, must_change_password: false, created_at: '2026-10-01T12:00:00Z', ...over });
  const usersApi = (extra = {}) => {
    const list = [row('vfy-developer', 'developer'), row(ME, 'engineer'), row('vfy-off', 'end_user', { enabled: false, must_change_password: true })];
    const find = call => list.find(u => u.username === decodeURIComponent(call.path.split('/')[4]));
    return base({
      'GET /api/auth/users': () => json(200, { users: list.map(u => ({ ...u })) }),
      'POST /api/auth/users': call => {
        const user = row(call.body.username.toLowerCase(), call.body.role, { must_change_password: true });
        list.push(user);
        return json(201, { user, password: 'GeneratedOnce-1234567890' });
      },
      'POST /api/auth/users/*': call => {
        const user = find(call), action = call.path.split('/')[5];
        if (!user) return apiError(404, 'USER_NOT_FOUND', 'There is no user.');
        if (action === 'reset-password') { user.must_change_password = true; return json(200, { user, password: 'ResetOnce-0987654321ab' }); }
        if (action === 'role') user.role = call.body.role;
        if (action === 'disable') user.enabled = false;
        if (action === 'enable') user.enabled = true;
        return json(200, { user });
      },
      ...extra,
    });
  };
  const open = (extra = {}) => ({ role: 'engineer', path: '/users', routes: usersApi(extra) });
  const rowTexts = page => page.evaluate(() => [...document.querySelectorAll('tbody tr')].map(r => [...r.querySelectorAll('td')].map(c => c.textContent.trim())));
  const rowPress = async (page, username, label) => {
    const done = await page.evaluate((u, t) => {
      const b = [...document.querySelectorAll(`tr[data-username="${u}"] button`)].find(x => x.textContent.trim() === t);
      if (!b) return false; b.click(); return true;
    }, username, label);
    if (!done) throw new Error(`no "${label}" button in the row of ${username}`);
  };
  const rowButtons = (page, username) => page.evaluate(u => [...document.querySelectorAll(`tr[data-username="${u}"] button`)].map(b => b.textContent.trim()), username);
  const goto = async (page, p) => { await page.goto(S.BASE + p, { waitUntil: 'domcontentloaded' }); };
  return [
    { name: 'Engineer nav has Users, which opens the Users page', open: { role: 'engineer', path: '/qa', routes: usersApi() }, async run(s) {
      await waitFor(s.page, () => document.querySelectorAll('nav a').length > 0, 'the nav');
      assert.deepEqual(await nav(s.page), ['Q&A', 'Import', 'Chunking', 'Retrieval', 'Gold Standard', 'Transfer', 'Collections', 'Health', 'Users', 'Service keys']);
      await press(s.page, 'Users', 'nav a');
      await waitPath(s.page, '/users');
      await waitText(s.page, 'vfy-developer');
    } },
    ...['developer', 'end_user'].map(role => ({ name: `${role === 'developer' ? 'Developer' : 'End User'} has no Users link and is sent from /users to Q&A`, open: { role, path: '/qa', routes: base() }, async run(s) {
      await waitText(s.page, 'Ask a Question');
      assert.equal((await nav(s.page)).includes('Users'), false);
      await goto(s.page, '/users');
      await waitText(s.page, 'Ask a Question');
      await waitPath(s.page, '/qa');
      assert.equal(callsTo(s, 'GET', '/api/auth/users').length, 0);
    } })),
    { name: 'The list shows each account; the own row has no Reset password', open: open(), async run(s) {
      await waitText(s.page, 'vfy-off');
      const heads = await s.page.evaluate(() => [...document.querySelectorAll('thead th')].map(t => t.textContent.trim()));
      assert.deepEqual(heads, ['Username', 'Role', 'Status', 'Password', 'Created', '']);
      const date = await s.page.evaluate(v => new Date(v).toLocaleDateString(), '2026-10-01T12:00:00Z');
      const rows = await rowTexts(s.page);
      assert.deepEqual(rows.map(r => [r[0], r[2], r[3], r[4]]), [['vfy-developer', 'Enabled', 'Set', date], [ME + ' (you)', 'Enabled', 'Set', date], ['vfy-off', 'Disabled', 'Change due', date]]);
      assert.deepEqual(await s.page.evaluate(() => [...document.querySelectorAll('tbody select')].map(x => x.value)), ['developer', 'engineer', 'end_user']);
      assert.deepEqual(await rowButtons(s.page, 'vfy-developer'), ['Reset password', 'Disable']);
      assert.deepEqual(await rowButtons(s.page, ME), ['Disable']);
      assert.ok(await bodyHas(s.page, 'Use Change password for your own'));
      assert.deepEqual(await rowButtons(s.page, 'vfy-off'), ['Reset password', 'Enable']);
    } },
    { name: 'Create user sends the name and role, shows the generated password once, and Done removes it', open: open(), async run(s) {
      await waitText(s.page, 'vfy-off');
      await fill(s.page, '#new-username', 'New.Person');
      await fill(s.page, '#new-role', 'developer');
      await press(s.page, 'Create user');
      await waitText(s.page, 'GeneratedOnce-1234567890');
      assert.deepEqual(callsTo(s, 'POST', '/api/auth/users').map(c => c.body), [{ username: 'New.Person', role: 'developer' }]);
      await waitText(s.page, 'shown only once');
      await waitFor(s.page, () => !!document.querySelector('tr[data-username="new.person"]'), 'the new row');
      assert.equal(await s.page.evaluate(() => document.querySelector('#new-username').value), '');
      assert.deepEqual(await s.page.evaluate(() => [Object.keys(sessionStorage), Object.keys(localStorage)]), [[], []]);
      await press(s.page, 'Done');
      await waitText(s.page, 'GeneratedOnce-1234567890', true);
    } },
    { name: 'Reset password of another account shows its new password once', open: open(), async run(s) {
      await waitText(s.page, 'vfy-off');
      await rowPress(s.page, 'vfy-developer', 'Reset password');
      await waitText(s.page, 'ResetOnce-0987654321ab');
      assert.equal(callsTo(s, 'POST', '/api/auth/users/vfy-developer/reset-password').length, 1);
      await waitFor(s.page, () => document.querySelector('tr[data-username="vfy-developer"] td:nth-child(4)').textContent.trim() === 'Change due', 'the change-due mark');
    } },
    { name: 'Changing a role, Disable and Enable call the API for that account', open: open(), async run(s) {
      await waitText(s.page, 'vfy-off');
      await fill(s.page, 'select[aria-label="Role of vfy-developer"]', 'end_user');
      await waitFor(s.page, () => document.querySelector('select[aria-label="Role of vfy-developer"]').value === 'end_user', 'the new role');
      assert.deepEqual(callsTo(s, 'POST', '/api/auth/users/vfy-developer/role').map(c => c.body), [{ role: 'end_user' }]);
      await rowPress(s.page, 'vfy-developer', 'Disable');
      await waitFor(s.page, () => document.querySelector('tr[data-username="vfy-developer"] td:nth-child(3)').textContent.trim() === 'Disabled', 'Disabled');
      await rowPress(s.page, 'vfy-off', 'Enable');
      await waitFor(s.page, () => document.querySelector('tr[data-username="vfy-off"] td:nth-child(3)').textContent.trim() === 'Enabled', 'Enabled');
      assert.equal(callsTo(s, 'POST', '/api/auth/users/vfy-developer/disable').length, 1);
      assert.equal(callsTo(s, 'POST', '/api/auth/users/vfy-off/enable').length, 1);
    } },
    { name: 'A refusal (the last Engineer) shows the API\'s message and reloads the list', open: open({
      'POST /api/auth/users/vfy-engineer/disable': apiError(409, 'LAST_ENGINEER', "'vfy-engineer' is the last enabled engineer; make another engineer first."),
    }), async run(s) {
      await waitText(s.page, 'vfy-off');
      const before = callsTo(s, 'GET', '/api/auth/users').length;
      await rowPress(s.page, ME, 'Disable');
      await waitText(s.page, 'is the last enabled engineer');
      await waitFor(s.page, () => document.querySelector('[role=alert]') !== null, 'the alert');
      assert.ok(callsTo(s, 'GET', '/api/auth/users').length > before, 'the list was reloaded');
      assert.equal(await s.page.evaluate(() => document.querySelector('tr[data-username="vfy-engineer"] td:nth-child(3)').textContent.trim()), 'Enabled');
    } },
    { name: 'A list that cannot be loaded says so', open: open({ 'GET /api/auth/users': apiError(500, 'INTERNAL_ERROR', 'Synthetic failure.') }), async run(s) {
      await waitText(s.page, 'Could not load the users: Synthetic failure.');
    } },
  ];
}

// The Service keys page (PR 3 of #25): Engineers manage service principals and
// their API keys. The API is stubbed with a small in-memory list; the real
// routes are checked in 17_auth.sh.
function serviceKeysCases() {
  const SECRET = 'rag_newkey00000a_' + 'S'.repeat(43);
  const key = (id, over = {}) => ({ id, label: '', created_at: '2026-10-01T12:00:00Z', expires_at: '2026-12-30T12:00:00Z', revoked_at: null, last_used_at: null, status: 'active', ...over });
  const principal = (name, role, over = {}) => ({ name, role, enabled: true, created_at: '2026-10-01T12:00:00Z', keys: [], ...over });
  const keysApi = (extra = {}) => {
    const list = [
      principal('vfy-svc', 'developer', { keys: [key('activekey0001', { label: 'nightly', last_used_at: '2026-10-20T08:00:00Z' }), key('expiredkey001', { status: 'expired' }), key('revokedkey01', { status: 'revoked', revoked_at: '2026-10-05T00:00:00Z' })] }),
      principal('vfy-off', 'end_user', { enabled: false }),
    ];
    const find = call => list.find(p => p.name === decodeURIComponent(call.path.split('/')[4]));
    return base({
      'GET /api/auth/service-principals': () => json(200, { principals: JSON.parse(JSON.stringify(list)) }),
      'POST /api/auth/service-principals': call => {
        const p = principal(call.body.name.toLowerCase(), call.body.role);
        list.push(p);
        return json(201, { principal: p });
      },
      'POST /api/auth/service-principals/*': call => {
        const p = find(call), parts = call.path.split('/');
        if (!p) return apiError(404, 'PRINCIPAL_NOT_FOUND', 'There is no service principal.');
        if (parts[5] === 'keys' && parts.length === 6) {
          const k = key('newkey00000a', { label: call.body.label });
          p.keys.unshift(k);
          return json(201, { principal: p, key: k, secret: SECRET });
        }
        if (parts[5] === 'keys' && parts[7] === 'revoke') {
          const k = p.keys.find(x => x.id === parts[6]);
          if (!k) return apiError(404, 'KEY_NOT_FOUND', 'No such key.');
          k.status = 'revoked'; k.revoked_at = '2026-10-28T00:00:00Z';
        }
        if (parts[5] === 'role') p.role = call.body.role;
        if (parts[5] === 'disable') p.enabled = false;
        if (parts[5] === 'enable') p.enabled = true;
        return json(200, { principal: p });
      },
      ...extra,
    });
  };
  const open = (extra = {}) => ({ role: 'engineer', path: '/service-keys', routes: keysApi(extra) });
  const keyRows = (page, name) => page.evaluate(n => [...document.querySelectorAll(`section[data-principal="${n}"] tbody tr`)].map(r => [...r.querySelectorAll('td')].map(c => c.textContent.trim())), name);
  const sectionPress = async (page, name, label) => {
    const done = await page.evaluate((n, t) => {
      const b = [...document.querySelectorAll(`section[data-principal="${n}"] button`)].find(x => x.textContent.trim() === t);
      if (!b) return false; b.click(); return true;
    }, name, label);
    if (!done) throw new Error(`no "${label}" button for ${name}`);
  };
  const status = (page, name) => page.evaluate(n => document.querySelector(`section[data-principal="${n}"] [data-testid=principal-status]`)?.textContent.trim(), name);
  const goto = async (page, p) => { await page.goto(S.BASE + p, { waitUntil: 'domcontentloaded' }); };
  return [
    { name: 'Engineer nav ends with Service keys, which opens the page', open: { role: 'engineer', path: '/qa', routes: keysApi() }, async run(s) {
      await waitFor(s.page, () => document.querySelectorAll('nav a').length > 0, 'the nav');
      assert.equal((await nav(s.page)).at(-1), 'Service keys');
      await press(s.page, 'Service keys', 'nav a');
      await waitPath(s.page, '/service-keys');
      await waitText(s.page, 'vfy-svc');
    } },
    ...['developer', 'end_user'].map(role => ({ name: `${role === 'developer' ? 'Developer' : 'End User'} has no Service keys link and is sent from /service-keys to Q&A`, open: { role, path: '/qa', routes: base() }, async run(s) {
      await waitText(s.page, 'Ask a Question');
      assert.equal((await nav(s.page)).includes('Service keys'), false);
      await goto(s.page, '/service-keys');
      await waitText(s.page, 'Ask a Question');
      await waitPath(s.page, '/qa');
      assert.equal(callsTo(s, 'GET', '/api/auth/service-principals').length, 0);
    } })),
    { name: 'The list shows each principal and its keys; only active keys can be revoked; rotation is explained', open: open(), async run(s) {
      await waitText(s.page, 'vfy-off');
      const day = v => s.page.evaluate(x => new Date(x).toLocaleDateString(), v);
      assert.deepEqual(await keyRows(s.page, 'vfy-svc'), [
        ['activekey0001', 'nightly', await day('2026-10-01T12:00:00Z'), await day('2026-12-30T12:00:00Z'), await day('2026-10-20T08:00:00Z'), 'Active', 'Revoke'],
        ['expiredkey001', '', await day('2026-10-01T12:00:00Z'), await day('2026-12-30T12:00:00Z'), 'never', 'Expired', ''],
        ['revokedkey01', '', await day('2026-10-01T12:00:00Z'), await day('2026-12-30T12:00:00Z'), 'never', 'Revoked', ''],
      ]);
      assert.deepEqual([await status(s.page, 'vfy-svc'), await status(s.page, 'vfy-off')], ['Enabled', 'Disabled']);
      assert.equal(await s.page.evaluate(() => document.querySelector('select[aria-label="Role of vfy-svc"]').value), 'developer');
      assert.ok(await bodyHas(s.page, 'To rotate a key, issue a new one, switch the service over to it, then revoke the old one'));
      assert.ok(await bodyHas(s.page, 'Authorization: Bearer <key>'));
      assert.equal(await s.page.evaluate(() => document.querySelector('input[aria-label="Lifetime in days for a new key of vfy-svc"]').value), '90');
    } },
    { name: 'Create principal sends the name and role', open: open(), async run(s) {
      await waitText(s.page, 'vfy-off');
      await fill(s.page, '#new-principal-name', 'Nightly.Import');
      await fill(s.page, '#new-principal-role', 'developer');
      await press(s.page, 'Create principal');
      await waitFor(s.page, () => !!document.querySelector('section[data-principal="nightly.import"]'), 'the new principal');
      assert.deepEqual(callsTo(s, 'POST', '/api/auth/service-principals').map(c => c.body), [{ name: 'Nightly.Import', role: 'developer' }]);
      assert.equal(await s.page.evaluate(() => document.querySelector('#new-principal-name').value), '');
    } },
    { name: 'Issue key sends the label and lifetime, shows the key once, and Done removes it', open: open(), async run(s) {
      await waitText(s.page, 'vfy-off');
      await fill(s.page, 'input[aria-label="Label for a new key of vfy-svc"]', 'rotated');
      await fill(s.page, 'input[aria-label="Lifetime in days for a new key of vfy-svc"]', '30');
      await sectionPress(s.page, 'vfy-svc', 'Issue key');
      await waitText(s.page, SECRET);
      assert.deepEqual(callsTo(s, 'POST', '/api/auth/service-principals/vfy-svc/keys').map(c => c.body), [{ label: 'rotated', days: 30 }]);
      await waitText(s.page, 'shown only once');
      await waitFor(s.page, () => !!document.querySelector('tr[data-key="newkey00000a"]'), 'the new key row');
      assert.deepEqual(await s.page.evaluate(() => [Object.keys(sessionStorage), Object.keys(localStorage)]), [[], []]);
      await press(s.page, 'Done');
      await waitText(s.page, SECRET, true);
      assert.equal(await bodyHas(s.page, SECRET), false);
    } },
    { name: 'Revoke, a role change, Disable and Enable call the API for that principal', open: open(), async run(s) {
      await waitText(s.page, 'vfy-off');
      await sectionPress(s.page, 'vfy-svc', 'Revoke');
      await waitFor(s.page, () => document.querySelector('tr[data-key="activekey0001"] td:nth-child(6)').textContent.trim() === 'Revoked', 'Revoked');
      assert.equal(callsTo(s, 'POST', '/api/auth/service-principals/vfy-svc/keys/activekey0001/revoke').length, 1);
      await fill(s.page, 'select[aria-label="Role of vfy-svc"]', 'end_user');
      await waitFor(s.page, () => document.querySelector('select[aria-label="Role of vfy-svc"]').value === 'end_user', 'the new role');
      assert.deepEqual(callsTo(s, 'POST', '/api/auth/service-principals/vfy-svc/role').map(c => c.body), [{ role: 'end_user' }]);
      await sectionPress(s.page, 'vfy-svc', 'Disable');
      await waitFor(s.page, () => document.querySelector('section[data-principal="vfy-svc"] [data-testid=principal-status]').textContent.trim() === 'Disabled', 'Disabled');
      await sectionPress(s.page, 'vfy-off', 'Enable');
      await waitFor(s.page, () => document.querySelector('section[data-principal="vfy-off"] [data-testid=principal-status]').textContent.trim() === 'Enabled', 'Enabled');
      assert.equal(callsTo(s, 'POST', '/api/auth/service-principals/vfy-svc/disable').length, 1);
      assert.equal(callsTo(s, 'POST', '/api/auth/service-principals/vfy-off/enable').length, 1);
    } },
    { name: 'A refusal shows the API\'s message and reloads the list', open: open({
      'POST /api/auth/service-principals': apiError(409, 'PRINCIPAL_EXISTS', "The name 'vfy-svc' is already taken."),
    }), async run(s) {
      await waitText(s.page, 'vfy-off');
      const before = callsTo(s, 'GET', '/api/auth/service-principals').length;
      await fill(s.page, '#new-principal-name', 'vfy-svc');
      await press(s.page, 'Create principal');
      await waitText(s.page, 'is already taken');
      await waitFor(s.page, () => document.querySelector('[role=alert]') !== null, 'the alert');
      assert.ok(callsTo(s, 'GET', '/api/auth/service-principals').length > before, 'the list was reloaded');
    } },
    { name: 'A list that cannot be loaded says so', open: open({ 'GET /api/auth/service-principals': apiError(500, 'INTERNAL_ERROR', 'Synthetic failure.') }), async run(s) {
      await waitText(s.page, 'Could not load the service principals: Synthetic failure.');
    } },
  ];
}

function qaCases() {
  return [
    { name: 'Ask is disabled for empty and blank questions and enabled for text', open: qaOpen('engineer'), async run(s) {
      await waitSelected(s.page, 0);
      assert.equal((await buttonState(s.page, 'Ask')).disabled, true);
      await fill(s.page, 'textarea', '   ');
      assert.equal((await buttonState(s.page, 'Ask')).disabled, true);
      await fill(s.page, 'textarea', 'a question');
      assert.equal((await buttonState(s.page, 'Ask')).disabled, false);
    } },
    { name: 'Ctrl+Enter submits the question once, and does nothing on an empty box', open: qaOpen('engineer'), async run(s) {
      await waitSelected(s.page, 0);
      await ctrlEnter(s.page);
      await new Promise(r => setTimeout(r, 300));
      assert.equal(callsTo(s, 'POST', '/api/query').length, 0);
      await fill(s.page, 'textarea', 'What is inert?');
      await ctrlEnter(s.page);
      await waitText(s.page, 'A plain answer.');
      const posts = callsTo(s, 'POST', '/api/query');
      assert.equal(posts.length, 1);
      assert.equal(posts[0].body.question, 'What is inert?');
      assert.equal(posts[0].body.collection, A);
    } },
    { name: 'Plain Enter inserts a newline and does not submit', open: qaOpen('engineer'), async run(s) {
      await waitSelected(s.page, 0);
      await s.page.focus('textarea');
      await s.page.keyboard.type('line one');
      await s.page.keyboard.press('Enter');
      await s.page.keyboard.type('line two');
      assert.equal(await s.page.evaluate(() => document.querySelector('textarea').value), 'line one\nline two');
      await new Promise(r => setTimeout(r, 300));
      assert.equal(callsTo(s, 'POST', '/api/query').length, 0);
    } },
    (() => {
      const h = hold();
      return { name: 'While a question is answered the button reads Thinking and is disabled, then Ask returns', open: qaOpen('engineer', { 'POST /api/query': () => h }), async run(s) {
        await askQuestion(s.page);
        await waitText(s.page, 'Thinking' + ELLIPSIS);
        assert.equal((await buttonState(s.page, 'Thinking' + ELLIPSIS)).disabled, true);
        h.release(json(200, queryReply()));
        await waitText(s.page, 'A plain answer.');
        assert.equal((await buttonState(s.page, 'Ask')).disabled, false);
      } };
    })(),
    { name: 'With citations off the query asks for none and no Sources panel shows, even if the reply carries some', open: qaOpen('engineer', { 'POST /api/query': json(200, queryReply({ citations: CITATIONS })) }), async run(s) {
      await waitSelected(s.page, 0);
      assert.equal(await s.page.evaluate(() => document.querySelector('input[type=checkbox]').checked), false);
      await askQuestion(s.page);
      await waitText(s.page, 'A plain answer.');
      assert.equal(callsTo(s, 'POST', '/api/query')[0].body.include_citations, false);
      assert.equal(await bodyHas(s.page, 'Sources'), false);
    },},
    { name: 'With citations on the query asks for them and the Sources panel lists file, chunk, score (n/a when null) and excerpt', open: qaOpen('engineer', { 'POST /api/query': json(200, queryReply({ citations: CITATIONS })) }), async run(s) {
      await waitSelected(s.page, 0);
      await s.page.click('input[type=checkbox]');
      await askQuestion(s.page);
      await waitText(s.page, 'Sources');
      assert.equal(callsTo(s, 'POST', '/api/query')[0].body.include_citations, true);
      const panel = await s.page.evaluate(() => [...document.querySelectorAll('h3')].find(h => h.textContent.trim() === 'Sources')?.parentElement.innerText.replace(/\s+/g, ' '));
      for (const want of ['alpha.txt', `chunk 2 ${DOT} score 0.123`, 'First excerpt.', 'beta.txt', `chunk 0 ${DOT} score n/a`, 'Second excerpt.']) assert.ok(panel.includes(want), `${want} in ${panel}`);
    } },
    { name: 'Unchecking the citations box hides the panel of the answer on screen and checking it brings it back without a new query', open: qaOpen('engineer', { 'POST /api/query': json(200, queryReply({ citations: CITATIONS })) }), async run(s) {
      await waitSelected(s.page, 0);
      await s.page.click('input[type=checkbox]');
      await askQuestion(s.page);
      await waitText(s.page, 'Sources');
      await s.page.click('input[type=checkbox]');
      await waitText(s.page, 'Sources', true);
      await s.page.click('input[type=checkbox]');
      await waitText(s.page, 'Sources');
      assert.equal(callsTo(s, 'POST', '/api/query').length, 1);
    } },
    { name: 'The answer renders markdown and the latency line shows both times', open: qaOpen('engineer', { 'POST /api/query': json(200, queryReply({ answer: 'This is **bold words** here.\n\n- first item\n- second item', retrieval_latency_ms: 12, llm_latency_ms: 345 })) }), async run(s) {
      await askQuestion(s.page);
      await waitText(s.page, 'bold words');
      const dom = await s.page.evaluate(() => ({
        strong: [...document.querySelectorAll('.prose strong')].map(x => x.textContent),
        items: [...document.querySelectorAll('.prose li')].map(x => x.textContent.trim()),
      }));
      assert.deepEqual(dom.strong, ['bold words']);
      assert.deepEqual(dom.items, ['first item', 'second item']);
      assert.equal(await bodyHas(s.page, '**'), false);
      assert.equal(await bodyHas(s.page, `Retrieved in 12ms ${DOT} Generated in 345ms`), true);
    } },
    (() => {
      let n = 0;
      return { name: 'A failed query shows the API message with no answer or latency line, and asking again clears it', open: qaOpen('engineer', { 'POST /api/query': () => (++n === 1 ? apiError(500, 'INTERNAL_ERROR', 'Synthetic query failure.') : json(200, queryReply())) }), async run(s) {
        await askQuestion(s.page);
        await waitText(s.page, 'Synthetic query failure.');
        assert.equal(await bodyHas(s.page, 'Retrieved in'), false);
        assert.equal(await bodyHas(s.page, 'A plain answer.'), false);
        assert.equal((await buttonState(s.page, 'Ask')).disabled, false);
        await ctrlEnter(s.page);
        await waitText(s.page, 'A plain answer.');
        assert.equal(await bodyHas(s.page, 'Synthetic query failure.'), false);
        assert.equal(await bodyHas(s.page, 'Retrieved in'), true);
      } };
    })(),
    { name: 'End User sees no method display and the query asks for response_format end_user', open: qaOpen('end_user'), async run(s) {
      await waitSelected(s.page, 0);
      assert.equal(await s.page.evaluate(() => document.querySelectorAll('select').length), 1);
      assert.equal(await s.page.evaluate(() => document.querySelectorAll('select[disabled]').length), 0);
      await askQuestion(s.page);
      await waitText(s.page, 'A plain answer.');
      assert.equal(callsTo(s, 'POST', '/api/query')[0].body.response_format, 'end_user');
    } },
    ...['engineer', 'developer'].map(role => ({ name: `${role === 'engineer' ? 'Engineer' : 'Developer'} sees the method display and the query asks for response_format engineer`, open: qaOpen(role), async run(s) {
      await waitSelected(s.page, 0);
      assert.equal(await s.page.evaluate(() => document.querySelectorAll('select').length), 2);
      assert.equal(await s.page.evaluate(() => document.querySelectorAll('select[disabled]').length), 1);
      await askQuestion(s.page);
      await waitText(s.page, 'A plain answer.');
      assert.equal(callsTo(s, 'POST', '/api/query')[0].body.response_format, 'engineer');
    } })),
  ];
}

function retrievalCases() {
  const saved = over => ({ 'GET /api/retrieval/config/*': call => json(200, retrievalConfig(call.path.split('/').pop(), over)) });
  const open = routes => ({ role: 'engineer', path: '/retrieval', routes: base(routes) });
  const alphaLabel = page => page.evaluate(() => [...document.querySelectorAll('label')].find(l => l.textContent.includes('Meaning:'))?.textContent.trim() || null);
  return [
    { name: 'Retrieval page says the settings shown are the saved ones', open: open(saved({ retrieval_mode: 'semantic', top_k: 9, is_default: false })), async run(s) {
      await waitText(s.page, 'Showing the settings saved for this collection.');
      assert.equal(await bodyHas(s.page, 'showing defaults'), false);
      assert.equal(await s.page.evaluate(() => document.querySelector('input[name=mode][value=semantic]').checked), true);
      assert.ok(await bodyHas(s.page, 'Top-K Results: 9'));
    } },
    { name: 'Retrieval page says defaults are shown when nothing is saved', open: open(saved({ is_default: true })), async run(s) {
      await waitText(s.page, `No settings saved for this collection yet ${DASH} showing defaults.`);
      assert.equal(await bodyHas(s.page, 'Showing the settings saved'), false);
    } },
    { name: 'The alpha slider shows for Hybrid only, starts at the saved value, and the changed value is saved', open: open({ ...saved({ retrieval_mode: 'hybrid', alpha: 0.25, top_k: 7, is_default: false }),
        'POST /api/retrieval/config': call => json(200, { ...call.body, is_default: false }) }), async run(s) {
      await waitText(s.page, 'Showing the settings saved');
      assert.equal(await alphaLabel(s.page), `Keyword \u2190 Balance \u2192 Meaning: 0.25`);
      assert.equal(await s.page.evaluate(() => document.querySelectorAll('input[type=range]').length), 2);
      await s.page.click('input[name=mode][value=semantic]');
      await waitFor(s.page, () => document.querySelectorAll('input[type=range]').length === 1, 'the alpha slider to go (semantic)');
      assert.equal(await alphaLabel(s.page), null);
      await s.page.click('input[name=mode][value=hnsw]');
      await waitFor(s.page, () => document.querySelectorAll('input[type=range]').length === 1, 'no alpha slider (vector)');
      await s.page.click('input[name=mode][value=hybrid]');
      await waitFor(s.page, () => document.querySelectorAll('input[type=range]').length === 2, 'the alpha slider to return');
      await fillNth(s.page, 'input[type=range]', 1, 0.4);
      await waitFor(s.page, () => [...document.querySelectorAll('label')].some(l => l.textContent.trim().endsWith('Meaning: 0.4')), 'alpha 0.4');
      await press(s.page, 'Save for this collection');
      await waitText(s.page, 'Saved!');
      const posts = callsTo(s, 'POST', '/api/retrieval/config');
      assert.equal(posts.length, 1);
      assert.deepEqual(posts[0].body, { collection: A, retrieval_mode: 'hybrid', top_k: 7, alpha: 0.4, ef: null, response_format: 'engineer' });
    } },
    (() => {
      const h = hold();
      return { name: 'Save is disabled and the page says Loading while the saved settings are read, then it is enabled', open: open({ 'GET /api/retrieval/config/*': () => h }), async run(s) {
        await waitText(s.page, 'Loading saved settings' + ELLIPSIS);
        assert.equal((await buttonState(s.page, 'Save for this collection')).disabled, true);
        h.release(json(200, retrievalConfig(A)));
        await waitText(s.page, 'Loading saved settings' + ELLIPSIS, true);
        assert.equal((await buttonState(s.page, 'Save for this collection')).disabled, false);
      } };
    })(),
  ];
}

// The proxy answers some errors itself, as HTML; the client turns them into text
// (ui/src/api/client.ts). Driven through the Import page's upload.
function proxyCases() {
  const one = (name, answer, want) => ({ name, open: { role: 'engineer', path: '/import', routes: base({ 'POST /api/ingest/upload': answer }) }, async run(s) {
    await waitSelected(s.page, 0);
    await queueFiles(s.page, ['doc.txt']);
    await press(s.page, 'Start Ingest');
    await waitText(s.page, want);
    const shown = await s.page.evaluate(() => document.querySelector('p.text-red-600')?.textContent.trim());
    assert.equal(shown, want);
    assert.ok(!/JSON|Unexpected token|<html/i.test(shown), 'no parser text: ' + shown);
  } });
  return [
    one('A proxy 413 page is shown as the upload limit message', html(413, 'Request Entity Too Large'), 'The upload is larger than the 512 MB limit. Split it into smaller batches.'),
    one('A proxy 502 page is shown as "the API is not responding"', html(502, 'Bad Gateway'), 'The API is not responding. It may still be starting; try again in a minute.'),
    one('A proxy 504 page is shown as "the API took too long"', html(504, 'Gateway Time-out'), 'The API took too long to respond.'),
    one('An HTML error page with no special text is shown as its status', html(500, 'Internal Server Error'), 'HTTP 500'),
    one('A 200 that is not JSON is reported as unreadable', { status: 200, type: 'text/html', body: '<html><body>not json</body></html>' }, 'The server sent a response the UI could not read.'),
    one("The API's own JSON message wins over the status text", apiError(413, 'PAYLOAD_TOO_LARGE', 'Synthetic API message.'), 'Synthetic API message.'),
  ];
}

async function runUiControlTests(browser, baseUrl, reporter) {
  S.BASE = baseUrl;
  const run = (section, cases) => S.runCases(browser, baseUrl, reporter, section, cases);
  await run('Collections table and create modal (#400)', collectionsCases());
  await run('Import queue, sliders and "+ New" modal (#400)', importCases());
  await run('Navigation, roles and redirects (#400); the API enforces the roles (#25)', navigationCases());
  await run('Login, logout, setup and password changes (#25)', authCases());
  await run('Users page: Engineers manage accounts (#25)', usersCases());
  await run('Service keys page: Engineers manage service principals and keys (#25)', serviceKeysCases());
  await run('Q&A input, citations toggle, answer display and End User view (#400)', qaCases());
  await run('Retrieval page status text, alpha slider and save (#400)', retrievalCases());
  await run('Client handling of the proxy error pages (#400)', proxyCases());
}

module.exports = { runUiControlTests };
if (require.main === module) (async () => {
  const { makeReporter } = require('./lib');
  const browser = await require('puppeteer-core').launch({ executablePath: process.env.RAG_CHROMIUM_PATH || '/usr/bin/chromium-browser', headless: true, args: ['--no-sandbox', '--disable-dev-shm-usage'] });
  const reporter = makeReporter();
  try { await runUiControlTests(browser, process.env.RAG_UI_BASE || 'http://127.0.0.1:3000', reporter); }
  finally { await browser.close(); }
  process.exitCode = reporter.summary() ? 0 : 1;
})().catch(e => { console.error(e); process.exitCode = 2; });
