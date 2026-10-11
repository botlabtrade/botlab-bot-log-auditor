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
 *   'server' - PAYPAL_CLIENT_ID + PAYPAL_CLIENT_SECRET secrets exist: Orders v2 create/capture/verify on the server.
 *   'sb'     - no secrets, PAYPAL_MODE=sb (default): PayPal JS SDK with the generic sandbox client id `sb`;
 *              the order is created and captured in the browser, the Worker checks the returned order shape.
 *              No PayPal account needed, but the server cannot confirm the payment with PayPal (demo-grade).
 *   'mock'   - no secrets, PAYPAL_MODE=mock: a "mock unlock" button, no PayPal at all.
 */
export function paymentMode(env) {
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

export async function createOrder(env, fingerprint, fetchImpl) {
  if (!/^[a-f0-9]{64}$/.test(fingerprint || '')) throw new Error('bad fingerprint');
  const body = {
    intent: 'CAPTURE',
    purchase_units: [{
      reference_id: 'pro-audit', custom_id: fingerprint.slice(0, 64), description: 'BOTLAB Pro audit (sandbox demo)',
      amount: { currency_code: 'USD', value: price(env) },
    }],
    payment_source: { paypal: { experience_context: { brand_name: 'BOTLAB Auditor', user_action: 'PAY_NOW', shipping_preference: 'NO_SHIPPING' } } },
  };
  const data = await call(env, 'POST', '/v2/checkout/orders', body, crypto.randomUUID(), fetchImpl);
  return { id: data.id, status: data.status };
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
