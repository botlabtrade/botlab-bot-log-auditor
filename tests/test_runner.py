"""The isolated runner service (vultr/runner/runner_server.py) and its client, over TCP on localhost.

The container-level isolation (network none, read-only fs, limits) is configured in vultr/docker-compose.yml
and checked statically in test_vultr_files.py; it cannot be exercised without Docker."""
import json
import os
import socket
import sys
import tempfile
import threading
import http.client

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "vultr", "runner"))

import agent  # noqa: E402
import runner_client  # noqa: E402
import runner_server  # noqa: E402
from conftest import read_sample  # noqa: E402


@pytest.fixture()
def server(monkeypatch):
    monkeypatch.setenv("RUNNER_TOKEN", "t0k")
    monkeypatch.setenv("RUNNER_MAX_TIMEOUT_S", "3")
    srv = runner_server.make_server(listen="127.0.0.1:0")
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    yield srv
    srv.shutdown()
    srv.server_close()


def _url(srv):
    return f"http://127.0.0.1:{srv.server_address[1]}"


def test_health_and_run(server):
    rc = runner_client.RemoteRunner(_url(server), "t0k")
    assert rc.health()
    r = rc.run_python('import json\nprint(json.dumps({"s": sum(json.load(open("d.json")))}))', {"d.json": "[1,2,3]"})
    assert r.ok, r.stderr
    assert r.json() == {"s": 6}
    assert "network none" in r.limits["container"] and r.limits["executor"].startswith("runner container")


def test_wrong_token_rejected(server):
    r = runner_client.RemoteRunner(_url(server), "wrong").run_python("print(1)")
    assert not r.ok and "401" in r.stderr


def test_timeout_is_clamped_by_server(server):
    r = runner_client.RemoteRunner(_url(server), "t0k").run_python("while True:\n    pass\n", timeout=60)
    assert r.timed_out and not r.ok and r.duration_s < 10


def test_sandbox_prelude_still_applies(server):
    rc = runner_client.RemoteRunner(_url(server), "t0k")
    r = rc.run_python("import socket\nsocket.create_connection(('1.1.1.1', 80))")
    assert not r.ok and "blocked by sandbox" in r.stderr
    r = rc.run_python("import subprocess")
    assert not r.ok and "blocked" in r.stderr


def _raw_post(srv, body: bytes, token="t0k"):
    c = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=10)
    c.request("POST", "/run", body=body, headers={"Content-Type": "application/json", "X-Runner-Token": token})
    resp = c.getresponse()
    return resp.status, json.loads(resp.read() or b"{}")


def test_bad_requests_rejected(server):
    assert _raw_post(server, b"not json")[0] == 400
    assert _raw_post(server, json.dumps({"code": "print(1)", "files": {"../x": "a"}}).encode())[0] == 400
    assert _raw_post(server, json.dumps({"code": "print(1)", "files": {"main.py": "a"}}).encode())[0] == 400
    assert _raw_post(server, json.dumps({"code": "x" * 60000}).encode())[0] == 413


def test_busy_returns_429(server):
    server.slots = threading.BoundedSemaphore(1)
    server.slots.acquire()
    try:
        assert _raw_post(server, json.dumps({"code": "print(1)"}).encode())[0] == 429
    finally:
        server.slots.release()


def test_full_audit_through_runner(server, monkeypatch):
    monkeypatch.setenv("RUNNER_URL", _url(server))
    monkeypatch.setenv("RUNNER_TOKEN", "t0k")
    monkeypatch.setenv("LLM_PROVIDER", "mock")
    res = agent.run_audit(read_sample("synthetic_640_trades_passes.csv"))
    assert res["agent_trace"]["executor"].startswith("runner container")
    assert all(s["ok"] for s in res["agent_trace"]["steps"])
    assert res["analysis_trace"]["limits"]["executor"].startswith("runner container")


def test_healthcheck_cli(server):
    assert runner_server.healthcheck(f"127.0.0.1:{server.server_address[1]}", "") == 0
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        free = s.getsockname()[1]
    assert runner_server.healthcheck(f"127.0.0.1:{free}", "") == 1


@pytest.mark.skipif(not hasattr(socket, "AF_UNIX") or os.name == "nt", reason="unix sockets not available here")
def test_unix_socket_transport(monkeypatch):
    monkeypatch.delenv("RUNNER_TOKEN", raising=False)
    path = os.path.join(tempfile.mkdtemp(), "r.sock")
    srv = runner_server.make_server(sock_path=path)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        rc = runner_client.RemoteRunner("unix://" + path)
        assert rc.health()
        assert rc.run_python("print(42)").stdout.strip() == "42"
        assert runner_server.healthcheck(None, path) == 0
    finally:
        srv.shutdown()
        srv.server_close()
