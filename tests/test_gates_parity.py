"""The Python port must give the same numbers and gate strings as the original gates.js."""
import pytest

import agent
import csv_normalize
import gates
import sandbox
from conftest import read_sample

EXAMPLES = ["synthetic_42_trades_small_lucky.csv", "synthetic_640_trades_passes.csv"]
needs_node = pytest.mark.skipif(not sandbox.node_available(), reason="node not installed")


@needs_node
@pytest.mark.parametrize("name", EXAMPLES)
@pytest.mark.parametrize("trials", [1, 20, 200])
def test_python_port_equals_gates_js_on_raw_csv(name, trials):
    text = read_sample(name)
    py = gates.check(text, trials)
    res = sandbox.gates_via_node(csv_text=text, trials=trials)
    assert res.ok, res.stderr
    node = res.json()
    assert agent.compare_with_node(py, node) == []
    assert py["flags"] == node["flags"]
    assert py["topSyms"] == node["topSyms"]
    assert py["need"] == node["need"]


@needs_node
@pytest.mark.parametrize("name", ["synthetic_bybit_closed_pnl.csv", "synthetic_binance_futures_trades.csv"])
def test_parity_on_normalised_exchange_exports(name):
    p = csv_normalize.normalize(read_sample(name), risk_usd=20)
    py = gates.grade(p, 20)
    res = sandbox.gates_via_node(parsed={k: p[k] for k in ("rows", "unit", "hasSym", "hasDay", "hasSide")}, trials=20)
    assert res.ok, res.stderr
    assert agent.compare_with_node(py, res.json()) == []


def test_expected_verdicts():
    assert gates.check(read_sample("synthetic_640_trades_passes.csv"))["verdict"] == "ready"
    small = gates.check(read_sample("synthetic_42_trades_small_lucky.csv"))
    assert small["verdict"] != "ready"
    assert small["G"]["G1"][0] is False


def test_to_fixed_matches_js_rounding():
    assert gates.to_fixed(0.125, 2) == "0.13"   # JS: (0.125).toFixed(2) === "0.13"
    assert gates.to_fixed(-0.125, 2) == "-0.13"
    assert gates.to_fixed(1.005, 2) == "1.00"   # binary 1.00499...
    assert gates.f_r(0.0, 2) == "0.00"
    assert gates.js_round(12.5) == 13


def test_infinite_mintrl_when_losing():
    p = {"rows": [{"v": -1.0 + (i % 3) * 0.2, "sym": "", "day": "", "side": ""} for i in range(50)],
         "unit": "R", "hasSym": False, "hasDay": False, "hasSide": False}
    r = gates.grade(p, 20)
    assert r["minTRL"] == float("inf")
    assert "need ∞" in r["G"]["G1"][1]
    assert gates.json_safe(r)["minTRL"] is None
