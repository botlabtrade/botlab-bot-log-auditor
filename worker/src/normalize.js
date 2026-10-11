/**
 * Exchange trade-history CSV -> normalised trade list.
 * JS port of ../../csv_normalize.py (same heuristics, same output shape).
 *
 *   generic - botlab-check-your-bot columns (r / pnl / symbol / date / side ...)
 *   bybit   - Bybit "Closed P&L" export
 *   binance - Binance USD-M futures "Trade History" export (net = realized profit - fee on closing fills)
 *
 * Output: {rows:[{v,sym,day,side}], unit:'R'|'$', hasSym, hasDay, hasSide, meta}
 * Pure functions, no I/O. The hosted demo has no LLM column-mapping fallback (heuristics only).
 *
 * MIT License (c) 2026 BOTLAB
 */
import { R_COLS, USD_COLS, SYM_COLS, DATE_COLS, SIDE_COLS } from './gates.js';

export function normHeader(h) {
  let s = String(h).trim().replace(/^"+|"+$/g, '').replace(/^﻿+/, '').toLowerCase();
  s = s.replace(/p&l/g, 'pnl').replace(/\$/g, '');
  return s.replace(/[^a-z0-9]+/g, '_').replace(/^_+|_+$/g, '');
}
const nh = names => names.map(normHeader);

export const R_ALIASES = nh(R_COLS).concat(['r_value', 'pnl_in_r', 'result_r']);
export const USD_ALIASES = nh(USD_COLS).concat(['closed_pnl', 'realized_profit', 'realised_pnl', 'net_profit', 'pnl_usdt', 'profit_usdt', 'realized_pnl_usdt']);
export const SYM_ALIASES = nh(SYM_COLS).concat(['contracts', 'contract', 'instrument', 'asset']);
export const DATE_ALIASES = nh(DATE_COLS).concat(['date_utc', 'trade_time', 'trade_time_utc_0', 'create_time', 'created_time',
  'updated_time', 'close_date', 'exit_date', 'datetime', 'closed_time', 'time_utc']);
export const SIDE_ALIASES = nh(SIDE_COLS).concat(['closing_direction', 'trade_type', 'position', 'type']);

/** RFC 4180-ish CSV reader (quotes, escaped quotes, CRLF), like Python's csv.reader. */
export function readCsv(text, delim) {
  const rows = [];
  let row = [], cell = '', q = false;
  for (let i = 0; i < text.length; i++) {
    const ch = text[i];
    if (q) {
      if (ch === '"') {
        if (text[i + 1] === '"') { cell += '"'; i++; } else q = false;
      } else cell += ch;
    } else if (ch === '"' && cell === '') q = true;
    else if (ch === delim) { row.push(cell); cell = ''; }
    else if (ch === '\n' || ch === '\r') {
      if (ch === '\r' && text[i + 1] === '\n') i++;
      row.push(cell); rows.push(row); row = []; cell = '';
    } else cell += ch;
  }
  if (cell !== '' || row.length) { row.push(cell); rows.push(row); }
  return rows;
}

export function sniffRows(text) {
  const t = String(text || '').replace(/^﻿/, '').trim();
  if (!t) throw new Error('Empty file.');
  const first = t.split(/\r?\n/)[0];
  const cnt = c => first.split(c).length - 1;
  const delim = cnt(';') > cnt(',') ? ';' : first.includes('\t') ? '\t' : ',';
  const rows = readCsv(t, delim).filter(r => r.some(c => c.trim()));
  if (rows.length < 2) throw new Error('Need at least one header row and one trade row.');
  return { headers: rows[0], body: rows.slice(1), delim };
}

/** '1,234.5' / '1.234,5' / '-12.3 USDT' / '(5.00)' -> number, else NaN. */
export function toNumber(s) {
  let t = String(s == null ? '' : s).trim().replace(/−/g, '-');
  if (!t) return NaN;
  const neg = t.startsWith('(') && t.endsWith(')');
  t = t.replace(/^[()]+|[()]+$/g, '').replace(/[^0-9,.\-+eE]/g, '');
  if (t.includes(',') && t.includes('.')) {
    t = t.lastIndexOf(',') > t.lastIndexOf('.') ? t.replace(/\./g, '').replace(/,/g, '.') : t.replace(/,/g, '');
  } else if (t.includes(',')) {
    t = (t.split(',').length - 1) === 1 ? t.replace(',', '.') : t.replace(/,/g, '');
  }
  // Python float(): whole string must be a number
  if (!/^[+-]?(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?$/.test(t)) return NaN;
  const v = Number(t);
  return neg ? -v : v;
}

/** gates.js normSide */
export function normSide(s) {
  const v = String(s || '').trim().toLowerCase();
  if (['l', 'long', 'buy', 'b', 'bid'].includes(v)) return 'L';
  if (['s', 'short', 'sell', 'ask'].includes(v)) return 'S';
  return '';
}

export function sideFrom(value, closingFill = false) {
  const v = String(value || '').trim().toLowerCase();
  if (v.includes('long')) return 'L';
  if (v.includes('short')) return 'S';
  const s = normSide(v);
  if (closingFill && s) return s === 'S' ? 'L' : 'S'; // closing SELL closes a long
  return s;
}

const ISO_RE = /^(\d{4})-(\d{2})-(\d{2})(?:[T ](\d{2}):(\d{2})(?::(\d{2})(?:\.(\d+))?)?)?\s*(Z|[+-]\d{2}(?::?\d{2})?)?$/i;

/** Date / epoch -> YYYY-MM-DD in UTC. Naive date-times are treated as UTC (same as gates.py to_day). */
export function toDay(raw) {
  raw = String(raw);
  const num = Number(raw);
  if (raw.trim() !== '' && Number.isFinite(num)) {
    const d = new Date(num > 1e12 ? num : num * 1000);
    if (!isNaN(d)) return d.toISOString().slice(0, 10);
  }
  const m = raw.trim().match(ISO_RE);
  if (m) {
    let ms = Date.UTC(+m[1], +m[2] - 1, +m[3], +(m[4] || 0), +(m[5] || 0), +(m[6] || 0));
    const tz = m[8];
    if (tz && tz.toUpperCase() !== 'Z') {
      const sign = tz[0] === '-' ? -1 : 1, d = tz.slice(1).replace(':', '');
      ms -= sign * ((+d.slice(0, 2)) * 60 + (+(d.slice(2, 4) || 0))) * 60000;
    }
    const d = new Date(ms);
    if (!isNaN(d)) return d.toISOString().slice(0, 10);
  }
  return raw.slice(0, 10);
}

const first = (hn, aliases) => hn.findIndex(h => aliases.includes(h));

export function detectFormat(hn) {
  const hs = new Set(hn);
  if (hs.has('closed_pnl') && (hs.has('contracts') || hs.has('closing_direction') || hs.has('trade_time_utc_0') || hs.has('qty'))) return 'bybit';
  if (hs.has('realized_profit') && (hs.has('date_utc') || hs.has('symbol'))) return 'binance';
  return 'generic';
}

export function heuristicMapping(headers) {
  const hn = headers.map(normHeader);
  const fmt = detectFormat(hn);
  let res, unit;
  if (fmt === 'bybit') { res = first(hn, ['closed_pnl']); unit = '$'; }
  else if (fmt === 'binance') { res = first(hn, ['realized_profit']); unit = '$'; }
  else {
    res = first(hn, R_ALIASES); unit = 'R';
    if (res < 0) { res = first(hn, USD_ALIASES); unit = '$'; }
  }
  if (res < 0) return null;
  const sideAliases = fmt === 'bybit' ? ['closing_direction', 'side', 'trade_type'] : SIDE_ALIASES;
  return {
    format: fmt, result: res, unit,
    symbol: first(hn, SYM_ALIASES), date: first(hn, DATE_ALIASES), side: first(hn, sideAliases),
    fee: fmt === 'binance' ? first(hn, ['fee', 'commission', 'trading_fee']) : -1,
    source: 'heuristic',
  };
}

const sortKey = ts => toDay(ts) + ' ' + ts;

/** `pre` = output of sniffRows(text), to avoid parsing the CSV twice. */
export function normalize(text, riskUsd = null, pre = null) {
  const { headers, body, delim } = pre || sniffRows(text);
  const dayCache = new Map();
  const dayOf = raw => { let d = dayCache.get(raw); if (d === undefined) { d = toDay(raw); dayCache.set(raw, d); } return d; };
  const mapping = heuristicMapping(headers);
  if (!mapping) throw new Error('Could not find a result column (R multiple or PnL). Columns seen: ' + headers.slice(0, 20).join(', '));
  const fmt = mapping.format;
  const { result: ri, symbol: si, date: di, side: sdi, fee: fi } = mapping;
  const cell = (row, i) => (i >= 0 && i < row.length ? row[i].trim() : '');

  let rows = [], skipped = 0, fees = 0;
  for (const row of body) {
    let v = toNumber(cell(row, ri));
    if (!Number.isFinite(v)) { skipped++; continue; }
    if (fmt === 'binance') {
      let fee = fi >= 0 ? toNumber(cell(row, fi)) : 0;
      fee = Number.isFinite(fee) ? fee : 0;
      fees += Math.abs(fee);
      if (v === 0) continue; // opening fill: no realized PnL
      v = v - Math.abs(fee);
    }
    rows.push({
      v,
      sym: si >= 0 ? cell(row, si) : '',
      day: di >= 0 && cell(row, di) ? dayOf(cell(row, di)) : '',
      side: sdi >= 0 ? sideFrom(cell(row, sdi), fmt === 'binance') : '',
      ts: di >= 0 ? cell(row, di) : '',
    });
  }
  if (!rows.length) throw new Error('Could not read any numeric trade rows.');

  let reordered = false;
  if (di >= 0 && rows.length > 1 && rows[0].ts && rows[rows.length - 1].ts && sortKey(rows[0].ts) > sortKey(rows[rows.length - 1].ts)) {
    rows.reverse(); reordered = true;
  }

  let unit = mapping.unit, converted = false;
  if (unit === '$' && riskUsd && riskUsd > 0) {
    rows.forEach(r => { r.v = r.v / riskUsd; });
    unit = 'R'; converted = true;
  }

  const notes = [];
  if (fmt === 'binance') notes.push(`Binance fills: net PnL = realized profit - fee on closing fills (total fees seen ${fees.toFixed(2)}).`);
  if (fmt === 'bybit') notes.push('Bybit Closed P&L already includes trading and funding fees.');
  if (reordered) notes.push('Rows were newest-first; reversed to time order.');
  if (converted) notes.push(`PnL converted to R using risk per trade = ${riskUsd}.`);
  if (skipped) notes.push(`${skipped} rows without a numeric result were skipped.`);

  const col = k => (mapping[k] >= 0 ? headers[mapping[k]] : null);
  return {
    rows: rows.map(({ v, sym, day, side }) => ({ v, sym, day, side })),
    unit, hasSym: si >= 0, hasDay: di >= 0, hasSide: sdi >= 0,
    meta: {
      format: fmt, mapping_source: mapping.source, delimiter: delim,
      columns: { result: col('result'), symbol: col('symbol'), date: col('date'), side: col('side') },
      n_rows_in_file: body.length, n_trades: rows.length, notes,
    },
  };
}
