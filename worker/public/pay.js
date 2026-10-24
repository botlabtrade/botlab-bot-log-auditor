/* Human approval page for an agent purchase (Braintree mode). MIT License (c) 2026 BOTLAB */
(function () {
  'use strict';
  const NONCE = (document.currentScript && document.currentScript.nonce) || '';
  const q = new URLSearchParams(location.search);
  const fp = q.get('fp') || '';
  const agent = (q.get('agent') || '').replace(/[^A-Za-z0-9 ._-]/g, '').slice(0, 40);
  const note = t => { const n = document.getElementById('note'); n.classList.remove('hidden'); n.textContent = t; };
  document.getElementById('fp').textContent = fp.slice(0, 16) + '…';
  if (agent) document.getElementById('who').textContent = `Your AI agent "${agent}" asked to buy a BOTLAB Pro audit of its trade log.`;
  if (!/^[a-f0-9]{64}$/.test(fp)) { note('This link is not valid (missing file fingerprint).'); return; }
  window.BotlabBT.mount({
    container: '#dropin', fingerprint: fp, agent: agent || 'agent', nonce: NONCE, onNote: note,
    onPaid: t => note(`Approved and paid (sandbox transaction ${t.id}). Your agent can now collect the audit. You can close this tab.`),
  }).catch(e => note('Could not load the checkout: ' + e.message));
})();
