import time

import pytest

import sandbox


def test_python_timeout_kills_process():
    t0 = time.monotonic()
    r = sandbox.run_python("while True:\n    pass\n", timeout=1.5)
    assert r.timed_out and not r.ok
    assert time.monotonic() - t0 < 10


def test_python_runs_and_returns_json():
    r = sandbox.run_python('import json\nprint(json.dumps({"x": sum(json.load(open("d.json")))}))', {"d.json": "[1,2,3]"})
    assert r.ok, r.stderr
    assert r.json() == {"x": 6}


def test_python_network_blocked():
    r = sandbox.run_python('import socket\nsocket.create_connection(("1.1.1.1", 80), timeout=2)\n')
    assert not r.ok and "blocked by sandbox" in r.stderr


def test_python_subprocess_blocked():
    r = sandbox.run_python("import subprocess\nsubprocess.run(['whoami'])\n")
    assert not r.ok and "blocked" in r.stderr


def test_python_file_outside_workdir_blocked():
    r = sandbox.run_python('open(r"' + __file__ + '").read()\n')
    assert not r.ok and "outside work dir" in r.stderr


def test_python_env_is_stripped(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-should-not-leak")
    monkeypatch.setenv("PAYPAL_CLIENT_SECRET", "nope")
    r = sandbox.run_python("import os, json\nprint(json.dumps(dict(os.environ)))\n")
    assert r.ok
    assert "sk-should-not-leak" not in r.stdout and "nope" not in r.stdout


def test_workdir_is_deleted():
    r = sandbox.run_python("import os\nprint(os.getcwd())\n")
    import os
    assert r.ok and not os.path.exists(r.stdout.strip())


needs_node = pytest.mark.skipif(not sandbox.node_available(), reason="node not installed")


@needs_node
def test_node_timeout():
    r = sandbox.run_node("for(;;){}", timeout=1.5, with_gates=False)
    assert r.timed_out and not r.ok


@needs_node
def test_node_network_and_fs_and_child_process_blocked():
    r = sandbox.run_node("require('https').get('https://example.com')", with_gates=False)
    assert not r.ok and "blocked by sandbox" in r.stderr
    r = sandbox.run_node("fetch('https://example.com')", with_gates=False)
    assert not r.ok and "blocked by sandbox" in r.stderr
    r = sandbox.run_node("require('fs').readFileSync(" + repr(__file__) + ")", with_gates=False)
    assert not r.ok and "ERR_ACCESS_DENIED" in r.stderr
    r = sandbox.run_node("require('child_process').execSync('whoami')", with_gates=False)
    assert not r.ok and "ERR_ACCESS_DENIED" in r.stderr


@needs_node
def test_node_output_capped():
    r = sandbox.run_node("process.stdout.write('x'.repeat(2_000_000))", with_gates=False)
    assert len(r.stdout) <= sandbox.MAX_OUTPUT
