/**
 * BOTLAB Bot Log Auditor - hosted demo (Cloudflare Worker).
 *
 * Stateless: uploaded CSV text lives only in the memory of the request that processes it.
 * The CSV is never written anywhere. The only optional storage is the RECEIPTS KV (payment receipts: ids, amount, input fingerprint).
 *
 * MIT License (c) 2026 BOTLAB
 */
import { grade, parseCSV, GATE_TITLES, DEFAULTS } from './gates.js';
import { normalize, sniffRows, heuristicMapping } from './normalize.js';
import { analyse, crossCheck } from './analysis.js';
import { buildFacts, writeReport, chat, AI_MODEL } from './report.js';
import { paypalConfigured, paymentMode, price, createOrder, captureOrder, verifyOrder, verifySbOrder, getOrder, verifyWebhook, receiptFrom } from './paypal.js';
import { braintreeConfigured, clientToken, charge, getTransaction, findPaid, checkTxn, receiptFromTxn, parseWebhook } from './braintree.js';

export const MAX_CSV_BYTES = 2 * 1024 * 1024;
export const MAX_BODY_BYTES = MAX_CSV_BYTES + 64 * 1024;
export const MAX_TRADES = 20000;
const CROSSCHECK_MAX_ROWS = 1000; // re-parse with gates.js only for small files (CPU budget)
const SAMPLES = ['synthetic_42_trades_small_lucky.csv', 'synthetic_640_trades_passes.csv', 'synthetic_bybit_closed_pnl.csv'];

export const STEPS = [
  ['parse', 'Parse CSV'],
  ['normalise', 'Normalise exchange format'],
  ['gates', 'Run 6 statistical gates (gates.js)'],
  ['crosscheck', 'Cross-check the numbers'],
  ['analysis', 'Deterministic analysis (months, coins, sides)'],
  ['report', 'Write AI audit report'],
];

/* ---------------------------------------------------------------- headers */
const cspFor = nonce => CSP.replace("script-src 'self'", `script-src 'self' 'nonce-${nonce}'`);
const CSP = [
  "default-src 'self'",
  "script-src 'self' https://www.paypal.com https://www.sandbox.paypal.com https://*.paypalobjects.com https://js.braintreegateway.com https://assets.braintreegateway.com",
  "style-src 'self' 'unsafe-inline' https://assets.braintreegateway.com",
  "img-src 'self' data: https://*.paypal.com https://*.paypalobjects.com https://assets.braintreegateway.com https://*.braintree-api.com",
  "connect-src 'self' https://*.paypal.com https://*.paypalobjects.com https://*.braintree-api.com https://*.braintreegateway.com",
  "frame-src https://www.paypal.com https://www.sandbox.paypal.com https://*.paypal.com https://assets.braintreegateway.com https://*.braintreegateway.com",
  "font-src 'self' data:",
  "object-src 'none'",
  "base-uri 'none'",
  "form-action 'self'",
  "frame-ancestors 'none'",
].join('; ');

export const SECURITY_HEADERS = {
  'Content-Security-Policy': CSP,
  'X-Content-Type-Options': 'nosniff',
  'X-Frame-Options': 'DENY',
  'Referrer-Policy': 'strict-origin-when-cross-origin',
  'Permissions-Policy': 'camera=(), microphone=(), geolocation=(), payment=(self "https://www.paypal.com" "https://www.sandbox.paypal.com")',
  'Cross-Origin-Opener-Policy': 'same-origin-allow-popups',
};

function withHeaders(res, extra = {}) {
  const r = new Response(res.body, res);
  for (const [k, v] of Object.entries({ ...SECURITY_HEADERS, ...extra })) r.headers.set(k, v);
  return r;
}

const json = (obj, status = 200) => new Response(JSON.stringify(obj), {
  status, headers: { 'Content-Type': 'application/json; charset=utf-8', 'Cache-Control': 'no-store', ...SECURITY_HEADERS },
});
const err = (msg, status = 400) => json({ error: msg }, status);

/* ---------------------------------------------------------------- rate limit (per isolate, best effort) */
const buckets = new Map();
const LIMITS = { audit: [20, 600], pro: [20, 600], chat: [40, 600], paypal: [30, 600], agent: [30, 600], webhook: [120, 600] };
export function rateLimited(ip, kind, now = Date.now()) {
  const [max, win] = LIMITS[kind] || [60, 600];
  const key = kind + ':' + ip;
  let b = buckets.get(key);
  if (!b || now > b.reset) { b = { n: 0, reset: now + win * 1000 }; buckets.set(key, b); }
  b.n++;
  if (buckets.size > 5000) for (const [k, v] of buckets) if (now > v.reset) buckets.delete(k);
  return b.n > max;
}
export function _resetRateLimits() { buckets.clear(); }

/* ---------------------------------------------------------------- input */
async function readJson(request) {
  const len = Number(request.headers.get('content-length') || 0);
  if (len > MAX_BODY_BYTES) throw new HttpError(413, 'Request too large (max 2 MB of CSV).');
  const text = await request.text();
  if (text.length > MAX_BODY_BYTES) throw new HttpError(413, 'Request too large (max 2 MB of CSV).');
  try { return JSON.parse(text); } catch { throw new HttpError(400, 'Body must be JSON.'); }
}

class HttpError extends Error { constructor(status, msg) { super(msg); this.status = status; } }

export function auditInput(body) {
  const csv = typeof body.csv === 'string' ? body.csv : '';
  if (!csv.trim()) throw new HttpError(400, 'Paste or upload a CSV first.');
  if (new TextEncoder().encode(csv).length > MAX_CSV_BYTES) throw new HttpError(413, 'CSV too large (max 2 MB).');
  let trials = parseInt(body.trials, 10);
  trials = Number.isFinite(trials) ? Math.min(100000, Math.max(1, trials)) : 20;
  let risk = parseFloat(body.risk_usd);
  risk = Number.isFinite(risk) && risk > 0 ? Math.min(risk, 1e9) : null;
  return { csv, trials, risk };
}

export async function fingerprint({ csv, trials, risk }) {
  const data = new TextEncoder().encode(JSON.stringify([csv.trim(), trials, risk]));
  const h = await crypto.subtle.digest('SHA-256', data);
  return [...new Uint8Array(h)].map(b => b.toString(16).padStart(2, '0')).join('');
}

/* ---------------------------------------------------------------- the agent pipeline */
/**
 * Runs parse -> normalise -> gates -> cross-check -> analysis -> report.
 * `emit(step)` is called on every step change (used for NDJSON streaming).
 */
export async function runAudit(input, env, { pro = false, quick = false, emit = () => {} } = {}) {
  const steps = STEPS.map(([id, label]) => ({ id, label, status: 'pending', detail: '', ms: null }));
  const t0 = {};
  const start = id => { const s = steps.find(x => x.id === id); s.status = 'running'; t0[id] = Date.now(); emit({ ...s }); };
  const done = (id, detail, status = 'done') => {
    const s = steps.find(x => x.id === id);
    Object.assign(s, { status, detail, ms: Date.now() - t0[id] }); emit({ ...s });
  };

  start('parse');
  const pre = sniffRows(input.csv);
  const { headers, body, delim } = pre;
  const map = heuristicMapping(headers);
  done('parse', `${body.length} data rows · ${headers.length} columns · delimiter ${delim === '\t' ? 'tab' : JSON.stringify(delim)}`);

  start('normalise');
  if (!map) throw new HttpError(422, 'Could not find a result column (R multiple or PnL). Columns seen: ' + headers.slice(0, 20).join(', '));
  const parsed = normalize(input.csv, input.risk, pre);
  if (parsed.rows.length > MAX_TRADES) throw new HttpError(413, `Too many trades for the hosted demo (max ${MAX_TRADES}).`);
  const meta = parsed.meta;
  done('normalise', `${meta.n_trades} trades · format ${meta.format} · unit ${parsed.unit}` + (meta.notes.length ? ' · ' + meta.notes.join(' ') : ''));

  start('gates');
  const r = grade(parsed, input.trials);
  done('gates', `${r.passed}/${r.total} gates passed · verdict ${r.verdict}`);

  start('crosscheck');
  let rawParsed = null;
  if (meta.format === 'generic' && !input.risk && parsed.rows.length <= CROSSCHECK_MAX_ROWS) { try { rawParsed = parseCSV(input.csv); } catch { rawParsed = null; } }
  const cc = crossCheck(parsed, r, rawParsed);
  done('crosscheck', cc.match ? `identical (${cc.checks.join(', ')})` : `${cc.diffs.length} differences`, cc.match ? 'done' : 'warn');

  start('analysis');
  const analysis = analyse(parsed.rows);
  const s = analysis.summary;
  done('analysis', `${s.months} months (${s.green_months} green / ${s.red_months} red) · ${s.days} trading days (${s.green_days} green / ${s.red_days} red) · ${s.coins} coins`);

  const facts = buildFacts(parsed, r, analysis, input.trials);
  start('report');
  if (quick) {                                   // agent quote: numbers only, the AI report comes with the paid audit
    done('report', 'skipped (quote only)', 'done');
    return { pro: false, quick: true, steps, input: meta, unit: r.unit, verdict: r.verdict, passed: r.passed, total: r.total,
      gates: Object.fromEntries(Object.entries(r.G).map(([g, v]) => [g, { title: GATE_TITLES[g], pass: v[0], detail: v[1] }])),
      summary: s, fingerprint: await fingerprint(input) };
  }
  const rep = await writeReport(env, facts, pro);
  done('report', `${rep.text.split(/\s+/).length} words · ${rep.source === 'workers-ai' ? 'Workers AI ' + rep.model : 'template fallback' + (rep.error ? ' (' + rep.error + ')' : '')}`,
    rep.source === 'workers-ai' ? 'done' : 'warn');

  const curve = r.curve, step = Math.max(1, Math.ceil(curve.length / 400));
  return {
    pro,
    steps,
    input: meta,
    unit: r.unit,
    facts: { ...facts, analysis: pro ? facts.analysis : { summary: s } },
    gates: Object.fromEntries(Object.entries(r.G).map(([g, v]) => [g, { title: GATE_TITLES[g], pass: v[0], detail: v[1] }])),
    crosscheck: cc,
    curve: curve.filter((_, i) => i % step === 0).map(v => Math.round(v * 1e4) / 1e4),
    analysis: pro ? { monthly: analysis.monthly, by_symbol: analysis.by_symbol, by_side: analysis.by_side,
      longest_losing_streak: analysis.longest_losing_streak, summary: s } : { summary: s },
    report: rep.text,
    report_source: rep.source,
    model: rep.model,
    fingerprint: await fingerprint(input),
  };
}

function streamAudit(input, env, ctx, opts) {
  const { readable, writable } = new TransformStream();
  const w = writable.getWriter(), enc = new TextEncoder();
  const send = o => w.write(enc.encode(JSON.stringify(o) + '\n')).catch(() => {});
  const p = (async () => {
    try {
      const result = await runAudit(input, env, { ...opts, emit: step => send({ type: 'step', step }) });
      await send({ type: 'result', result });
    } catch (e) {
      await send({ type: 'error', error: publicError(e) });
    } finally { await w.close().catch(() => {}); }
  })();
  if (ctx && ctx.waitUntil) ctx.waitUntil(p);
  return new Response(readable, { headers: { 'Content-Type': 'application/x-ndjson; charset=utf-8', 'Cache-Control': 'no-store', ...SECURITY_HEADERS } });
}

const publicError = e => (e instanceof HttpError || /^(Need|Empty|Could not|Too many|CSV)/.test(String(e.message)) ? String(e.message) : 'Audit failed.');

/* ---------------------------------------------------------------- receipts + agentic commerce */
/** Optional KV binding RECEIPTS: keeps payment receipts (ids, amount, input fingerprint), never the CSV. */
async function saveReceipt(env, r) {
  if (!env.RECEIPTS || !r || !r.order_id) return;
  try { await env.RECEIPTS.put('order:' + r.order_id, JSON.stringify(r), { expirationTtl: 90 * 86400 }); } catch { /* best effort */ }
}
async function loadReceipt(env, orderId) {
  if (!env.RECEIPTS) return null;
  try { const t = await env.RECEIPTS.get('order:' + orderId); return t ? JSON.parse(t) : null; } catch { return null; }
}

const MOCK_ID = fp => 'MOCK-' + fp.slice(0, 16);

/** Machine-readable description so another AI agent can discover and buy an audit (agentic commerce). */
export function agentManifest(origin, env) {
  return {
    name: 'BOTLAB Bot Log Auditor',
    description: 'Statistical audit of a trading bot trade log: six gates (sample size, Deflated Sharpe, recency, concentration, losing side, drawdown) plus an AI-written report. Educational, not financial advice.',
    payment: { provider: paymentMode(env) === 'braintree' ? 'Braintree (PayPal)' : 'PayPal', environment: 'sandbox', currency: 'USD', price: price(env), mode: paymentMode(env),
      human_approval: 'Each purchase is approved by a human on PayPal (approve_url) before the agent can collect the audit.' },
    flow: [
      { step: 'quote', method: 'POST', url: origin + '/api/agent/quote', body: '{csv, trials?, risk_usd?, agent?, max_price_usd}', returns: 'free gate summary + offer + PayPal order (order_id, approve_url)' },
      { step: 'approve', who: 'the human owner of the agent', url: 'approve_url (PayPal sandbox checkout)' },
      { step: 'fulfil', method: 'POST', url: origin + '/api/agent/fulfil', body: '{csv, trials?, risk_usd?, order_id}', returns: '202 awaiting_approval | 200 {receipt, audit}' },
      { step: 'receipt', method: 'GET', url: origin + '/api/receipt?order_id=...' },
    ],
    limits: { max_csv_bytes: MAX_CSV_BYTES, max_trades: MAX_TRADES },
    policy: 'Never gives buy/sell advice or profit predictions. The CSV is processed in memory and never stored.',
  };
}

async function agentQuote(request, env, body) {
  const input = auditInput(body);
  const offerPrice = price(env);
  if (body.max_price_usd != null && !(Number(body.max_price_usd) >= Number(offerPrice))) {
    return json({ error: `Over budget: the Pro audit costs ${offerPrice} USD, max_price_usd is ${body.max_price_usd}.`, price: offerPrice }, 402);
  }
  const q = await runAudit(input, env, { quick: true });
  const origin = new URL(request.url).origin;
  const mode = paymentMode(env);
  let payment;
  if (mode === 'braintree') {
    // The human approves by paying on our own checkout page (Braintree Drop-in: PayPal wallet or card).
    const agent = String(body.agent || '').replace(/[^A-Za-z0-9 ._-]/g, '').slice(0, 40);
    payment = { mode, order_id: q.fingerprint, status: 'AWAITING_APPROVAL',
      approve_url: origin + '/pay.html?fp=' + q.fingerprint + (agent ? '&agent=' + encodeURIComponent(agent) : '') };
  } else if (mode === 'server') {
    const o = await createOrder(env, q.fingerprint, undefined, { agent: body.agent,
      returnUrl: origin + '/paid.html', cancelUrl: origin + '/paid.html?cancelled=1' });
    payment = { mode, order_id: o.id, status: o.status, approve_url: o.approve_url };
  } else if (mode === 'mock') {
    const id = MOCK_ID(q.fingerprint);
    payment = { mode, order_id: id, status: 'APPROVED', approve_url: origin + '/paid.html?mock=1&token=' + id,
      note: 'Demo deployment without PayPal keys: the order is simulated and counts as approved.' };
  } else {
    return json({ error: 'Agent checkout needs server-side PayPal (sandbox keys) on this deployment.', fingerprint: q.fingerprint }, 503);
  }
  return json({
    quote: { verdict: q.verdict, passed: q.passed, total: q.total, gates: q.gates, summary: q.summary, input: q.input, unit: q.unit },
    fingerprint: q.fingerprint,
    offer: { product: 'Pro audit: AI report, month-by-month green/red days, per-coin and per-side tables, robustness checklist', price: offerPrice, currency: 'USD' },
    payment,
    next: 'A human approves approve_url on PayPal; then POST the same csv/trials/risk_usd with order_id to /api/agent/fulfil.',
  });
}

async function agentFulfil(env, body) {
  const input = auditInput(body);
  const fp = await fingerprint(input);
  const orderId = String(body.order_id || '');
  const mode = paymentMode(env);
  let receipt;
  if (mode === 'braintree') {
    const t = await findPaid(env, fp);
    if (!t) return json({ status: 'awaiting_approval', order_id: fp, approve_url: null }, 202);
    const v = checkTxn(t, fp, env);
    if (!v.ok) return err('Pro not unlocked: ' + v.reason, 402);
    receipt = receiptFromTxn(t, 'agent');
    await saveReceipt(env, receipt);
  } else if (mode === 'server') {
    let o = await getOrder(env, orderId);
    if (o.custom_id && o.custom_id !== fp) return err('Order was paid for a different file.', 402);
    if (o.status === 'CREATED' || o.status === 'PAYER_ACTION_REQUIRED' || o.status === 'SAVED') {
      return json({ status: 'awaiting_approval', order_id: orderId, approve_url: o.approve_url }, 202);
    }
    if (o.status === 'APPROVED') { await captureOrder(env, orderId); o = await getOrder(env, orderId); }
    const v = await verifyOrder(env, orderId, fp);
    if (!v.ok) return err('Pro not unlocked: ' + v.reason, 402);
    receipt = receiptFrom(o, 'agent');
    await saveReceipt(env, receipt);
  } else if (mode === 'mock') {
    if (orderId !== MOCK_ID(fp)) return err('Pro not unlocked: unknown mock order for this file.', 402);
    receipt = { order_id: orderId, status: 'COMPLETED', amount: price(env) + ' USD', fingerprint: fp, source: 'agent-mock', at: new Date().toISOString(), sandbox: true };
  } else {
    return err('Agent checkout needs server-side PayPal (sandbox keys) on this deployment.', 503);
  }
  const audit = await runAudit(input, env, { pro: true });
  return json({ status: 'fulfilled', receipt, audit });
}

/* ---------------------------------------------------------------- router */
export default {
  async fetch(request, env, ctx) {
    const url = new URL(request.url);
    const path = url.pathname;
    const ip = request.headers.get('cf-connecting-ip') || 'local';
    try {
      if (path.startsWith('/api/')) {
        if (path === '/api/config' && request.method === 'GET') {
          const mode = paymentMode(env);
          const payments = { mode, price: price(env), currency: 'USD',
            label: { braintree: 'Braintree Sandbox (PayPal)', server: 'PayPal Sandbox', sb: 'Payments demo mode · PayPal sandbox (sb)', mock: 'Payments demo mode' }[mode],
            client_id: mode === 'server' ? env.PAYPAL_CLIENT_ID : mode === 'sb' ? 'sb' : null };
          return json({
            ai: { model: AI_MODEL, binding: Boolean(env.AI) },
            payments,
            limits: { max_csv_bytes: MAX_CSV_BYTES, max_trades: MAX_TRADES },
            gates: { G1_MIN_N: DEFAULTS.G1_MIN_N },
            samples: SAMPLES,
          });
        }
        if (path === '/api/agent/manifest' && request.method === 'GET') return json(agentManifest(url.origin, env));
        if (path === '/api/receipt' && request.method === 'GET') {
          const id = url.searchParams.get('order_id') || '';
          if (!/^[A-Za-z0-9-_]{6,40}$/.test(id)) return err('bad order id');
          let r = await loadReceipt(env, id);
          if (!r && paymentMode(env) === 'braintree') {
            const t = await getTransaction(env, id);
            if (!t || !checkTxn(t, t.order_id, env).ok) return err('No completed payment for this transaction.', 404);
            r = receiptFromTxn(t, 'braintree-lookup');
          }
          if (!r && paymentMode(env) === 'server') {
            const o = await getOrder(env, id);
            if (o.status !== 'COMPLETED') return err('No completed payment for this order (status ' + o.status + ').', 404);
            r = receiptFrom(o, 'paypal-lookup');
          }
          return r ? json(r) : err('No receipt for this order.', 404);
        }
        if (request.method !== 'POST') return err('Method not allowed.', 405);
        if (path === '/api/braintree/webhook') {
          if (rateLimited(ip, 'webhook')) return err('Too many webhook calls.', 429);
          if (!braintreeConfigured(env)) return err('Braintree is off on this deployment.', 503);
          const form = await request.formData().catch(() => null);
          const n = form && await parseWebhook(env, form.get('bt_signature'), form.get('bt_payload'));
          if (!n) return err('Webhook signature not verified.', 401);
          if (n.transaction_id && /^transaction_(settled|settlement_pending|disbursed)$|^check$/.test(n.kind || '')) {
            try {
              const t = await getTransaction(env, n.transaction_id);
              if (t) await saveReceipt(env, receiptFromTxn(t, 'webhook:' + n.kind));
            } catch { /* receipt is best effort */ }
          }
          return json({ received: true, kind: n.kind });
        }
        const ct = request.headers.get('content-type') || '';
        if (!ct.includes('application/json')) return err('Content-Type must be application/json.', 415);

        if (path === '/api/audit' || path === '/api/pro') {
          const pro = path === '/api/pro';
          if (rateLimited(ip, pro ? 'pro' : 'audit')) return err('Too many audits from your address. Try again in a few minutes.', 429);
          const body = await readJson(request);
          const input = auditInput(body);
          if (pro) {
            const fp = await fingerprint(input);
            const mode = paymentMode(env);
            let v;
            if (mode === 'braintree') v = await getTransaction(env, String(body.bt_transaction || '')).then(t => checkTxn(t, fp, env)).catch(e => ({ ok: false, reason: e.message }));
            else if (mode === 'server') v = await verifyOrder(env, String(body.order_id || ''), fp).catch(e => ({ ok: false, reason: e.message }));
            else if (mode === 'sb') v = verifySbOrder(body.sb_order, fp, env);
            else v = body.demo === true ? { ok: true } : { ok: false, reason: 'send demo=true for the mock unlock' };
            if (!v.ok) return err('Pro not unlocked: ' + v.reason, 402);
          }
          const wantsStream = url.searchParams.get('stream') === '1' || (request.headers.get('accept') || '').includes('application/x-ndjson');
          if (wantsStream) return streamAudit(input, env, ctx, { pro });
          return json(await runAudit(input, env, { pro }));
        }

        if (path === '/api/chat') {
          if (rateLimited(ip, 'chat')) return err('Too many questions. Try again in a few minutes.', 429);
          const body = await readJson(request);
          const question = typeof body.question === 'string' ? body.question.trim() : '';
          if (!question) return err('Ask a question.');
          if (question.length > 1000) return err('Question too long (max 1000 characters).');
          if (typeof body.report !== 'string' || !body.report.trim()) return err('Run an audit first.');
          return json(await chat(env, { facts: body.facts, report: body.report, history: body.history, question }));
        }

        if (path === '/api/paypal/webhook') {
          if (rateLimited(ip, 'webhook')) return err('Too many webhook calls.', 429);
          if (!paypalConfigured(env) || !env.PAYPAL_WEBHOOK_ID) return err('Webhooks are off (no sandbox keys / PAYPAL_WEBHOOK_ID).', 503);
          const event = await readJson(request);
          const ok = await verifyWebhook(env, request.headers, event).catch(() => false);
          if (!ok) return err('Webhook signature not verified.', 401);
          const res = (event && event.resource) || {};
          if (event.event_type === 'PAYMENT.CAPTURE.COMPLETED') {
            const orderId = (((res.supplementary_data || {}).related_ids) || {}).order_id;
            if (orderId) await saveReceipt(env, { order_id: orderId, capture_id: res.id || null, status: 'COMPLETED',
              amount: res.amount ? `${res.amount.value} ${res.amount.currency_code}` : null, fingerprint: res.custom_id || null,
              source: 'webhook', event_id: event.id || null, at: new Date().toISOString(), sandbox: true });
          }
          return json({ received: true, event_type: event.event_type || null });
        }

        if (path === '/api/agent/quote' || path === '/api/agent/fulfil') {
          if (rateLimited(ip, 'agent')) return err('Too many agent requests. Try again in a few minutes.', 429);
          const body = await readJson(request);
          return path === '/api/agent/quote' ? await agentQuote(request, env, body) : await agentFulfil(env, body);
        }

        if (path === '/api/bt/token' || path === '/api/bt/checkout') {
          if (rateLimited(ip, 'paypal')) return err('Too many payment requests.', 429);
          if (!braintreeConfigured(env)) return err('Braintree is off on this deployment.', 503);
          const body = await readJson(request);
          if (path === '/api/bt/token') return json({ client_token: await clientToken(env), price: price(env), currency: 'USD',
            paypal: String(env.BRAINTREE_PAYPAL || '').toLowerCase() === 'on' });
          const t = await charge(env, String(body.nonce || ''), String(body.fingerprint || ''));
          if (t.completed) await saveReceipt(env, receiptFromTxn(t, body.agent ? 'agent-approval' : 'checkout'));
          return json({ id: t.id, status: t.status, completed: t.completed, method: t.method });
        }

        if (path === '/api/paypal/create' || path === '/api/paypal/capture') {
          if (rateLimited(ip, 'paypal')) return err('Too many payment requests.', 429);
          if (!paypalConfigured(env)) return err('Server-side PayPal is off (no sandbox keys configured).', 503);
          const body = await readJson(request);
          if (path === '/api/paypal/create') return json(await createOrder(env, String(body.fingerprint || '')));
          const cap = await captureOrder(env, String(body.order_id || ''));
          if (cap.completed) { try { await saveReceipt(env, receiptFrom(await getOrder(env, cap.id), 'checkout')); } catch { /* receipt is best effort */ } }
          return json(cap);
        }
        return err('Not found.', 404);
      }

      if (request.method !== 'GET' && request.method !== 'HEAD') return err('Method not allowed.', 405);
      if (!env.ASSETS) return err('Not found.', 404);
      const res = await env.ASSETS.fetch(request);
      if ((res.headers.get('content-type') || '').includes('text/html')) {
        // per-response nonce so the PayPal SDK may add its own inline script (data-csp-nonce)
        const nonce = btoa(String.fromCharCode(...crypto.getRandomValues(new Uint8Array(16))));
        const html = (await res.text()).replaceAll('__CSP_NONCE__', nonce);
        const h = new Headers(res.headers); h.delete('content-length'); h.delete('etag');
        return withHeaders(new Response(html, { status: res.status, headers: h }), { 'Content-Security-Policy': cspFor(nonce), 'Cache-Control': 'no-store' });
      }
      const extra = path.startsWith('/samples/') ? { 'Content-Type': 'text/csv; charset=utf-8' } : {};
      return withHeaders(res, extra);
    } catch (e) {
      if (e instanceof HttpError) return err(e.message, e.status);
      if (/^(Need|Empty|Could not)/.test(String(e.message))) return err(e.message, 422);
      if (/^(PayPal|Braintree)|bad (order id|fingerprint|transaction id|payment nonce)/.test(String(e.message))) return err(String(e.message).slice(0, 200), 502);
      return err('Internal error.', 500);
    }
  },
};
