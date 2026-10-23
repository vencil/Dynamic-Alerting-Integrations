"""An ops tool's HTTP goes through the lib — observed, not spelled (#3215).

``test_query_error``'s static gate matched the names a tool calls; every
review found another spelling it missed (``getattr`` with a built string, the
lib's own ``urllib`` attribute, ``sys.modules``, ``exec``, ``asyncio``…).
Here every ``test_json_stdout_contract`` recipe is run again under
``_net_hook_runner``, which logs what the tool's process DID, in any thread:
each ``socket.connect``, each HTTP request line, each ``curl`` / ``wget``
however started, each Python child it cannot see into. A connection with no
lib frame on the stack is direct.

* A tool not in ``DIRECT_HTTP_ALLOWED`` that connects directly fails.
* Nobody reads the Prometheus / Alertmanager API directly — any request to
  ``/api/v1/`` (Prometheus also takes queries by POST), a GET / HEAD to
  ``/api/v2/``, anything to ``/federate``: those answers are read by the lib
  (#3133), even by a tool allowed to connect directly for something else.
* Nobody starts ``curl`` / ``wget``, or a Python child that is not a repo tool.
* A tool that can use HTTP and has a recipe must be SEEN: no event logged
  means the hook was not there, not that the tool is clean.

The hook only sees what a recipe runs, so its reach is pinned too: the ops
tools that can use HTTP but have no recipe are ``KNOWN_UNEXERCISED`` (it only
shrinks), and ``DIRECT_HTTP_ALLOWED`` must equal the tools that import a raw
HTTP module — the one list, checked at run time and against the source.
"""
from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

import test_json_stdout_contract as contract  # noqa: E402  (tests/shared on sys.path)
from test_json_stdout_contract import (  # noqa: E402,F401  (fixtures)
    fake_kubectl_dir, sandbox_repo, stub_url)

OPS_DIR = contract.OPS_DIR
RUNNER = Path(__file__).with_name("_net_hook_runner.py")

# Ops tools that open connections themselves, each for something that is not
# a Prometheus / Alertmanager read. The one list: the run-time check holds a
# tool to it, the static check below holds it to the tools' imports.
DIRECT_HTTP_ALLOWED: dict[str, str] = {
    "_federation_revocation_reconciler.py": "queries VictoriaLogs and serves its own endpoint",
    "discover_instance_mappings.py": "scrapes an exporter's /metrics (--endpoint)",
    "generator_service.py": "is a socket server",
    "maintenance_scheduler.py": "writes silences and pushes to the Pushgateway",
    "notification_tester.py": "delivers test notifications to receivers",
    "policy_opa_bridge.py": "queries OPA (its own redirect-refusing opener)",
    "threshold_govern.py": "writes to tenant-api (--apply)",
}

# Ops tools that can use HTTP but that no recipe runs: the hook cannot see
# them. ⛔ Only shrinks — give a tool a recipe and remove it here.
KNOWN_UNEXERCISED: set[str] = {
    "_federation_revocation_reconciler.py",
    "baseline_discovery.py",
    "generator_service.py",
    "validate_migration.py",
}

# A floor for what no recipe runs; the run-time check is the authority.
_RAW_HTTP_MODULES = ("urllib.request", "http.client", "http.server", "socket", "requests",
                     "urllib3", "httpx", "aiohttp", "asyncio", "ssl", "smtplib", "ftplib",
                     "xmlrpc", "importlib")
_DYNAMIC = {"__import__"}


def _ops_files() -> list[Path]:
    out = sorted(p for p in OPS_DIR.rglob("*.py") if "__pycache__" not in p.parts)
    assert out, "no ops tool enumerated"
    return out


def _imports(path: Path) -> set[str]:
    """Modules a file imports, ``module.name`` for a from-import, and the
    dotted attributes it reads (``urllib.request`` after a bare ``import urllib``)."""
    out: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            out |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            out.add(node.module)
            out |= {f"{node.module}.{a.name}" for a in node.names}
        elif isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            out.add(f"{node.value.id}.{node.attr}")
        elif isinstance(node, ast.Name) and node.id in _DYNAMIC:
            out.add(node.id)
    return out


def _is_raw(name: str) -> bool:
    return name in _DYNAMIC or any(name == r or name.startswith(r + ".") for r in _RAW_HTTP_MODULES)


def _raw_http_importers() -> set[str]:
    return {p.relative_to(OPS_DIR).as_posix() for p in _ops_files() if any(map(_is_raw, _imports(p)))}


def _lib_http_names() -> set[str]:
    """What the lib serves over HTTP: its own names, read from the module —
    not a second list."""
    import _lib_prometheus
    return {n for n, v in vars(_lib_prometheus).items()
            if callable(v) and getattr(v, "__module__", "") == "_lib_prometheus" and not n.startswith("_")}


def _http_capable() -> set[str]:
    """Ops tools that can use HTTP: a raw-HTTP import, the lib's own module
    or any name it defines (through the facade too), or a sibling ops module
    that can. Over-approximates (a pure helper counts) — the safe side for a
    coverage pin."""
    lib = _lib_http_names()
    files = {p.relative_to(OPS_DIR).as_posix(): _imports(p) for p in _ops_files()}
    stems = {Path(k).stem: k for k in files}
    out = {k for k, names in files.items()
           if any(map(_is_raw, names))
           or any(n == "_lib_prometheus" or n.startswith("_lib_prometheus.")
                  or (n.startswith(("_lib_python.", "_lib_prometheus.")) and n.rsplit(".", 1)[-1] in lib)
                  for n in names)}
    while True:
        more = {k for k, names in files.items() if k not in out
                and any(stems.get(n.split(".")[0]) in out for n in names)}
        if not more:
            return out
        out |= more


def _reads_the_api(method: str, path: str) -> bool:
    return (path.startswith("/api/v1/") or path.startswith("/federate")
            or (path.startswith("/api/v2/") and method in ("GET", "HEAD")))


def _violations(tool: str, events: list[dict]) -> list[str]:
    out = []
    for e in events:
        if e["event"] == "fetcher":
            out.append(f"{e['frame']} starts {e['argv']}")
        elif e["event"] == "python-child":
            out.append(f"{e['frame']} starts a Python child the hook cannot see into: {e['argv']}")
        elif e["via_lib"]:
            continue
        elif e["event"] == "request" and _reads_the_api(e["method"], e["path"]):
            out.append(f"{e['frame']} sends {e['method']} {e['path']} directly — read it through _lib_prometheus")
        elif e["event"] == "socket.connect" and tool not in DIRECT_HTTP_ALLOWED:
            out.append(f"{e['frame']} connects to {e['address']} without the lib")
    return out


_BY_TOOL: dict[str, list] = {}
for _r in contract.RECIPES:
    _path = contract.JSON_TOOL_PATHS[_r.tool]
    if _path.parent.name == "ops":
        _BY_TOOL.setdefault(_path.relative_to(contract.OPS_DIR.relative_to(contract.REPO_ROOT)).as_posix(), []).append(_r)


@pytest.mark.parametrize("tool", sorted(_BY_TOOL))
def test_a_tools_http_goes_through_the_lib(tool, tmp_path, stub_url, sandbox_repo,
                                           fake_kubectl_dir):
    if os.name == "nt":
        pytest.skip("the recipes' fake kubectl does not intercept on Windows (see the contract test)")
    events: list[dict] = []
    for i, recipe in enumerate(_BY_TOOL[tool]):
        log = tmp_path / f"net-{i}.jsonl"
        work = tmp_path / f"r{i}"
        work.mkdir()
        contract._run(recipe, work, stub_url, sandbox_repo, fake_kubectl_dir, net_log=log)
        if log.exists():
            events += [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    bad = _violations(tool, events)
    assert not bad, f"{tool}: {bad}"
    if tool in _http_capable():
        assert events, (f"{tool} can use HTTP and has recipes, but the hook logged nothing: "
                        "the recipes no longer reach the network, or the hook is not running")


def test_the_allowlist_is_the_tools_that_import_raw_http():
    measured = _raw_http_importers()
    assert set(DIRECT_HTTP_ALLOWED) == measured, (
        f"new raw-HTTP importer (read through _lib_prometheus, or add it with a reason): "
        f"{sorted(measured - set(DIRECT_HTTP_ALLOWED))}; no longer one, remove it: "
        f"{sorted(set(DIRECT_HTTP_ALLOWED) - measured)}")


def test_the_unexercised_set_is_exact():
    measured = _http_capable() - set(_BY_TOOL)
    assert measured, "no HTTP-capable tool found without a recipe — check the enumeration"
    assert measured == KNOWN_UNEXERCISED, (
        f"HTTP-capable ops tool with no recipe (give it one): {sorted(measured - KNOWN_UNEXERCISED)}; "
        f"has a recipe now, remove it (only shrinks): {sorted(KNOWN_UNEXERCISED - measured)}")


# ── the hook itself ──────────────────────────────────────────────────
# Each spelling a review found, run for real under the runner: the hook must
# log it as direct. The static gate missed most of them.

class _Ok(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"ok")

    do_POST = do_GET


@pytest.fixture(scope="module")
def ok_url():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Ok)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


_PROBES = {
    "urlopen": "import urllib.request\nurllib.request.urlopen(U + '/api/v1/x', timeout=5).read()",
    "lib-attribute": "import _lib_prometheus as L\nL.urllib.request.urlopen(U + '/api/v1/x', timeout=5).read()",
    "built-name": "import _lib_prometheus as L\ngetattr(L.urllib.request, 'url' + 'open')(U + '/api/v1/x', timeout=5).read()",
    "sys-modules": "import sys, _lib_prometheus\nsys.modules['urllib.request'].urlopen(U + '/api/v1/x', timeout=5).read()",
    "built-import": "import builtins\ngetattr(builtins, '__imp' + 'ort__')('urllib.request', fromlist=['x']).urlopen(U + '/api/v1/x', timeout=5).read()",
    "exec": "exec(\"import urllib.request as r; r.urlopen(U + '/api/v1/x', timeout=5).read()\")",
    "http-client": "import http.client, urllib.parse as p\nu = p.urlparse(U)\nc = http.client.HTTPConnection(u.hostname, u.port, timeout=5)\nc.request('GET', '/api/v1/x'); c.getresponse().read()",
    "asyncio": "import asyncio, urllib.parse as p\nasync def m():\n    u = p.urlparse(U); r, w = await asyncio.open_connection(u.hostname, u.port)\n    w.write(b'GET /api/v1/x HTTP/1.0\\r\\n\\r\\n'); await r.read()\nasyncio.run(m())",
    "curl": "import subprocess\nsubprocess.run(['curl', '-s', '-o', '/dev/null', U], timeout=5, check=False)",
    "curl-shell": "import subprocess\nsubprocess.run('curl -s -o /dev/null ' + U, shell=True, timeout=5)",
    "curl-env": "import subprocess\nsubprocess.run(['env', 'curl', '-s', '-o', '/dev/null', U], timeout=5)",
    "curl-os-system": "import os\nos.system('curl -s -o /dev/null ' + U)",
    "thread": "import threading, urllib.request\nt = threading.Thread(target=urllib.request.urlopen, args=(U + '/x',))\nt.start(); t.join()",
    "executor": "import concurrent.futures as cf, urllib.request\nwith cf.ThreadPoolExecutor(2) as ex:\n    list(ex.map(lambda u: urllib.request.urlopen(u, timeout=5).read(), [U + '/x']))",
    "python-child": "import subprocess, sys\nsubprocess.run([sys.executable, '-c', 'import urllib.request as r; r.urlopen(%r).read()' % (U + '/x',)], timeout=30)",
}


@pytest.mark.parametrize("name", sorted(_PROBES))
def test_the_hook_sees_every_spelling(name, tmp_path, ok_url):
    ops = tmp_path / "scripts" / "tools" / "ops"
    ops.mkdir(parents=True)
    tool = ops / "probe.py"
    tool.write_text(f"import sys\nsys.path.insert(0, {str(contract.TOOLS_DIR)!r})\n"
                    f"U = {ok_url!r}\n{_PROBES[name]}\n", encoding="utf-8")
    log = tmp_path / "net.jsonl"
    env = dict(os.environ, NET_HOOK_LOG=str(log), NET_HOOK_OPS=str(ops),
               NET_HOOK_LIB=str(contract.TOOLS_DIR / "_lib_prometheus.py"))
    proc = subprocess.run([sys.executable, str(RUNNER), str(tool)], capture_output=True,
                          text=True, encoding="utf-8", timeout=60, env=env)
    if name == "curl" and "No such file" in proc.stderr + proc.stdout:
        pytest.skip("no curl on this host")
    events = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()] \
        if log.exists() else []
    assert _violations("probe.py", events), (name, events, proc.stderr[-300:])


def test_the_hook_passes_a_read_through_the_lib(tmp_path, ok_url):
    ops = tmp_path / "scripts" / "tools" / "ops"
    ops.mkdir(parents=True)
    tool = ops / "probe.py"
    tool.write_text(f"import sys\nsys.path.insert(0, {str(contract.TOOLS_DIR)!r})\n"
                    f"import _lib_prometheus as L\nprint(L.probe_health({ok_url!r} + '/-/healthy'))\n",
                    encoding="utf-8")
    log = tmp_path / "net.jsonl"
    env = dict(os.environ, NET_HOOK_LOG=str(log), NET_HOOK_OPS=str(ops),
               NET_HOOK_LIB=str(contract.TOOLS_DIR / "_lib_prometheus.py"))
    proc = subprocess.run([sys.executable, str(RUNNER), str(tool)], capture_output=True,
                          text=True, encoding="utf-8", timeout=60, env=env)
    assert "ok" in proc.stdout, proc.stderr
    events = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    assert events and all(e["via_lib"] for e in events), events
    assert not _violations("probe.py", events)


def _ev(event, via_lib=False, **kw):
    return {"event": event, "frame": "t.py", "via_lib": via_lib, **kw}


@pytest.mark.parametrize("tool,events,bad", [
    ("check_alert.py", [_ev("socket.connect", address="x")], True),
    ("check_alert.py", [_ev("socket.connect", via_lib=True, address="x")], False),
    ("notification_tester.py", [_ev("socket.connect", address="x")], False),
    ("notification_tester.py", [_ev("request", path="/api/v1/query", method="GET")], True),
    ("notification_tester.py", [_ev("request", path="/api/v1/query", method="POST")], True),
    ("notification_tester.py", [_ev("request", path="/api/v2/alerts", method="HEAD")], True),
    ("maintenance_scheduler.py", [_ev("request", path="/api/v2/silences", method="POST")], False),
    ("discover_instance_mappings.py", [_ev("request", path="/federate", method="GET")], True),
    ("discover_instance_mappings.py", [_ev("request", path="/metrics", method="GET")], False),
    ("notification_tester.py", [_ev("fetcher", argv=["sh", "-c", "curl u"])], True),
    ("check_alert.py", [_ev("python-child", argv=["python3", "-c", "x"])], True),
    ("check_alert.py", [_ev("request", via_lib=True, path="/api/v1/alerts", method="GET")], False),
], ids=["not-allowed", "via-lib", "allowed", "allowed-but-reads-the-api", "post-query-is-a-read",
        "head-v2", "allowed-write", "allowed-but-federate", "allowed-metrics", "curl-via-shell",
        "python-child", "lib-read"])
def test_the_rules(tool, events, bad):
    assert bool(_violations(tool, events)) is bad


@pytest.mark.parametrize("read", [
    "import urllib.request\nurllib.request.urlopen(U + '/api/v1/query', timeout=5).read()",
    "import urllib.request\nurllib.request.urlopen(U + '/api/v1/query', data=b'query=up', timeout=5).read()",
    "import http.client, urllib.parse as p\nu = p.urlparse(U)\nc = http.client.HTTPConnection(u.hostname, u.port, timeout=5)\nc.request('GET', '/api/v1/query'); c.getresponse().read()",
], ids=["urllib-get", "urllib-post", "http-client"])
def test_the_hook_sees_an_allowed_tools_direct_api_read(tmp_path, ok_url, read):
    """A tool allowed to connect directly (for a webhook, OPA…) still may not
    GET the Prometheus / Alertmanager API itself — seen through the request,
    not the socket."""
    ops = tmp_path / "scripts" / "tools" / "ops"
    ops.mkdir(parents=True)
    tool = ops / "probe.py"
    tool.write_text(f"U = {ok_url!r}\n{read}\n", encoding="utf-8")
    log = tmp_path / "net.jsonl"
    env = dict(os.environ, NET_HOOK_LOG=str(log), NET_HOOK_OPS=str(ops),
               NET_HOOK_LIB=str(contract.TOOLS_DIR / "_lib_prometheus.py"))
    subprocess.run([sys.executable, str(RUNNER), str(tool)], capture_output=True, text=True,
                   encoding="utf-8", timeout=60, env=env, check=True)
    events = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    assert any("/api/v1/query" in v for v in _violations("notification_tester.py", events)), events
