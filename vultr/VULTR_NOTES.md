# Vultr notes (checked 12 Oct 2026)

What the Vultr version uses, what was checked in public docs, and what is still **unverified**.
Nothing here was tested against a live Vultr account: no key, no paid call, no instance was created.

## 1. Serverless Inference (LLM)

| Item | Finding | Status | Source |
|---|---|---|---|
| Base URL | `https://api.vultrinference.com/v1` | Docs show `.../v1/chat/completions` and `.../v1/models` | [Tool calling guide](https://docs.vultr.com/how-to-use-tool-calling-with-vultr-serverless-inference) (updated 3 Nov 2025); [Chat endpoint](https://docs.vultr.com/products/compute/serverless-inference/management/usage/chat) |
| Chat endpoint | `POST /v1/chat/completions`, OpenAI-style body (`model`, `messages`, `max_tokens`, `temperature`) | Docs show it; our client already sends exactly this | same |
| Auth header | `Authorization: Bearer <inference API key>` | Docs show it | Tool calling guide |
| Which key | The **inference subscription key**, not the main Vultr account API key. Shown in the portal (Serverless > Inference > subscription) or via `GET https://api.vultr.com/v2/inference/{id}` | Docs show it; portal path not checked by hand | [Chat endpoint](https://docs.vultr.com/products/compute/serverless-inference/management/usage/chat) |
| Model list | `GET /v1/models` with the same bearer key | Docs show it | same |
| Native tool calling | Supported (`tools`, `tool_choice`, `tool_calls`, `role: "tool"`) | Docs show it. **We don't depend on it**: the agent uses a plain-JSON protocol that works with any chat model | Tool calling guide |
| Chat models | `kimi-k2-instruct` appears in the Vultr guide. Third-party price lists (Oct 2026) also show Kimi K2.6, GLM-5 / 5.2, MiniMax-M2.7, Qwen3.6 27B, MiMo-V2.6 Flash/Pro, DeepSeek V3.2 | **UNVERIFIED**: the exact model ids must come from `GET /v1/models` on the day | [typingmind Vultr estimator](https://custom.typingmind.com/tools/estimate-llm-usage-costs/vultr) (third party) |
| Pricing | Product page: from $10/month with 50M tokens, overage $0.0002 per 1K tokens. A Mar 2026 doc gives $0.55 / $2.75 per 1M input/output tokens. These conflict | **UNVERIFIED**: check the portal before the event; hackathon credits may cover it | [vultr.com/products/cloud-inference](https://www.vultr.com/products/cloud-inference/), [usage & cost doc](https://docs.vultr.com/support/products/serverless/how-do-i-monitor-the-usage-and-cost-of-my-vultr-serverless-inference-subscription), [overage doc](https://docs.vultr.com/support/products/serverless/what-happens-if-i-exceed-the-included-tokens-in-my-vultr-serverless-inference-subscription) |

**Model choice (to do on 3 Nov):** call `GET /v1/models`, then pick an instruct/chat model that writes good
Python and follows JSON output well (a Kimi K2 or Qwen coder-class model if it's listed). Put its exact id
in `LLM_MODEL`. Avoid "reasoning" models that wrap answers in long thinking text. The parser tolerates
extra text around the JSON, but they cost more tokens.

**Check the key from the server, not from chat** (owner types the key into `vultr/.env` only):
```bash
set -a; . /opt/botlab-auditor/vultr/.env; set +a
curl -s https://api.vultrinference.com/v1/models -H "Authorization: Bearer $LLM_API_KEY" | head -c 2000
```

### App configuration
```
LLM_PROVIDER=openai_compatible        # or the alias: vultr (base URL then defaults to Vultr)
LLM_BASE_URL=https://api.vultrinference.com/v1
LLM_API_KEY=<inference key, typed by the owner into vultr/.env>
LLM_MODEL=<id from /v1/models>
```
The UI pill then shows "LLM: Vultr Serverless Inference" (`OpenAICompatibleLLM.endpoint_label`).

**Token budget per audit (estimate, unverified):** 1 analysis-code call, up to 5 agent calls (4 steps plus a
finish call), 1 report call, and optional chat. That is roughly 25–45K tokens per audit. With
`MAX_RUNNING_JOBS=3` and `AGENT_MAX_STEPS=4`, a public demo should stay well inside a 50M-token allowance.

## 2. Cloud Compute (hosting)

| Item | Finding | Status | Source |
|---|---|---|---|
| Smallest plan that fits | 1 vCPU / 1 GB RAM / 25 GB SSD (Regular Performance), about $5/month ($0.007/h). Three containers use about 250–400 MB at idle, and the deploy script adds 1 GB swap for the build. **1 vCPU / 2 GB (~$10/month) is the safer choice** if 1 GB is unavailable or the build runs out of memory | **UNVERIFIED**: third-party trackers, and one says Regular plans were removed. Check vultr.com/pricing on the day | [costbench](https://www.costbench.com/software/cloud-infrastructure/vultr/), [getpulsesignal](https://getpulsesignal.com/changes/vultr) |
| OS | Ubuntu 24.04 LTS x64 | standard Vultr image | Vultr deploy page |
| Billing | Hourly while the instance **exists**, even when it's stopped. Destroy the instance to stop paying | General Vultr behaviour; **verify** | Vultr billing FAQ |

## 3. Hackathon

| Item | Finding | Status | Source |
|---|---|---|---|
| Event | "Vultr: Agent Rush Hackathon", hybrid. Online 3–8 Nov 2026, registration opens 3 Nov 15:00 UTC, submissions close 8 Nov 22:00 UTC | lablab page as indexed | [lablab.ai/ai-hackathons/vultr-hackathon](https://lablab.ai/ai-hackathons/vultr-hackathon/live) |
| Tracks / prizes / credits | "Announced soon"; "SDKs & credits included" | **UNVERIFIED** | same |
| Theme fit | Agents with sandboxed code execution (e.g. "CSV Analyst") | from the brief | — |

## 4. What was verified locally (Windows Server dev box, no Docker)

- `python -m pytest -q`: all tests pass (the unix-socket runner test is skipped on Windows).
- A mock end-to-end run of the web app, both with the in-process sandbox and with the app talking to `runner_server.py` over HTTP.
- `docker-compose.yml` parses as YAML, and static tests check the runner isolation settings. **`docker compose config` and image builds were NOT run** because Docker isn't installed here. Run `bash deploy.sh up` on the server (it runs `docker compose config --quiet` first).

## 5. Known risks to check on the server

1. **Node cross-check under rlimits:** V8 reserves a lot of virtual memory, so `run_node` now sets `RLIMIT_AS` to 4096 MB (env `SANDBOX_NODE_AS_MB`). The heap is still capped by `--max-old-space-size` and the container's `mem_limit`. This has not been tested on Linux. If the "Cross-check with gates.js" step shows a warning on the server, raise `SANDBOX_NODE_AS_MB`. The audit completes either way.
2. **Shared socket volume permissions:** both images create `/run/botlab` owned by uid 10001, and the named volume inherits that on first use. If the runner logs `Permission denied`, run `docker compose down -v`, then `up` again.
3. **Caddy HTTPS:** needs the domain's DNS A record pointing at the instance IP *before* the first request, with ports 80 and 443 open (`deploy.sh` opens them in ufw). Check that the Vultr firewall group, if one is used, also allows them.
