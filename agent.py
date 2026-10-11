"""The auditor agent: a fixed, inspectable pipeline of tool steps.

  1. parse      - detect exchange format, map columns (heuristics, LLM fallback), normalise trades
  2. gates      - six statistical gates (Python port) + cross-check against the original gates.js
                  executed by Node inside the sandbox
  3. analysis   - the LLM writes a small Python analysis script; it runs in the sandbox
                  (falls back to a built-in script if the generated one fails)
  4. report     - the LLM writes a plain-English audit from the computed FACTS only

MIT License (c) 2026 BOTLAB
"""
from __future__ import annotations

import json
import math
import time

import csv_normalize
import gates
import llm as llm_mod
import sandbox

STEPS = [
    ("parse", "Parse & normalise trades"),
    ("gates", "Run 6 statistical gates"),
    ("crosscheck", "Cross-check with gates.js in sandbox"),
    ("analysis", "Sandboxed analysis code"),
    ("report", "Write audit report"),
]

PARITY_KEYS = ("n", "mean", "sd", "psr0", "dsr", "wr", "dd", "passed", "total", "verdict")


class Progress:
    def __init__(self, cb=None):
        self.cb = cb or (lambda steps: None)
        self.steps = [{"id": k, "label": l, "status": "pending", "detail": "", "ms": None} for k, l in STEPS]
        self._t = {}

    def start(self, sid):
        self._set(sid, status="running")
        self._t[sid] = time.monotonic()

    def done(self, sid, detail="", status="done"):
        ms = int((time.monotonic() - self._t.get(sid, time.monotonic())) * 1000)
        self._set(sid, status=status, detail=detail, ms=ms)

    def _set(self, sid, **kw):
        for s in self.steps:
            if s["id"] == sid:
                s.update(kw)
        self.cb([dict(s) for s in self.steps])


def compare_with_node(py: dict, node: dict, tol: float = 1e-9) -> list[str]:
    diffs = []
    for k in PARITY_KEYS:
        a, b = py.get(k), node.get(k)
        if isinstance(a, float) or isinstance(b, float):
            if a is None or b is None or not (abs(a - b) <= tol * max(1.0, abs(a), abs(b))):
                diffs.append(f"{k}: py={a} node={b}")
        elif a != b:
            diffs.append(f"{k}: py={a} node={b}")
    for g, (ok, detail) in py["G"].items():
        nok, ndetail = node["G"][g]
        if ok != nok or detail != ndetail:
            diffs.append(f"{g}: py={ok}/{detail!r} node={nok}/{ndetail!r}")
    return diffs


def build_facts(parsed: dict, r: dict, analysis: dict | None, trials: int) -> dict:
    return gates.json_safe({
        "gates": {g: {"title": gates.GATE_TITLES[g], "pass": v[0], "detail": v[1]} for g, v in r["G"].items()},
        "metrics": {"n": r["n"], "mean": r["mean"], "sd": r["sd"], "wr": r["wr"], "psr0": r["psr0"], "dsr": r["dsr"],
                    "minTRL": r["minTRL"], "dd": r["dd"], "skew": r["skew"], "kurt": r["kurt"], "unit": r["unit"],
                    "trials": trials},
        "passed": r["passed"], "total": r["total"], "verdict": r["verdict"], "verdictText": r["verdictText"],
        "flags": r["flags"], "topSyms": r["topSyms"],
        "input": parsed.get("meta", {}),
        "analysis": analysis,
    })


def validate_analysis(out) -> bool:
    return isinstance(out, dict) and isinstance(out.get("monthly"), list) and isinstance(out.get("by_side"), dict)


def run_analysis(parsed: dict, llm: llm_mod.BaseLLM, timeout: float = 8.0) -> tuple[dict | None, dict]:
    trades = json.dumps(parsed["rows"])
    attempts = []
    code_sources = []
    try:
        code_sources.append(("llm:" + llm.name, llm.write_analysis_code()))
    except Exception as e:  # LLM down -> built-in script still runs in the sandbox
        attempts.append({"source": "llm:" + llm.name, "ok": False, "error": str(e)[:200]})
    if llm.name != "mock":
        code_sources.append(("builtin", llm_mod.MOCK_ANALYSIS_CODE.strip()))
    for source, code in code_sources:
        res = sandbox.run_python(code, {"trades.json": trades}, timeout=timeout)
        out = None
        if res.ok:
            try:
                out = res.json()
            except ValueError:
                out = None
        ok = validate_analysis(out)
        attempts.append({"source": source, "ok": ok, "timed_out": res.timed_out, "duration_s": res.duration_s,
                         "stderr": res.stderr[-300:] if not ok else "", "code": code[:4000]})
        if ok:
            return out, {"attempts": attempts, "limits": res.limits}
    return None, {"attempts": attempts}


def run_audit(csv_text: str, trials: int = 20, risk_usd: float | None = None, llm: llm_mod.BaseLLM | None = None,
              pro: bool = False, on_progress=None) -> dict:
    llm = llm or llm_mod.get_llm()
    p = Progress(on_progress)
    result = {"provider": llm.name, "model": llm.model, "pro": pro}

    p.start("parse")
    parsed = csv_normalize.normalize(csv_text, risk_usd=risk_usd, llm=llm)
    meta = parsed["meta"]
    p.done("parse", f"{meta['n_trades']} trades · format {meta['format']} · unit {parsed['unit']}")
    result["input"] = meta

    p.start("gates")
    r = gates.grade(parsed, trials)
    p.done("gates", f"{r['passed']}/{r['total']} gates passed · verdict {r['verdict']}")

    p.start("crosscheck")
    if sandbox.node_available():
        node_parsed = {k: parsed[k] for k in ("rows", "unit", "hasSym", "hasDay", "hasSide")}
        res = sandbox.gates_via_node(parsed=node_parsed, trials=trials)
        if res.ok:
            diffs = compare_with_node(r, res.json())
            result["crosscheck"] = {"engine": "node gates.js (sandboxed)", "match": not diffs, "diffs": diffs[:10],
                                    "duration_s": res.duration_s, "limits": res.limits}
            p.done("crosscheck", ("identical results" if not diffs else f"{len(diffs)} differences") + f" · {res.duration_s}s",
                   status="done" if not diffs else "warn")
        else:
            result["crosscheck"] = {"match": None, "error": (res.stderr or "")[-300:], "timed_out": res.timed_out}
            p.done("crosscheck", "node run failed", status="warn")
    else:
        result["crosscheck"] = {"match": None, "error": "node not installed"}
        p.done("crosscheck", "skipped (node not installed)", status="skip")

    p.start("analysis")
    analysis, trace = run_analysis(parsed, llm)
    result["analysis_trace"] = trace
    p.done("analysis", (f"{len(analysis['monthly'])} months analysed by {trace['attempts'][-1]['source']} code"
                        if analysis else "analysis failed (report continues without it)"),
           status="done" if analysis else "warn")

    facts = build_facts(parsed, r, analysis, trials)
    result["facts"] = facts
    curve = r["curve"]
    step = max(1, math.ceil(len(curve) / 400))
    result["curve"] = [round(v, 4) for v in curve[::step]]

    p.start("report")
    try:
        result["report"] = llm.write_report(facts, pro=pro)
        result["report_source"] = llm.name
    except Exception as e:
        result["report"] = llm_mod.MockLLM().write_report(facts, pro=pro)
        result["report_source"] = f"mock (fallback: {str(e)[:120]})"
    p.done("report", f"{len(result['report'].split())} words · {result['report_source']}")
    result["steps"] = p.steps
    return result
