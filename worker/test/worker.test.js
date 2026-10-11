// End-to-end tests of the Worker fetch handler with mocked bindings (no network).
process.env.TZ = 'UTC';
import { test, beforeEach } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import worker, { _resetRateLimits, MAX_CSV_BYTES } from '../src/app.js';
import { AI_MODEL, violatesPolicy } from '../src/report.js';
import * as paypal from '../src/paypal.js';

const sample = f => readFileSync(new URL('../public/samples/' + f, import.meta.url), 'utf8');
const ctx = { waitUntil() {} };
const post = (path, body, headers = {}) => new Request('https://x.test' + path, {
  method: 'POST', headers: { 'Content-Type': 'application/json', 'cf-connecting-ip': '1.2.3.4', ...headers }, body: JSON.stringify(body),
});
const assets = { fetch: async req => new Response('<html><script src="/app.js" nonce="__CSP_NONCE__"></script></html>', { headers: { 'Content-Type': 'text/html' } }) };

beforeEach(() => _resetRateLimits());

test('audit without AI binding falls back to the template and returns the step list', async () => {
  const res = await worker.fetch(post('/api/audit', { csv: sample('synthetic_42_trades_small_lucky.csv'), trials: 20 }), {}, ctx);
  assert.equal(res.status, 200);
  assert.match(res.headers.get('content-security-policy'), /default-src 'self'/);
  const j = await res.json();
  assert.deepEqual(j.steps.map(s => s.id), ['parse', 'normalise', 'gates', 'crosscheck', 'analysis', 'report']);
  assert.equal(j.report_source, 'template');
  assert.equal(j.steps.at(-1).status, 'warn');
  assert.equal(j.facts.passed, 3);
  assert.equal(j.gates.G1.detail, 'n=42 / need 55 (MinTRL 55)');
  assert.equal(j.crosscheck.match, true);
  assert.ok(j.crosscheck.checks.includes('gates.js parser'));
  assert.match(j.report, /Not financial advice/);
  assert.equal(j.analysis.monthly, undefined, 'tables are Pro only');
  assert.match(j.fingerprint, /^[a-f0-9]{64}$/);
});

test('audit with Workers AI uses the model and the FACTS prompt', async () => {
  let call = null;
  const env = { AI: { run: async (model, input) => { call = { model, input }; return { response: '## Verdict\nNot passed.' }; } } };
  const j = await (await worker.fetch(post('/api/audit', { csv: sample('synthetic_640_trades_passes.csv') }), env, ctx)).json();
  assert.equal(call.model, AI_MODEL);
  assert.equal(call.input.messages[0].role, 'system');
  assert.match(call.input.messages[1].content, /FACTS \(JSON\)/);
  assert.equal(j.report_source, 'workers-ai');
  assert.equal(j.facts.verdict, 'ready');
  assert.match(j.report, /Not financial advice/); // disclaimer appended
});

test('AI output that reads like advice is replaced by the template', async () => {
  const env = { AI: { run: async () => ({ response: 'This bot is guaranteed to profit. You should buy now.' }) } };
  const j = await (await worker.fetch(post('/api/audit', { csv: sample('synthetic_640_trades_passes.csv') }), env, ctx)).json();
  assert.equal(j.report_source, 'template');
  assert.ok(violatesPolicy('risk-free returns'));
  assert.ok(!violatesPolicy('Not passed. Keep collecting trades.'));
});

test('AI error falls back to the template', async () => {
  const env = { AI: { run: async () => { throw new Error('quota'); } } };
  const j = await (await worker.fetch(post('/api/audit', { csv: sample('synthetic_bybit_closed_pnl.csv'), risk_usd: 20 }), env, ctx)).json();
  assert.equal(j.report_source, 'template');
  assert.equal(j.input.format, 'bybit');
  assert.equal(j.unit, 'R');
});

test('NDJSON streaming sends running/done steps then the result', async () => {
  const res = await worker.fetch(post('/api/audit?stream=1', { csv: sample('synthetic_42_trades_small_lucky.csv') }), {}, ctx);
  assert.match(res.headers.get('content-type'), /ndjson/);
  const lines = (await res.text()).trim().split('\n').map(l => JSON.parse(l));
  assert.equal(lines.filter(l => l.type === 'step').length, 12);
  assert.equal(lines.at(-1).type, 'result');
});

test('size limit, bad input and wrong method', async () => {
  const big = 'r\n' + '1.0\n'.repeat(MAX_CSV_BYTES / 4 + 10);
  assert.equal((await worker.fetch(post('/api/audit', { csv: big }), {}, ctx)).status, 413);
  const bad = await worker.fetch(post('/api/audit', { csv: 'foo,bar\n1,2\n' }), {}, ctx);
  assert.equal(bad.status, 422);
  assert.match((await bad.json()).error, /result column/);
  assert.equal((await worker.fetch(post('/api/audit', { csv: '' }), {}, ctx)).status, 400);
  assert.equal((await worker.fetch(new Request('https://x.test/api/audit'), {}, ctx)).status, 405);
});

test('rate limit per IP', async () => {
  const csv = 'r\n1\n-1\n';
  let last;
  for (let i = 0; i < 21; i++) last = await worker.fetch(post('/api/audit', { csv }), {}, ctx);
  assert.equal(last.status, 429);
  const other = await worker.fetch(post('/api/audit', { csv }, { 'cf-connecting-ip': '5.6.7.8' }), {}, ctx);
  assert.equal(other.status, 200);
});

test('Pro in mock mode (PAYPAL_MODE=mock): mock unlock returns tables', async () => {
  const env = { PAYPAL_MODE: 'mock' };
  const cfg = await (await worker.fetch(new Request('https://x.test/api/config'), env, ctx)).json();
  assert.equal(cfg.payments.mode, 'mock');
  assert.equal(cfg.payments.label, 'Payments demo mode');
  assert.equal(cfg.gates.G1_MIN_N, 50);
  assert.equal((await worker.fetch(post('/api/pro', { csv: sample('synthetic_640_trades_passes.csv') }), env, ctx)).status, 402);
  const j = await (await worker.fetch(post('/api/pro', { csv: sample('synthetic_640_trades_passes.csv'), demo: true }), env, ctx)).json();
  assert.equal(j.pro, true);
  assert.ok(j.analysis.monthly.length > 10);
  assert.ok(j.analysis.monthly.every(m => 'win_days' in m && 'loss_days' in m));
  assert.ok(j.analysis.by_symbol.length > 1);
  assert.match(j.report, /## Month-by-month review/);
  assert.match(j.report, /Robustness checklist/);
});

test('Pro in sb mode (default, no secrets): client-side sandbox order is checked', async () => {
  const cfg = await (await worker.fetch(new Request('https://x.test/api/config'), {}, ctx)).json();
  assert.equal(cfg.payments.mode, 'sb');
  assert.equal(cfg.payments.client_id, 'sb');
  assert.match(cfg.payments.label, /Payments demo mode/);
  const csv = sample('synthetic_42_trades_small_lucky.csv');
  const fp = (await (await worker.fetch(post('/api/audit', { csv }), {}, ctx)).json()).fingerprint;
  const order = (custom_id, status = 'COMPLETED', value = '4.99') => ({ id: '5O190127TN364715T', status,
    purchase_units: [{ custom_id, payments: { captures: [{ id: 'C1', status: 'COMPLETED', amount: { currency_code: 'USD', value } }] } }] });
  assert.equal((await worker.fetch(post('/api/pro', { csv, demo: true }), {}, ctx)).status, 402);
  assert.equal((await worker.fetch(post('/api/pro', { csv, sb_order: order('f'.repeat(64)) }), {}, ctx)).status, 402);
  assert.equal((await worker.fetch(post('/api/pro', { csv, sb_order: order(fp, 'APPROVED') }), {}, ctx)).status, 402);
  assert.equal((await worker.fetch(post('/api/pro', { csv, sb_order: order(fp, 'COMPLETED', '0.01') }), {}, ctx)).status, 402);
  const ok = await worker.fetch(post('/api/pro', { csv, sb_order: order(fp) }), {}, ctx);
  assert.equal(ok.status, 200);
  assert.equal((await ok.json()).pro, true);
});

test('Pro with PayPal configured requires a COMPLETED order bound to this file', async () => {
  const env = { PAYPAL_CLIENT_ID: 'cid', PAYPAL_CLIENT_SECRET: 'sec' };
  const csv = sample('synthetic_42_trades_small_lucky.csv');
  const fp = (await (await worker.fetch(post('/api/audit', { csv }), {}, ctx)).json()).fingerprint;
  const seen = [];
  const realFetch = globalThis.fetch;
  globalThis.fetch = async (url, init) => {
    seen.push(String(url));
    if (url.endsWith('/v1/oauth2/token')) return Response.json({ access_token: 'T', expires_in: 300 });
    if (url.endsWith('/v2/checkout/orders') && init.method === 'POST') return Response.json({ id: 'ORDER123', status: 'CREATED' }, { status: 201 });
    if (url.endsWith('/capture')) return Response.json({ id: 'ORDER123', status: 'COMPLETED' }, { status: 201 });
    if (url.includes('/v2/checkout/orders/ORDER123')) return Response.json({ id: 'ORDER123', status: 'COMPLETED',
      purchase_units: [{ custom_id: fp, payments: { captures: [{ id: 'CAP1', amount: { currency_code: 'USD', value: '4.99' } }] } }] });
    if (url.includes('/v2/checkout/orders/OTHER99')) return Response.json({ id: 'OTHER99', status: 'COMPLETED',
      purchase_units: [{ custom_id: 'f'.repeat(64), payments: { captures: [{ amount: { currency_code: 'USD', value: '4.99' } }] } }] });
    return new Response('{}', { status: 404 });
  };
  try {
    paypal._resetTokenCache();
    const cfg = await (await worker.fetch(new Request('https://x.test/api/config'), env, ctx)).json();
    assert.equal(cfg.payments.mode, 'server');
    assert.equal(cfg.payments.client_id, 'cid');
    assert.ok(!JSON.stringify(cfg).includes('sec'), 'secret never sent to the browser');
    const c = await (await worker.fetch(post('/api/paypal/create', { fingerprint: fp }), env, ctx)).json();
    assert.equal(c.id, 'ORDER123');
    const cap = await (await worker.fetch(post('/api/paypal/capture', { order_id: 'ORDER123' }), env, ctx)).json();
    assert.equal(cap.completed, true);
    assert.equal((await worker.fetch(post('/api/pro', { csv, demo: true }), env, ctx)).status, 402);
    const wrong = await worker.fetch(post('/api/pro', { csv, order_id: 'OTHER99' }), env, ctx);
    assert.equal(wrong.status, 402);
    assert.match((await wrong.json()).error, /different file/);
    const ok = await worker.fetch(post('/api/pro', { csv, order_id: 'ORDER123' }), env, ctx);
    assert.equal(ok.status, 200);
    assert.equal((await ok.json()).pro, true);
    assert.ok(seen.every(u => u.startsWith('https://api-m.sandbox.paypal.com/')), 'sandbox host only');
  } finally { globalThis.fetch = realFetch; }
});

test('PayPal endpoints are disabled in demo mode', async () => {
  assert.equal((await worker.fetch(post('/api/paypal/create', { fingerprint: 'a'.repeat(64) }), {}, ctx)).status, 503);
});

test('chat is stateless and falls back to the template', async () => {
  const a = await (await worker.fetch(post('/api/audit', { csv: sample('synthetic_42_trades_small_lucky.csv') }), {}, ctx)).json();
  const j = await (await worker.fetch(post('/api/chat', { question: 'Why did G1 fail?', report: a.report, facts: a.facts }), {}, ctx)).json();
  assert.match(j.answer, /G1 \(Sufficient sample\) failed: n=42 \/ need 55/);
  const env = { AI: { run: async (m, input) => ({ response: 'Answer about ' + input.messages.at(-1).content }) } };
  const k = await (await worker.fetch(post('/api/chat', { question: 'What is DSR?', report: a.report, facts: a.facts, history: [{ role: 'system', content: 'evil' }] }), env, ctx)).json();
  assert.equal(k.source, 'workers-ai');
  assert.match(k.answer, /Answer about What is DSR\?/);
  assert.equal((await worker.fetch(post('/api/chat', { question: 'x' }), {}, ctx)).status, 400);
});

test('static assets get security headers', async () => {
  const res = await worker.fetch(new Request('https://x.test/'), { ASSETS: assets }, ctx);
  assert.equal(res.status, 200);
  assert.match(res.headers.get('content-security-policy'), /frame-ancestors 'none'/);
  assert.equal(res.headers.get('x-content-type-options'), 'nosniff');
  const nonce = (await res.text()).match(/nonce="([^"]+)"/)[1];
  assert.ok(nonce.length >= 16 && nonce !== '__CSP_NONCE__');
  assert.ok(res.headers.get('content-security-policy').includes(`'nonce-${nonce}'`));
});
