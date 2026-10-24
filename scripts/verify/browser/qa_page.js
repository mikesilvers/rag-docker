// Q&A answers belong to the query that produced them (#511). Every API call is
// answered by the check (see stub.js), and each query's answer is held until the
// case releases it, so a response can arrive after the collection changed or
// after a newer query. Non-ASCII UI text is written as \u escapes.
const assert = require('node:assert/strict');
const S = require('./stub');
const { json, apiError, hold, callsTo, waitFor, waitText, press, fill, buttonState } = S;

const A = 'VfyQaA', B = 'VfyQaB';
const ELLIPSIS = '\u2026';
const COLS = [
  { name: A, object_count: 3, index_type: 'hnsw', distance_metric: 'cosine', created_at: null, hnsw_config: null },
  { name: B, object_count: 4, index_type: 'hnsw', distance_metric: 'cosine', created_at: null, hnsw_config: null },
];
const retrievalConfig = name => ({ collection: name, retrieval_mode: 'hnsw', top_k: 5, alpha: 0.75, ef: null, response_format: 'engineer', is_default: true });
const reply = (answer, over = {}) => ({
  answer, retrieval_latency_ms: 12, llm_latency_ms: 34, chunks_retrieved: 1,
  citations: [{ source_file: answer.replace(/\W+/g, '_') + '.txt', chunk_index: 0, score: 0.5, excerpt: 'Excerpt of ' + answer }],
  ...over,
});

// Each POST /api/query gets its own held answer, in the order the page sent them.
function qaOpen(held) {
  return {
    role: 'engineer', path: '/qa', routes: {
      'GET /api/collections': json(200, { collections: COLS }),
      'GET /api/retrieval/config/*': call => json(200, retrievalConfig(call.path.split('/').pop())),
      'POST /api/query': () => { const h = hold(); held.push(h); return h; },
    },
  };
}

const queries = s => callsTo(s, 'POST', '/api/query');
async function untilQueries(s, n) {
  const end = Date.now() + 8000;
  while (queries(s).length < n) {
    if (Date.now() > end) throw new Error(`expected ${n} queries, saw ${queries(s).length}`);
    await new Promise(r => setTimeout(r, 25));
  }
}
// Releases a held answer and waits until the page has it and React has rendered.
async function release(s, h, answer, question) {
  const done = s.page.waitForResponse(r => r.url().endsWith('/api/query') && (r.request().postData() || '').includes(JSON.stringify(question)), { timeout: 8000 });
  h.release(answer);
  await done;
  await s.page.evaluate(() => new Promise(r => requestAnimationFrame(() => requestAnimationFrame(() => setTimeout(r, 50)))));
}

async function ready(page) {
  await waitFor(page, () => document.querySelector('select')?.value, 'the collection select to be filled');
}
async function ctrlEnter(page) {
  await page.focus('textarea');
  await page.keyboard.down('Control'); await page.keyboard.press('Enter'); await page.keyboard.up('Control');
}
async function ask(page, question, how = 'ctrl') {
  await fill(page, 'textarea', question);
  if (how === 'ctrl') await ctrlEnter(page); else await press(page, 'Ask');
}
const choose = (page, name) => fill(page, 'select', name);

// What the page shows as the result: answer, latency line, Sources panel, error line.
const shown = page => page.evaluate(() => ({
  collection: document.querySelector('select').value,
  answer: document.querySelector('.prose')?.innerText.trim() || null,
  latency: document.body.innerText.includes('Retrieved in'),
  sources: [...document.querySelectorAll('h3')].some(h => h.textContent.trim() === 'Sources'),
  error: [...document.querySelectorAll('p.text-red-600')].map(p => p.textContent.trim()).join(' | ') || null,
}));
const NOTHING = { answer: null, latency: false, sources: false, error: null };
const assertNothingFrom = (got, collection, why) => assert.deepEqual(got, { collection, ...NOTHING }, why);

function qaCases() {
  const cases = [];
  const add = (name, run) => { const held = []; cases.push({ name, open: qaOpen(held), run: s => run(s, held) }); };

  // T1: the issue's reproduction.
  add("A query sent on A and answered after the switch to B shows nothing under B, and Ask is enabled", async (s, held) => {
    await ready(s.page);
    await s.page.click('input[type=checkbox]');
    await ask(s.page, 'Question on A?');
    await untilQueries(s, 1);
    await choose(s.page, B);
    await release(s, held[0], json(200, reply('Answer from VfyQaA only')), 'Question on A?');
    assertNothingFrom(await shown(s.page), B, "A's answer, latency or citations are shown with B selected");
    assert.equal((await buttonState(s.page, 'Ask'))?.disabled, false, 'Ask is not enabled under B');
  });
  // T2: Ctrl+Enter respects the loading gate.
  add('Ctrl+Enter while a question is being answered sends no second query', async (s, held) => {
    await ready(s.page);
    await ask(s.page, 'First question?');
    await untilQueries(s, 1);
    await waitText(s.page, 'Thinking' + ELLIPSIS);
    await fill(s.page, 'textarea', 'Second question?');
    await ctrlEnter(s.page);
    await new Promise(r => setTimeout(r, 300));
    assert.equal(queries(s).length, 1, 'Ctrl+Enter sent a second query while the first was in flight');
    await release(s, held[0], json(200, reply('First answer.')), 'First question?');
    assert.equal((await shown(s.page)).answer, 'First answer.');
  });
  // T3: overlapping queries, the first answered last.
  add('Two queries in flight (A, then B after the switch): the first answered last does not replace the latest', async (s, held) => {
    await ready(s.page);
    await ask(s.page, 'Older question?');
    await untilQueries(s, 1);
    await choose(s.page, B);
    await ask(s.page, 'Latest question?');
    await untilQueries(s, 2);
    assert.deepEqual(queries(s).map(c => [c.body.question, c.body.collection]), [['Older question?', A], ['Latest question?', B]]);
    await release(s, held[1], json(200, reply('Latest answer.')), 'Latest question?');
    assert.equal((await shown(s.page)).answer, 'Latest answer.');
    await release(s, held[0], json(200, reply('Older answer.')), 'Older question?');
    const got = await shown(s.page);
    assert.equal(got.answer, 'Latest answer.', "the older query's answer replaced the latest one");
    assert.equal(got.collection, B);
  });
  // T4: a collection change clears the answer on screen.
  add('Changing the collection clears the answer, citations and latency already shown', async (s, held) => {
    await ready(s.page);
    await s.page.click('input[type=checkbox]');
    await ask(s.page, 'Question on A?');
    await untilQueries(s, 1);
    await release(s, held[0], json(200, reply('Answer on A.')), 'Question on A?');
    const before = await shown(s.page);
    assert.deepEqual([before.answer, before.latency, before.sources], ['Answer on A.', true, true]);
    await choose(s.page, B);
    assertNothingFrom(await shown(s.page), B, "A's answer is still shown after the switch to B");
  });
  // T5: a stale failure is a stale response too.
  add('A query sent on A that fails after the switch to B shows no error under B', async (s, held) => {
    await ready(s.page);
    await ask(s.page, 'Question on A?');
    await untilQueries(s, 1);
    await choose(s.page, B);
    await release(s, held[0], apiError(500, 'INTERNAL_ERROR', 'Synthetic failure on A.'), 'Question on A?');
    assertNothingFrom(await shown(s.page), B, "A's error is shown with B selected");
  });
  // T6: a collection change clears an error on screen.
  add('Changing the collection clears an error already shown', async (s, held) => {
    await ready(s.page);
    await ask(s.page, 'Question on A?');
    await untilQueries(s, 1);
    await release(s, held[0], apiError(500, 'INTERNAL_ERROR', 'Synthetic failure on A.'), 'Question on A?');
    assert.equal((await shown(s.page)).error, 'Synthetic failure on A.');
    await choose(s.page, B);
    assertNothingFrom(await shown(s.page), B, "A's error is still shown after the switch to B");
  });
  // T7: A -> B -> A is a new selection; the first query on A is stale.
  add('A query sent on A and answered after A -> B -> A is not shown', async (s, held) => {
    await ready(s.page);
    await ask(s.page, 'Question on A?');
    await untilQueries(s, 1);
    await choose(s.page, B);
    await choose(s.page, A);
    await release(s, held[0], json(200, reply('Answer from the first visit to A.')), 'Question on A?');
    assertNothingFrom(await shown(s.page), A, 'the query from before A -> B -> A was shown');
  });
  // T8: the switch ends the wait, so B can be asked at once with the button.
  add('After the switch to B, Ask is enabled at once and asks B; the late A answer does not replace it', async (s, held) => {
    await ready(s.page);
    await ask(s.page, 'Question on A?');
    await untilQueries(s, 1);
    await choose(s.page, B);
    assert.equal((await buttonState(s.page, 'Ask'))?.disabled, false, 'Ask is not enabled under B while A is in flight');
    await ask(s.page, 'Question on B?', 'button');
    await untilQueries(s, 2);
    assert.deepEqual([queries(s)[1].body.question, queries(s)[1].body.collection], ['Question on B?', B]);
    await release(s, held[1], json(200, reply('Answer on B.')), 'Question on B?');
    await release(s, held[0], json(200, reply('Answer from VfyQaA only')), 'Question on A?');
    assert.equal((await shown(s.page)).answer, 'Answer on B.');
  });
  return cases;
}

async function runQaPageTests(browser, baseUrl, reporter) {
  S.BASE = baseUrl;
  await S.runCases(browser, baseUrl, reporter, 'Q&A answers belong to their query and collection (#511)', qaCases());
}

module.exports = { runQaPageTests };
if (require.main === module) (async () => {
  const { makeReporter } = require('./lib');
  const browser = await require('puppeteer-core').launch({ executablePath: process.env.RAG_CHROMIUM_PATH || '/usr/bin/chromium-browser', headless: true, args: ['--no-sandbox', '--disable-dev-shm-usage'] });
  const reporter = makeReporter();
  try { await runQaPageTests(browser, process.env.RAG_UI_BASE || 'http://127.0.0.1:3000', reporter); }
  finally { await browser.close(); }
  process.exitCode = reporter.summary() ? 0 : 1;
})().catch(e => { console.error(e); process.exitCode = 2; });
