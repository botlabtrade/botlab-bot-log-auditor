import pytest

import csv_normalize as cn
from conftest import read_sample


def test_generic_r_columns():
    p = cn.normalize("symbol,date,side,r\nBTCUSDT,2026-01-01,long,1.5\nETHUSDT,2026-01-02,short,-1\n")
    assert p["unit"] == "R"
    assert [r["v"] for r in p["rows"]] == [1.5, -1.0]
    assert [r["side"] for r in p["rows"]] == ["L", "S"]
    assert p["meta"]["format"] == "generic"


def test_semicolon_and_decimal_comma():
    p = cn.normalize("Coin;Close_Time;PnL\nBTC;2026-01-01 10:00;1,5\nETH;2026-01-02 11:00;-2,25\n")
    assert p["unit"] == "$"
    assert [r["v"] for r in p["rows"]] == [1.5, -2.25]
    assert p["rows"][0]["day"] == "2026-01-01"


def test_quoted_thousands_and_currency_suffix():
    p = cn.normalize('symbol,time,pnl\nBTC,1767225600000,"1,234.50 USDT"\nETH,1767312000,(5.00)\n')
    assert [r["v"] for r in p["rows"]] == [1234.5, -5.0]
    assert p["rows"][0]["day"] == "2026-01-01"   # epoch ms
    assert p["rows"][1]["day"] == "2026-01-02"   # epoch s


def test_bybit_closed_pnl_export():
    p = cn.normalize(read_sample("synthetic_bybit_closed_pnl.csv"))
    m = p["meta"]
    assert m["format"] == "bybit"
    assert m["columns"]["result"] == "Closed P&L"
    assert m["columns"]["side"] == "Closing Direction"
    assert p["unit"] == "$" and len(p["rows"]) == 360
    assert set(r["side"] for r in p["rows"]) == {"L", "S"}
    days = [r["day"] for r in p["rows"]]
    assert days == sorted(days), "newest-first export must be reversed to time order"


def test_binance_trade_history_export():
    p = cn.normalize(read_sample("synthetic_binance_futures_trades.csv"))
    assert p["meta"]["format"] == "binance"
    assert len(p["rows"]) == 160                      # opening fills (realized 0) dropped
    assert set(r["side"] for r in p["rows"]) == {"L", "S"}   # closing SELL -> long


def test_binance_side_inversion_and_fee():
    txt = ("Date(UTC),Symbol,Side,Price,Quantity,Amount,Fee,Realized Profit\n"
           "2026-01-01 10:00:00,BTCUSDT,BUY,100,1,100,0.05 USDT,0\n"
           "2026-01-01 12:00:00,BTCUSDT,SELL,110,1,110,0.05 USDT,10\n")
    p = cn.normalize(txt)
    assert len(p["rows"]) == 1
    assert p["rows"][0]["side"] == "L"
    assert p["rows"][0]["v"] == pytest.approx(9.95)


def test_risk_conversion_to_r():
    p = cn.normalize("pnl\n20\n-10\n", risk_usd=10)
    assert p["unit"] == "R"
    assert [r["v"] for r in p["rows"]] == [2.0, -1.0]


def test_unknown_columns_without_llm_raises():
    with pytest.raises(ValueError, match="Could not find a result column"):
        cn.normalize("foo,bar\n1,2\n")


def test_unknown_columns_uses_llm_mapping():
    class FakeLLM:
        def infer_mapping(self, headers, rows):
            return {"result_column": "Gewinn", "unit": "$", "symbol_column": "Paar", "date_column": None, "side_column": None}

    p = cn.normalize("Paar,Gewinn\nBTC,5\nETH,-3\n", llm=FakeLLM())
    assert p["meta"]["mapping_source"] == "llm"
    assert [r["v"] for r in p["rows"]] == [5.0, -3.0]
    assert p["rows"][0]["sym"] == "BTC"


def test_header_normalisation():
    assert cn.norm_header("Closed P&L") == "closed_pnl"
    assert cn.norm_header("Trade Time(UTC+0)") == "trade_time_utc_0"
    assert cn.norm_header("﻿Date(UTC)") == "date_utc"
