"""Agentic-commerce demo: a trading bot buys an audit of its own trade log, paying with PayPal (sandbox).

The buying agent stays inside a budget and never pays on its own: it gets a PayPal approve link,
a human (the bot's owner) approves the spend on PayPal, then the agent captures and collects the
Pro audit. If the audit says the edge is not proven, the agent recommends pausing the book.

    python agent_buyer.py --api https://botlab-auditor.botsite.workers.dev --csv samples/synthetic_42_trades_small_lucky.csv \
        --agent fastbot-5m --max-price 5

Standard library only. MIT License (c) 2026 BOTLAB
"""
import argparse
import json
import sys
import time
import urllib.error
import urllib.request


def call(api, path, body=None):
    req = urllib.request.Request(api.rstrip("/") + path, method="POST" if body is not None else "GET",
                                 data=None if body is None else json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json", "User-Agent": "botlab-agent-buyer/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode())
        except Exception:
            return e.code, {"error": str(e)}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--api", required=True, help="auditor base URL")
    ap.add_argument("--csv", required=True, help="trade log CSV of the bot")
    ap.add_argument("--agent", default="botlab-agent", help="name shown on the PayPal order")
    ap.add_argument("--max-price", type=float, default=5.0, help="budget in USD; the agent refuses anything above it")
    ap.add_argument("--trials", type=int, default=20)
    ap.add_argument("--risk-usd", type=float, default=None)
    ap.add_argument("--wait", type=int, default=600, help="seconds to wait for the human approval")
    ap.add_argument("--out", default="audit_result.json")
    a = ap.parse_args(argv)

    csv = open(a.csv, encoding="utf-8").read()
    base = {"csv": csv, "trials": a.trials, "risk_usd": a.risk_usd}

    st, manifest = call(a.api, "/api/agent/manifest")
    if st != 200:
        sys.exit(f"auditor not reachable: {manifest}")
    print(f"[agent] found service: {manifest['name']} | price {manifest['payment']['price']} USD via {manifest['payment']['provider']} "
          f"({manifest['payment']['environment']})")

    st, q = call(a.api, "/api/agent/quote", dict(base, agent=a.agent, max_price_usd=a.max_price))
    if st != 200:
        sys.exit(f"[agent] no purchase: {q.get('error')}")
    fq = q["quote"]
    print(f"[agent] free check: {fq['passed']}/{fq['total']} gates passed, verdict {fq['verdict']}")
    print(f"[agent] offer: {q['offer']['product']} for {q['offer']['price']} {q['offer']['currency']} (budget {a.max_price})")
    pay = q["payment"]
    print(f"[agent] PayPal order {pay['order_id']} created. Waiting for my owner to approve the spend:\n        {pay['approve_url']}")

    deadline = time.time() + a.wait
    while True:
        st, f = call(a.api, "/api/agent/fulfil", dict(base, order_id=pay["order_id"]))
        if st == 200:
            break
        if st == 202 and time.time() < deadline:
            time.sleep(5)
            continue
        sys.exit(f"[agent] not fulfilled ({st}): {f.get('error') or f.get('status')}")

    r, audit = f["receipt"], f["audit"]
    facts = audit["facts"]
    print(f"[agent] paid: order {r['order_id']} | capture {r.get('capture_id')} | {r['amount']} (sandbox)")
    print(f"[agent] Pro audit: {facts['passed']}/{facts['total']} gates | {facts['verdictText']}")
    failed = [f"{k} {g['title']}" for k, g in audit["gates"].items() if g["pass"] is False]
    if facts["verdict"] != "ready":
        print("[agent] decision: edge NOT proven -> recommend pausing this book on real money. Failed: " + "; ".join(failed))
    else:
        print("[agent] decision: all gates passed -> keep running, re-audit after the next 50 trades.")
    json.dump(f, open(a.out, "w", encoding="utf-8"), indent=1)
    print(f"[agent] saved {a.out}")


if __name__ == "__main__":
    main()
