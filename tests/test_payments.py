import json

import pytest

import payments_paypal as pp
import pdf_export


class FakePayPalHTTP:
    """Records calls and answers like the PayPal sandbox REST API."""

    def __init__(self, capture_status="COMPLETED", fail_auth=False):
        self.calls = []
        self.capture_status = capture_status
        self.fail_auth = fail_auth

    def __call__(self, method, url, headers, body, timeout=30.0):
        self.calls.append((method, url, headers, body))
        assert url.startswith(pp.SANDBOX_BASE)
        if url.endswith("/v1/oauth2/token"):
            if self.fail_auth:
                return 401, {"error": "invalid_client"}
            assert headers["Authorization"].startswith("Basic ") and body == b"grant_type=client_credentials"
            return 200, {"access_token": "A21-test", "expires_in": 32400}
        assert headers["Authorization"] == "Bearer A21-test"
        if url.endswith("/v2/checkout/orders"):
            b = json.loads(body)
            assert b["intent"] == "CAPTURE"
            assert b["purchase_units"][0]["amount"] == {"currency_code": "USD", "value": "4.99"}
            assert "PayPal-Request-Id" in headers
            return 200, {"id": "5O190127TN364715T", "status": "PAYER_ACTION_REQUIRED",
                         "links": [{"rel": "self", "href": "x"},
                                   {"rel": "payer-action", "href": "https://www.sandbox.paypal.com/checkoutnow?token=5O190127TN364715T"}]}
        if url.endswith("/capture"):
            return 201, {"id": "5O190127TN364715T", "status": self.capture_status,
                         "purchase_units": [{"payments": {"captures": [{"id": "3C679366HH908993F",
                                                                       "amount": {"currency_code": "USD", "value": "4.99"}}]}}]}
        return 404, {"name": "RESOURCE_NOT_FOUND"}


ENV = {"PAYPAL_CLIENT_ID": "cid", "PAYPAL_CLIENT_SECRET": "sec", "PUBLIC_BASE_URL": "http://127.0.0.1:8000"}


def test_disabled_without_keys():
    p = pp.Payments(env={})
    assert not p.enabled and p.status()["label"] == "payments disabled (demo mode)"
    with pytest.raises(pp.PayPalError):
        p.start("abc")


def test_sandbox_create_and_capture_flow():
    http = FakePayPalHTTP()
    p = pp.Payments(env=ENV, transport=http)
    assert p.mode == "sandbox"
    order = p.start("a" * 32)
    assert order["approve_url"].startswith("https://www.sandbox.paypal.com/")
    create_body = json.loads(http.calls[1][3])
    ctx = create_body["payment_source"]["paypal"]["experience_context"]
    assert ctx["return_url"] == "http://127.0.0.1:8000/paypal/return?audit=" + "a" * 32
    cap = p.finish(order["id"])
    assert cap["completed"] and cap["capture_id"] == "3C679366HH908993F"
    assert sum(1 for c in http.calls if c[1].endswith("/oauth2/token")) == 1  # token cached


def test_capture_not_completed():
    p = pp.Payments(env=ENV, transport=FakePayPalHTTP(capture_status="PENDING"))
    order = p.start("b" * 32)
    assert p.finish(order["id"])["completed"] is False


def test_auth_failure_raises():
    p = pp.Payments(env=ENV, transport=FakePayPalHTTP(fail_auth=True))
    with pytest.raises(pp.PayPalError, match="auth failed"):
        p.start("c" * 32)


def test_live_base_url_refused():
    with pytest.raises(pp.PayPalError):
        pp.PayPalSandbox("a", "b", base_url="https://api-m.paypal.com")
    with pytest.raises(pp.PayPalError):
        pp._default_transport("GET", "https://api-m.paypal.com/v1/x", {}, None)


def test_bad_order_id_rejected():
    p = pp.Payments(env=ENV, transport=FakePayPalHTTP())
    with pytest.raises(pp.PayPalError):
        p.finish("../../v1/evil")


def test_mock_mode_roundtrip():
    p = pp.Payments(env={"PAYPAL_MODE": "mock"})
    o = p.start("d" * 32)
    assert "/paypal/return?audit=" in o["approve_url"] and "token=" + o["id"] in o["approve_url"]
    assert p.finish(o["id"])["completed"]


def test_pdf_is_valid_looking():
    pdf = pdf_export.build_pdf("Title", "## Verdict\n\n- item **bold**\n\n| a | b |\n|---|---|\n| 1 | 2 |\n" + "line\n" * 120)
    assert pdf.startswith(b"%PDF-1.4") and pdf.rstrip().endswith(b"%%EOF")
    assert pdf.count(b"/Type /Page ") >= 2
