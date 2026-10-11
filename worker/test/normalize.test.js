// Unit tests for the JS port of csv_normalize.py (mirrors tests/test_parsing.py).
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { normalize, normHeader, toNumber, sideFrom, toDay, readCsv, detectFormat } from '../src/normalize.js';

const sample = name => readFileSync(new URL('../../samples/' + name, import.meta.url), 'utf8');

test('generic R columns', () => {
  const p = normalize('symbol,date,side,r\nBTCUSDT,2026-01-01,long,1.5\nETHUSDT,2026-01-02,short,-1\n');
  assert.equal(p.unit, 'R');
  assert.deepEqual(p.rows.map(r => r.v), [1.5, -1]);
  assert.deepEqual(p.rows.map(r => r.side), ['L', 'S']);
  assert.equal(p.meta.format, 'generic');
});

test('semicolon and decimal comma', () => {
  const p = normalize('Coin;Close_Time;PnL\nBTC;2026-01-01 10:00;1,5\nETH;2026-01-02 11:00;-2,25\n');
  assert.equal(p.unit, '$');
  assert.deepEqual(p.rows.map(r => r.v), [1.5, -2.25]);
  assert.equal(p.rows[0].day, '2026-01-01');
});

test('quoted thousands, currency suffix, parentheses, epoch ms/s', () => {
  const p = normalize('symbol,time,pnl\nBTC,1767225600000,"1,234.50 USDT"\nETH,1767312000,(5.00)\n');
  assert.deepEqual(p.rows.map(r => r.v), [1234.5, -5]);
  assert.equal(p.rows[0].day, '2026-01-01');
  assert.equal(p.rows[1].day, '2026-01-02');
});

test('Bybit Closed P&L export', () => {
  const p = normalize(sample('synthetic_bybit_closed_pnl.csv'));
  assert.equal(p.meta.format, 'bybit');
  assert.equal(p.meta.columns.result, 'Closed P&L');
  assert.equal(p.meta.columns.side, 'Closing Direction');
  assert.equal(p.unit, '$');
  assert.equal(p.rows.length, 360);
  assert.deepEqual(new Set(p.rows.map(r => r.side)), new Set(['L', 'S']));
  const days = p.rows.map(r => r.day);
  assert.deepEqual(days, [...days].sort(), 'newest-first export must be reversed');
});

test('Binance trade history export', () => {
  const p = normalize(sample('synthetic_binance_futures_trades.csv'));
  assert.equal(p.meta.format, 'binance');
  assert.equal(p.rows.length, 160);
  assert.deepEqual(new Set(p.rows.map(r => r.side)), new Set(['L', 'S']));
});

test('Binance side inversion and fee', () => {
  const p = normalize('Date(UTC),Symbol,Side,Price,Quantity,Amount,Fee,Realized Profit\n' +
    '2026-01-01 10:00:00,BTCUSDT,BUY,100,1,100,0.05 USDT,0\n2026-01-01 12:00:00,BTCUSDT,SELL,110,1,110,0.05 USDT,10\n');
  assert.equal(p.rows.length, 1);
  assert.equal(p.rows[0].side, 'L');
  assert.ok(Math.abs(p.rows[0].v - 9.95) < 1e-12);
});

test('risk conversion to R', () => {
  const p = normalize('pnl\n20\n-10\n', 10);
  assert.equal(p.unit, 'R');
  assert.deepEqual(p.rows.map(r => r.v), [2, -1]);
});

test('headers', () => {
  assert.equal(normHeader('Closed P&L'), 'closed_pnl');
  assert.equal(normHeader('Trade Time(UTC+0)'), 'trade_time_utc_0');
  assert.equal(normHeader('﻿Date(UTC)'), 'date_utc');
  assert.equal(detectFormat(['symbol', 'date', 'r']), 'generic');
});

test('toNumber edge cases', () => {
  assert.equal(toNumber('1.234,5'), 1234.5);
  assert.equal(toNumber('−12.3'), -12.3);
  assert.equal(toNumber('1,234,567'), 1234567);
  assert.ok(Number.isNaN(toNumber('')));
  assert.ok(Number.isNaN(toNumber('abc')));
  assert.ok(Number.isNaN(toNumber('1.2.3')));
});

test('side and date helpers', () => {
  assert.equal(sideFrom('Close Long'), 'L');
  assert.equal(sideFrom('Close Short'), 'S');
  assert.equal(sideFrom('SELL', true), 'L');
  assert.equal(sideFrom('buy'), 'L');
  assert.equal(toDay('2026-03-01T23:30:00-02:00'), '2026-03-02');
  assert.equal(toDay('2026-03-01 23:30:00'), '2026-03-01');
  assert.equal(toDay('garbage-value'), 'garbage-va');
});

test('CSV reader handles quotes and CRLF', () => {
  assert.deepEqual(readCsv('a,"b,c","d""e"\r\n1,2,3', ','), [['a', 'b,c', 'd"e'], ['1', '2', '3']]);
});

test('errors', () => {
  assert.throws(() => normalize(''), /Empty file/);
  assert.throws(() => normalize('foo,bar\n1,2\n'), /Could not find a result column/);
  assert.throws(() => normalize('r\nx\n'), /Could not read any numeric/);
});
