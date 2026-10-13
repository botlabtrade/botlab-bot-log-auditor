"""Sandboxed code execution for the auditor agent.

Every piece of code the agent runs (the reused gates.js via Node, and the
analysis snippets the LLM writes in Python) goes through `run()`:

* a fresh temporary working directory that is deleted afterwards;
* a stripped environment (no API keys, no PAYPAL_*, no proxies, no PATH extras);
* a hard wall-clock timeout (the process tree is killed);
* capped stdout/stderr;
* network disabled in-process: Python gets a prelude that replaces socket /
  subprocess / ctypes entry points; Node runs with the permission model
  (`--permission`, fs read limited to the temp dir, no child processes,
  no workers) plus a preload that stubs net/http/https/dns/tls/fetch;
* on POSIX: RLIMIT_AS (memory), RLIMIT_CPU, RLIMIT_NOFILE, RLIMIT_FSIZE.

This is defence in depth for a demo, not a hardened multi-tenant jail. For a
public deployment run the whole app inside a container / microVM (see README).

MIT License (c) 2026 BOTLAB
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field

HERE = os.path.dirname(os.path.abspath(__file__))
VENDOR = os.path.join(HERE, "vendor")

DEFAULT_TIMEOUT = 10.0
DEFAULT_MEM_MB = 512
MAX_OUTPUT = 256 * 1024

PY_PRELUDE = r'''
# --- sandbox prelude (injected) ---
import builtins as _b, sys as _s, os as _o
def _blocked(*a, **k):
    raise PermissionError("blocked by sandbox")
import socket as _sock
for _n in ("socket", "create_connection", "create_server", "getaddrinfo", "gethostbyname", "socketpair", "fromfd"):
    setattr(_sock, _n, _blocked)
for _n in ("system", "popen", "execv", "execve", "execl", "execvp", "spawnv", "spawnl", "fork", "kill", "startfile", "remove", "unlink", "rmdir", "removedirs", "rename", "replace", "chdir"):
    if hasattr(_o, _n):
        setattr(_o, _n, _blocked)
_o.environ.clear()
_real_open = _b.open
_ROOT = _o.path.realpath(_o.getcwd())
def _open(file, mode="r", *a, **k):
    p = _o.path.realpath(file) if isinstance(file, (str, bytes, _o.PathLike)) else None
    if p is not None and not str(p).startswith(_ROOT):
        raise PermissionError("sandbox: file access outside work dir")
    return _real_open(file, mode, *a, **k)
_b.open = _open
import io as _io
_io.open = _open
_BLOCK_MODS = {"subprocess", "ctypes", "_ctypes", "multiprocessing", "urllib.request", "http.client", "ftplib", "smtplib", "telnetlib", "ssl", "_socket", "asyncio", "shutil"}
_real_import = _b.__import__
def _imp(name, *a, **k):
    if name in _BLOCK_MODS or name.split(".")[0] in {"subprocess", "ctypes", "multiprocessing", "asyncio", "shutil"}:
        raise ImportError("module '%s' is blocked by sandbox" % name)
    return _real_import(name, *a, **k)
_b.__import__ = _imp
del _n
# --- end prelude ---
'''

NODE_NETBLOCK = r'''
'use strict';
const blocked = () => { throw new Error('network blocked by sandbox'); };
const FNS = ['connect', 'createConnection', 'request', 'get', 'createServer', 'createSocket', 'lookup',
  'resolve', 'resolve4', 'resolve6', 'resolveAny', 'lookupService', 'reverse'];
for (const m of ['net', 'http', 'https', 'http2', 'tls', 'dgram', 'dns', 'dns/promises']) {
  try {
    const mod = require(m);
    for (const k of FNS) { if (typeof mod[k] === 'function') { try { mod[k] = blocked; } catch (e) {} } }
  } catch (e) {}
}
try { require('net').Socket.prototype.connect = blocked; } catch (e) {}
globalThis.fetch = blocked;
globalThis.WebSocket = undefined;
'''


@dataclass
class SandboxResult:
    ok: bool
    returncode: int | None
    stdout: str
    stderr: str
    timed_out: bool = False
    duration_s: float = 0.0
    limits: dict = field(default_factory=dict)

    def json(self):
        """Parse stdout as JSON (last non-empty line if needed)."""
        try:
            return json.loads(self.stdout)
        except ValueError:
            lines = [l for l in self.stdout.strip().splitlines() if l.strip()]
            return json.loads(lines[-1]) if lines else None


def _clean_env(workdir: str) -> dict:
    env = {
        "PATH": os.path.dirname(sys.executable) + os.pathsep + (os.path.dirname(shutil.which("node")) if shutil.which("node") else ""),
        "PYTHONIOENCODING": "utf-8",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONNOUSERSITE": "1",
        "HOME": workdir,
        "TMP": workdir,
        "TEMP": workdir,
        "NODE_OPTIONS": "",
        "NO_PROXY": "*",
    }
    for k in ("SYSTEMROOT", "SystemRoot", "WINDIR", "COMSPEC"):  # Windows needs these to start processes
        if k in os.environ:
            env[k] = os.environ[k]
    return env


def _posix_limits(mem_mb: int, cpu_s: int):
    def apply():  # runs in the child before exec
        import resource
        mem = mem_mb * 1024 * 1024
        resource.setrlimit(resource.RLIMIT_AS, (mem, mem))
        resource.setrlimit(resource.RLIMIT_CPU, (cpu_s, cpu_s))
        resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))
        resource.setrlimit(resource.RLIMIT_FSIZE, (16 * 1024 * 1024, 16 * 1024 * 1024))
        os.setsid()
    return apply


def run(argv_builder, files: dict[str, str], timeout: float = DEFAULT_TIMEOUT,
        mem_mb: int = DEFAULT_MEM_MB, stdin: str | None = None) -> SandboxResult:
    """Run a command inside a fresh temp dir.

    argv_builder: callable(workdir) -> argv list.
    files: {relative_name: text} written into the temp dir first.
    """
    workdir = tempfile.mkdtemp(prefix="botlab_sbx_")
    t0 = time.monotonic()
    limits = {"timeout_s": timeout, "mem_mb": mem_mb if os.name == "posix" else None,
              "network": "blocked", "cwd": "temp dir (deleted after run)", "env": "stripped"}
    try:
        for name, text in files.items():
            path = os.path.join(workdir, name)
            if os.path.commonpath([os.path.realpath(path), os.path.realpath(workdir)]) != os.path.realpath(workdir):
                raise ValueError("file name escapes sandbox")
            with open(path, "w", encoding="utf-8", newline="\n") as f:
                f.write(text)
        argv = argv_builder(workdir)
        kwargs = dict(cwd=workdir, env=_clean_env(workdir), stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
                      stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if os.name == "posix":
            kwargs["preexec_fn"] = _posix_limits(mem_mb, int(timeout) + 1)
        else:
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        proc = subprocess.Popen(argv, **kwargs)
        try:
            out, err = proc.communicate(input=stdin.encode() if stdin is not None else None, timeout=timeout)
            timed_out = False
        except subprocess.TimeoutExpired:
            _kill_tree(proc)
            out, err = proc.communicate()
            timed_out = True
        out_s = out[:MAX_OUTPUT].decode("utf-8", "replace")
        err_s = err[:MAX_OUTPUT].decode("utf-8", "replace")
        if timed_out:
            err_s = (err_s + f"\n[sandbox] killed after {timeout}s timeout").strip()
        return SandboxResult(ok=(not timed_out and proc.returncode == 0), returncode=proc.returncode,
                             stdout=out_s, stderr=err_s, timed_out=timed_out,
                             duration_s=round(time.monotonic() - t0, 3), limits=limits)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def _kill_tree(proc: subprocess.Popen):
    try:
        if os.name == "posix":
            import signal
            os.killpg(proc.pid, signal.SIGKILL)
        else:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True, timeout=5)
    except Exception:
        pass
    try:
        proc.kill()
    except Exception:
        pass


# ---------- Python ----------
def run_python(code: str, inputs: dict[str, str] | None = None, timeout: float = DEFAULT_TIMEOUT,
               mem_mb: int = DEFAULT_MEM_MB) -> SandboxResult:
    """Run untrusted Python (e.g. LLM-written analysis code) in the sandbox.

    `-I` isolated mode (ignores PYTHON* env vars and user site), `-S` no site import, `-B` no .pyc.
    """
    files = dict(inputs or {})
    files["main.py"] = PY_PRELUDE + "\n" + code
    return run(lambda wd: [sys.executable, "-I", "-S", "-B", "main.py"], files, timeout, mem_mb)


# ---------- Node ----------
def node_available() -> bool:
    return shutil.which("node") is not None


def run_node(script: str, inputs: dict[str, str] | None = None, timeout: float = DEFAULT_TIMEOUT,
             mem_mb: int = DEFAULT_MEM_MB, with_gates: bool = True) -> SandboxResult:
    node = shutil.which("node")
    if not node:
        return SandboxResult(ok=False, returncode=None, stdout="", stderr="node not installed")
    files = dict(inputs or {})
    files["main.js"] = script
    files["netblock.js"] = NODE_NETBLOCK
    if with_gates:
        with open(os.path.join(VENDOR, "gates.js"), encoding="utf-8") as f:
            files["gates.js"] = f.read()

    def argv(wd):
        return [node, "--permission", f"--allow-fs-read={wd}", "--disallow-code-generation-from-strings",
                f"--max-old-space-size={mem_mb}", "--require", "./netblock.js", "main.js"]
    # V8 reserves far more virtual address space than it uses, so an RLIMIT_AS equal to the heap size can stop
    # Node from starting on Linux. The heap stays capped by --max-old-space-size (and by the container limit).
    as_mb = int(os.environ.get("SANDBOX_NODE_AS_MB", max(mem_mb, 4096)))
    return run(argv, files, timeout, as_mb)


GATES_RUNNER_JS = r'''
'use strict';
const fs = require('fs');
const gates = require('./gates.js');
const inp = JSON.parse(fs.readFileSync('./input.json', 'utf8'));
const r = inp.csv != null ? gates.check(inp.csv, inp.trials) : gates.grade(inp.parsed, inp.trials);
const { curve, ...rest } = r;
process.stdout.write(JSON.stringify(rest));
'''


def gates_via_node(parsed: dict | None = None, csv_text: str | None = None, trials: int = 20,
                   timeout: float = DEFAULT_TIMEOUT) -> SandboxResult:
    """Run the original, unmodified gates.js inside the sandbox."""
    payload = {"trials": trials}
    if csv_text is not None:
        payload["csv"] = csv_text
    else:
        payload["parsed"] = parsed
    return run_node(GATES_RUNNER_JS, {"input.json": json.dumps(payload)}, timeout=timeout)
