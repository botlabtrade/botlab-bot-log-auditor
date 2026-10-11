/* BOTLAB Bot Log Auditor - hosted demo UI. MIT License (c) 2026 BOTLAB */
(function () {
'use strict';
const NONCE = (document.currentScript && document.currentScript.nonce) || ''; // passed to the PayPal SDK (data-csp-nonce)
const $ = s => document.querySelector(s);
const esc = s => String(s == null ? '' : s).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const fmt = (x, d = 2, sign = false) => x == null ? '–' : (sign && x > 0 ? '+' : '') + Number(x).toFixed(d);
let cfg = null, last = null, lastInput = null, chatHistory = [], busy = false, ppLoaded = false;

/* theme */
try { const t = localStorage.getItem('botlab-theme'); if (t) document.documentElement.dataset.theme = t; } catch (e) {}
$('#themeBtn').onclick = () => {
  const cur = document.documentElement.dataset.theme;
  const dark = cur ? cur === 'dark' : matchMedia('(prefers-color-scheme: dark)').matches;
  const t = dark ? 'light' : 'dark';
  document.documentElement.dataset.theme = t;
  try { localStorage.setItem('botlab-theme', t); } catch (e) {}
};

/* config */
fetch('/api/config').then(r => r.json()).then(c => {
  cfg = c;
  $('#llmPill span').textContent = 'AI: Workers AI · ' + c.ai.model.split('/').pop();
  $('#payPill span').textContent = c.payments.label;
  if (c.payments.mode !== 'server') $('#payPill').classList.add('off');
  c.samples.forEach(name => {
    const b = document.createElement('button');
    b.type = 'button';
    b.textContent = name.replace(/^synthetic_/, '').replace(/\.csv$/, '').replace(/_/g, ' ');
    b.onclick = () => fetch('/samples/' + encodeURIComponent(name)).then(r => r.text()).then(t => {
      $('#csv').value = t; $('#risk').value = /bybit|binance/.test(name) ? '20' : ''; $('#runBtn').click();
    });
    $('#samples').appendChild(b);
  });
}).catch(() => { $('#err').textContent = 'Could not load configuration.'; });

/* file input */
const drop = $('#drop'), file = $('#file');
drop.onclick = () => file.click();
drop.onkeydown = e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); file.click(); } };
drop.ondragover = e => { e.preventDefault(); drop.classList.add('over'); };
drop.ondragleave = () => drop.classList.remove('over');
drop.ondrop = e => { e.preventDefault(); drop.classList.remove('over'); if (e.dataTransfer.files[0]) readFile(e.dataTransfer.files[0]); };
file.onchange = () => file.files[0] && readFile(file.files[0]);
function readFile(f) {
  if (f.size > 2 * 1024 * 1024) { $('#err').textContent = 'File too large (max 2 MB).'; return; }
  const r = new FileReader(); r.onload = () => { $('#csv').value = r.result; }; r.readAsText(f);
}

/* streaming audit (NDJSON: one step per line, then the result) */
async function streamAudit(path, payload, onStep) {
  const r = await fetch(path + '?stream=1', { method: 'POST', headers: { 'Content-Type': 'application/json', Accept: 'application/x-ndjson' }, body: JSON.stringify(payload) });
  if (!r.ok) { const j = await r.json().catch(() => ({})); throw new Error(j.error || 'Request failed (' + r.status + ')'); }
  const reader = r.body.getReader(), dec = new TextDecoder();
  let buf = '', result = null;
  for (;;) {
    const { value, done } = await reader.read();
    if (value) buf += dec.decode(value, { stream: true });
    let i;
    while ((i = buf.indexOf('\n')) >= 0) {
      const line = buf.slice(0, i).trim(); buf = buf.slice(i + 1);
      if (!line) continue;
      const msg = JSON.parse(line);
      if (msg.type === 'step') onStep(msg.step);
      else if (msg.type === 'result') result = msg.result;
      else if (msg.type === 'error') throw new Error(msg.error);
    }
    if (done) break;
  }
  if (!result) throw new Error('The audit stopped before finishing.');
  return result;
}

let stepState = [];
function onStep(s) {
  const i = stepState.findIndex(x => x.id === s.id);
  if (i >= 0) stepState[i] = s; else stepState.push(s);
  $('#steps').innerHTML = stepState.map(s => `<li class="${esc(s.status)}"><span class="dot"></span><div><div class="lbl">${esc(s.label)}</div><div class="det">${esc(s.detail || (s.status === 'running' ? 'working…' : ''))}</div></div><span class="ms">${s.ms != null ? s.ms + ' ms' : ''}</span></li>`).join('');
}

$('#runBtn').onclick = async () => {
  if (busy) return;
  $('#err').textContent = '';
  const csv = $('#csv').value;
  if (!csv.trim()) { $('#err').textContent = 'Paste or upload a CSV first, or try a sample.'; return; }
  lastInput = { csv, trials: $('#trials').value, risk_usd: $('#risk').value };
  busy = true; $('#runBtn').disabled = true;
  stepState = []; $('#steps').innerHTML = ''; $('#progressCard').classList.remove('hidden');
  $('#results').classList.add('hidden'); $('#chatlog').innerHTML = ''; chatHistory = [];
  try {
    last = await streamAudit('/api/audit', lastInput, onStep);
    render(last);
  } catch (e) { $('#err').textContent = e.message; }
  busy = false; $('#runBtn').disabled = false;
};

function render(res) {
  const f = res.facts, m = f.metrics, u = m.unit;
  $('#results').classList.remove('hidden');
  $('#verdict').className = 'verdict ' + f.verdict;
  $('#verdict').innerHTML = `<span class="big">${f.passed}/${f.total}</span><span>${esc(f.verdictText)}</span>`;
  const tiles = [['Trades', m.n], ['Avg / trade', fmt(m.mean, 3, true) + ' ' + u], ['Win rate', fmt(m.wr, 0) + '%'],
    ['PSR(0)', fmt(m.psr0)], ['DSR', fmt(m.dsr)], ['Max drawdown', fmt(m.dd, 1) + ' ' + u]];
  $('#tiles').innerHTML = tiles.map(t => `<div class="tile"><div class="k">${esc(t[0])}</div><div class="v">${esc(t[1])}</div></div>`).join('');
  drawCurve(res.curve || []);
  const inp = res.input || {};
  $('#inputNote').textContent = `Detected format: ${inp.format} · result column "${(inp.columns || {}).result}" · ${inp.n_trades} trades` + (inp.notes && inp.notes.length ? ' · ' + inp.notes.join(' ') : '');
  const s = (res.analysis || {}).summary;
  $('#summary').innerHTML = s ? [`${s.months} months`, `<span class="green">${s.green_months} green</span> / <span class="red">${s.red_months} red</span> months`,
    `${s.days} trading days`, `<span class="green">${s.green_days} green</span> / <span class="red">${s.red_days} red</span> days`, `${s.coins} coins`]
    .map(x => `<span class="chip">${x}</span>`).join('') : '';
  $('#gateTable tbody').innerHTML = Object.entries(res.gates).map(([k, g]) => {
    const cls = g.pass === null ? 'na' : g.pass ? 'pass' : 'fail';
    return `<tr><td><strong>${esc(k)}</strong> ${esc(g.title)}</td><td><span class="badge ${cls}">${g.pass === null ? 'N/A' : g.pass ? 'PASS' : 'FAIL'}</span></td><td>${esc(g.detail)}</td></tr>`;
  }).join('');
  const cc = res.crosscheck || {};
  $('#crossNote').textContent = cc.match ? 'Cross-check: identical (' + cc.checks.join(', ') + '). Gate math is the unchanged open-source gates.js.'
    : 'Cross-check found differences: ' + (cc.diffs || []).join('; ');
  $('#reportSrc').textContent = '· ' + (res.report_source === 'workers-ai' ? 'Workers AI / ' + res.model : 'deterministic template (AI unavailable)');
  $('#report').innerHTML = md(res.report || '');
  $('#printMeta').textContent = `${inp.n_trades} trades · format ${inp.format} · generated ${new Date().toISOString().slice(0, 16).replace('T', ' ')} UTC`;
  if (res.pro) renderPro(res); else resetPro();
}

/* Pro */
function resetPro() {
  $('#proBody').classList.add('hidden'); $('#printBtn').classList.add('hidden'); $('#reportCard').classList.remove('hidden');
  $('#payNote').classList.add('hidden'); $('#paypalButtons').innerHTML = '';
  const pay = cfg ? cfg.payments : { mode: 'mock', price: '4.99' };
  if (pay.mode === 'server' || pay.mode === 'sb') {
    $('#demoBtn').classList.add('hidden');
    note(pay.mode === 'server'
      ? `PayPal Sandbox checkout · $${pay.price} (test money only; log in with a sandbox buyer account). Captured on the server with Orders v2.`
      : `Payments demo mode · PayPal sandbox buttons (client id "sb") · $${pay.price} test money, no real payment. Order created and captured in the browser.`);
    loadPayPal(pay).then(() => renderButtons(pay.mode)).catch(e => note('Could not load PayPal: ' + e.message));
  } else {
    $('#demoBtn').classList.remove('hidden');
    note('Payments demo mode: PayPal is switched off on this deployment, so Pro unlocks without payment.');
  }
}
function note(t) { $('#payNote').classList.remove('hidden'); $('#payNote').textContent = t; }

function loadPayPal(pay) {
  if (ppLoaded) return Promise.resolve();
  return new Promise((ok, bad) => {
    const s = document.createElement('script');
    s.src = 'https://www.paypal.com/sdk/js?client-id=' + encodeURIComponent(pay.client_id) + '&currency=USD&intent=capture&components=buttons';
    if (NONCE) { s.nonce = NONCE; s.setAttribute('data-csp-nonce', NONCE); }
    s.onload = () => { ppLoaded = true; ok(); };
    s.onerror = () => bad(new Error('SDK blocked or offline'));
    document.head.appendChild(s);
  });
}

function renderButtons(mode) {
  if (!window.paypal || !last) return;
  $('#paypalButtons').innerHTML = '';
  const amount = cfg.payments.price;
  const server = mode === 'server';
  window.paypal.Buttons({
    style: { layout: 'horizontal', tagline: false, height: 40 },
    createOrder: async (data, actions) => {
      if (!server) {
        // 'sb' mode: no credentials, the SDK creates the sandbox order in the browser.
        return actions.order.create({ intent: 'CAPTURE', purchase_units: [{ reference_id: 'pro-audit', custom_id: last.fingerprint,
          description: 'BOTLAB Pro audit (sandbox demo)', amount: { currency_code: 'USD', value: amount } }],
          application_context: { shipping_preference: 'NO_SHIPPING', brand_name: 'BOTLAB Auditor' } });
      }
      const r = await fetch('/api/paypal/create', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ fingerprint: last.fingerprint }) });
      const j = await r.json(); if (!r.ok) throw new Error(j.error || 'create failed');
      return j.id;
    },
    onApprove: async (data, actions) => {
      if (!server) {
        note('Payment approved. Capturing in the PayPal sandbox…');
        const order = await actions.order.capture();
        if (!order || order.status !== 'COMPLETED') { note('Capture failed: ' + (order && order.status)); return; }
        note('Sandbox payment captured (order ' + order.id + '). Writing the Pro audit…');
        await runPro({ sb_order: order }, order.id);
        return;
      }
      note('Payment approved. Capturing on the server…');
      const r = await fetch('/api/paypal/capture', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ order_id: data.orderID }) });
      const j = await r.json();
      if (!r.ok || !j.completed) { note('Capture failed: ' + (j.error || j.status)); return; }
      note('Payment captured (PayPal Sandbox order ' + data.orderID + '). Writing the Pro audit…');
      await runPro({ order_id: data.orderID }, data.orderID);
    },
    onCancel: () => note('Payment cancelled.'),
    onError: e => note('PayPal error: ' + (e && e.message ? e.message : e)),
  }).render('#paypalButtons');
}

$('#demoBtn').onclick = async () => { $('#demoBtn').disabled = true; note('Demo unlock: writing the Pro audit…'); await runPro({ demo: true }); $('#demoBtn').disabled = false; };

async function runPro(extra, orderId) {
  if (!lastInput) return;
  stepState = []; $('#steps').innerHTML = '';
  try {
    const res = await streamAudit('/api/pro', Object.assign({}, lastInput, extra), onStep);
    last = res; render(res);
    note(extra.demo ? 'Pro audit unlocked (demo mode, no payment taken).' : 'Pro audit unlocked (paid with sandbox test money, PayPal order ' + orderId + ').');
  } catch (e) { note(e.message); }
}

function renderPro(res) {
  $('#demoBtn').classList.add('hidden'); $('#paypalButtons').innerHTML = '';
  $('#reportCard').classList.add('hidden');
  $('#proBody').classList.remove('hidden'); $('#printBtn').classList.remove('hidden');
  $('#proReport').innerHTML = md(res.report || '');
  const a = res.analysis || {}, u = res.unit;
  const months = (a.monthly || []).filter(r => r.month !== 'unknown');
  $('#monthTable tbody').innerHTML = months.length ? months.map(r => `<tr><td>${esc(r.month)}</td><td class="num">${r.n}</td><td class="num ${r.sum >= 0 ? 'green' : 'red'}">${fmt(r.sum, 2, true)} ${esc(u)}</td><td class="num green">${r.win_days}</td><td class="num red">${r.loss_days}</td></tr>`).join('') : '<tr><td colspan="5">No date column.</td></tr>';
  $('#coinTable tbody').innerHTML = (a.by_symbol || []).map(r => `<tr><td>${esc(r.sym)}</td><td class="num">${r.n}</td><td class="num">${r.wr}%</td><td class="num ${r.sum >= 0 ? 'green' : 'red'}">${fmt(r.sum, 2, true)} ${esc(u)}</td></tr>`).join('');
  const bs = a.by_side || {};
  $('#sideNote').textContent = `Longs: ${bs.L ? bs.L.n : 0} trades, ${fmt(bs.L ? bs.L.sum : 0, 2, true)} ${u} · Shorts: ${bs.S ? bs.S.n : 0} trades, ${fmt(bs.S ? bs.S.sum : 0, 2, true)} ${u} · Longest losing streak: ${a.longest_losing_streak} trades.`;
}
$('#printBtn').onclick = () => window.print();

/* chat (stateless: the report and facts travel with every question) */
$('#chatForm').onsubmit = async e => {
  e.preventDefault();
  const q = $('#chatIn').value.trim(); if (!q || !last) return;
  addMsg('user', q); $('#chatIn').value = '';
  try {
    const r = await fetch('/api/chat', { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ question: q, report: last.report, facts: last.facts, history: chatHistory.slice(-6) }) });
    const j = await r.json();
    const a = r.ok ? j.answer : (j.error || 'error');
    addMsg('assistant', a);
    chatHistory.push({ role: 'user', content: q }, { role: 'assistant', content: a });
  } catch (err) { addMsg('assistant', err.message); }
};
function addMsg(role, text) {
  const d = document.createElement('div'); d.className = 'msg ' + role;
  d.innerHTML = role === 'user' ? esc(text) : md(text);
  $('#chatlog').appendChild(d); $('#chatlog').scrollTop = 1e9;
}

/* equity curve */
function drawCurve(c) {
  const svg = $('#curve');
  if (c.length < 2) { svg.innerHTML = ''; return; }
  const W = 600, H = 120, lo = Math.min(0, ...c), hi = Math.max(0, ...c), span = hi - lo || 1;
  const X = i => (i / (c.length - 1)) * W, Y = v => H - 6 - ((v - lo) / span) * (H - 12);
  const pts = c.map((v, i) => X(i).toFixed(1) + ',' + Y(v).toFixed(1)).join(' ');
  const col = c[c.length - 1] >= 0 ? 'var(--pass)' : 'var(--fail)';
  svg.innerHTML = `<line x1="0" x2="${W}" y1="${Y(0)}" y2="${Y(0)}" stroke="var(--border)" stroke-dasharray="4 4"/><polyline points="${pts}" fill="none" stroke="${col}" stroke-width="2" vector-effect="non-scaling-stroke"/>`;
}

/* tiny, safe markdown (escape first) */
function md(src) {
  const lines = esc(src).split('\n'); let out = '', list = null, tbl = [];
  const inline = s => s.replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>').replace(/(^|\s)_(.+?)_(?=\s|$)/g, '$1<em>$2</em>').replace(/`([^`]+)`/g, '<code>$1</code>');
  const closeList = () => { if (list) { out += `</${list}>`; list = null; } };
  const flushTbl = () => {
    if (!tbl.length) return;
    const rows = tbl.filter(r => !/^\|?\s*:?-{3,}/.test(r)).map(r => r.replace(/^\||\|$/g, '').split('|').map(c => inline(c.trim())));
    out += '<div class="tbl"><table><thead><tr>' + rows[0].map(c => `<th>${c}</th>`).join('') + '</tr></thead><tbody>' +
      rows.slice(1).map(r => '<tr>' + r.map(c => `<td>${c}</td>`).join('') + '</tr>').join('') + '</tbody></table></div>';
    tbl = [];
  };
  for (const l of lines) {
    if (/^\s*\|/.test(l)) { closeList(); tbl.push(l.trim()); continue; } else flushTbl();
    let m;
    if ((m = l.match(/^(#{1,4})\s+(.*)/))) { closeList(); out += `<h3>${inline(m[2])}</h3>`; }
    else if ((m = l.match(/^\s*[-*]\s+(.*)/))) { if (list !== 'ul') { closeList(); out += '<ul>'; list = 'ul'; } out += `<li>${inline(m[1])}</li>`; }
    else if ((m = l.match(/^\s*\d+\.\s+(.*)/))) { if (list !== 'ol') { closeList(); out += '<ol>'; list = 'ol'; } out += `<li>${inline(m[1])}</li>`; }
    else if (!l.trim()) { closeList(); }
    else { closeList(); out += `<p>${inline(l)}</p>`; }
  }
  flushTbl(); closeList();
  return out;
}
})();
