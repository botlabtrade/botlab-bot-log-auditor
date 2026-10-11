"""Exchange trade-history CSV -> normalised trade list.

Supported out of the box:
  * generic   - the botlab-check-your-bot columns (r / pnl / symbol / date / side ...)
  * bybit     - Bybit "Closed P&L" export (Contracts, Closing Direction, Closed P&L, Trade Time ...)
  * binance   - Binance USD-M futures "Trade History" export (Date(UTC), Symbol, Side, Fee, Realized Profit)

If none of the heuristics finds a result column, `normalize()` can ask an LLM
(any object with `infer_mapping(headers, sample_rows)`) to propose a mapping.

Output shape is the same as gates.parse_csv:
  {rows:[{v, sym, day, side}], unit:'R'|'$', hasSym, hasDay, hasSide}
plus `meta` describing what was detected.

MIT License (c) 2026 BOTLAB
"""
from __future__ import annotations

import csv
import io
import math
import re

import gates


def norm_header(h: str) -> str:
    h = str(h).strip().strip('"').lstrip("﻿").lower()
    h = h.replace("p&l", "pnl").replace("$", "")
    return re.sub(r"[^a-z0-9]+", "_", h).strip("_")


def _nh(names):
    return [norm_header(n) for n in names]


R_ALIASES = _nh(gates.R_COLS) + ["r_value", "pnl_in_r", "result_r"]
USD_ALIASES = _nh(gates.USD_COLS) + ["closed_pnl", "realized_profit", "realised_pnl", "net_profit", "pnl_usdt", "profit_usdt", "realized_pnl_usdt"]
SYM_ALIASES = _nh(gates.SYM_COLS) + ["contracts", "contract", "instrument", "asset"]
DATE_ALIASES = _nh(gates.DATE_COLS) + ["date_utc", "trade_time", "trade_time_utc_0", "create_time", "created_time",
                                       "updated_time", "close_date", "exit_date", "datetime", "closed_time", "time_utc"]
SIDE_ALIASES = _nh(gates.SIDE_COLS) + ["closing_direction", "trade_type", "position", "type"]


def sniff_rows(text: str):
    text = str(text or "").lstrip("﻿").strip()
    if not text:
        raise ValueError("Empty file.")
    first = text.splitlines()[0]
    if first.count(";") > first.count(","):
        delim = ";"
    elif "\t" in first:
        delim = "\t"
    else:
        delim = ","
    reader = csv.reader(io.StringIO(text), delimiter=delim)
    rows = [r for r in reader if any(c.strip() for c in r)]
    if len(rows) < 2:
        raise ValueError("Need at least one header row and one trade row.")
    return rows[0], rows[1:], delim


def to_number(s) -> float:
    """'1,234.5' / '1.234,5' / '-12.3 USDT' / '(5.00)' -> float, else nan."""
    t = str(s or "").strip().replace("−", "-")
    if not t:
        return math.nan
    neg = t.startswith("(") and t.endswith(")")
    t = re.sub(r"[^0-9,.\-+eE]", "", t.strip("()"))
    if "," in t and "." in t:
        if t.rfind(",") > t.rfind("."):
            t = t.replace(".", "").replace(",", ".")
        else:
            t = t.replace(",", "")
    elif "," in t:
        t = t.replace(",", ".", 1) if t.count(",") == 1 else t.replace(",", "")
    try:
        v = float(t)
    except ValueError:
        return math.nan
    return -v if neg else v


def side_from(value: str, closing_fill: bool = False) -> str:
    v = str(value or "").strip().lower()
    if "long" in v:
        return "L"
    if "short" in v:
        return "S"
    s = gates.norm_side(v)
    if closing_fill and s:
        # a closing SELL fill closes a long, a closing BUY closes a short
        return "L" if s == "S" else "S"
    return s


def _first(headers_n, aliases):
    for i, h in enumerate(headers_n):
        if h in aliases:
            return i
    return -1


def detect_format(headers_n) -> str:
    hs = set(headers_n)
    if "closed_pnl" in hs and ({"contracts", "closing_direction"} & hs or "trade_time_utc_0" in hs or "qty" in hs):
        return "bybit"
    if "realized_profit" in hs and ("date_utc" in hs or "symbol" in hs):
        return "binance"
    return "generic"


def heuristic_mapping(headers) -> dict | None:
    hn = [norm_header(h) for h in headers]
    fmt = detect_format(hn)
    if fmt == "bybit":
        res, unit = _first(hn, ["closed_pnl"]), "$"
    elif fmt == "binance":
        res, unit = _first(hn, ["realized_profit"]), "$"
    else:
        res, unit = _first(hn, R_ALIASES), "R"
        if res < 0:
            res, unit = _first(hn, USD_ALIASES), "$"
    if res < 0:
        return None
    side_aliases = (["closing_direction", "side", "trade_type"] if fmt == "bybit" else SIDE_ALIASES)
    return {
        "format": fmt,
        "result": res, "unit": unit,
        "symbol": _first(hn, SYM_ALIASES),
        "date": _first(hn, DATE_ALIASES),
        "side": _first(hn, side_aliases),
        "fee": _first(hn, ["fee", "commission", "trading_fee"]) if fmt == "binance" else -1,
        "source": "heuristic",
    }


def _mapping_from_llm(headers, body, llm) -> dict | None:
    if llm is None or not hasattr(llm, "infer_mapping"):
        return None
    proposal = llm.infer_mapping(headers, body[:5])
    if not proposal:
        return None
    idx = {h: i for i, h in enumerate(headers)}

    def col(name):
        return idx.get(proposal.get(name) or "", -1)

    res = col("result_column")
    if res < 0:
        return None
    unit = "R" if str(proposal.get("unit", "")).upper() == "R" else "$"
    return {"format": "llm-mapped", "result": res, "unit": unit, "symbol": col("symbol_column"),
            "date": col("date_column"), "side": col("side_column"), "fee": -1, "source": "llm"}


def normalize(text: str, risk_usd: float | None = None, llm=None) -> dict:
    headers, body, delim = sniff_rows(text)
    mapping = heuristic_mapping(headers) or _mapping_from_llm(headers, body, llm)
    if not mapping:
        raise ValueError("Could not find a result column (R multiple or PnL). Columns seen: " + ", ".join(headers[:20]))
    fmt = mapping["format"]
    ri, si, di, sdi, fi = mapping["result"], mapping["symbol"], mapping["date"], mapping["side"], mapping["fee"]

    def cell(row, i):
        return row[i].strip() if 0 <= i < len(row) else ""

    rows, skipped, fees = [], 0, 0.0
    for row in body:
        v = to_number(cell(row, ri))
        if not math.isfinite(v):
            skipped += 1
            continue
        if fmt == "binance":
            fee = to_number(cell(row, fi)) if fi >= 0 else 0.0
            fee = fee if math.isfinite(fee) else 0.0
            fees += abs(fee)
            if v == 0:  # opening fill: no realized PnL; fee is still a cost
                continue
            v = v - abs(fee)
        rows.append({
            "v": v,
            "sym": cell(row, si) if si >= 0 else "",
            "day": gates.to_day(cell(row, di)) if di >= 0 and cell(row, di) else "",
            "side": side_from(cell(row, sdi), closing_fill=(fmt == "binance")) if sdi >= 0 else "",
            "ts": cell(row, di) if di >= 0 else "",
        })
    if not rows:
        raise ValueError("Could not read any numeric trade rows.")

    # exchange exports are usually newest-first; the gates need time order
    if di >= 0 and len(rows) > 1 and rows[0]["ts"] and rows[-1]["ts"] and _sort_key(rows[0]["ts"]) > _sort_key(rows[-1]["ts"]):
        rows.reverse()
        reordered = True
    else:
        reordered = False

    unit = mapping["unit"]
    converted = False
    if unit == "$" and risk_usd and risk_usd > 0:
        for r in rows:
            r["v"] = r["v"] / risk_usd
        unit, converted = "R", True

    notes = []
    if fmt == "binance":
        notes.append(f"Binance fills: net PnL = realized profit - fee on closing fills (total fees seen {fees:.2f}).")
    if fmt == "bybit":
        notes.append("Bybit Closed P&L already includes trading and funding fees.")
    if reordered:
        notes.append("Rows were newest-first; reversed to time order.")
    if converted:
        notes.append(f"PnL converted to R using risk per trade = {risk_usd}.")
    if skipped:
        notes.append(f"{skipped} rows without a numeric result were skipped.")

    return {
        "rows": [{k: r[k] for k in ("v", "sym", "day", "side")} for r in rows],
        "unit": unit,
        "hasSym": si >= 0,
        "hasDay": di >= 0,
        "hasSide": sdi >= 0,
        "meta": {
            "format": fmt, "mapping_source": mapping["source"], "delimiter": delim,
            "columns": {k: (headers[mapping[k]] if mapping[k] >= 0 else None) for k in ("result", "symbol", "date", "side")},
            "n_rows_in_file": len(body), "n_trades": len(rows), "notes": notes,
        },
    }


def _sort_key(ts: str) -> str:
    d = gates.to_day(ts)
    return d + " " + ts
