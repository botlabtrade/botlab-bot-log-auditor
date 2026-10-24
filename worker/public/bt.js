/* Braintree (a PayPal service) Drop-in checkout, shared by the main page and /pay.html. MIT License (c) 2026 BOTLAB */
(function () {
  'use strict';
  const DROPIN = 'https://js.braintreegateway.com/web/dropin/1.44.1/js/dropin.min.js';
  let loading = null;

  function loadDropin(nonce) {
    if (window.braintree && window.braintree.dropin) return Promise.resolve();
    if (loading) return loading;
    loading = new Promise((ok, bad) => {
      const s = document.createElement('script');
      s.src = DROPIN;
      if (nonce) s.nonce = nonce;
      s.onload = () => ok();
      s.onerror = () => { loading = null; bad(new Error('Braintree Drop-in blocked or offline')); };
      document.head.appendChild(s);
    });
    return loading;
  }

  async function post(path, body) {
    const r = await fetch(path, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
    const j = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(j.error || 'request failed (' + r.status + ')');
    return j;
  }

  /**
   * Render Drop-in (PayPal wallet + card) into `container` and a pay button after it.
   * On success calls onPaid({id, status}) with the Braintree transaction bound to `fingerprint`.
   */
  async function mount({ container, fingerprint, agent, nonce, onPaid, onNote }) {
    const note = onNote || (() => {});
    await loadDropin(nonce);
    const { client_token: token, price, paypal } = await post('/api/bt/token', {});
    const el = typeof container === 'string' ? document.querySelector(container) : container;
    el.innerHTML = '';
    const box = document.createElement('div');
    const btn = document.createElement('button');
    btn.className = 'btn'; btn.type = 'button'; btn.textContent = 'Pay $' + price + ' (sandbox)';
    btn.disabled = true;
    el.appendChild(box); el.appendChild(btn);
    // The PayPal wallet appears only when the Braintree merchant has a linked PayPal sandbox (BRAINTREE_PAYPAL=on).
    const opts = { authorization: token, container: box, card: { cardholderName: false } };
    if (paypal) opts.paypal = { flow: 'checkout', amount: price, currency: 'USD' };
    let inst;
    try {
      inst = await window.braintree.dropin.create(opts);
    } catch (e) {
      if (!opts.paypal) throw e;
      box.innerHTML = '';
      delete opts.paypal;
      inst = await window.braintree.dropin.create(opts);
    }
    btn.disabled = false;
    btn.onclick = async () => {
      btn.disabled = true;
      try {
        const pm = await inst.requestPaymentMethod();
        note('Charging in the Braintree sandbox…');
        const t = await post('/api/bt/checkout', { nonce: pm.nonce, fingerprint, agent: agent || undefined });
        if (!t.completed) throw new Error('transaction status ' + t.status);
        inst.teardown().catch(() => {});
        el.innerHTML = '';
        onPaid(t);
      } catch (e) {
        note('Payment failed: ' + e.message);
        btn.disabled = false;
      }
    };
    return inst;
  }

  window.BotlabBT = { mount };
})();
