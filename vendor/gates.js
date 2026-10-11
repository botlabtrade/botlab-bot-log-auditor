/*!
 * BOTLAB "Check your bot" - gate math.
 * Pure functions: CSV parsing + six gates (G1..G6). No network, no DOM.
 * Works in the browser (window.BotlabGates) and in Node (require / createRequire).
 * MIT License (c) 2026 BOTLAB
 */
(function (root, factory) {
  if (typeof module === 'object' && module.exports) module.exports = factory();
  else root.BotlabGates = factory();
})(typeof self !== 'undefined' ? self : this, function () {
  'use strict';

  /** Thresholds. Same defaults as the online tool. */
  const DEFAULTS = Object.freeze({
    G1_MIN_N: 50,          // G1: minimum trade count (floor under MinTRL)
    G1_CONF: 0.95,          // G1: MinTRL confidence level
    G2_PSR_MIN: 0.95,       // G2: Probabilistic Sharpe vs 0
    G2_DSR_MIN: 0.5,        // G2: Deflated Sharpe after N trials
    G3_LAST_LONG: 100,      // G3: window for "still net positive"
    G3_LAST_SHORT: 30,      // G3: short window
    G3_SHORT_MIN: -0.1,     // G3: short-window average must be above this (per trade)
    G4_SYM_TOP: 3,          // G4: top-K coins
    G4_SYM_SHARE_MAX: 0.5,  // G4: top-K coins share of gross profit must be below this
    G4_DAY_TOP: 2,          // G4: top-K days
    G4_DAY_SHARE_MAX: 0.5,  // G4: top-K days share of net profit must be below this
    G5_LEAK_MIN_N: 50,      // G5: a side with at least this many trades...
    G6_MAX_DD_R: 15,        // G6: max drawdown (in R) must be below this
  });

  const GATE_TITLES = Object.freeze({
    G1: 'Sufficient sample',
    G2: 'Not just luck',
    G3: 'Still working recently',
    G4: 'Not reliant on a few coins or days',
    G5: 'No losing side',
    G6: 'Acceptable drawdown',
  });

  const R_COLS = ['r', 'r_after', 'pnl_r', 'r_mult', 'rmult', 'r_multiple', 'realized_r'];
  const USD_COLS = ['pnl', 'pnl_usd', 'profit', 'pnl$', 'net_pnl', 'realized_pnl', 'return', 'ret'];
  const SYM_COLS = ['symbol', 'sym', 'coin', 'pair', 'ticker', 'market'];
  const DATE_COLS = ['date', 'exit_time', 'closed_at', 'close_time', 'exit_ts', 'time', 'ts', 'timestamp', 'entry_time', 'opened_at'];
  const SIDE_COLS = ['side', 'direction', 'dir', 'position_side', 'pos_side'];

  /* ---------- normal distribution helpers ---------- */
  /** Standard normal CDF (Abramowitz & Stegun 26.2.17). */
  function ncdf(x) {
    const t = 1 / (1 + 0.2316419 * Math.abs(x));
    const d = 0.3989423 * Math.exp(-x * x / 2);
    const p = d * t * (0.3193815 + t * (-0.3565638 + t * (1.781478 + t * (-1.821256 + t * 1.330274))));
    return x > 0 ? 1 - p : p;
  }
  /** Inverse standard normal CDF (Acklam's rational approximation). */
  function ninv(p) {
    const a = [-39.6968302866538, 220.946098424521, -275.928510446969, 138.357751867269, -30.6647980661472, 2.50662827745924],
      b = [-54.4760987982241, 161.585836858041, -155.698979859887, 66.8013118877197, -13.2806815528857],
      c = [-0.00778489400243029, -0.322396458041136, -2.40075827716184, -2.54973253934373, 4.37466414146497, 2.93816398269878],
      d = [0.00778469570904146, 0.32246712907004, 2.445134137143, 3.75440866190742], pl = 0.02425;
    if (p < pl) {
      const q = Math.sqrt(-2 * Math.log(p));
      return (((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / ((((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1);
    }
    if (p > 1 - pl) return -ninv(1 - p);
    const q = p - 0.5, r = q * q;
    return (((((a[0] * r + a[1]) * r + a[2]) * r + a[3]) * r + a[4]) * r + a[5]) * q / (((((b[0] * r + b[1]) * r + b[2]) * r + b[3]) * r + b[4]) * r + 1);
  }

  /* ---------- CSV ---------- */
  function normSide(s) {
    const v = String(s || '').trim().toLowerCase();
    if (['l', 'long', 'buy', 'b', 'bid'].includes(v)) return 'L';
    if (['s', 'short', 'sell', 'ask'].includes(v)) return 'S';
    return '';
  }

  /**
   * Parse a trade CSV (comma, semicolon or tab separated, first row = header).
   * Returns {rows:[{v, sym, day, side}], unit:'R'|'$', hasSym, hasDay, hasSide}.
   * Throws Error on unusable input.
   */
  function parseCSV(txt) {
    const lines = String(txt || '').trim().split(/\r?\n/).filter(l => l.trim());
    if (lines.length < 2) throw new Error('Need at least one header row and one trade row.');
    const h0 = lines[0];
    const sep = (h0.match(/;/g) || []).length > (h0.match(/,/g) || []).length ? ';' : h0.includes('\t') ? '\t' : ',';
    const head = h0.split(sep).map(h => h.trim().replace(/^"|"$/g, '').toLowerCase());
    const pick = names => head.findIndex(h => names.includes(h));
    let ri = pick(R_COLS), unit = 'R';
    if (ri < 0) { ri = pick(USD_COLS); unit = '$'; }
    if (ri < 0) throw new Error('No result column found (r / pnl_r / pnl / pnl_usd).');
    const si = pick(SYM_COLS), di = pick(DATE_COLS), sdi = pick(SIDE_COLS);
    const rows = [];
    for (const l of lines.slice(1)) {
      const c = l.split(sep).map(x => x.trim().replace(/^"|"$/g, ''));
      const v = parseFloat(String(c[ri]).replace(',', '.'));
      if (!isFinite(v)) continue;
      let day = '';
      if (di >= 0 && c[di]) {
        const raw = c[di], num = Number(raw);
        const dt = isFinite(num) && raw !== '' ? new Date(num > 1e12 ? num : num * 1000) : new Date(raw.replace(' ', 'T'));
        day = isNaN(dt) ? String(raw).slice(0, 10) : dt.toISOString().slice(0, 10);
      }
      rows.push({ v, sym: si >= 0 ? c[si] : '', day, side: sdi >= 0 ? normSide(c[sdi]) : '' });
    }
    if (!rows.length) throw new Error('Could not read any numeric rows.');
    return { rows, unit, hasSym: si >= 0, hasDay: di >= 0, hasSide: sdi >= 0 };
  }

  /* ---------- statistics ---------- */
  const sum = a => a.reduce((x, y) => x + y, 0);

  /** Mean, sample SD, skew, (non-excess) kurtosis and per-trade Sharpe. */
  function moments(x) {
    const n = x.length, mean = sum(x) / n;
    const sd = Math.sqrt(x.reduce((a, b) => a + (b - mean) ** 2, 0) / Math.max(1, n - 1)) || 1e-9;
    const sk = x.reduce((a, b) => a + ((b - mean) / sd) ** 3, 0) / n;
    const ku = x.reduce((a, b) => a + ((b - mean) / sd) ** 4, 0) / n;
    return { n, mean, sd, sk, ku, sr: mean / sd };
  }

  /** Probabilistic Sharpe Ratio: P(true SR > sr0) (Bailey & Lopez de Prado 2012). */
  function psr(m, sr0) {
    const den = Math.sqrt(Math.max(1e-9, 1 - m.sk * m.sr + (m.ku - 1) / 4 * m.sr * m.sr));
    return ncdf((m.sr - sr0) * Math.sqrt(m.n - 1) / den);
  }

  /** Expected max Sharpe of N unskilled trials (benchmark SR0 for the Deflated Sharpe). */
  function sr0ForTrials(n, trials) {
    const N = Math.max(1, trials | 0), g = 0.5772156649;
    return N > 1 ? Math.sqrt(1 / (n - 1)) * ((1 - g) * ninv(1 - 1 / N) + g * ninv(1 - 1 / (N * Math.E))) : 0;
  }

  /** Minimum Track Record Length at confidence `conf` (Infinity if SR <= 0). */
  function minTRL(m, conf) {
    const z = ninv(conf == null ? 0.95 : conf);
    return m.sr > 0 ? Math.ceil(1 + (1 - m.sk * m.sr + (m.ku - 1) / 4 * m.sr * m.sr) * (z / m.sr) ** 2) : Infinity;
  }

  function equity(x) {
    let eq = 0, pk = 0, dd = 0;
    const curve = x.map(v => { eq += v; pk = Math.max(pk, eq); dd = Math.max(dd, pk - eq); return eq; });
    return { curve, maxDD: dd };
  }

  /** Sum by key, top-K by sum, share = sum(max(0, top)) / base. */
  function groupShare(rows, key, k, base) {
    const m = {};
    rows.forEach(r => { m[r[key]] = (m[r[key]] || 0) + r.v; });
    const top = Object.entries(m).sort((a, b) => b[1] - a[1]).slice(0, k);
    return { share: top.reduce((a, b) => a + Math.max(0, b[1]), 0) / base, top };
  }

  const fR = (x, d) => (x > 0 ? '+' : '') + Number(x).toFixed(d == null ? 2 : d);

  /* ---------- the six gates ---------- */
  /**
   * Score a parsed trade list.
   * @param {object} P   output of parseCSV (or {rows:[{v,sym,day,side}], unit, hasSym, hasDay, hasSide})
   * @param {number} trials number of variants tested before picking this one (for DSR)
   * @param {object} [opts] threshold overrides (see DEFAULTS)
   * Each gate is [pass, detail] where pass is true, false, or null (could not be scored).
   */
  function grade(P, trials, opts) {
    const K = Object.assign({}, DEFAULTS, opts || {});
    const x = P.rows.map(r => r.v), m = moments(x), n = m.n;
    const N = Math.max(1, trials | 0);
    const psr0 = psr(m, 0), dsr = psr(m, sr0ForTrials(n, N));
    const trl = minTRL(m, K.G1_CONF);
    const wr = x.filter(v => v > 0).length / n * 100;
    const { curve, maxDD } = equity(x);
    const avgLast = k => { const s = x.slice(-k); return sum(s) / s.length; };
    const posSum = sum(x.filter(v => v > 0)) || 1e-9, tot = sum(x);

    const sym = P.hasSym ? groupShare(P.rows, 'sym', K.G4_SYM_TOP, posSum) : null;
    const day = P.hasDay ? groupShare(P.rows, 'day', K.G4_DAY_TOP, Math.abs(tot) || 1e-9) : null;
    const symShare = sym ? sym.share : null, dayShare = day ? day.share : null;
    const topSyms = sym ? sym.top : [];

    const need = Math.max(K.G1_MIN_N, trl);
    const last100 = avgLast(K.G3_LAST_LONG), last30 = avgLast(K.G3_LAST_SHORT);

    const G = {
      G1: [n >= need, `n=${n} / need ${isFinite(need) ? need : '∞'} (MinTRL ${isFinite(trl) ? trl : '∞'})`],
      G2: [psr0 >= K.G2_PSR_MIN && dsr >= K.G2_DSR_MIN, `PSR(0)=${psr0.toFixed(2)} · DSR=${dsr.toFixed(2)} with ${N} trials`],
      G3: [last100 > 0 && last30 > K.G3_SHORT_MIN, `last ${K.G3_LAST_LONG} ${fR(last100, 3)} · last ${K.G3_LAST_SHORT} ${fR(last30, 3)}`],
      G4: [(symShare == null || symShare < K.G4_SYM_SHARE_MAX) && (dayShare == null || dayShare < K.G4_DAY_SHARE_MAX),
        [symShare != null ? `top ${K.G4_SYM_TOP} coins ${Math.round(symShare * 100)}%` : 'no coin column',
         dayShare != null ? `best ${K.G4_DAY_TOP} days ${Math.round(dayShare * 100)}%` : 'no date column'].join(' · ')],
      G5: (() => {
        if (!P.hasSide) return [null, 'no side column (add side = long/short)'];
        const side = k => P.rows.filter(r => r.side === k);
        const mean = a => a.reduce((s, r) => s + r.v, 0) / (a.length || 1);
        const leak = ['L', 'S'].filter(k => side(k).length >= K.G5_LEAK_MIN_N && mean(side(k)) < 0);
        return [!leak.length, ['L', 'S'].map(k => `${k === 'L' ? 'long' : 'short'} ${side(k).length} · ${fR(mean(side(k)), 2)}`).join(' · ')];
      })(),
      G6: [P.unit !== 'R' ? null : maxDD < K.G6_MAX_DD_R, P.unit === 'R' ? `max DD ${maxDD.toFixed(1)}R (limit ${K.G6_MAX_DD_R}R)` : 'scored only when the unit is R'],
    };

    const flags = [
      wr >= 80 && 'A win rate of 80% or more is a classic red flag for look-ahead bias or overfitting.',
      n < 30 && 'Fewer than 30 trades says almost nothing yet.',
      symShare != null && symShare >= K.G4_SYM_SHARE_MAX && `Profit concentrated in ${topSyms.map(t => t[0]).join(', ')}.`,
      psr0 >= K.G2_PSR_MIN && dsr < K.G2_DSR_MIN && 'Looks good in isolation but fails once the number of trials is accounted for: likely overfit.',
    ].filter(Boolean);

    const scored = Object.values(G).filter(g => g[0] !== null);
    const passed = scored.filter(g => g[0] === true).length, total = scored.length;
    const verdict = passed === total ? 'ready' : (G.G2[0] && G.G3[0]) ? 'watch' : 'no';
    const verdictText = {
      ready: 'Passes every gate that could be scored: worth a small live trial alongside paper trading.',
      watch: 'There is a statistical signal, but it does not qualify yet. Keep collecting trades.',
      no: `Not passed (${passed} of ${total} gates). Do not commit real money yet.`,
    }[verdict];

    return { n, mean: m.mean, sd: m.sd, skew: m.sk, kurt: m.ku, sr: m.sr, wr, psr0, dsr, minTRL: trl, need,
      dd: maxDD, curve, G, topSyms, unit: P.unit, trials: N, passed, total, verdict, verdictText, flags };
  }

  /** Convenience: CSV text -> report. */
  function check(csvText, trials, opts) { return grade(parseCSV(csvText), trials == null ? 20 : trials, opts); }

  /** Plain-text report (used by the CLI). */
  function formatReport(r) {
    const u = r.unit, out = [];
    out.push('BOTLAB "Check your bot" - gate report');
    out.push('');
    out.push(`Trades        ${r.n}`);
    out.push(`Avg per trade ${fR(r.mean, 3)}${u === 'R' ? 'R' : ' (' + u + ')'}`);
    out.push(`Win rate      ${r.wr.toFixed(0)}%`);
    out.push(`PSR(0)        ${r.psr0.toFixed(2)}`);
    out.push(`DSR           ${r.dsr.toFixed(2)}  (${r.trials} trials)`);
    out.push(`Max drawdown  ${r.dd.toFixed(1)} ${u}`);
    out.push('');
    for (const [g, [ok, why]] of Object.entries(r.G)) {
      const mark = ok === null ? ' -- ' : ok ? 'PASS' : 'FAIL';
      out.push(`[${mark}] ${g} ${GATE_TITLES[g]}: ${why}`);
    }
    out.push('');
    out.push(`Verdict: ${r.verdictText}`);
    if (r.flags.length) { out.push(''); out.push('Red flags:'); r.flags.forEach(f => out.push(' - ' + f)); }
    out.push('');
    out.push('Educational tool. Not financial advice. Past results don\'t predict future results.');
    return out.join('\n');
  }

  return { DEFAULTS, GATE_TITLES, ncdf, ninv, parseCSV, moments, psr, sr0ForTrials, minTRL, equity, grade, check, formatReport };
});
