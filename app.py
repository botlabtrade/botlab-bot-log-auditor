"""BOTLAB Bot Log Auditor - web server (standard library only).

    python app.py            -> http://127.0.0.1:8000

Env: HOST (default 127.0.0.1), PORT (default 8000), plus the LLM_* / ANTHROPIC_* and PAYPAL_* variables
documented in README.md.

MIT License (c) 2026 BOTLAB
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
import traceback
import uuid
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import agent
import llm as llm_mod
import pdf_export
from payments_paypal import Payments, PayPalError

HERE = os.path.dirname(os.path.abspath(__file__))
STATIC = os.path.join(HERE, "static")
SAMPLES = os.path.join(HERE, "samples")
MAX_UPLOAD = 5 * 1024 * 1024
MAX_JOBS = 200

JOBS: dict[str, dict] = {}
ORDERS: dict[str, str] = {}  # paypal order id -> audit id
LOCK = threading.Lock()
PAYMENTS = Payments()
ID_RE = re.compile(r"^[a-f0-9]{32}$")


def provider_info():
    try:
        l = llm_mod.get_llm()
        return {"provider": l.name, "model": l.model}
    except llm_mod.LLMError as e:
        return {"provider": "misconfigured", "model": str(e)}


def public_job(job: dict) -> dict:
    keep = ("id", "status", "error", "steps", "pro", "created", "chat", "pro_status")
    out = {k: job.get(k) for k in keep}
    res = job.get("result")
    if res:
        out["result"] = {k: res.get(k) for k in ("provider", "model", "input", "crosscheck", "facts", "curve", "report",
                                                 "report_source", "analysis_trace")}
        if job.get("pro_report"):
            out["result"]["pro_report"] = job["pro_report"]
    return out


def _run_job(job_id: str, csv_text: str, trials: int, risk: float | None, pro: bool = False):
    def on_progress(steps):
        with LOCK:
            JOBS[job_id]["steps"] = steps

    try:
        res = agent.run_audit(csv_text, trials=trials, risk_usd=risk, pro=pro, on_progress=on_progress)
        with LOCK:
            JOBS[job_id].update(status="done", result=res, steps=res["steps"])
    except Exception as e:  # surface parse errors to the UI, keep the server alive
        with LOCK:
            JOBS[job_id].update(status="error", error=str(e)[:500])
        if not isinstance(e, ValueError):
            traceback.print_exc()


def _run_pro(job_id: str):
    with LOCK:
        job = JOBS[job_id]
        job["pro_status"] = "running"
        facts = job["result"]["facts"]
    try:
        l = llm_mod.get_llm()
        try:
            text = l.write_report(facts, pro=True)
        except Exception:
            text = llm_mod.MockLLM().write_report(facts, pro=True)
        with LOCK:
            job["pro_report"] = text
            job["pro_status"] = "done"
    except Exception as e:
        with LOCK:
            job["pro_status"] = "error: " + str(e)[:200]


def create_job(csv_text: str, trials: int, risk: float | None) -> str:
    job_id = uuid.uuid4().hex
    with LOCK:
        if len(JOBS) >= MAX_JOBS:
            oldest = sorted(JOBS.values(), key=lambda j: j["created"])[: len(JOBS) - MAX_JOBS + 1]
            for j in oldest:
                JOBS.pop(j["id"], None)
        JOBS[job_id] = {"id": job_id, "status": "running", "created": time.time(), "steps": [], "pro": False,
                        "chat": [], "trials": trials}
    threading.Thread(target=_run_job, args=(job_id, csv_text, trials, risk), daemon=True).start()
    return job_id


class Handler(BaseHTTPRequestHandler):
    server_version = "BotlabAuditor/0.1"

    def log_message(self, fmt, *args):  # quieter log, never prints bodies
        print("[http] " + fmt % args)

    # ---------- helpers ----------
    def _send(self, code, body: bytes, ctype="application/json; charset=utf-8", extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8"))

    def _err(self, msg, code=400):
        self._json({"error": msg}, code)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n > MAX_UPLOAD:
            raise ValueError("Upload too large (max 5 MB)")
        raw = self.rfile.read(n) if n else b"{}"
        return json.loads(raw.decode("utf-8") or "{}")

    def _redirect(self, url):
        self.send_response(HTTPStatus.SEE_OTHER)
        self.send_header("Location", url)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _job(self, job_id):
        if not ID_RE.match(job_id or ""):
            return None
        with LOCK:
            return JOBS.get(job_id)

    # ---------- routes ----------
    def do_GET(self):
        u = urlparse(self.path)
        path, q = u.path, parse_qs(u.query)
        if path in ("/", "/index.html"):
            with open(os.path.join(STATIC, "index.html"), "rb") as f:
                return self._send(200, f.read(), "text/html; charset=utf-8",
                                  {"Content-Security-Policy": "default-src 'self'; script-src 'self' 'unsafe-inline'; "
                                   "style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; "
                                   "form-action 'self' https://www.sandbox.paypal.com; frame-ancestors 'none'"})
        if path == "/api/config":
            return self._json({"llm": provider_info(), "payments": PAYMENTS.status(),
                               "samples": sorted(f for f in os.listdir(SAMPLES) if f.endswith(".csv"))})
        m = re.match(r"^/api/samples/([A-Za-z0-9_.-]+\.csv)$", path)
        if m:
            p = os.path.join(SAMPLES, m.group(1))
            if not os.path.isfile(p):
                return self._err("not found", 404)
            with open(p, "rb") as f:
                return self._send(200, f.read(), "text/csv; charset=utf-8")
        m = re.match(r"^/api/audit/([a-f0-9]{32})$", path)
        if m:
            job = self._job(m.group(1))
            if not job:
                return self._err("not found", 404)
            with LOCK:
                return self._json(public_job(job))
        m = re.match(r"^/api/audit/([a-f0-9]{32})/pdf$", path)
        if m:
            job = self._job(m.group(1))
            if not job or not job.get("pro") or not job.get("pro_report"):
                return self._err("Pro audit not unlocked", 403)
            pdf = pdf_export.build_pdf("BOTLAB Bot Log Auditor - Pro audit", job["pro_report"],
                                       footer="Educational audit only. Not financial advice.")
            return self._send(200, pdf, "application/pdf",
                              {"Content-Disposition": f'attachment; filename="botlab-audit-{job["id"][:8]}.pdf"'})
        if path == "/paypal/return":
            return self._paypal_return(q)
        if path == "/healthz":
            return self._json({"ok": True})
        return self._err("not found", 404)

    def do_POST(self):
        path = urlparse(self.path).path
        try:
            body = self._body()
        except (ValueError, UnicodeDecodeError) as e:
            return self._err(str(e) if "large" in str(e) else "invalid JSON body", 413 if "large" in str(e) else 400)
        if path == "/api/audit":
            csv_text = body.get("csv") or ""
            if not isinstance(csv_text, str) or not csv_text.strip():
                return self._err("Paste or upload a CSV first.")
            try:
                trials = int(body.get("trials") or 20)
                risk = float(body["risk_usd"]) if body.get("risk_usd") not in (None, "") else None
            except (TypeError, ValueError):
                return self._err("trials must be an integer and risk_usd a number")
            if trials < 1 or trials > 100000:
                return self._err("trials must be between 1 and 100000")
            if risk is not None and risk <= 0:
                return self._err("risk_usd must be positive")
            return self._json({"id": create_job(csv_text, trials, risk)}, 202)
        m = re.match(r"^/api/audit/([a-f0-9]{32})/chat$", path)
        if m:
            job = self._job(m.group(1))
            if not job or job.get("status") != "done":
                return self._err("audit not ready", 409)
            msg = str(body.get("message") or "").strip()[:2000]
            if not msg:
                return self._err("empty message")
            with LOCK:
                history = list(job["chat"])
                facts = job["result"]["facts"]
                report = job.get("pro_report") or job["result"]["report"]
            try:
                answer = llm_mod.get_llm().chat(facts, report, history, msg)
            except Exception as e:
                answer = llm_mod.MockLLM().chat(facts, report, history, msg) + f"\n\n(LLM unavailable: {str(e)[:120]})"
            with LOCK:
                job["chat"] += [{"role": "user", "content": msg}, {"role": "assistant", "content": answer}]
                job["chat"] = job["chat"][-20:]
            return self._json({"answer": answer})
        if path == "/api/paypal/create":
            job = self._job(str(body.get("audit_id") or ""))
            if not job or job.get("status") != "done":
                return self._err("audit not ready", 409)
            if not PAYMENTS.enabled:
                return self._err(PAYMENTS.status()["label"], 503)
            try:
                order = PAYMENTS.start(job["id"])
            except PayPalError as e:
                return self._err(str(e), 502)
            with LOCK:
                ORDERS[order["id"]] = job["id"]
            return self._json({"order_id": order["id"], "approve_url": order["approve_url"], "mode": PAYMENTS.mode})
        return self._err("not found", 404)

    def _paypal_return(self, q):
        order_id = (q.get("token") or [""])[0]
        audit_id = (q.get("audit") or [""])[0]
        with LOCK:
            bound = ORDERS.get(order_id)
        if not bound or bound != audit_id:
            return self._redirect("/?paypal=unknown-order")
        try:
            cap = PAYMENTS.finish(order_id)
        except PayPalError:
            return self._redirect(f"/?audit={audit_id}&paypal=capture-failed")
        if not cap.get("completed"):
            return self._redirect(f"/?audit={audit_id}&paypal=not-completed")
        with LOCK:
            job = JOBS.get(audit_id)
            if not job:
                return self._redirect("/?paypal=audit-expired")
            job["pro"] = True
            job["payment"] = {"order_id": order_id, "capture_id": cap.get("capture_id"), "mode": PAYMENTS.mode}
            ORDERS.pop(order_id, None)
        threading.Thread(target=_run_pro, args=(audit_id,), daemon=True).start()
        return self._redirect(f"/?audit={audit_id}&paypal=success")


def main():
    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "8000"))
    srv = ThreadingHTTPServer((host, port), Handler)
    info = provider_info()
    print(f"BOTLAB Bot Log Auditor on http://{host}:{port}  | LLM: {info['provider']} ({info['model']})"
          f"  | payments: {PAYMENTS.status()['label']}")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
