"""Where model-written Python runs.

* RUNNER_URL unset (local dev, Windows)  -> the in-process sandbox in sandbox.py (separate python -I -S process,
                                            prelude, temp dir, timeout, stripped env).
* RUNNER_URL=unix:///run/botlab/runner.sock or http://host:port
                                         -> the separate "runner" container (vultr/runner/runner_server.py), which
                                            has no network at all (--network none), a read-only root fs, tmpfs /tmp,
                                            memory/CPU/pids limits, a non-root user and the default seccomp profile.
                                            The app talks to it with a tiny JSON-over-HTTP protocol:
                                              POST /run  {"code", "files": {name: text}, "timeout", "mem_mb"}
                                              -> {"ok", "returncode", "stdout", "stderr", "timed_out", "duration_s", "limits"}
                                              GET /healthz -> {"ok": true}
* If the runner is configured but unreachable the call FAILS CLOSED (error result) unless
  RUNNER_FALLBACK_LOCAL=1 is set explicitly.

Env: RUNNER_URL, RUNNER_TOKEN (optional shared secret sent as X-Runner-Token), RUNNER_FALLBACK_LOCAL.

MIT License (c) 2026 BOTLAB
"""
from __future__ import annotations

import http.client
import json
import os
import socket
from urllib.parse import unquote, urlparse

import sandbox

MAX_RESPONSE = 2 * 1024 * 1024


class _UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, path: str, timeout: float):
        super().__init__("localhost", timeout=timeout)
        self._path = path

    def connect(self):
        if not hasattr(socket, "AF_UNIX"):
            raise OSError("unix sockets are not supported on this platform")
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(self.timeout)
        s.connect(self._path)
        self.sock = s


class RemoteRunner:
    """Client for the runner container."""

    def __init__(self, url: str, token: str | None = None):
        self.url = url
        self.token = token or ""
        u = urlparse(url)
        self.scheme = u.scheme
        if u.scheme == "unix":
            self.sock_path = unquote(u.path)
        elif u.scheme == "http":
            self.host, self.port = u.hostname or "127.0.0.1", u.port or 80
        else:
            raise ValueError("RUNNER_URL must start with unix:// or http://")

    @property
    def label(self) -> str:
        return "runner container (" + self.scheme + ")"

    def _conn(self, timeout: float):
        if self.scheme == "unix":
            return _UnixHTTPConnection(self.sock_path, timeout)
        return http.client.HTTPConnection(self.host, self.port, timeout=timeout)

    def _request(self, method: str, path: str, body: dict | None, timeout: float):
        conn = self._conn(timeout)
        try:
            data = json.dumps(body).encode("utf-8") if body is not None else None
            headers = {"Content-Type": "application/json"}
            if self.token:
                headers["X-Runner-Token"] = self.token
            conn.request(method, path, body=data, headers=headers)
            resp = conn.getresponse()
            raw = resp.read(MAX_RESPONSE + 1)
            if len(raw) > MAX_RESPONSE:
                raise OSError("runner response too large")
            return resp.status, json.loads(raw.decode("utf-8") or "{}")
        finally:
            conn.close()

    def health(self, timeout: float = 3.0) -> bool:
        try:
            status, obj = self._request("GET", "/healthz", None, timeout)
            return status == 200 and bool(obj.get("ok"))
        except (OSError, ValueError, http.client.HTTPException):
            return False

    def run_python(self, code: str, inputs: dict[str, str] | None = None, timeout: float = sandbox.DEFAULT_TIMEOUT,
                   mem_mb: int = sandbox.DEFAULT_MEM_MB) -> sandbox.SandboxResult:
        body = {"code": code, "files": dict(inputs or {}), "timeout": timeout, "mem_mb": mem_mb}
        try:
            status, obj = self._request("POST", "/run", body, timeout + 10)
        except (OSError, ValueError, http.client.HTTPException) as e:
            return _error(f"runner unreachable: {e.__class__.__name__}: {str(e)[:200]}")
        if status != 200:
            return _error(f"runner HTTP {status}: {str(obj.get('error', ''))[:200]}")
        limits = dict(obj.get("limits") or {})
        limits["executor"] = self.label
        return sandbox.SandboxResult(ok=bool(obj.get("ok")), returncode=obj.get("returncode"),
                                     stdout=str(obj.get("stdout") or "")[:sandbox.MAX_OUTPUT],
                                     stderr=str(obj.get("stderr") or "")[:sandbox.MAX_OUTPUT],
                                     timed_out=bool(obj.get("timed_out")), duration_s=float(obj.get("duration_s") or 0),
                                     limits=limits)


class LocalRunner:
    label = "in-process sandbox (local dev)"

    def health(self, timeout: float = 3.0) -> bool:
        return True

    def run_python(self, code, inputs=None, timeout=sandbox.DEFAULT_TIMEOUT, mem_mb=sandbox.DEFAULT_MEM_MB):
        res = sandbox.run_python(code, inputs, timeout=timeout, mem_mb=mem_mb)
        res.limits = dict(res.limits, executor=self.label)
        return res


class _FallbackRunner:
    """Remote first, local if the remote is unreachable (only when RUNNER_FALLBACK_LOCAL=1)."""

    def __init__(self, remote: RemoteRunner):
        self.remote, self.local = remote, LocalRunner()
        self.label = remote.label + " with local fallback"

    def health(self, timeout=3.0):
        return True

    def run_python(self, code, inputs=None, timeout=sandbox.DEFAULT_TIMEOUT, mem_mb=sandbox.DEFAULT_MEM_MB):
        res = self.remote.run_python(code, inputs, timeout, mem_mb)
        if not res.ok and res.returncode is None and res.stderr.startswith("runner unreachable"):
            return self.local.run_python(code, inputs, timeout, mem_mb)
        return res


def _error(msg: str) -> sandbox.SandboxResult:
    return sandbox.SandboxResult(ok=False, returncode=None, stdout="", stderr=msg, limits={"executor": "runner"})


def get_runner():
    url = (os.environ.get("RUNNER_URL") or "").strip()
    if not url:
        return LocalRunner()
    remote = RemoteRunner(url, os.environ.get("RUNNER_TOKEN"))
    if os.environ.get("RUNNER_FALLBACK_LOCAL", "").strip() in ("1", "true", "yes"):
        return _FallbackRunner(remote)
    return remote
