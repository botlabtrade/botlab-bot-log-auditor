# BOTLAB Bot Log Auditor

An AI agent that audits a trading bot's real trade history. Upload an exchange CSV and the agent
maps the columns, runs six statistical gates inside a code sandbox, cross-checks them against the
open-source reference implementation, has the LLM write and run its own analysis code in the
sandbox, and then writes an honest, plain-English audit report you can ask follow-up questions about.
The paid "Pro audit" (PayPal Sandbox) adds a longer report and a PDF export.

> Educational tool. Not financial advice. It never tells you to trade, and it never promises profits.

**Live demo:** https://botlab-auditor.botsite.workers.dev · **Video (2:45):** https://youtu.be/oQc5DGtB8RY

## How PayPal and AI are used (PayPal AI Hackathon)

| Tool | Where | What it does |
|---|---|---|
| **PayPal JavaScript SDK** (sandbox) | `worker/public/app.js`, `worker/src/paypal.js` | Renders PayPal checkout buttons that unlock the Pro audit for USD 4.99 of sandbox (test) money. The hosted demo uses the public sandbox client id `sb` (order created and captured in the browser, then verified by the Worker). |
| **PayPal Orders v2 REST API** (sandbox) | `worker/src/paypal.js`, `payments_paypal.py` | Server-side create order → buyer approves → server capture → Pro unlocked. Active when `PAYPAL_CLIENT_ID` / `PAYPAL_CLIENT_SECRET` sandbox secrets are set (`npx wrangler secret put …`). |
| **Cloudflare Workers AI – Llama 3.3 70B Instruct** | `worker/src/report.js` | Writes the plain-English audit (verdict, failed gates and why, what to test next, risk warnings) and answers follow-up questions, grounded in the gate numbers. Falls back to a deterministic template if the model errors or drifts into advice. |
| **Python agent + sandbox** (any LLM: Anthropic or OpenAI-compatible) | `agent.py`, `sandbox.py`, `llm.py` | Full agent version: the LLM maps unknown CSV columns and writes analysis code that runs in a restricted sandbox (no network, no subprocess, temp dir, timeout). |
| **Cloudflare Workers** | `worker/` | Stateless hosting; uploaded files are processed in memory and never stored. |

Setup: hosted version → see [`worker/README.md`](worker/README.md) (`npm install`, `npx wrangler deploy`); Python version → [Run locally](#run-locally) below.

![screenshot](docs/screenshots/audit_desktop_dark.png)

## The problem

Most retail trading bots that look profitable aren't. They were tested on 40 trades, tuned across 50
variants, carried by one coin or one lucky day, or they stopped working months ago. Exchange
dashboards show PnL. They don't tell you whether that PnL could be luck. Statistics like the
Deflated Sharpe Ratio and Minimum Track Record Length answer that question, but few traders can
compute or read them. This project turns those statistics into a short audit that anyone can read.

## How it works

```
 Browser (static/index.html)
   | upload / paste CSV, trials, risk per trade
   v
 app.py  (stdlib http.server, JSON API, in-memory jobs, progress polling)
   |
   v
 agent.py  ── step 1  csv_normalize.py   Bybit / Binance / generic column mapping
   |                                     (LLM proposes a mapping only if the heuristics fail)
   |          step 2  gates.py           six gates, a Python port of gates.js
   |          step 3  sandbox.py ──────> node vendor/gates.js   (original code, cross-check)
   |          step 4  llm.py   writes Python analysis code ──> sandbox.py (python -I -S)
   |                           (built-in script used if the generated code fails)
   |          step 5  agent_loop.py  the LLM picks up to 4 follow-up analyses ("run_python" tool calls),
   |                           each runs in the sandbox / runner container, results are fed back
   |          step 6  llm.py   writes the report from computed FACTS only
   v
 llm.py  providers: anthropic | openai_compatible (Vultr Serverless Inference) | mock (offline)
 payments_paypal.py  PayPal REST v2 Orders, sandbox only: create -> approve -> capture -> unlock Pro
 pdf_export.py       dependency-free PDF of the Pro report
```

**Sandbox (`sandbox.py`).** Every piece of executed code runs as a separate process:
- in a fresh temp directory that is deleted afterwards
- with a stripped environment, so no API keys or PayPal secrets
- under a hard timeout that kills the process tree
- with stdout/stderr capped
- with the network disabled

Python runs isolated (`-I -S -B`) behind a prelude that blocks sockets, subprocess, ctypes and file
access outside the work dir. Node runs under the permission model (`--permission`, fs read limited to
the temp dir, no child processes) with a preload that stubs net/http/https/dns/fetch. On Linux, rlimits
also cap memory, CPU, open files and file size. This is defence in depth for a demo. A public
deployment should also run the app in a container or microVM.

**The six gates** (from botlab-check-your-bot): G1 sample size vs MinTRL, G2 PSR/DSR (luck after N
trials), G3 still working recently, G4 not reliant on a few coins or days, G5 no losing side, and
G6 drawdown under 15R.

## Vultr version (agent loop + isolated runner)

- **Agentic loop** (`agent_loop.py`): after the gates, the LLM may call `run_python` up to `AGENT_MAX_STEPS`
  (default 4) times with a plain-JSON protocol (`{"tool": "run_python", "why", "code"}` or `{"tool": "finish",
  "summary"}`). Hard limits: steps, per-step timeout, output size, code size, total time budget. Every step is
  streamed to the UI. The mock provider runs a deterministic 3-step plan, so it all works offline.
- **Runner container** (`vultr/runner/runner_server.py`, `runner_client.py`): when `RUNNER_URL` is set,
  model-written code runs in a separate container with no network, a read-only fs, tmpfs /tmp, a non-root user,
  dropped capabilities and mem/CPU/pids limits. The app reaches it over a unix socket. Without `RUNNER_URL`, the
  in-process sandbox is used (local dev, Windows).
- **Vultr Serverless Inference**: `LLM_PROVIDER=openai_compatible` (or `vultr`), `LLM_BASE_URL=https://api.vultrinference.com/v1`.
- **Deploy**: `vultr/docker-compose.yml` (app + runner + Caddy with automatic HTTPS), `vultr/deploy.sh`,
  step-by-step guide `vultr/DEPLOY.md`, notes and sources `vultr/VULTR_NOTES.md`.

## Run locally

```bash
pip install -r requirements.txt   # only pytest; the app itself is standard library
python app.py                     # http://127.0.0.1:8000
python -m pytest -q               # tests
```

Python 3.10+ is required. Node.js 18+ is optional: it's needed for the sandboxed gates.js cross-check,
and that step is skipped if Node isn't installed. With no environment variables set, the app runs fully
offline with the **mock** LLM and payments disabled. Try the samples in `samples/`, which are all
synthetic.

## Environment variables

The app does not read `.env` files. Export the variables in your shell. `.env.example` lists them all.

| Variable | Purpose |
|---|---|
| `LLM_PROVIDER` | `mock`, `anthropic` or `openai_compatible`. Auto-detected if unset |
| `ANTHROPIC_API_KEY` | Anthropic key. The model defaults to `claude-sonnet-5-5` |
| `LLM_BASE_URL`, `LLM_API_KEY`, `LLM_MODEL` | Any OpenAI-compatible endpoint, e.g. Vultr Serverless Inference `https://api.vultrinference.com/v1` |
| `PAYPAL_CLIENT_ID`, `PAYPAL_CLIENT_SECRET` | PayPal Developer **sandbox** REST app credentials |
| `PAYPAL_MODE` | `sandbox` (the default when keys are set), `mock` (offline demo of the flow) or `disabled` |
| `PRO_PRICE_USD` | Default `4.99` |
| `PUBLIC_BASE_URL` | Base URL PayPal redirects back to (default `http://127.0.0.1:8000`) |
| `HOST`, `PORT` | Default `127.0.0.1:8000` |
| `AGENT_MAX_STEPS`, `AGENT_STEP_TIMEOUT_S`, `AGENT_MAX_OUTPUT`, `AGENT_MAX_CODE`, `AGENT_TOTAL_BUDGET_S` | Agent loop limits (defaults 4, 6, 4000, 8000, 90; `AGENT_MAX_STEPS=0` disables it) |
| `RUNNER_URL`, `RUNNER_TOKEN`, `RUNNER_FALLBACK_LOCAL` | Isolated runner (`unix:///run/botlab/runner.sock` or `http://host:port`). Fails closed unless the fallback is `1` |
| `MAX_RUNNING_JOBS` | Concurrent audits (default 4); extra requests get HTTP 429 |

Keys are read only from the environment. They are never logged, never returned to the browser and
never passed to sandboxed code.

## New vs reused

**Reused**, from [botlab-check-your-bot](../github_repo) (MIT, (c) 2026 BOTLAB):
- `vendor/gates.js`, copied unchanged
- the two `samples/synthetic_42_*` and `synthetic_640_*` example CSVs

This project builds on botlab-check-your-bot.

**New for this project** (written October 2026):
- `gates.py`, a numerically identical Python port (verified against gates.js in tests)
- the agent pipeline
- exchange CSV normalisation (Bybit, Binance, generic, LLM fallback)
- the sandbox
- the LLM provider layer and report and chat prompts
- PayPal Sandbox payments
- the PDF export
- the web UI
- the synthetic exchange samples
- the tests

## Tests

`python -m pytest -q` runs the test suite:
- parsing variants
- parity with gates.js on the example CSVs and the normalised exchange exports
- sandbox timeout, network, fs, subprocess and env isolation
- the mock report, determinism, and fallbacks
- Anthropic and OpenAI-compatible request shapes (mocked HTTP)
- the PayPal create/capture flow (mocked HTTP), sandbox-only enforcement, and the PDF
- the agent loop (step limit, timeout, output cap, invalid replies, LLM failure), the runner service and client,
  and static checks of the Vultr Docker files

## Limitations and disclaimer

The auditor sees trades, not code. It cannot detect look-ahead bias, unrealistic fills or wrong cost
modelling. A passing audit is not evidence that a strategy will keep working. This software is for
education. It is not investment advice, and nothing in it is a recommendation to buy or sell anything.
Past results do not predict future results.

## License

MIT, (c) 2026 BOTLAB. See `LICENSE`.
