# BOTLAB Bot Log Auditor: hosted demo (Cloudflare Worker)

This is the public "Try it out" version of the auditor. It runs as a **standalone Cloudflare Worker** called
`botlab-auditor` on `*.workers.dev`. It has no route on botlab.org.uk and no access to any other server.
The VPS that runs the trading bots holds live exchange keys, so a public app that processes uploaded files
is kept away from it on purpose.

Expected URL after deploy: `https://botlab-auditor.<your-account-subdomain>.workers.dev`

## Architecture

```
Browser (public/index.html + app.js + styles.css)
  | POST /api/audit?stream=1   (CSV text <= 2 MB, trials, risk per trade)
  v
Worker (src/app.js), stateless, NDJSON step stream
  1 parse        src/normalize.js  sniffRows: delimiter, quotes, header
  2 normalise    src/normalize.js  Bybit / Binance / generic heuristics (port of csv_normalize.py)
  3 gates        src/vendor/gates.cjs  byte-identical copy of ../vendor/gates.js (G1..G6, G1 floor 50)
  4 cross-check  src/analysis.js   independent recomputation (n, mean, win rate, drawdown); for generic
                                   CSVs the raw text is also re-parsed with gates.js's own parser
  5 analysis     src/analysis.js   months with green/red days, per coin, per side, losing streak
  6 report       src/report.js     env.AI.run('@cf/meta/llama-3.3-70b-instruct-fp8-fast')
                                   -> deterministic template if AI errors or breaks the no-advice guard
POST /api/chat        stateless follow-up: the client sends report + facts + question
POST /api/pro         same pipeline, longer report + monthly / per-coin tables (after unlock)
POST /api/paypal/*    server-side Orders v2 (only when PayPal secrets are set)
GET  /*               static assets via the ASSETS binding, with CSP + security headers
```

**AI model.** Cloudflare Workers AI, `@cf/meta/llama-3.3-70b-instruct-fp8-fast` (const `AI_MODEL` in
`src/report.js`). The prompt gets only FACTS computed by deterministic code, and asks for a plain-English
audit with these sections: verdict, failed gates and why, what to test next, risk warnings. It must not give
financial advice or promise profits. A regex guard rejects replies that read like advice ("guaranteed",
"you should buy" and similar), and the disclaimer is always appended. If the AI binding errors, runs out of
quota, or is unavailable (as in `wrangler dev --local`), the step is marked `warn` and the deterministic
template report is used instead.

**Sandboxed analysis: Python vs hosted.** The Python app in the repo root shows the full agent. There the LLM
writes its own Python analysis code, and `sandbox.py` runs it in an isolated process with no network, a temp
dir and a timeout. A public Worker must never `eval` model-written code. So the hosted demo runs the safe
deterministic subset: the same analysis as the built-in script, written as pure JS functions
(`src/analysis.js`). The gate math is the unchanged open-source `gates.js`.

## Payments (Pro unlock), three modes

| Mode | When | Flow |
|---|---|---|
| `server` | secrets `PAYPAL_CLIENT_ID` and `PAYPAL_CLIENT_SECRET` are set | PayPal JS SDK buttons. **Server-side Orders v2**: `/api/paypal/create`, then the buyer approves in the PayPal popup, then `/api/paypal/capture`. `/api/pro` re-reads the order from `https://api-m.sandbox.paypal.com` and unlocks only if it is `COMPLETED`, the amount is USD `PRO_PRICE_USD`, and `custom_id` equals the SHA-256 fingerprint of the submitted CSV. |
| `sb` (default without secrets) | no secrets, `PAYPAL_MODE = "sb"` | **No PayPal account needed.** The PayPal JS SDK loads with the generic sandbox client id `sb`. The order is created and captured **in the browser** (`actions.order.create` / `actions.order.capture`). The Worker then checks the returned order: id format, `COMPLETED`, capture `COMPLETED`, amount, and that `custom_id` matches the fingerprint. The Worker has no credentials, so it cannot confirm the payment with PayPal. This is a demo-grade unlock, and the UI labels it "Payments demo mode". |
| `mock` | no secrets, `PAYPAL_MODE = "mock"` | No PayPal at all. A "mock unlock" button. |

Only the PayPal **sandbox** is used. There is no live switch. The client id is public by design, so it is sent
to the browser. The secret never is. Pro gives you the extended AI report, the month-by-month table with
green and red days, the per-coin and per-side tables, and a print-friendly page ("Print / save as PDF" uses
`window.print()`).

## Security

- **Stateless.** The CSV exists only in the memory of the request. There are no KV, D1, R2 or Cache writes, and observability logs are disabled in `wrangler.toml`.
- **Limits.** The CSV can be at most 2 MB (checked against Content-Length and the real size, which returns 413). A file can have at most 20,000 trades. A question can be at most 1,000 characters.
- **Rate limit.** Per IP and per isolate, in memory (best effort): 20 audits, 20 Pro runs, 40 chats and 30 PayPal calls per 10 minutes. Going over returns 429.
- **Headers.** A strict CSP is set: `default-src 'self'`, and scripts only from self plus a per-response nonce. The PayPal SDK gets the nonce through `data-csp-nonce`, and scripts can also come from `www.paypal.com`, `www.sandbox.paypal.com` and `*.paypalobjects.com`. Frames are limited to PayPal, and the CSP also sets `frame-ancestors 'none'` and `object-src 'none'`. The other headers are `X-Frame-Options: DENY`, `nosniff` and a strict `Referrer-Policy`.
- **No external scripts except the PayPal SDK.** There is no inline script in the HTML.
- **Prompt hygiene.** Coin names are sanitised before they reach the model. Chat history accepts only `user` and `assistant` roles.

**CPU note.** On the Workers **Free** plan each request gets about 10 ms of CPU. In a local benchmark, a
640-trade log took roughly 10 to 17 ms of CPU, and a 20,000-trade log about 160 ms. Waiting for Workers AI
does not count as CPU. Small samples work on Free. Large uploads may hit "Exceeded CPU limit" there, and the
Workers Paid plan raises the limit.

## Files

```
worker/
  wrangler.toml         name botlab-auditor, workers_dev, [assets], [ai] binding AI, vars
  src/index.js          entry (re-exports app.js)
  src/app.js            router, pipeline, streaming, limits, headers
  src/normalize.js      CSV reader + exchange normaliser (port of csv_normalize.py)
  src/gates.js          ESM wrapper; src/vendor/gates.cjs = unchanged ../vendor/gates.js
  src/analysis.js       deterministic analysis + cross-check
  src/report.js         Workers AI prompts, no-advice guard, template fallback, chat
  src/paypal.js         Orders v2 sandbox client, sb order check, mode selection
  public/               UI + 3 synthetic sample CSVs
  test/*.test.js        node:test (normaliser, gate parity, worker endpoints)
```

## Local development and tests

```powershell
cd C:\Users\Administrator\Desktop\BOTLAB_launch\hackathon_auditor\worker
npm install
npm test                       # 30 tests: normaliser, parity vs gates.js AND gates.py, endpoints
npx wrangler dev --local       # http://127.0.0.1:8787 (AI is remote-only, so the report uses the template fallback)
npx wrangler dev --local --var PAYPAL_MODE:mock   # try the Pro page without PayPal
```

The parity tests check five things:
- `src/vendor/gates.cjs` is byte-identical to `../vendor/gates.js`.
- Every threshold matches `gates.py` (G1 floor is 50).
- The column lists match.
- On the generic samples, the Worker gives the same results as `gates.js check()`.
- On all four samples (Bybit and Binance included, with and without risk conversion), the Worker gives the same results as Python `csv_normalize.normalize` plus `gates.grade`, and every gate detail string is identical.

**Windows note.** If `wrangler dev` crashes with "access violation", the machine's Visual C++ runtime is
too old (2015). Install the latest VC++ Redistributable, or copy a recent `msvcp140.dll`,
`vcruntime140.dll` and `vcruntime140_1.dll` next to `node_modules\@cloudflare\workerd-windows-64\bin\workerd.exe`.

## Deploy (owner)

```powershell
cd C:\Users\Administrator\Desktop\BOTLAB_launch\hackathon_auditor\worker
npm install
npx wrangler login            # once, opens the browser
npx wrangler deploy           # prints https://botlab-auditor.<subdomain>.workers.dev
# optional, only with a PayPal Developer sandbox REST app:
npx wrangler secret put PAYPAL_CLIENT_ID
npx wrangler secret put PAYPAL_CLIENT_SECRET
```

Without the secrets the site works in `sb` mode. To switch to the mock unlock, set `PAYPAL_MODE = "mock"` in
`wrangler.toml` and deploy again. Workers AI needs no key, because the `[ai]` binding uses the account's
free daily allowance.

Educational tool, not financial advice. MIT, (c) 2026 BOTLAB.
