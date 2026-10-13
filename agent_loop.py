"""Bounded agentic loop: after the gates, the LLM chooses follow-up analyses and runs them in the sandbox.

Protocol (tool-call style JSON, works with any chat model, no native tool-calling needed):
    model -> {"tool": "run_python", "why": "...", "code": "..."}   executed in the sandbox / runner container
    model -> {"tool": "finish", "summary": "..."}                  ends the loop
    loop  -> "TOOL RESULT step k: ok=..., timed_out=..., stdout: ..., stderr: ..."   fed back as a user turn

Hard limits (env, with defaults):
    AGENT_MAX_STEPS        4     tool calls (0 disables the loop); hard cap 8
    AGENT_STEP_TIMEOUT_S   6     sandbox wall clock per step; hard cap 20
    AGENT_MAX_OUTPUT       4000  characters of stdout/stderr fed back to the model and stored per step
    AGENT_MAX_CODE         8000  characters of code accepted per step
    AGENT_TOTAL_BUDGET_S   90    wall clock for the whole loop (LLM + sandbox)
At most AGENT_MAX_STEPS + 1 LLM calls are made (the extra one asks for the final summary).

MIT License (c) 2026 BOTLAB
"""
from __future__ import annotations

import json
import os
import time

import gates
import llm as llm_mod
import runner_client


def _env_num(name, default, lo, hi, cast=int):
    try:
        v = cast(os.environ.get(name, default))
    except (TypeError, ValueError):
        v = default
    return max(lo, min(hi, v))


def limits_from_env() -> dict:
    return {"max_steps": _env_num("AGENT_MAX_STEPS", 4, 0, 8),
            "step_timeout_s": _env_num("AGENT_STEP_TIMEOUT_S", 6.0, 0.5, 20.0, float),
            "max_output": _env_num("AGENT_MAX_OUTPUT", 4000, 200, 20000),
            "max_code": _env_num("AGENT_MAX_CODE", 8000, 200, 50000),
            "total_budget_s": _env_num("AGENT_TOTAL_BUDGET_S", 90.0, 5.0, 600.0, float)}


def _clip(s: str, n: int) -> str:
    s = s or ""
    return s if len(s) <= n else s[:n] + f"\n[truncated {len(s) - n} chars]"


def _context(facts: dict) -> str:
    slim = {k: facts.get(k) for k in ("gates", "metrics", "passed", "total", "verdict", "flags", "topSyms")}
    a = facts.get("analysis") or {}
    slim["analysis_overview"] = {"months": len(a.get("monthly") or []), "by_side": a.get("by_side"),
                                 "longest_losing_streak": a.get("longest_losing_streak")}
    return json.dumps(gates.json_safe(slim), ensure_ascii=False, default=str)[:12000]


def run_agent_loop(parsed: dict, facts: dict, llm: llm_mod.BaseLLM, runner=None, limits: dict | None = None,
                   on_step=None) -> dict:
    """Run the loop. `on_step(event)` is called with {"phase": "start"|"end", "step": k, ...} for the UI stream.

    Returns {"steps": [...], "summary": str, "stopped": reason, "limits": {...}, "executor": label}."""
    lim = dict(limits_from_env(), **(limits or {}))
    runner = runner or runner_client.get_runner()
    on_step = on_step or (lambda ev: None)
    trades = json.dumps(parsed["rows"])
    t0 = time.monotonic()
    out = {"steps": [], "summary": "", "stopped": "", "limits": lim, "executor": getattr(runner, "label", "?")}
    if lim["max_steps"] <= 0:
        out["stopped"] = "disabled (AGENT_MAX_STEPS=0)"
        return out

    messages = [{"role": "user", "content":
                 "FACTS (JSON):\n" + _context(facts) +
                 f"\n\nYou have {lim['max_steps']} run_python calls. Choose the follow-up analyses that best test the "
                 "weakest gates. Reply with one JSON object."}]
    step = 0
    while True:
        if time.monotonic() - t0 > lim["total_budget_s"]:
            out["stopped"] = "total time budget used"
            break
        if step >= lim["max_steps"]:
            messages.append({"role": "user", "content": "Step budget used. Reply now with the finish JSON object."})
        try:
            action = llm.next_agent_action(messages, step + 1, lim["max_steps"])
        except Exception as e:  # LLM down -> stop the loop, the audit continues
            out["stopped"] = f"LLM error: {str(e)[:160]}"
            break
        if action.get("tool") == "finish":
            out["summary"] = _clip(action.get("summary", ""), 3000)
            out["stopped"] = "model finished"
            break
        if step >= lim["max_steps"]:
            out["stopped"] = "step limit reached"
            break
        step += 1
        rec = {"step": step, "tool": action.get("tool"), "why": action.get("why", ""), "code": "", "ok": False,
               "timed_out": False, "duration_s": 0.0, "stdout": "", "stderr": "", "result": None}
        on_step({"phase": "start", "step": step, "why": rec["why"]})
        if action.get("tool") != "run_python":
            rec["stderr"] = "invalid reply: expected one JSON object with tool run_python or finish"
            messages.append({"role": "assistant", "content": _clip(action.get("raw", ""), 1000)})
        else:
            code = action["code"]
            rec["code"] = _clip(code, lim["max_code"])
            messages.append({"role": "assistant", "content": json.dumps(
                {"tool": "run_python", "why": rec["why"], "code": rec["code"]})})
            if len(code) > lim["max_code"]:
                rec["stderr"] = f"code too long ({len(code)} > {lim['max_code']} chars); not executed"
            else:
                res = runner.run_python(code, {"trades.json": trades}, timeout=lim["step_timeout_s"])
                rec.update(ok=res.ok, timed_out=res.timed_out, duration_s=res.duration_s,
                           stdout=_clip(res.stdout, lim["max_output"]), stderr=_clip(res.stderr, lim["max_output"] // 2),
                           limits=res.limits)
                if res.ok:
                    try:
                        parsed_out = res.json()
                        if len(json.dumps(parsed_out, default=str)) <= lim["max_output"]:
                            rec["result"] = parsed_out
                    except (ValueError, IndexError):
                        pass
        out["steps"].append(rec)
        on_step({"phase": "end", "step": step, "ok": rec["ok"], "why": rec["why"], "duration_s": rec["duration_s"],
                 "timed_out": rec["timed_out"]})
        messages.append({"role": "user", "content":
                         f"TOOL RESULT step {step}: ok={rec['ok']}, timed_out={rec['timed_out']}\n"
                         f"stdout:\n{rec['stdout']}\nstderr:\n{rec['stderr'][-1500:]}"})
    if not out["summary"]:
        n_ok = sum(1 for s in out["steps"] if s["ok"])
        out["summary"] = f"- The agent ran {len(out['steps'])} follow-up analyses ({n_ok} succeeded); see the trace."
    out["duration_s"] = round(time.monotonic() - t0, 3)
    return out


def facts_view(loop: dict) -> dict:
    """Small version for FACTS (fed to the report writer)."""
    return {"summary": loop.get("summary", ""), "stopped": loop.get("stopped", ""),
            "steps": [{"step": s["step"], "why": s["why"], "ok": s["ok"], "timed_out": s["timed_out"],
                       "result": s["result"] if s["result"] is not None else _clip(s["stdout"] or s["stderr"], 600)}
                      for s in loop.get("steps", [])]}
