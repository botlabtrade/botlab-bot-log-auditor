// Gate parity: the Worker must produce exactly the same gate results as
// vendor/gates.js (Node) and gates.py + csv_normalize.py (Python).
process.env.TZ = 'UTC'; // gates.js parses naive date-times in local time; the Worker runtime is UTC
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { spawnSync } from 'node:child_process';
import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';
import * as W from '../src/gates.js';
import { normalize } from '../src/normalize.js';

const require = createRequire(import.meta.url);
const repo = fileURLToPath(new URL('../../', import.meta.url));
const VendorGates = require(repo + 'vendor/gates.js');
const SAMPLES = ['synthetic_42_trades_small_lucky', 'synthetic_640_trades_passes', 'synthetic_binance_futures_trades', 'synthetic_bybit_closed_pnl'];
const sample = f => readFileSync(repo + 'samples/' + f + '.csv', 'utf8');
const KEYS = ['n', 'mean', 'sd', 'psr0', 'dsr', 'wr', 'dd', 'passed', 'total', 'verdict'];

function same(a, b, label) {
  for (const k of KEYS) {
    if (typeof a[k] === 'number') assert.ok(Math.abs(a[k] - b[k]) <= 1e-9 * Math.max(1, Math.abs(a[k])), `${label} ${k}: ${a[k]} vs ${b[k]}`);
    else assert.equal(a[k], b[k], `${label} ${k}`);
  }
  for (const g of Object.keys(a.G)) assert.deepEqual(a.G[g], b.G[g], `${label} ${g}`);
}

test('vendored gates.cjs is byte-identical to vendor/gates.js', () => {
  assert.equal(readFileSync(new URL('../src/vendor/gates.cjs', import.meta.url), 'utf8'), readFileSync(repo + 'vendor/gates.js', 'utf8'));
});

test('thresholds: G1 floor is 50 and every DEFAULT matches gates.py', () => {
  assert.equal(W.DEFAULTS.G1_MIN_N, 50);
  assert.equal(VendorGates.DEFAULTS.G1_MIN_N, 50);
  const py = readFileSync(repo + 'gates.py', 'utf8');
  for (const [k, v] of Object.entries(W.DEFAULTS)) {
    const m = py.match(new RegExp(`"${k}":\\s*([-0-9.]+)`));
    assert.ok(m, 'gates.py missing ' + k);
    assert.equal(Number(m[1]), v, k);
  }
});

test('column lists mirror gates.js', () => {
  const src = readFileSync(repo + 'vendor/gates.js', 'utf8');
  for (const name of ['R_COLS', 'USD_COLS', 'SYM_COLS', 'DATE_COLS', 'SIDE_COLS']) {
    const m = src.match(new RegExp(`const ${name} = (\\[[^\\]]*\\])`));
    assert.deepEqual(JSON.parse(m[1].replace(/'/g, '"')), W[name], name);
  }
});

test('generic samples: Worker normaliser + gates == vendor gates.js check()', () => {
  for (const f of SAMPLES.slice(0, 2)) {
    for (const trials of [1, 20, 200]) same(W.grade(normalize(sample(f)), trials), VendorGates.check(sample(f), trials), `${f}/${trials}`);
  }
});

const PY = `
import json, sys
repo = ${JSON.stringify(repo)}
sys.path.insert(0, repo)
import gates, csv_normalize
out = {}
for f in ${JSON.stringify(SAMPLES)}:
    for risk in (None, 20.0):
        p = csv_normalize.normalize(open(repo + 'samples/' + f + '.csv', encoding='utf-8').read(), risk_usd=risk)
        r = gates.grade(p, 20)
        d = gates.json_safe({k: r[k] for k in ('n','mean','sd','psr0','dsr','wr','dd','passed','total','verdict','G')})
        d['format'] = p['meta']['format']; d['unit'] = p['unit']; d['v'] = [x['v'] for x in p['rows']]
        d['notes'] = p['meta']['notes']
        out[f + '|' + str(risk)] = d
print(json.dumps(out))
`;
const py = spawnSync('python', ['-c', PY], { encoding: 'utf8' });
const havePy = py.status === 0;

test('all samples (incl. Bybit/Binance, with and without risk) == Python gates.py + csv_normalize.py',
  { skip: !havePy && 'python not available' }, () => {
    const ref = JSON.parse(py.stdout);
    let count = 0;
    for (const f of SAMPLES) {
      for (const risk of [null, 20]) {
        const key = f + '|' + (risk ? '20.0' : 'None');
        const p = normalize(sample(f), risk), r = W.grade(p, 20), e = ref[key];
        assert.equal(p.meta.format, e.format, key);
        assert.equal(p.unit, e.unit, key);
        assert.deepEqual(p.meta.notes, e.notes.map(n => n.replace(/= (\d+)\.0\.$/, '= $1.')), key); // Python prints 20.0
        assert.equal(p.rows.length, e.v.length, key);
        p.rows.forEach((row, i) => assert.ok(Math.abs(row.v - e.v[i]) < 1e-12, `${key} row ${i}`));
        same(r, e, key);
        count++;
      }
    }
    assert.equal(count, 8);
  });
