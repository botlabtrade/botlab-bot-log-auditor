/**
 * PayPal Sandbox, Orders v2 (create -> buyer approves in PayPal JS SDK popup -> server capture).
 * Only https://api-m.sandbox.paypal.com is ever called. There is deliberately no live switch.
 *
 * Stateless unlock: the order's custom_id is a SHA-256 fingerprint of the audited input.
 * /api/pro re-reads the order from PayPal and only unlocks if it is COMPLETED, the amount matches,
 * and custom_id matches the fingerprint of the CSV being submitted. Nothing is stored.
 *
 * MIT License (c) 2026 BOTLAB
 */
export const SANDBOX_BASE = 'https://api-m.sandbox.paypal.com';

export function paypalConfigured(env) {
  return Boolean(env && env.PAYPAL_CLIENT_ID && env.PAYPAL_CLIENT_SECRET);
}

/**
 * Payment mode:
 *   'braintree' - BRAINTREE_MERCHANT_ID/PUBLIC_KEY/PRIVATE_KEY secrets exist: Braintree (a PayPal service) sandbox,
 *              Drop-in UI (PayPal wallet or card) + server-side charge and verification (src/braintree.js).
 *   'server' - PAYPAL_CLIENT_ID + PAYPAL_CLIENT_SECRET secrets exist: Orders v2 create/capture/verify on the server.
 *   'sb'     - no secrets, PAYPAL_MODE=sb (default): PayPal JS SDK with the generic sandbox client id `sb`;
 *              the order is created and captured in the browser, the Worker checks the returned order shape.
 *              No PayPal account needed, but the server cannot confirm the payment with PayPal (demo-grade).
 *   'mock'   - no secrets, PAYPAL_MODE=mock: a "mock unlock" button, no PayPal at all.
 */
export function paymentMode(env) {
  if (env && env.BRAINTREE_MERCHANT_ID && env.BRAINTREE_PUBLIC_KEY && env.BRAINTREE_PRIVATE_KEY) return 'braintree';
  if (paypalConfigured(env)) return 'server';
  return String((env && env.PAYPAL_MODE) || 'sb').toLowerCase() === 'mock' ? 'mock' : 'sb';
}

/** Check an order object returned by actions.order.capture() in 'sb' mode. Returns {ok, reason}. */
export function verifySbOrder(order, fingerprint, env) {
  if (!order || typeof order !== 'object') return { ok: false, reason: 'missing order' };
  if (!ORDER_ID_RE.test(String(order.id || ''))) return { ok: false, reason: 'bad order id' };
  if (order.status !== 'COMPLETED') return { ok: false, reason: `order status ${order.status}` };
  const pu = (order.purchase_units || [])[0] || {};
  if (pu.custom_id !== fingerprint) return { ok: false, reason: 'order was paid for a different file' };
  const cap = (((pu.payments || {}).captures) || [])[0] || {};
  if (cap.status !== 'COMPLETED') return { ok: false, reason: 'capture not completed' };
  const amt = cap.amount || pu.amount || {};
  if (amt.currency_code !== 'USD' || amt.value !== price(env)) return { ok: false, reason: 'amount mismatch' };
  return { ok: true, capture_id: cap.id || null };
}

export function price(env) {
  const p = String((env && env.PRO_PRICE_USD) || '4.99');
  return /^\d{1,4}\.\d{2}$/.test(p) ? p : '4.99';
}

let tokenCache = { key: '', token: '', exp: 0 };

async function ppFetch(path, init, fetchImpl) {
  const url = SANDBOX_BASE + path;
  if (!url.startsWith(SANDBOX_BASE + '/')) throw new Error('refusing non-sandbox PayPal URL');
  return (fetchImpl || fetch)(url, init);
}

async function accessToken(env, fetchImpl) {
  const now = Date.now();
  const key = env.PAYPAL_CLIENT_ID;
  if (tokenCache.token && tokenCache.key === key && now < tokenCache.exp - 60000) return tokenCache.token;
  const basic = btoa(`${env.PAYPAL_CLIENT_ID}:${env.PAYPAL_CLIENT_SECRET}`);
  const res = await ppFetch('/v1/oauth2/token', {
    method: 'POST',
    headers: { Authorization: `Basic ${basic}`, 'Content-Type': 'application/x-www-form-urlencoded' },
    body: 'grant_type=client_credentials',
  }, fetchImpl);
  const data = await res.json().catch(() => ({}));
  if (res.status !== 200 || !data.access_token) throw new Error(`PayPal auth failed (${res.status})`);
  tokenCache = { key, token: data.access_token, exp: now + (Number(data.expires_in) || 300) * 1000 };
  return data.access_token;
}

async function call(env, method, path, body, requestId, fetchImpl) {
  const headers = { Authorization: `Bearer ${await accessToken(env, fetchImpl)}`, 'Content-Type': 'application/json' };
  if (requestId) headers['PayPal-Request-Id'] = requestId;
  const res = await ppFetch(path, { method, headers, body: body == null ? undefined : JSON.stringify(body) }, fetchImpl);
  const data = await res.json().catch(() => ({}));
  if (res.status >= 400) throw new Error(`PayPal ${method} ${path.split('?')[0]} -> ${res.status} ${data.name || data.error || 'error'}`);
  return data;
}

const ORDER_ID_RE = /^[A-Za-z0-9-_]{6,40}$/;

/**
 * Create a sandbox order bound to one audited input (custom_id = fingerprint).
 * opts.returnUrl / opts.cancelUrl: used by the agent flow, where the buyer approves on PayPal's own page
 * (approve_url) instead of the JS SDK popup. opts.agent: free-text label of the buying agent (soft_descriptor-safe).
 */
export async function createOrder(env, fingerprint, fetchImpl, opts = {}) {
  if (!/^[a-f0-9]{64}$/.test(fingerprint || '')) throw new Error('bad fingerprint');
  const ctx = { brand_name: 'BOTLAB Auditor', user_action: 'PAY_NOW', shipping_preference: 'NO_SHIPPING' };
  if (opts.returnUrl) ctx.return_url = opts.returnUrl;
  if (opts.cancelUrl) ctx.cancel_url = opts.cancelUrl;
  const agent = String(opts.agent || '').replace(/[^A-Za-z0-9 ._-]/g, '').slice(0, 40);
  const body = {
    intent: 'CAPTURE',
    purchase_units: [{
      reference_id: 'pro-audit', custom_id: fingerprint.slice(0, 64),
      description: agent ? `BOTLAB Pro audit for agent ${agent} (sandbox)` : 'BOTLAB Pro audit (sandbox demo)',
      amount: { currency_code: 'USD', value: price(env) },
    }],
    payment_source: { paypal: { experience_context: ctx } },
  };
  const data = await call(env, 'POST', '/v2/checkout/orders', body, crypto.randomUUID(), fetchImpl);
  const link = (data.links || []).find(l => l.rel === 'payer-action' || l.rel === 'approve');
  return { id: data.id, status: data.status, approve_url: link ? link.href : null };
}

/** Read an order (status + the fields we bind to). */
export async function getOrder(env, orderId, fetchImpl) {
  if (!ORDER_ID_RE.test(orderId || '')) throw new Error('bad order id');
  const data = await call(env, 'GET', `/v2/checkout/orders/${encodeURIComponent(orderId)}`, null, null, fetchImpl);
  const pu = (data.purchase_units || [])[0] || {};
  const cap = (((pu.payments || {}).captures) || [])[0] || {};
  const link = (data.links || []).find(l => l.rel === 'payer-action' || l.rel === 'approve');
  return { id: data.id || orderId, status: data.status, custom_id: pu.custom_id || null,
    capture_id: cap.id || null, amount: cap.amount || pu.amount || null, approve_url: link ? link.href : null };
}

/**
 * Verify a webhook delivery with PayPal (POST /v1/notifications/verify-webhook-signature).
 * Needs PAYPAL_WEBHOOK_ID (the sandbox webhook's id). Returns true only on verification_status SUCCESS.
 */
export async function verifyWebhook(env, headers, event, fetchImpl) {
  if (!env.PAYPAL_WEBHOOK_ID) return false;
  const h = k => headers.get(k) || '';
  const body = {
    auth_algo: h('paypal-auth-algo'), cert_url: h('paypal-cert-url'), transmission_id: h('paypal-transmission-id'),
    transmission_sig: h('paypal-transmission-sig'), transmission_time: h('paypal-transmission-time'),
    webhook_id: env.PAYPAL_WEBHOOK_ID, webhook_event: event,
  };
  if (!body.transmission_id || !body.transmission_sig) return false;
  const data = await call(env, 'POST', '/v1/notifications/verify-webhook-signature', body, null, fetchImpl);
  return data.verification_status === 'SUCCESS';
}

/** The receipt we keep for a paid order: ids, amount, the input fingerprint. Never the CSV. */
export function receiptFrom(order, source) {
  return { order_id: order.id, capture_id: order.capture_id || null, status: order.status,
    amount: order.amount ? `${order.amount.value} ${order.amount.currency_code}` : null,
    fingerprint: order.custom_id || null, source, at: new Date().toISOString(), sandbox: true };
}

export async function captureOrder(env, orderId, fetchImpl) {
  if (!ORDER_ID_RE.test(orderId || '')) throw new Error('bad order id');
  const data = await call(env, 'POST', `/v2/checkout/orders/${encodeURIComponent(orderId)}/capture`, {}, `cap-${orderId}`, fetchImpl);
  return { id: data.id || orderId, status: data.status, completed: data.status === 'COMPLETED' };
}

/** Verify a captured order unlocks this exact input. Returns {ok, reason}. */
export async function verifyOrder(env, orderId, fingerprint, fetchImpl) {
  if (!ORDER_ID_RE.test(orderId || '')) return { ok: false, reason: 'bad order id' };
  const data = await call(env, 'GET', `/v2/checkout/orders/${encodeURIComponent(orderId)}`, null, null, fetchImpl);
  const pu = (data.purchase_units || [])[0] || {};
  const cap = (((pu.payments || {}).captures) || [])[0] || {};
  if (data.status !== 'COMPLETED') return { ok: false, reason: `order status ${data.status}` };
  if (pu.custom_id !== fingerprint) return { ok: false, reason: 'order was paid for a different file' };
  const amt = cap.amount || pu.amount || {};
  if (amt.currency_code !== 'USD' || amt.value !== price(env)) return { ok: false, reason: 'amount mismatch' };
  return { ok: true, capture_id: cap.id || null };
}

export function _resetTokenCache() { tokenCache = { key: '', token: '', exp: 0 }; }
