/* Return page of the agent checkout (PayPal return_url / cancel_url). MIT License (c) 2026 BOTLAB */
(function () {
  'use strict';
  const q = new URLSearchParams(location.search);
  if (q.get('cancelled')) {
    document.getElementById('title').textContent = 'Payment cancelled';
    document.getElementById('msg').textContent = 'Nothing was charged. Your agent will see the order as not approved.';
  } else if (q.get('mock')) {
    document.getElementById('msg').textContent = 'Demo deployment without PayPal keys: this simulated order already counts as approved. Your agent can collect the audit.';
  }
})();
