"""LLM provider abstraction.

Providers
  * anthropic          - Anthropic Messages API. Env: ANTHROPIC_API_KEY, optional LLM_MODEL (default claude-sonnet-5-5)
  * openai_compatible  - any /v1/chat/completions endpoint, e.g. Vultr Serverless Inference.
                         Env: LLM_BASE_URL (e.g. https://api.vultrinference.com/v1), LLM_API_KEY, LLM_MODEL
  * mock               - deterministic templates, no network. Default when no key is set.

  * vultr              - alias of openai_compatible with LLM_BASE_URL defaulting to https://api.vultrinference.com/v1

Select with LLM_PROVIDER=anthropic|openai_compatible|vultr|mock (auto-detected if unset).
Keys are read from environment variables only and are never logged or returned to the UI.

MIT License (c) 2026 BOTLAB
"""
from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request

ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
VULTR_BASE_URL = "https://api.vultrinference.com/v1"  # Vultr Serverless Inference (OpenAI-compatible)
ANTHROPIC_DEFAULT_MODEL = "claude-sonnet-5-5"

SYSTEM_PROMPT = """You are BOTLAB Auditor, a careful statistician who audits trading-bot trade logs.
You receive FACTS computed by deterministic code (six statistical gates and a sandboxed analysis).
Rules:
- Use only the numbers in FACTS. Never invent numbers, trades, dates or coins.
- Plain English for a non-statistician. Short sentences. Markdown headings and bullet lists.
- This is an educational audit, NOT financial advice. Never tell the user to buy, sell, invest or
  increase/decrease position size. Never promise or predict profits. Never say a bot "will" make money.
- Explain what each FAILED gate means and why it failed, citing the gate detail.
- Suggest what to TEST next (more trades, out-of-sample period, per-side review, cost modelling, look-ahead checks).
- Mention the tool's limits: it cannot see the bot's code, so it cannot detect look-ahead bias or wrong cost modelling.
- End with the line: "Educational audit only. Not financial advice. Past results do not predict future results."
"""

REPORT_SECTIONS_BASIC = "## Verdict\n## Gates that failed and why\n## What to test next\n## Risk warnings"
REPORT_SECTIONS_PRO = (REPORT_SECTIONS_BASIC + "\n## Month-by-month review\n## Per-coin and per-side review\n"
                       "## Robustness checklist (10 items)\n## Questions to ask about the bot's code")

ANALYSIS_CODE_PROMPT = """Write a short Python 3 script (standard library only: json, math, statistics, collections, datetime)
that reads ./trades.json (a list of objects with keys v (float result), sym (str), day (YYYY-MM-DD or ''),
side ('L','S' or '')) and prints ONE JSON object to stdout with keys:
"monthly": list of {"month","n","sum","win_days","loss_days"}, "by_side": {"L":{"n","sum"},"S":{"n","sum"}},
"by_symbol": list of {"sym","n","sum"} sorted by sum desc, "longest_losing_streak": int.
No network, no files other than trades.json, no subprocess. Output only the code in one ```python block."""

DISCLAIMER = "Educational audit only. Not financial advice. Past results do not predict future results."


class LLMError(RuntimeError):
    pass


def _http_post_json(url: str, headers: dict, body: dict, timeout: float = 60.0) -> dict:
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST", headers={"Content-Type": "application/json", **headers})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise LLMError(f"LLM HTTP {e.code}: {e.read()[:300].decode('utf-8', 'replace')}") from None
    except urllib.error.URLError as e:
        raise LLMError(f"LLM connection error: {e.reason}") from None


def _facts_json(facts: dict) -> str:
    return json.dumps(facts, ensure_ascii=False, default=str)[:24000]


def _extract_code(text: str) -> str:
    m = re.search(r"```(?:python)?\s*\n(.*?)```", text, re.S)
    return (m.group(1) if m else text).strip()


def _extract_json(text: str):
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except ValueError:
        return None


class BaseLLM:
    name = "base"
    model = ""
    transport = staticmethod(_http_post_json)  # tests may replace

    def complete(self, system: str, messages: list[dict], max_tokens: int = 1500) -> str:
        raise NotImplementedError

    # ---- agent skills ----
    def write_report(self, facts: dict, pro: bool = False) -> str:
        sections = REPORT_SECTIONS_PRO if pro else REPORT_SECTIONS_BASIC
        if (facts.get("agent") or {}).get("steps"):
            sections += "\n## Agent follow-up analyses"
        prompt = (f"FACTS (JSON):\n{_facts_json(facts)}\n\nWrite the {'PRO (detailed)' if pro else 'standard'} audit report "
                  f"with exactly these sections:\n{sections}\n"
                  f"{'Aim for 700-1000 words.' if pro else 'Aim for 250-400 words.'}")
        text = self.complete(SYSTEM_PROMPT, [{"role": "user", "content": prompt}], max_tokens=2500 if pro else 1200)
        return ensure_disclaimer(text)

    def chat(self, facts: dict, report: str, history: list[dict], message: str) -> str:
        msgs = [{"role": "user", "content": f"FACTS (JSON):\n{_facts_json(facts)}\n\nREPORT:\n{report}\n\n"
                                            "Answer my follow-up questions about this audit."},
                {"role": "assistant", "content": "Understood. Ask me anything about this audit."}]
        msgs += [m for m in history[-8:] if m.get("role") in ("user", "assistant")]
        msgs.append({"role": "user", "content": message[:2000]})
        return ensure_disclaimer(self.complete(SYSTEM_PROMPT, msgs, max_tokens=700))

    def infer_mapping(self, headers: list[str], sample_rows: list[list[str]]):
        prompt = ("A trade-history CSV has these headers:\n" + json.dumps(headers) + "\nSample rows:\n" + json.dumps(sample_rows) +
                  '\nReturn ONLY JSON: {"result_column": <header holding per-trade profit/loss or R multiple>, '
                  '"unit": "R" or "$", "symbol_column": <header or null>, "date_column": <header or null>, '
                  '"side_column": <header or null>}. Use exact header strings.')
        return _extract_json(self.complete("You map CSV columns. Reply with JSON only.", [{"role": "user", "content": prompt}], 300))

    def write_analysis_code(self) -> str:
        return _extract_code(self.complete("You write small, safe, standard-library Python scripts.",
                                           [{"role": "user", "content": ANALYSIS_CODE_PROMPT}], 1200))

    # ---- agentic loop (tool-call style JSON protocol, see agent_loop.py) ----
    def next_agent_action(self, messages: list[dict], step: int, max_steps: int) -> dict:
        """Return {"tool": "run_python", "why", "code"} or {"tool": "finish", "summary"}.

        `messages` is the running conversation built by agent_loop (user/assistant turns)."""
        text = self.complete(AGENT_SYSTEM_PROMPT.format(max_steps=max_steps), messages, max_tokens=1500)
        return parse_agent_action(text)


AGENT_SYSTEM_PROMPT = """You are the BOTLAB audit agent. Six statistical gates have already been computed (FACTS).
You may now run up to {max_steps} short follow-up analyses on the trade log to check the weakest points
(e.g. edge decay over time, dependence on the best coin or days, drawdown under reshuffled trade order,
losing side, streaks). Each analysis is Python code executed in an isolated sandbox.

Reply with EXACTLY ONE JSON object and nothing else, one of:
{{"tool": "run_python", "why": "<one sentence: what you check and why>", "code": "<python script>"}}
{{"tool": "finish", "summary": "<3-6 short bullet lines of plain-English findings>"}}

Sandbox rules for code: Python 3 standard library only (json, math, statistics, collections, random,
itertools, datetime). No network, no subprocess, no files except ./trades.json (a list of objects with keys
v (float result), sym (str), day ('YYYY-MM-DD' or ''), side ('L', 'S' or '')). Print ONE small JSON object
to stdout (under 4000 characters). Time limit a few seconds per run. Use a fixed random seed.
In the summary use only numbers that appear in FACTS or in tool results. Educational audit only:
never advise buying, selling or sizing, never predict profits."""


def _first_json_object(text: str):
    dec = json.JSONDecoder()
    for i, ch in enumerate(text or ""):
        if ch == "{":
            try:
                obj, _ = dec.raw_decode(text[i:])
            except ValueError:
                continue
            if isinstance(obj, dict):
                return obj
    return None


def parse_agent_action(text: str) -> dict:
    """Parse a model reply into an agent action. Never raises; unknown replies become {"tool": "invalid"}."""
    obj = _first_json_object(text)
    if isinstance(obj, dict):
        tool = str(obj.get("tool") or obj.get("action") or obj.get("name") or "").strip().lower()
        args = obj.get("arguments") if isinstance(obj.get("arguments"), dict) else obj
        if tool == "run_python" and isinstance(args.get("code"), str) and args["code"].strip():
            return {"tool": "run_python", "why": str(args.get("why") or "")[:300], "code": args["code"]}
        if tool == "finish":
            return {"tool": "finish", "summary": str(args.get("summary") or "")[:3000]}
    if text and "```" in text:  # tolerate a bare fenced code block
        code = _extract_code(text)
        if code:
            return {"tool": "run_python", "why": "(model replied with a code block)", "code": code}
    return {"tool": "invalid", "raw": (text or "")[:500]}


def ensure_disclaimer(text: str) -> str:
    text = (text or "").strip()
    return text if DISCLAIMER.lower() in text.lower() else text + "\n\n_" + DISCLAIMER + "_"


class AnthropicLLM(BaseLLM):
    name = "anthropic"

    def __init__(self, api_key: str, model: str | None = None):
        self._key = api_key
        self.model = model or ANTHROPIC_DEFAULT_MODEL

    def complete(self, system, messages, max_tokens=1500):
        out = self.transport(ANTHROPIC_URL, {"x-api-key": self._key, "anthropic-version": "2023-06-01"},
                             {"model": self.model, "max_tokens": max_tokens, "system": system, "messages": messages})
        parts = [b.get("text", "") for b in out.get("content", []) if b.get("type") == "text"]
        if not parts:
            raise LLMError("Empty response from Anthropic")
        return "".join(parts)


class OpenAICompatibleLLM(BaseLLM):
    name = "openai_compatible"

    def __init__(self, base_url: str, api_key: str, model: str):
        self.base_url = base_url.rstrip("/")
        self._key = api_key
        self.model = model

    @property
    def endpoint_label(self) -> str:
        return "Vultr Serverless Inference" if "vultrinference.com" in self.base_url else "OpenAI-compatible endpoint"

    def complete(self, system, messages, max_tokens=1500):
        out = self.transport(self.base_url + "/chat/completions", {"Authorization": f"Bearer {self._key}"},
                             {"model": self.model, "max_tokens": max_tokens, "temperature": 0.2,
                              "messages": [{"role": "system", "content": system}] + messages})
        try:
            return out["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            raise LLMError("Unexpected response from OpenAI-compatible endpoint") from None


# ---------------------------------------------------------------- mock
MOCK_ANALYSIS_CODE = r'''
import json, collections
trades = json.load(open("trades.json"))
months = collections.OrderedDict()
days = collections.defaultdict(float)
for t in trades:
    m = (t.get("day") or "")[:7] or "unknown"
    months.setdefault(m, {"month": m, "n": 0, "sum": 0.0})
    months[m]["n"] += 1
    months[m]["sum"] += t["v"]
    if t.get("day"):
        days[t["day"]] += t["v"]
for m in months.values():
    m["win_days"] = sum(1 for d, s in days.items() if d.startswith(m["month"]) and s > 0)
    m["loss_days"] = sum(1 for d, s in days.items() if d.startswith(m["month"]) and s < 0)
    m["sum"] = round(m["sum"], 4)
by_side = {}
for k in ("L", "S"):
    xs = [t["v"] for t in trades if t.get("side") == k]
    by_side[k] = {"n": len(xs), "sum": round(sum(xs), 4)}
by_sym = collections.defaultdict(lambda: [0, 0.0])
for t in trades:
    by_sym[t.get("sym") or "?"][0] += 1
    by_sym[t.get("sym") or "?"][1] += t["v"]
by_symbol = sorted(({"sym": s, "n": a[0], "sum": round(a[1], 4)} for s, a in by_sym.items()), key=lambda r: -r["sum"])
streak = best = 0
for t in trades:
    streak = streak + 1 if t["v"] < 0 else 0
    best = max(best, streak)
print(json.dumps({"monthly": list(months.values()), "by_side": by_side, "by_symbol": by_symbol, "longest_losing_streak": best}))
'''


class MockLLM(BaseLLM):
    """Deterministic, offline. Produces a template report from the FACTS only."""
    name = "mock"
    model = "mock-template-v1"

    def complete(self, system, messages, max_tokens=1500):
        return "Mock provider: no free-form completion available."

    def infer_mapping(self, headers, sample_rows):
        return None  # mock cannot guess; heuristics must succeed

    def write_analysis_code(self) -> str:
        return MOCK_ANALYSIS_CODE.strip()

    def next_agent_action(self, messages, step, max_steps):
        """Deterministic plan: the follow-ups in MOCK_AGENT_PLAN, then finish with a summary built from
        the tool results the loop fed back (so the summary only cites computed numbers)."""
        if step <= min(max_steps, len(MOCK_AGENT_PLAN)):
            why, code = MOCK_AGENT_PLAN[step - 1]
            return {"tool": "run_python", "why": why, "code": code.strip()}
        return {"tool": "finish", "summary": _mock_agent_summary(messages)}

    def write_report(self, facts, pro=False):
        g = facts["gates"]
        unit = facts["metrics"]["unit"]
        failed = [k for k, v in g.items() if v["pass"] is False]
        unscored = [k for k, v in g.items() if v["pass"] is None]
        m = facts["metrics"]
        L = ["## Verdict", "",
             f"**{facts['verdictText']}**", "",
             f"The log has {m['n']} trades with an average of {m['mean']:+.3f} {unit} per trade, a win rate of "
             f"{m['wr']:.0f}% and a maximum drawdown of {m['dd']:.1f} {unit}. "
             f"{facts['passed']} of {facts['total']} scored gates passed.", ""]
        L += ["## Gates that failed and why", ""]
        if not failed:
            L.append("- None of the scored gates failed.")
        for k in failed:
            L.append(f"- **{k} {g[k]['title']}**: {g[k]['detail']}. {GATE_EXPLAIN[k]}")
        for k in unscored:
            L.append(f"- {k} {g[k]['title']} could not be scored: {g[k]['detail']}.")
        L += ["", "## What to test next", ""]
        nexts = [NEXT_STEP[k] for k in failed] or ["Keep paper trading in parallel and re-run this audit every 100 new trades."]
        nexts.append("Re-run the strategy on a period it was never tuned on (out-of-sample) and compare.")
        nexts.append("Check the bot's code for look-ahead bias and realistic fees, slippage and funding; this audit cannot see code.")
        L += [f"- {s}" for s in nexts]
        ag = facts.get("agent") or {}
        if ag.get("steps"):
            ok = sum(1 for st in ag["steps"] if st.get("ok"))
            L += ["", "## Agent follow-up analyses", "",
                  f"The agent ran {len(ag['steps'])} follow-up analyses in the sandbox ({ok} succeeded).", ""]
            L += [ag.get("summary") or "- No summary."]
        L += ["", "## Risk warnings", ""]
        warns = list(facts.get("flags") or [])
        if unit != "R":
            warns.append("Results are in currency, not R multiples, so drawdown (G6) was not scored. Provide risk per trade to convert.")
        warns.append("A statistical pass is not a guarantee. Markets change and edges decay.")
        L += [f"- {w}" for w in warns]
        if pro:
            L += self._pro_sections(facts)
        return ensure_disclaimer("\n".join(L))

    def _pro_sections(self, facts):
        a = facts.get("analysis") or {}
        unit = facts["metrics"]["unit"]
        L = ["", "## Month-by-month review", ""]
        monthly = a.get("monthly") or []
        if monthly:
            L += ["| Month | Trades | Result | Green days | Red days |", "|---|---|---|---|---|"]
            L += [f"| {r['month']} | {r['n']} | {r['sum']:+.2f} {unit} | {r.get('win_days', '-')} | {r.get('loss_days', '-')} |" for r in monthly]
            red = [r["month"] for r in monthly if r["sum"] < 0]
            L += ["", f"{len(monthly) - len(red)} of {len(monthly)} months were net positive."
                  + (f" Losing months: {', '.join(red)}." if red else "")]
        else:
            L.append("No date column, so a monthly breakdown is not possible.")
        L += ["", "## Per-coin and per-side review", ""]
        bs = a.get("by_side") or {}
        for k, name in (("L", "Longs"), ("S", "Shorts")):
            if bs.get(k, {}).get("n"):
                L.append(f"- {name}: {bs[k]['n']} trades, total {bs[k]['sum']:+.2f} {unit}.")
        syms = a.get("by_symbol") or []
        if syms:
            L.append(f"- Best coins: {', '.join(s['sym'] + ' ' + format(s['sum'], '+.2f') for s in syms[:3])}.")
            L.append(f"- Worst coins: {', '.join(s['sym'] + ' ' + format(s['sum'], '+.2f') for s in syms[-3:])}.")
        if "longest_losing_streak" in a:
            L.append(f"- Longest losing streak: {a['longest_losing_streak']} trades in a row.")
        L += ["", "## Robustness checklist (10 items)", ""]
        L += [f"{i}. {s}" for i, s in enumerate(CHECKLIST, 1)]
        L += ["", "## Questions to ask about the bot's code", ""]
        L += [f"- {q}" for q in CODE_QUESTIONS]
        return L

    def chat(self, facts, report, history, message):
        q = message.lower()
        g = facts["gates"]
        for k in g:
            if k.lower() in q:
                v = g[k]
                state = "passed" if v["pass"] else ("could not be scored" if v["pass"] is None else "failed")
                why = GATE_EXPLAIN[k] if v["pass"] is False else "What it checks: " + GATE_CHECKS[k]
                return ensure_disclaimer(f"{k} ({v['title']}) {state}: {v['detail']}. {why}")
        if "dsr" in q or "luck" in q or "sharpe" in q:
            return ensure_disclaimer(GATE_EXPLAIN["G2"])
        if "next" in q or "test" in q:
            return ensure_disclaimer("Next: collect more trades, test out-of-sample, and audit the code for look-ahead bias and costs.")
        return ensure_disclaimer("(Mock mode) I can explain any gate: ask e.g. 'why did G1 fail?' or 'what is DSR?'. "
                                 "Set ANTHROPIC_API_KEY or LLM_BASE_URL/LLM_API_KEY for free-form answers.")


MOCK_AGENT_PLAN = [
    ("Check whether the edge decays over time: expectancy per consecutive block of trades.", r'''
import json
t = [x["v"] for x in json.load(open("trades.json"))]
k = max(10, len(t) // 5)
blocks = [t[i:i + k] for i in range(0, len(t), k)]
means = [round(sum(b) / len(b), 4) for b in blocks if b]
print(json.dumps({"check": "edge_decay", "block_size": k, "block_means": means,
                  "first_half_mean": round(sum(t[:len(t)//2]) / max(1, len(t)//2), 4),
                  "second_half_mean": round(sum(t[len(t)//2:]) / max(1, len(t) - len(t)//2), 4)}))
'''),
    ("Check dependence on the single best coin and the two best days: total with them removed.", r'''
import json, collections
tr = json.load(open("trades.json"))
tot = sum(x["v"] for x in tr)
by_sym = collections.Counter(); by_day = collections.Counter()
for x in tr:
    by_sym[x.get("sym") or "?"] += x["v"]
    if x.get("day"):
        by_day[x["day"]] += x["v"]
best_sym = max(by_sym, key=by_sym.get) if by_sym else None
best_days = [d for d, _ in by_day.most_common(2)]
print(json.dumps({"check": "concentration", "total": round(tot, 4), "best_coin": best_sym,
                  "total_without_best_coin": round(tot - by_sym.get(best_sym, 0.0), 4),
                  "best_days": best_days,
                  "total_without_best_2_days": round(tot - sum(by_day[d] for d in best_days), 4)}))
'''),
    ("Stress the drawdown: reshuffle the trade order 500 times (seed 7) and read the 95th percentile.", r'''
import json, random
t = [x["v"] for x in json.load(open("trades.json"))]
def mdd(xs):
    peak = cur = dd = 0.0
    for v in xs:
        cur += v; peak = max(peak, cur); dd = max(dd, peak - cur)
    return dd
rng = random.Random(7)
sims = []
for _ in range(500):
    s = t[:]; rng.shuffle(s); sims.append(mdd(s))
sims.sort()
print(json.dumps({"check": "drawdown_reshuffle", "runs": 500, "actual_max_dd": round(mdd(t), 4),
                  "median_max_dd": round(sims[len(sims)//2], 4), "p95_max_dd": round(sims[int(0.95 * len(sims)) - 1], 4)}))
'''),
]


def _mock_agent_summary(messages) -> str:
    res = {}
    for m in messages:
        if m.get("role") == "user" and str(m.get("content", "")).startswith("TOOL RESULT"):
            obj = _first_json_object(m["content"].split("stdout:", 1)[-1])
            if isinstance(obj, dict) and obj.get("check"):
                res[obj["check"]] = obj
    L = []
    d = res.get("edge_decay")
    if d:
        trend = "lower" if d["second_half_mean"] < d["first_half_mean"] else "not lower"
        L.append(f"- Edge over time: the second half averages {d['second_half_mean']:+.3f} per trade vs "
                 f"{d['first_half_mean']:+.3f} in the first half ({trend}).")
    c = res.get("concentration")
    if c:
        L.append(f"- Concentration: total {c['total']:+.2f}; without the best coin ({c['best_coin']}) "
                 f"{c['total_without_best_coin']:+.2f}; without the two best days {c['total_without_best_2_days']:+.2f}.")
    s = res.get("drawdown_reshuffle")
    if s:
        L.append(f"- Drawdown stress: actual max drawdown {s['actual_max_dd']:.2f}; across {s['runs']} reshuffled "
                 f"orders the median is {s['median_max_dd']:.2f} and the 95th percentile {s['p95_max_dd']:.2f}.")
    if not L:
        L.append("- No follow-up analysis produced a usable result.")
    return "\n".join(L)


GATE_EXPLAIN = {
    "G1": "There are not enough trades yet to be 95% confident the true edge is above zero (Minimum Track Record Length).",
    "G2": "The Probabilistic and Deflated Sharpe ratios say the result could plausibly be luck, especially after counting how many variants were tried.",
    "G3": "Recent trades are not net positive, so the edge may have faded.",
    "G4": "Too much of the profit comes from a few coins or a few days; remove them and the result may disappear.",
    "G5": "One side (long or short) has many trades and loses on average; it may be dragging the bot down.",
    "G6": "The worst peak-to-trough drawdown is larger than 15R, which is hard to sit through.",
}
GATE_CHECKS = {
    "G1": "whether there are enough trades to be 95% confident the true edge is above zero (at least 50 and at least MinTRL).",
    "G2": "whether the Sharpe ratio is unlikely to be luck: PSR(0) >= 0.95 and Deflated Sharpe >= 0.5 after counting the variants tried.",
    "G3": "whether the last 100 trades are still net positive and the last 30 are not clearly negative.",
    "G4": "whether the top 3 coins make under 50% of gross profit and the best 2 days under 50% of net profit.",
    "G5": "whether a side (long or short) with 50+ trades loses on average.",
    "G6": "whether the worst peak-to-trough drawdown stays under 15R.",
}
NEXT_STEP = {
    "G1": "Collect more trades (paper or tiny size) until the sample reaches the 'need' figure in G1.",
    "G2": "Count honestly how many variants were tried and re-check; test the final rules on fresh data.",
    "G3": "Compare the last 100 trades with earlier ones by market regime to see what changed.",
    "G4": "Re-run the gates with the top coins or best days removed and see if anything is left.",
    "G5": "Audit the losing side separately; check whether its losses come from one market regime.",
    "G6": "Inspect the drawdown period trade by trade: clustered losses, correlated positions, or a regime change?",
}
CHECKLIST = [
    "Fees, slippage and funding modelled at realistic levels",
    "No future data used in signals (look-ahead)",
    "Out-of-sample period never used for tuning",
    "Number of variants tried recorded (for DSR)",
    "Results stable when the best coin is removed",
    "Results stable when the best week is removed",
    "Both long and short sides reviewed separately",
    "Paper/live fills compared with backtest fills",
    "Behaviour in bull, bear and sideways regimes compared",
    "Kill-switch / max-drawdown rule defined before going live",
]
CODE_QUESTIONS = [
    "Are signals computed on closed candles only?",
    "Are stop-loss and take-profit fills assumed at exact prices, or with slippage?",
    "Were any parameters chosen after looking at this same period?",
    "Does the coin universe include coins that were delisted (survivorship)?",
]


def get_llm(provider: str | None = None) -> BaseLLM:
    provider = (provider or os.environ.get("LLM_PROVIDER") or "").strip().lower()
    if not provider:
        if os.environ.get("ANTHROPIC_API_KEY"):
            provider = "anthropic"
        elif os.environ.get("LLM_BASE_URL") and os.environ.get("LLM_API_KEY"):
            provider = "openai_compatible"
        else:
            provider = "mock"
    if provider == "anthropic":
        key = os.environ.get("ANTHROPIC_API_KEY")
        if not key:
            raise LLMError("LLM_PROVIDER=anthropic but ANTHROPIC_API_KEY is not set")
        return AnthropicLLM(key, os.environ.get("LLM_MODEL") or ANTHROPIC_DEFAULT_MODEL)
    if provider == "vultr":  # convenience alias: OpenAI-compatible client pointed at Vultr Serverless Inference
        key = os.environ.get("LLM_API_KEY") or os.environ.get("VULTR_INFERENCE_API_KEY")
        model = os.environ.get("LLM_MODEL")
        if not (key and model):
            raise LLMError("LLM_PROVIDER=vultr needs LLM_API_KEY (Vultr inference key) and LLM_MODEL")
        return OpenAICompatibleLLM(os.environ.get("LLM_BASE_URL") or VULTR_BASE_URL, key, model)
    if provider == "openai_compatible":
        base, key, model = os.environ.get("LLM_BASE_URL"), os.environ.get("LLM_API_KEY"), os.environ.get("LLM_MODEL")
        if not (base and key and model):
            raise LLMError("openai_compatible needs LLM_BASE_URL, LLM_API_KEY and LLM_MODEL")
        return OpenAICompatibleLLM(base, key, model)
    return MockLLM()
