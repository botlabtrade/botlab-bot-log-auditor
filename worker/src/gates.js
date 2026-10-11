/**
 * Thin ES-module wrapper around the vendored, byte-identical copy of vendor/gates.js
 * (botlab-check-your-bot gate math). Do not edit src/vendor/gates.cjs: the tests assert it
 * is identical to ../vendor/gates.js so the hosted demo uses exactly the same math.
 * MIT License (c) 2026 BOTLAB
 */
import Gates from './vendor/gates.cjs';

export default Gates;
export const { DEFAULTS, GATE_TITLES, parseCSV, grade, check, formatReport, moments, equity } = Gates;

// Column-name lists are private inside gates.js; these mirror them exactly (asserted in tests).
export const R_COLS = ['r', 'r_after', 'pnl_r', 'r_mult', 'rmult', 'r_multiple', 'realized_r'];
export const USD_COLS = ['pnl', 'pnl_usd', 'profit', 'pnl$', 'net_pnl', 'realized_pnl', 'return', 'ret'];
export const SYM_COLS = ['symbol', 'sym', 'coin', 'pair', 'ticker', 'market'];
export const DATE_COLS = ['date', 'exit_time', 'closed_at', 'close_time', 'exit_ts', 'time', 'ts', 'timestamp', 'entry_time', 'opened_at'];
export const SIDE_COLS = ['side', 'direction', 'dir', 'position_side', 'pos_side'];
