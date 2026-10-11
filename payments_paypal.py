"""PayPal Sandbox payments (REST v2 Orders): create order -> buyer approves -> capture.

Used to unlock the "Pro audit" (longer report + PDF export).

Env:
  PAYPAL_CLIENT_ID, PAYPAL_CLIENT_SECRET  - from a PayPal Developer *sandbox* REST app
  PAYPAL_MODE   = sandbox (default when keys exist) | mock (offline demo/tests) | disabled
  PRO_PRICE_USD = price of a Pro audit (default 4.99)
  PUBLIC_BASE_URL = where PayPal should redirect back (default http://127.0.0.1:8000)

Only the sandbox host is ever used. There is deliberately no switch to live.

MIT License (c) 2026 BOTLAB
"""
from __future__ import annotations

import base64
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

SANDBOX_BASE = "https://api-m.sandbox.paypal.com"


class PayPalError(RuntimeError):
    pass


def _default_transport(method: str, url: str, headers: dict, body: bytes | None, timeout: float = 30.0):
    """Returns (status, parsed_json)."""
    if not url.startswith(SANDBOX_BASE):
        raise PayPalError("refusing non-sandbox PayPal URL")
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            return resp.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw)
        except ValueError:
            return e.code, {"raw": raw[:300].decode("utf-8", "replace")}
    except urllib.error.URLError as e:
        raise PayPalError(f"PayPal connection error: {e.reason}") from None


class PayPalSandbox:
    """Thin client for the three calls we need."""

    def __init__(self, client_id: str, client_secret: str, transport=None, base_url: str = SANDBOX_BASE):
        if base_url != SANDBOX_BASE:
            raise PayPalError("Only the PayPal sandbox is supported")
        self.base = base_url
        self._cid = client_id
        self._secret = client_secret
        self._transport = transport or _default_transport
        self._token = None
        self._token_exp = 0.0

    def _access_token(self) -> str:
        if self._token and time.time() < self._token_exp - 60:
            return self._token
        basic = base64.b64encode(f"{self._cid}:{self._secret}".encode()).decode()
        status, data = self._transport("POST", self.base + "/v1/oauth2/token",
                                       {"Authorization": f"Basic {basic}", "Content-Type": "application/x-www-form-urlencoded"},
                                       b"grant_type=client_credentials")
        if status != 200 or "access_token" not in data:
            raise PayPalError(f"PayPal auth failed ({status})")
        self._token = data["access_token"]
        self._token_exp = time.time() + float(data.get("expires_in", 300))
        return self._token

    def _call(self, method, path, body=None, request_id=None):
        headers = {"Authorization": f"Bearer {self._access_token()}", "Content-Type": "application/json"}
        if request_id:
            headers["PayPal-Request-Id"] = request_id  # idempotency
        status, data = self._transport(method, self.base + path, headers, json.dumps(body).encode() if body is not None else None)
        if status >= 400:
            name = data.get("name") or data.get("error") or "error"
            raise PayPalError(f"PayPal {method} {path} -> {status} {name}")
        return data

    def create_order(self, amount: str, currency: str, reference: str, return_url: str, cancel_url: str,
                     description: str = "BOTLAB Pro audit") -> dict:
        body = {
            "intent": "CAPTURE",
            "purchase_units": [{"reference_id": reference[:120], "description": description[:120],
                                "amount": {"currency_code": currency, "value": amount}}],
            "payment_source": {"paypal": {"experience_context": {
                "brand_name": "BOTLAB Auditor", "user_action": "PAY_NOW", "shipping_preference": "NO_SHIPPING",
                "return_url": return_url, "cancel_url": cancel_url}}},
        }
        data = self._call("POST", "/v2/checkout/orders", body, request_id=str(uuid.uuid4()))
        approve = next((l["href"] for l in data.get("links", []) if l.get("rel") in ("payer-action", "approve")), None)
        if not approve:
            raise PayPalError("No approval link in PayPal response")
        return {"id": data["id"], "status": data.get("status"), "approve_url": approve}

    def capture_order(self, order_id: str) -> dict:
        if not order_id or not all(c.isalnum() or c in "-_" for c in order_id):
            raise PayPalError("bad order id")
        data = self._call("POST", f"/v2/checkout/orders/{urllib.parse.quote(order_id)}/capture", {},
                          request_id=f"cap-{order_id}")
        status = data.get("status")
        cap = None
        try:
            cap = data["purchase_units"][0]["payments"]["captures"][0]
        except (KeyError, IndexError, TypeError):
            pass
        return {"id": data.get("id", order_id), "status": status, "completed": status == "COMPLETED",
                "capture_id": cap and cap.get("id"), "amount": cap and cap.get("amount")}


class MockPayPal:
    """Offline stand-in with the same interface. Approval URL points back at our own server."""

    def __init__(self, public_base: str):
        self.public_base = public_base.rstrip("/")
        self.orders: dict[str, dict] = {}

    def create_order(self, amount, currency, reference, return_url, cancel_url, description="BOTLAB Pro audit"):
        oid = "MOCK" + uuid.uuid4().hex[:13].upper()
        self.orders[oid] = {"status": "CREATED", "amount": amount, "currency": currency, "return_url": return_url}
        sep = "&" if "?" in return_url else "?"
        return {"id": oid, "status": "CREATED", "approve_url": f"{return_url}{sep}token={oid}&mock=1"}

    def capture_order(self, order_id):
        o = self.orders.get(order_id)
        if not o:
            raise PayPalError("unknown mock order")
        o["status"] = "COMPLETED"
        return {"id": order_id, "status": "COMPLETED", "completed": True, "capture_id": "MOCKCAP" + order_id[-6:],
                "amount": {"currency_code": o["currency"], "value": o["amount"]}}


class Payments:
    """Facade used by the web app."""

    def __init__(self, env=None, transport=None):
        env = os.environ if env is None else env
        self.price = env.get("PRO_PRICE_USD", "4.99")
        self.currency = "USD"
        self.public_base = env.get("PUBLIC_BASE_URL", "http://127.0.0.1:8000").rstrip("/")
        cid, secret = env.get("PAYPAL_CLIENT_ID"), env.get("PAYPAL_CLIENT_SECRET")
        mode = (env.get("PAYPAL_MODE") or ("sandbox" if cid and secret else "disabled")).lower()
        if mode == "sandbox" and not (cid and secret):
            mode = "disabled"
        self.mode = mode
        if mode == "sandbox":
            self.client = PayPalSandbox(cid, secret, transport=transport)
        elif mode == "mock":
            self.client = MockPayPal(self.public_base)
        else:
            self.client = None

    @property
    def enabled(self):
        return self.client is not None

    def status(self) -> dict:
        label = {"sandbox": "PayPal Sandbox", "mock": "Mock payments (offline demo)",
                 "disabled": "payments disabled (demo mode)"}.get(self.mode, self.mode)
        return {"mode": self.mode, "enabled": self.enabled, "label": label, "price": self.price, "currency": self.currency}

    def start(self, audit_id: str) -> dict:
        if not self.enabled:
            raise PayPalError("payments disabled (demo mode)")
        ret = f"{self.public_base}/paypal/return?audit={urllib.parse.quote(audit_id)}"
        cancel = f"{self.public_base}/?audit={urllib.parse.quote(audit_id)}&paypal=cancelled"
        return self.client.create_order(self.price, self.currency, audit_id, ret, cancel)

    def finish(self, order_id: str) -> dict:
        if not self.enabled:
            raise PayPalError("payments disabled (demo mode)")
        return self.client.capture_order(order_id)
