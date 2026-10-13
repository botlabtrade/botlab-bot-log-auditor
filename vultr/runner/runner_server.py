"""BOTLAB sandbox runner: executes model-written Python for the auditor app, in its own container.

The container is started with no network (network_mode: none), a read-only root fs, tmpfs /tmp,
memory / CPU / pids limits, a non-root user, all capabilities dropped, no-new-privileges and Docker's
default seccomp profile (see vultr/docker-compose.yml). Inside it, every run additionally goes through
sandbox.run_python (python -I -S -B, prelude that blocks sockets/subprocess/ctypes/file access outside
the temp dir, rlimits, hard timeout, capped output).

Protocol (JSON over HTTP; over a unix socket in production, TCP for local tests):
    GET  /healthz -> {"ok": true}
    POST /run     {"code": str, "files": {name: text}, "timeout": float, "mem_mb": int}
               -> {"ok", "returncode", "stdout", "stderr", "timed_out", "duration_s", "limits"}

Env: RUNNER_SOCKET (default /run/botlab/runner.sock) or RUNNER_LISTEN=host:port, RUNNER_TOKEN (optional
shared secret, header X-Runner-Token), RUNNER_MAX_CONCURRENCY (default 2), RUNNER_MAX_TIMEOUT_S (20),
RUNNER_MAX_MEM_MB (384).

    python runner_server.py               serve
    python runner_server.py --healthcheck exit 0 if the server answers /healthz

MIT License (c) 2026 BOTLAB
"""
from __future__ import annotations

import hmac
import json
import os
import socket
import socketserver
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
for _p in (HERE, os.path.dirname(os.path.dirname(HERE))):  # container: /app ; repo: vultr/runner/../..
    if os.path.isfile(os.path.join(_p, "sandbox.py")) and _p not in sys.path:
        sys.path.insert(0, _p)

import sandbox  # noqa: E402

MAX_BODY = 8 * 1024 * 1024
MAX_CODE = 50_000
MAX_FILES = 8
NAME_OK = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-")


def _cfg():
    return {"token": os.environ.get("RUNNER_TOKEN", ""),
            "max_timeout": float(os.environ.get("RUNNER_MAX_TIMEOUT_S", "20")),
            "max_mem": int(os.environ.get("RUNNER_MAX_MEM_MB", "384")),
            "concurrency": int(os.environ.get("RUNNER_MAX_CONCURRENCY", "2"))}


class Handler(BaseHTTPRequestHandler):
    server_version = "BotlabRunner/0.1"
    protocol_version = "HTTP/1.0"

    def address_string(self):  # unix sockets have no peer address
        return "local"

    def log_message(self, fmt, *args):  # never log bodies (code / trades)
        sys.stderr.write("[runner] " + (fmt % args) + "\n")

    def _json(self, obj, code=200):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authorised(self) -> bool:
        tok = self.server.cfg["token"]
        return not tok or hmac.compare_digest(self.headers.get("X-Runner-Token", ""), tok)

    def do_GET(self):
        if self.path == "/healthz":
            return self._json({"ok": True})
        return self._json({"error": "not found"}, 404)

    def do_POST(self):
        if self.path != "/run":
            return self._json({"error": "not found"}, 404)
        if not self._authorised():
            return self._json({"error": "unauthorised"}, 401)
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return self._json({"error": "bad length"}, 400)
        if n <= 0 or n > MAX_BODY:
            return self._json({"error": "body too large or empty"}, 413)
        try:
            req = json.loads(self.rfile.read(n).decode("utf-8"))
            code = req["code"]
            files = req.get("files") or {}
            assert isinstance(code, str) and isinstance(files, dict)
        except Exception:
            return self._json({"error": "invalid request"}, 400)
        if len(code) > MAX_CODE or len(files) > MAX_FILES:
            return self._json({"error": "code too long or too many files"}, 413)
        for name, text in files.items():
            if not (isinstance(name, str) and isinstance(text, str) and name and set(name) <= NAME_OK
                    and not name.startswith(".") and name != "main.py"):
                return self._json({"error": "bad file name"}, 400)
        cfg = self.server.cfg
        try:
            timeout = max(0.5, min(cfg["max_timeout"], float(req.get("timeout") or 6)))
            mem_mb = max(64, min(cfg["max_mem"], int(req.get("mem_mb") or cfg["max_mem"])))
        except (TypeError, ValueError):
            return self._json({"error": "bad limits"}, 400)
        if not self.server.slots.acquire(blocking=False):
            return self._json({"error": "runner busy"}, 429)
        try:
            res = sandbox.run_python(code, files, timeout=timeout, mem_mb=mem_mb)
        finally:
            self.server.slots.release()
        limits = dict(res.limits, container="network none, read-only rootfs, tmpfs /tmp, non-root, caps dropped, "
                                            "pids/mem/cpu limits, default seccomp")
        return self._json({"ok": res.ok, "returncode": res.returncode, "stdout": res.stdout, "stderr": res.stderr,
                           "timed_out": res.timed_out, "duration_s": res.duration_s, "limits": limits})


class _Common:
    daemon_threads = True

    def setup_runner(self):
        self.cfg = _cfg()
        self.slots = threading.BoundedSemaphore(max(1, self.cfg["concurrency"]))


class TCPRunner(_Common, socketserver.ThreadingMixIn, HTTPServer):
    pass


if hasattr(socketserver, "UnixStreamServer"):
    class UnixRunner(_Common, socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
        pass


def make_server(listen: str | None = None, sock_path: str | None = None):
    if listen:
        host, port = listen.rsplit(":", 1)
        srv = TCPRunner((host, int(port)), Handler)
    else:
        if os.path.exists(sock_path):
            os.unlink(sock_path)
        srv = UnixRunner(sock_path, Handler)
        os.chmod(sock_path, 0o660)
    srv.setup_runner()
    return srv


def healthcheck(listen: str | None, sock_path: str) -> int:
    req = b"GET /healthz HTTP/1.0\r\nHost: runner\r\n\r\n"
    try:
        if listen:
            host, port = listen.rsplit(":", 1)
            s = socket.create_connection((host, int(port)), timeout=3)
        else:
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.settimeout(3)
            s.connect(sock_path)
        with s:
            s.sendall(req)
            data = s.recv(4096)
        return 0 if b" 200 " in data.split(b"\r\n", 1)[0] + b" " else 1
    except OSError:
        return 1


def main():
    listen = os.environ.get("RUNNER_LISTEN")
    sock_path = os.environ.get("RUNNER_SOCKET", "/run/botlab/runner.sock")
    if "--healthcheck" in sys.argv:
        sys.exit(healthcheck(listen, sock_path))
    srv = make_server(listen, sock_path)
    print(f"[runner] listening on {listen or 'unix:' + sock_path} · max concurrency {srv.cfg['concurrency']}", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
