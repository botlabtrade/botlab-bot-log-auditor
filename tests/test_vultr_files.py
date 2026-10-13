"""Static checks of the Vultr deployment files (Docker is not available on the dev machine)."""
import os
import re

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
V = os.path.join(ROOT, "vultr")
yaml = pytest.importorskip("yaml")


def _read(name):
    with open(os.path.join(V, name), encoding="utf-8") as f:
        return f.read()


def test_compose_runner_is_isolated():
    d = yaml.safe_load(_read("docker-compose.yml"))
    r = d["services"]["runner"]
    assert r["network_mode"] == "none"
    assert r["read_only"] is True
    assert any(t.startswith("/tmp:") for t in r["tmpfs"])
    assert r["user"].split(":")[0] != "0"
    assert r["cap_drop"] == ["ALL"] and "no-new-privileges:true" in r["security_opt"]
    assert not any("seccomp" in s and "unconfined" in s for s in r["security_opt"])
    assert r["mem_limit"] and r["cpus"] and r["pids_limit"]
    assert "env_file" not in r, "runner must not receive app secrets"
    assert "ports" not in r


def test_compose_app_and_caddy():
    d = yaml.safe_load(_read("docker-compose.yml"))
    app, caddy = d["services"]["app"], d["services"]["caddy"]
    assert app["environment"]["RUNNER_URL"].startswith("unix://")
    assert app["read_only"] is True and "ports" not in app
    assert app["depends_on"]["runner"]["condition"] == "service_healthy"
    assert "80:80" in caddy["ports"] and "443:443" in caddy["ports"]
    assert caddy["environment"]["SITE_ADDRESS"] == "${DOMAIN:-:80}"
    assert "{$SITE_ADDRESS}" in _read("Caddyfile") and "reverse_proxy app:8000" in _read("Caddyfile")


def test_dockerfiles_non_root_and_copy_existing_files():
    for name in ("Dockerfile.app", "Dockerfile.runner"):
        txt = _read(name)
        assert "FROM python:3.12-slim" in txt and re.search(r"^USER botlab$", txt, re.M)
        assert "HEALTHCHECK" in txt
        joined = re.sub(r"\\\n\s*", " ", txt)
        copies = re.findall(r"^COPY (?!--from)(.+)$", joined, re.M)
        assert copies
        for line in copies:
            for src in line.split()[:-1]:
                assert os.path.exists(os.path.join(ROOT, src)), f"{name}: {src} missing"
        if name == "Dockerfile.app":
            assert "runner_client.py" in joined and "agent_loop.py" in joined


def test_env_example_has_no_secrets():
    txt = _read(".env.example")
    for line in txt.splitlines():
        if re.match(r"^(LLM_API_KEY|PAYPAL_CLIENT_SECRET|PAYPAL_CLIENT_ID|RUNNER_TOKEN)=", line):
            assert line.split("=", 1)[1].strip() == ""
    assert "LLM_BASE_URL=https://api.vultrinference.com/v1" in txt


def test_deploy_script_has_no_secret_handling():
    txt = _read("deploy.sh")
    assert txt.startswith("#!/usr/bin/env bash") and "set -euo pipefail" in txt
    assert "LLM_API_KEY" not in txt and "\r\n" not in txt
