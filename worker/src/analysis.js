/**
 * Deterministic analysis for the hosted demo.
 *
 * The Python app (repo root) lets the LLM write analysis code and runs it in a process sandbox.
 * A public Worker must never eval model-written code, so here the same analysis (the built-in
 * script from llm.py MOCK_ANALYSIS_CODE) is implemented as pure functions.
 *
 * MIT License (c) 2026 BOTLAB
 */

const r4 = x => Math.round(x * 1e4) / 1e4;

/** Month table (trades, result, green/red days), per side, per coin, longest losing streak. */
export function analyse(rows) {
  const months = new Map();
  const days = new Map();
  for (const t of rows) {
    const m = (t.day || '').slice(0, 7) || 'unknown';
    if (!months.has(m)) months.set(m, { month: m, n: 0, sum: 0 });
    const o = months.get(m);
    o.n += 1; o.sum += t.v;
    if (t.day) days.set(t.day, (days.get(t.day) || 0) + t.v);
  }
  const dayCount = new Map(); // month -> [green days, red days]
  for (const [d, s] of days) {
    const m = d.slice(0, 7), c = dayCount.get(m) || [0, 0];
    if (s > 0) c[0]++; else if (s < 0) c[1]++;
    dayCount.set(m, c);
  }
  const monthly = [...months.values()].map(o => {
    const c = dayCount.get(o.month) || [0, 0];
    return { month: o.month, n: o.n, sum: r4(o.sum), win_days: c[0], loss_days: c[1] };
  });

  const bySide = {};
  for (const k of ['L', 'S']) {
    const xs = rows.filter(t => t.side === k).map(t => t.v);
    bySide[k] = { n: xs.length, sum: r4(xs.reduce((a, b) => a + b, 0)) };
  }

  const sym = new Map();
  for (const t of rows) {
    const k = t.sym || '?';
    const a = sym.get(k) || { n: 0, sum: 0, wins: 0 };
    a.n++; a.sum += t.v; if (t.v > 0) a.wins++;
    sym.set(k, a);
  }
  const bySymbol = [...sym.entries()]
    .map(([s, a]) => ({ sym: s, n: a.n, sum: r4(a.sum), wr: Math.round(a.wins / a.n * 100) }))
    .sort((a, b) => b.sum - a.sum);

  let streak = 0, best = 0;
  for (const t of rows) { streak = t.v < 0 ? streak + 1 : 0; best = Math.max(best, streak); }

  const greenMonths = monthly.filter(m => m.sum > 0).length;
  const greenDays = [...days.values()].filter(s => s > 0).length;
  const redDays = [...days.values()].filter(s => s < 0).length;
  return {
    monthly, by_side: bySide, by_symbol: bySymbol, longest_losing_streak: best,
    summary: { months: monthly.length, green_months: greenMonths, red_months: monthly.filter(m => m.sum < 0).length,
      days: days.size, green_days: greenDays, red_days: redDays, coins: bySymbol.length },
  };
}

/**
 * Cross-check: recompute headline numbers independently of gates.js and compare.
 * For generic CSVs also re-parse the raw text with gates.js's own parser and compare trade lists.
 */
export function crossCheck(parsed, report, rawParsed) {
  const x = parsed.rows.map(r => r.v), n = x.length;
  const mean = x.reduce((a, b) => a + b, 0) / n;
  const wr = x.filter(v => v > 0).length / n * 100;
  let eq = 0, pk = 0, dd = 0;
  for (const v of x) { eq += v; pk = Math.max(pk, eq); dd = Math.max(dd, pk - eq); }
  const diffs = [];
  const close = (a, b) => Math.abs(a - b) <= 1e-9 * Math.max(1, Math.abs(a), Math.abs(b));
  if (n !== report.n) diffs.push(`n: ${n} vs ${report.n}`);
  if (!close(mean, report.mean)) diffs.push(`mean: ${mean} vs ${report.mean}`);
  if (!close(wr, report.wr)) diffs.push(`win rate: ${wr} vs ${report.wr}`);
  if (!close(dd, report.dd)) diffs.push(`drawdown: ${dd} vs ${report.dd}`);
  const checks = ['trade count', 'mean', 'win rate', 'max drawdown'];
  if (rawParsed) {
    checks.push('gates.js parser');
    if (rawParsed.rows.length !== n) diffs.push(`gates.js parser read ${rawParsed.rows.length} trades, normaliser ${n}`);
    else if (rawParsed.rows.some((r, i) => !close(r.v, x[i]))) diffs.push('gates.js parser and normaliser disagree on trade values');
  }
  return { match: diffs.length === 0, checks, diffs };
}
