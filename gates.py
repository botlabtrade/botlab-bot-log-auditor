"""Python port of vendor/gates.js (botlab-check-your-bot).

Pure functions: CSV parsing + six gates (G1..G6). No network, no I/O.
The numbers and gate detail strings are kept identical to gates.js so the
two implementations can be cross-checked (see tests/test_gates_parity.py).

MIT License (c) 2026 BOTLAB
"""
from __future__ import annotations

import math
import re
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP

DEFAULTS = {
    "G1_MIN_N": 50,
    "G1_CONF": 0.95,
    "G2_PSR_MIN": 0.95,
    "G2_DSR_MIN": 0.5,
    "G3_LAST_LONG": 100,
    "G3_LAST_SHORT": 30,
    "G3_SHORT_MIN": -0.1,
    "G4_SYM_TOP": 3,
    "G4_SYM_SHARE_MAX": 0.5,
    "G4_DAY_TOP": 2,
    "G4_DAY_SHARE_MAX": 0.5,
    "G5_LEAK_MIN_N": 50,
    "G6_MAX_DD_R": 15,
}

GATE_TITLES = {
    "G1": "Sufficient sample",
    "G2": "Not just luck",
    "G3": "Still working recently",
    "G4": "Not reliant on a few coins or days",
    "G5": "No losing side",
    "G6": "Acceptable drawdown",
}

R_COLS = ["r", "r_after", "pnl_r", "r_mult", "rmult", "r_multiple", "realized_r"]
USD_COLS = ["pnl", "pnl_usd", "profit", "pnl$", "net_pnl", "realized_pnl", "return", "ret"]
SYM_COLS = ["symbol", "sym", "coin", "pair", "ticker", "market"]
DATE_COLS = ["date", "exit_time", "closed_at", "close_time", "exit_ts", "time", "ts", "timestamp", "entry_time", "opened_at"]
SIDE_COLS = ["side", "direction", "dir", "position_side", "pos_side"]

INF = math.inf


def js_sum(xs) -> float:
    """Left-to-right float sum like Array.reduce. (Python 3.12+ sum() is compensated, which differs.)"""
    t = 0.0
    for v in xs:
        t += v
    return t


# ---------- JS-compatible formatting ----------
def to_fixed(x: float, d: int = 2) -> str:
    """Number.prototype.toFixed: exact binary value, ties away from zero."""
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return "NaN"
    if math.isinf(x):
        return "Infinity" if x > 0 else "-Infinity"
    q = Decimal(x).quantize(Decimal(1).scaleb(-d), rounding=ROUND_HALF_UP)
    s = format(q, "f")
    return s


def f_r(x: float, d: int = 2) -> str:
    return ("+" if x > 0 else "") + to_fixed(x, d)


def js_round(x: float) -> int:
    return int(math.floor(x + 0.5))


# ---------- normal distribution helpers ----------
def ncdf(x: float) -> float:
    t = 1 / (1 + 0.2316419 * abs(x))
    d = 0.3989423 * math.exp(-x * x / 2)
    p = d * t * (0.3193815 + t * (-0.3565638 + t * (1.781478 + t * (-1.821256 + t * 1.330274))))
    return 1 - p if x > 0 else p


def ninv(p: float) -> float:
    a = [-39.6968302866538, 220.946098424521, -275.928510446969, 138.357751867269, -30.6647980661472, 2.50662827745924]
    b = [-54.4760987982241, 161.585836858041, -155.698979859887, 66.8013118877197, -13.2806815528857]
    c = [-0.00778489400243029, -0.322396458041136, -2.40075827716184, -2.54973253934373, 4.37466414146497, 2.93816398269878]
    dd = [0.00778469570904146, 0.32246712907004, 2.445134137143, 3.75440866190742]
    pl = 0.02425
    if p < pl:
        q = math.sqrt(-2 * math.log(p))
        return (((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / ((((dd[0] * q + dd[1]) * q + dd[2]) * q + dd[3]) * q + 1)
    if p > 1 - pl:
        return -ninv(1 - p)
    q = p - 0.5
    r = q * q
    return (((((a[0] * r + a[1]) * r + a[2]) * r + a[3]) * r + a[4]) * r + a[5]) * q / (((((b[0] * r + b[1]) * r + b[2]) * r + b[3]) * r + b[4]) * r + 1)


# ---------- CSV (same rules as gates.js parseCSV) ----------
def norm_side(s) -> str:
    v = str(s or "").strip().lower()
    if v in ("l", "long", "buy", "b", "bid"):
        return "L"
    if v in ("s", "short", "sell", "ask"):
        return "S"
    return ""


_NUM_RE = re.compile(r"^\s*[+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?")


def js_parse_float(s) -> float:
    """Approximation of JS parseFloat: leading numeric prefix, else NaN."""
    m = _NUM_RE.match(str(s))
    if not m:
        st = str(s).strip()
        if st.startswith("Infinity") or st.startswith("+Infinity"):
            return INF
        if st.startswith("-Infinity"):
            return -INF
        return math.nan
    return float(m.group(0))


def to_day(raw: str) -> str:
    """Date/epoch -> YYYY-MM-DD (UTC). Naive date-times are treated as UTC."""
    raw = str(raw)
    try:
        num = float(raw)
        if math.isfinite(num) and raw != "":
            ts = num / 1000 if num > 1e12 else num
            return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")
    except (ValueError, OverflowError, OSError):
        pass
    try:
        s = raw.replace(" ", "T", 1)
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).strftime("%Y-%m-%d")
    except ValueError:
        return raw[:10]


def parse_csv(txt: str) -> dict:
    """Port of gates.js parseCSV. Returns {rows:[{v,sym,day,side}], unit, hasSym, hasDay, hasSide}."""
    text = str(txt or "").lstrip("﻿").strip()
    lines = [l for l in re.split(r"\r?\n", text) if l.strip()]
    if len(lines) < 2:
        raise ValueError("Need at least one header row and one trade row.")
    h0 = lines[0]
    if h0.count(";") > h0.count(","):
        sep = ";"
    elif "\t" in h0:
        sep = "\t"
    else:
        sep = ","
    head = [re.sub(r'^"|"$', "", h.strip()).lower() for h in h0.split(sep)]

    def pick(names):
        for i, h in enumerate(head):
            if h in names:
                return i
        return -1

    ri, unit = pick(R_COLS), "R"
    if ri < 0:
        ri, unit = pick(USD_COLS), "$"
    if ri < 0:
        raise ValueError("No result column found (r / pnl_r / pnl / pnl_usd).")
    si, di, sdi = pick(SYM_COLS), pick(DATE_COLS), pick(SIDE_COLS)
    rows = []
    for l in lines[1:]:
        c = [re.sub(r'^"|"$', "", x.strip()) for x in l.split(sep)]
        cell = c[ri] if ri < len(c) else "undefined"
        v = js_parse_float(str(cell).replace(",", ".", 1))
        if not math.isfinite(v):
            continue
        day = ""
        if di >= 0 and di < len(c) and c[di]:
            day = to_day(c[di])
        rows.append({
            "v": v,
            "sym": (c[si] if si < len(c) else "") if si >= 0 else "",
            "day": day,
            "side": norm_side(c[sdi] if sdi < len(c) else "") if sdi >= 0 else "",
        })
    if not rows:
        raise ValueError("Could not read any numeric rows.")
    return {"rows": rows, "unit": unit, "hasSym": si >= 0, "hasDay": di >= 0, "hasSide": sdi >= 0}


# ---------- statistics ----------
def moments(x):
    n = len(x)
    mean = js_sum(x) / n
    sd = math.sqrt(js_sum((b - mean) ** 2 for b in x) / max(1, n - 1)) or 1e-9
    sk = js_sum(((b - mean) / sd) ** 3 for b in x) / n
    ku = js_sum(((b - mean) / sd) ** 4 for b in x) / n
    return {"n": n, "mean": mean, "sd": sd, "sk": sk, "ku": ku, "sr": mean / sd}


def psr(m, sr0):
    den = math.sqrt(max(1e-9, 1 - m["sk"] * m["sr"] + (m["ku"] - 1) / 4 * m["sr"] * m["sr"]))
    if math.isinf(sr0):
        return 0.0
    return ncdf((m["sr"] - sr0) * math.sqrt(m["n"] - 1) / den)


def sr0_for_trials(n, trials):
    N = max(1, int(trials))
    g = 0.5772156649
    if N <= 1:
        return 0.0
    if n <= 1:
        return INF
    return math.sqrt(1 / (n - 1)) * ((1 - g) * ninv(1 - 1 / N) + g * ninv(1 - 1 / (N * math.e)))


def min_trl(m, conf=0.95):
    z = ninv(0.95 if conf is None else conf)
    if m["sr"] > 0:
        return math.ceil(1 + (1 - m["sk"] * m["sr"] + (m["ku"] - 1) / 4 * m["sr"] * m["sr"]) * (z / m["sr"]) ** 2)
    return INF


def equity(x):
    eq = pk = dd = 0.0
    curve = []
    for v in x:
        eq += v
        pk = max(pk, eq)
        dd = max(dd, pk - eq)
        curve.append(eq)
    return curve, dd


def group_share(rows, key, k, base):
    m: dict = {}
    for r in rows:
        m[r[key]] = m.get(r[key], 0) + r["v"]
    top = sorted(m.items(), key=lambda kv: -kv[1])[:k]
    return js_sum(max(0, t[1]) for t in top) / base, [list(t) for t in top]


def _fmt_int(v):
    return str(v) if math.isfinite(v) else "∞"


# ---------- the six gates ----------
def grade(P: dict, trials: int = 20, opts: dict | None = None) -> dict:
    K = dict(DEFAULTS)
    K.update(opts or {})
    x = [r["v"] for r in P["rows"]]
    m = moments(x)
    n = m["n"]
    N = max(1, int(trials))
    psr0 = psr(m, 0)
    dsr = psr(m, sr0_for_trials(n, N))
    trl = min_trl(m, K["G1_CONF"])
    wr = len([v for v in x if v > 0]) / n * 100
    curve, max_dd = equity(x)

    def avg_last(k):
        s = x[-k:]
        return js_sum(s) / len(s)

    pos_sum = js_sum(v for v in x if v > 0) or 1e-9
    tot = js_sum(x)

    sym_share = day_share = None
    top_syms = []
    if P.get("hasSym"):
        sym_share, top_syms = group_share(P["rows"], "sym", K["G4_SYM_TOP"], pos_sum)
    if P.get("hasDay"):
        day_share, _ = group_share(P["rows"], "day", K["G4_DAY_TOP"], abs(tot) or 1e-9)

    need = max(K["G1_MIN_N"], trl)
    last_long, last_short = avg_last(K["G3_LAST_LONG"]), avg_last(K["G3_LAST_SHORT"])

    G = {}
    G["G1"] = [n >= need, f"n={n} / need {_fmt_int(need)} (MinTRL {_fmt_int(trl)})"]
    G["G2"] = [psr0 >= K["G2_PSR_MIN"] and dsr >= K["G2_DSR_MIN"],
               f"PSR(0)={to_fixed(psr0, 2)} · DSR={to_fixed(dsr, 2)} with {N} trials"]
    G["G3"] = [last_long > 0 and last_short > K["G3_SHORT_MIN"],
               f"last {K['G3_LAST_LONG']} {f_r(last_long, 3)} · last {K['G3_LAST_SHORT']} {f_r(last_short, 3)}"]
    g4_parts = [
        f"top {K['G4_SYM_TOP']} coins {js_round(sym_share * 100)}%" if sym_share is not None else "no coin column",
        f"best {K['G4_DAY_TOP']} days {js_round(day_share * 100)}%" if day_share is not None else "no date column",
    ]
    G["G4"] = [(sym_share is None or sym_share < K["G4_SYM_SHARE_MAX"]) and (day_share is None or day_share < K["G4_DAY_SHARE_MAX"]),
               " · ".join(g4_parts)]
    if not P.get("hasSide"):
        G["G5"] = [None, "no side column (add side = long/short)"]
    else:
        def side(k):
            return [r for r in P["rows"] if r["side"] == k]

        def mean_(a):
            return js_sum(r["v"] for r in a) / (len(a) or 1)

        leak = [k for k in ("L", "S") if len(side(k)) >= K["G5_LEAK_MIN_N"] and mean_(side(k)) < 0]
        G["G5"] = [not leak, " · ".join(
            f"{'long' if k == 'L' else 'short'} {len(side(k))} · {f_r(mean_(side(k)), 2)}" for k in ("L", "S"))]
    if P["unit"] == "R":
        G["G6"] = [max_dd < K["G6_MAX_DD_R"], f"max DD {to_fixed(max_dd, 1)}R (limit {K['G6_MAX_DD_R']}R)"]
    else:
        G["G6"] = [None, "scored only when the unit is R"]

    flags = []
    if wr >= 80:
        flags.append("A win rate of 80% or more is a classic red flag for look-ahead bias or overfitting.")
    if n < 30:
        flags.append("Fewer than 30 trades says almost nothing yet.")
    if sym_share is not None and sym_share >= K["G4_SYM_SHARE_MAX"]:
        flags.append(f"Profit concentrated in {', '.join(str(t[0]) for t in top_syms)}.")
    if psr0 >= K["G2_PSR_MIN"] and dsr < K["G2_DSR_MIN"]:
        flags.append("Looks good in isolation but fails once the number of trials is accounted for: likely overfit.")

    scored = [g for g in G.values() if g[0] is not None]
    passed = len([g for g in scored if g[0] is True])
    total = len(scored)
    verdict = "ready" if passed == total else ("watch" if (G["G2"][0] and G["G3"][0]) else "no")
    verdict_text = {
        "ready": "Passes every gate that could be scored: worth a small live trial alongside paper trading.",
        "watch": "There is a statistical signal, but it does not qualify yet. Keep collecting trades.",
        "no": f"Not passed ({passed} of {total} gates). Do not commit real money yet.",
    }[verdict]

    return {
        "n": n, "mean": m["mean"], "sd": m["sd"], "skew": m["sk"], "kurt": m["ku"], "sr": m["sr"], "wr": wr,
        "psr0": psr0, "dsr": dsr, "minTRL": trl, "need": need, "dd": max_dd, "curve": curve, "G": G,
        "topSyms": top_syms, "unit": P["unit"], "trials": N, "passed": passed, "total": total,
        "verdict": verdict, "verdictText": verdict_text, "flags": flags,
    }


def check(csv_text: str, trials: int = 20, opts: dict | None = None) -> dict:
    return grade(parse_csv(csv_text), 20 if trials is None else trials, opts)


def json_safe(obj):
    """Replace inf/nan with None (same as JSON.stringify)."""
    if isinstance(obj, float) and not math.isfinite(obj):
        return None
    if isinstance(obj, dict):
        return {k: json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [json_safe(v) for v in obj]
    return obj


def format_report(r: dict) -> str:
    u = r["unit"]
    out = ['BOTLAB "Check your bot" - gate report', "",
           f"Trades        {r['n']}",
           f"Avg per trade {f_r(r['mean'], 3)}{'R' if u == 'R' else ' (' + u + ')'}",
           f"Win rate      {to_fixed(r['wr'], 0)}%",
           f"PSR(0)        {to_fixed(r['psr0'], 2)}",
           f"DSR           {to_fixed(r['dsr'], 2)}  ({r['trials']} trials)",
           f"Max drawdown  {to_fixed(r['dd'], 1)} {u}", ""]
    for g, (ok, why) in r["G"].items():
        mark = " -- " if ok is None else ("PASS" if ok else "FAIL")
        out.append(f"[{mark}] {g} {GATE_TITLES[g]}: {why}")
    out += ["", f"Verdict: {r['verdictText']}"]
    if r["flags"]:
        out += ["", "Red flags:"] + [" - " + f for f in r["flags"]]
    out += ["", "Educational tool. Not financial advice. Past results don't predict future results."]
    return "\n".join(out)
