"""Regenerate the synthetic exchange-format sample CSVs (deterministic, seed 7).

These are SYNTHETIC trades shaped like real exchange exports; no real account data.
"""
import csv
import os
import random
from datetime import datetime, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
COINS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "DOGEUSDT", "ADAUSDT", "LINKUSDT", "AVAXUSDT"]


def bybit(path, n=360, seed=7):
    rnd = random.Random(seed)
    t = datetime(2026, 3, 1, 8, 0)
    rows = []
    for i in range(n):
        t += timedelta(hours=rnd.randint(3, 14))
        sym = rnd.choice(COINS)
        long = rnd.random() < 0.6
        entry = round(rnd.uniform(0.5, 100), 4)
        qty = round(rnd.uniform(10, 200), 2)
        # a modest edge on longs, shorts roughly break-even
        r = rnd.gauss(0.12 if long else -0.05, 1.0)
        pnl = round(r * 20, 4)
        exitp = round(entry * (1 + (pnl / (qty * entry)) * (1 if long else -1)), 4)
        rows.append([sym, "Close Long" if long else "Close Short", qty, entry, exitp, pnl,
                     round(qty * entry * 0.00055, 4), round(qty * exitp * 0.00055, 4), 0,
                     rnd.choice(["Trade", "TakeProfit", "StopLoss"]), t.strftime("%Y-%m-%d %H:%M:%S")])
    rows.reverse()  # Bybit exports newest first
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["Contracts", "Closing Direction", "Qty", "Entry Price", "Exit Price", "Closed P&L",
                    "Opening Fee", "Closing Fee", "Funding Fee", "Exit Type", "Trade Time(UTC+0)"])
        w.writerows(rows)


def binance(path, n=160, seed=11):
    rnd = random.Random(seed)
    t = datetime(2026, 6, 1, 0, 0)
    rows = []
    for i in range(n):
        t += timedelta(hours=rnd.randint(4, 20))
        sym = rnd.choice(COINS)
        long = rnd.random() < 0.5
        price = round(rnd.uniform(0.5, 100), 4)
        qty = round(rnd.uniform(5, 100), 2)
        open_side, close_side = ("BUY", "SELL") if long else ("SELL", "BUY")
        fee_o = round(qty * price * 0.0005, 6)
        rows.append([t.strftime("%Y-%m-%d %H:%M:%S"), sym, open_side, price, qty, round(qty * price, 4), f"{fee_o} USDT", "0"])
        t2 = t + timedelta(hours=rnd.randint(1, 3))
        pnl = round(rnd.gauss(-0.4, 9.0), 4)
        rows.append([t2.strftime("%Y-%m-%d %H:%M:%S"), sym, close_side, price, qty, round(qty * price, 4),
                     f"{fee_o} USDT", str(pnl)])
        t = t2
    rows.reverse()
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["Date(UTC)", "Symbol", "Side", "Price", "Quantity", "Amount", "Fee", "Realized Profit"])
        w.writerows(rows)


if __name__ == "__main__":
    bybit(os.path.join(HERE, "synthetic_bybit_closed_pnl.csv"))
    binance(os.path.join(HERE, "synthetic_binance_futures_trades.csv"))
    print("ok")
