/**
 * BOTLAB Bot Log Auditor - hosted demo (Cloudflare Worker).
 *
 * Stateless: uploaded CSV text lives only in the memory of the request that processes it.
 * Nothing is written to KV, D1, R2, cache or logs.
 *
 * MIT License (c) 2026 BOTLAB
 */
import { grade, parseCSV, GATE_TITLES, DEFAULTS } from './gates.js';
import { normalize, sniffRows, heuristicMapping } from './normalize.js';
import { analyse, crossCheck } from './analysis.js';
import { buildFacts, writeReport, chat, AI_MODEL } from './report.js';
import { paypalConfigured, paymentMode, price, createOrder, captureOrder, verifyOrder, verifySbOrder } from './paypal.js';

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
  "script-src 'self' https://www.paypal.com https://www.sandbox.paypal.com https://*.paypalobjects.com",
  "style-src 'self' 'unsafe-inline'",
  "img-src 'self' data: https://*.paypal.com https://*.paypalobjects.com",
  "connect-src 'self' https://*.paypal.com https://*.paypalobjects.com",
  "frame-src https://www.paypal.com https://www.sandbox.paypal.com https://*.paypal.com",
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
const LIMITS = { audit: [20, 600], pro: [20, 600], chat: [40, 600], paypal: [30, 600] };
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
export async function runAudit(input, env, { pro = false, emit = () => {} } = {}) {
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
            label: { server: 'PayPal Sandbox', sb: 'Payments demo mode · PayPal sandbox (sb)', mock: 'Payments demo mode' }[mode],
            client_id: mode === 'server' ? env.PAYPAL_CLIENT_ID : mode === 'sb' ? 'sb' : null };
          return json({
            ai: { model: AI_MODEL, binding: Boolean(env.AI) },
            payments,
            limits: { max_csv_bytes: MAX_CSV_BYTES, max_trades: MAX_TRADES },
            gates: { G1_MIN_N: DEFAULTS.G1_MIN_N },
            samples: SAMPLES,
          });
        }
        if (request.method !== 'POST') return err('Method not allowed.', 405);
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
            if (mode === 'server') v = await verifyOrder(env, String(body.order_id || ''), fp).catch(e => ({ ok: false, reason: e.message }));
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

        if (path === '/api/paypal/create' || path === '/api/paypal/capture') {
          if (rateLimited(ip, 'paypal')) return err('Too many payment requests.', 429);
          if (!paypalConfigured(env)) return err('Server-side PayPal is off (no sandbox keys configured).', 503);
          const body = await readJson(request);
          if (path === '/api/paypal/create') return json(await createOrder(env, String(body.fingerprint || '')));
          return json(await captureOrder(env, String(body.order_id || '')));
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
      if (/^PayPal|bad (order id|fingerprint)/.test(String(e.message))) return err(String(e.message).slice(0, 200), 502);
      return err('Internal error.', 500);
    }
  },
};
