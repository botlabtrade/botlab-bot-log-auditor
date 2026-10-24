/**
 * Braintree (a PayPal service) Sandbox via the Braintree GraphQL API.
 * Only https://payments.sandbox.braintree-api.com is ever called. There is deliberately no live switch.
 *
 * Binding a payment to the audited input, statelessly: the transaction's orderId is the SHA-256
 * fingerprint of the input. /api/pro re-reads the transaction from Braintree and only unlocks if it is
 * paid, the amount matches and orderId matches the fingerprint of the CSV being submitted.
 *
 * MIT License (c) 2026 BOTLAB
 */
import { price } from './paypal.js';

export const BT_GRAPHQL = 'https://payments.sandbox.braintree-api.com/graphql';
export const BT_VERSION = '2019-01-01';
export const PAID = new Set(['AUTHORIZED', 'SUBMITTED_FOR_SETTLEMENT', 'SETTLING', 'SETTLEMENT_PENDING', 'SETTLED']);
const FP_RE = /^[a-f0-9]{64}$/;
const TXN_RE = /^[A-Za-z0-9_=-]{4,80}$/;

export function braintreeConfigured(env) {
  return Boolean(env && env.BRAINTREE_MERCHANT_ID && env.BRAINTREE_PUBLIC_KEY && env.BRAINTREE_PRIVATE_KEY);
}

async function gql(env, query, variables, fetchImpl) {
  const auth = btoa(`${env.BRAINTREE_PUBLIC_KEY}:${env.BRAINTREE_PRIVATE_KEY}`);
  const res = await (fetchImpl || fetch)(BT_GRAPHQL, {
    method: 'POST',
    headers: { Authorization: `Basic ${auth}`, 'Braintree-Version': BT_VERSION, 'Content-Type': 'application/json' },
    body: JSON.stringify({ query, variables }),
  });
  const data = await res.json().catch(() => ({}));
  if (res.status >= 400) throw new Error(`Braintree HTTP ${res.status}`);
  if (data.errors && data.errors.length) throw new Error('Braintree: ' + String(data.errors[0].message || 'error').slice(0, 160));
  return data.data || {};
}

/** Client token for Drop-in UI in the browser (safe to send: it is short-lived and has no secret). */
export async function clientToken(env, fetchImpl) {
  const d = await gql(env, 'mutation ($input: CreateClientTokenInput) { createClientToken(input: $input) { clientToken } }', { input: {} }, fetchImpl);
  return (d.createClientToken || {}).clientToken;
}

const TXN_FIELDS = 'id status orderId amount { value currencyCode } paymentMethodSnapshot { __typename } createdAt';
const txnOut = t => t && ({ id: t.id, status: t.status, order_id: t.orderId || null,
  amount: t.amount ? { value: String(t.amount.value), currency_code: t.amount.currencyCode } : null,
  method: t.paymentMethodSnapshot ? t.paymentMethodSnapshot.__typename : null, created_at: t.createdAt || null });

/** Charge a Drop-in nonce for the Pro audit of one input. */
export async function charge(env, nonce, fingerprint, fetchImpl) {
  if (!FP_RE.test(fingerprint || '')) throw new Error('bad fingerprint');
  if (typeof nonce !== 'string' || !/^[A-Za-z0-9_-]{8,200}$/.test(nonce)) throw new Error('bad payment nonce');
  const d = await gql(env, `mutation ($input: ChargePaymentMethodInput!) { chargePaymentMethod(input: $input) { transaction { ${TXN_FIELDS} } } }`,
    { input: { paymentMethodId: nonce, transaction: { amount: price(env), orderId: fingerprint } } }, fetchImpl);
  const t = txnOut(((d.chargePaymentMethod || {}).transaction));
  if (!t) throw new Error('Braintree: no transaction');
  return { ...t, completed: PAID.has(t.status) };
}

export async function getTransaction(env, id, fetchImpl) {
  if (!TXN_RE.test(id || '')) throw new Error('bad transaction id');
  const d = await gql(env, `query ($id: ID!) { node(id: $id) { ... on Transaction { ${TXN_FIELDS} } } }`, { id }, fetchImpl);
  return txnOut(d.node);
}

/** Paid transaction bound to this fingerprint, or null (used by the agent flow: no id needed). */
export async function findPaid(env, fingerprint, fetchImpl) {
  if (!FP_RE.test(fingerprint || '')) throw new Error('bad fingerprint');
  const d = await gql(env, `query ($o: String!) { search { transactions(input: { orderId: { is: $o } }) { edges { node { ${TXN_FIELDS} } } } } }`,
    { o: fingerprint }, fetchImpl);
  const edges = (((d.search || {}).transactions || {}).edges) || [];
  return edges.map(e => txnOut(e.node)).find(t => PAID.has(t.status)) || null;
}

/** Same contract as paypal.verifyOrder: {ok, reason, capture_id}. */
export function checkTxn(t, fingerprint, env) {
  if (!t) return { ok: false, reason: 'transaction not found' };
  if (!PAID.has(t.status)) return { ok: false, reason: `transaction status ${t.status}` };
  if (t.order_id !== fingerprint) return { ok: false, reason: 'transaction was paid for a different file' };
  if (!t.amount || t.amount.currency_code !== 'USD' || t.amount.value !== price(env)) return { ok: false, reason: 'amount mismatch' };
  return { ok: true, capture_id: t.id };
}

export function receiptFromTxn(t, source) {
  return { order_id: t.id, capture_id: t.id, status: t.status, amount: t.amount ? `${t.amount.value} ${t.amount.currency_code}` : null,
    fingerprint: t.order_id || null, method: t.method, provider: 'braintree', source, at: new Date().toISOString(), sandbox: true };
}

/* ---------------------------------------------------------------- webhooks */
const hex = buf => [...new Uint8Array(buf)].map(b => b.toString(16).padStart(2, '0')).join('');

/** HMAC-SHA1(key = SHA1(private key), payload) as Braintree signs webhooks. */
export async function signPayload(privateKey, payload) {
  const keyBytes = await crypto.subtle.digest('SHA-1', new TextEncoder().encode(privateKey));
  const key = await crypto.subtle.importKey('raw', keyBytes, { name: 'HMAC', hash: 'SHA-1' }, false, ['sign']);
  return hex(await crypto.subtle.sign('HMAC', key, new TextEncoder().encode(payload)));
}

/**
 * Verify bt_signature ("public_key|hex&public_key|hex...") over bt_payload (base64 XML).
 * Returns the parsed notification {kind, transaction_id, order_id, status, amount} or null if not verified.
 */
export async function parseWebhook(env, btSignature, btPayload) {
  if (!braintreeConfigured(env) || typeof btSignature !== 'string' || typeof btPayload !== 'string') return null;
  const expected = await signPayload(env.BRAINTREE_PRIVATE_KEY, btPayload);
  const ok = btSignature.split('&').some(pair => {
    const [pub, sig] = pair.split('|');
    return pub === env.BRAINTREE_PUBLIC_KEY && sig && sig.length === expected.length && timingSafeEq(sig, expected);
  });
  if (!ok) return null;
  let xml;
  try { xml = atob(btPayload.replace(/\s+/g, '')); } catch { return null; }
  const tag = (name, src = xml) => { const m = src.match(new RegExp(`<${name}[^>]*>([^<]*)</${name}>`)); return m ? m[1] : null; };
  const txn = (xml.match(/<transaction>[\s\S]*?<\/transaction>/) || [''])[0];
  return { kind: tag('kind'), transaction_id: txn ? tag('id', txn) : null, order_id: txn ? tag('order-id', txn) : null,
    status: txn ? tag('status', txn) : null, amount: txn ? tag('amount', txn) : null };
}

function timingSafeEq(a, b) {
  let d = 0;
  for (let i = 0; i < a.length; i++) d |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return d === 0;
}
