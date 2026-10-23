"""An ops tool's HTTP goes through the lib — observed, not spelled (#3215).

``test_query_error``'s static gate matched the names a tool calls; every
review found another spelling it missed (``getattr`` with a built string, the
lib's own ``urllib`` attribute, ``sys.modules``, ``exec``, ``asyncio``…).
Here every ``test_json_stdout_contract`` recipe is run again under
``_net_hook_runner``, which logs what the process DID: each
``socket.connect``, each urllib request, each ``curl`` / ``wget``. A
connection opened by an ops frame with no lib frame on the stack is direct.

* A tool not in ``DIRECT_HTTP_ALLOWED`` that connects directly fails.
* Nobody GETs the Prometheus / Alertmanager API (``/api/v1``, ``/api/v2``,
  ``/federate``) directly — those answers are read by the lib (#3133), even
  for a tool allowed to connect directly for something else.
* Nobody shells out to ``curl`` / ``wget``.

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

_RAW_HTTP_MODULES = ("urllib.request", "http.client", "http.server", "socket", "requests")
_LIB_API_PATHS = ("/api/v1/", "/api/v2/", "/federate")


def _imports(path: Path) -> set[str]:
    out: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            out |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            out.add(node.module)
            out |= {f"{node.module}.{a.name}" for a in node.names}
    return out


def _raw_http_importers() -> set[str]:
    return {p.name for p in sorted(OPS_DIR.glob("*.py"))
            if any(m == r or m.startswith(r + ".") for m in _imports(p) for r in _RAW_HTTP_MODULES)}


def _lib_http_names() -> set[str]:
    """What the lib serves over HTTP: its own names, read from the module —
    not a second list."""
    import _lib_prometheus
    return {n for n, v in vars(_lib_prometheus).items()
            if callable(v) and getattr(v, "__module__", "") == "_lib_prometheus" and not n.startswith("_")}


def _http_capable() -> set[str]:
    lib = _lib_http_names() - {"QueryError"}
    out = set()
    for p in sorted(OPS_DIR.glob("*.py")):
        names = _imports(p)
        if any(n.rsplit(".", 1)[-1] in lib for n in names if "_lib_p" in n) or p.name in _raw_http_importers():
            out.add(p.name)
    return out


def _violations(tool: str, events: list[dict]) -> list[str]:
    out = []
    for e in events:
        if e["via_lib"]:
            continue
        if e["event"] == "subprocess.Popen":
            out.append(f"{e['frame']} shells out to {e['argv']}")
        elif e["event"] == "urllib.Request" and e["method"] == "GET" and any(
                p in e["url"] for p in _LIB_API_PATHS):
            out.append(f"{e['frame']} GETs {e['url']} directly — read it through _lib_prometheus")
        elif e["event"] == "socket.connect" and tool not in DIRECT_HTTP_ALLOWED:
            out.append(f"{e['frame']} connects to {e['address']} without the lib")
    return out


_BY_TOOL: dict[str, list] = {}
for _r in contract.RECIPES:
    _path = contract.JSON_TOOL_PATHS[_r.tool]
    if _path.parent.name == "ops":
        _BY_TOOL.setdefault(_path.name, []).append(_r)


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
    ("notification_tester.py", [_ev("urllib.Request", url="http://p/api/v1/query", method="GET")], True),
    ("maintenance_scheduler.py", [_ev("urllib.Request", url="http://am/api/v2/silences", method="POST")], False),
    ("discover_instance_mappings.py", [_ev("urllib.Request", url="http://p/federate?x", method="GET")], True),
    ("discover_instance_mappings.py", [_ev("urllib.Request", url="http://e:9104/metrics", method="GET")], False),
    ("notification_tester.py", [_ev("subprocess.Popen", argv=["curl", "u"])], True),
    ("check_alert.py", [_ev("urllib.Request", via_lib=True, url="http://p/api/v1/alerts", method="GET")], False),
], ids=["not-allowed", "via-lib", "allowed", "allowed-but-reads-the-api", "allowed-write",
        "allowed-but-federate", "allowed-metrics", "curl", "lib-read"])
def test_the_rules(tool, events, bad):
    assert bool(_violations(tool, events)) is bad


def test_the_hook_sees_an_allowed_tools_direct_api_read(tmp_path, ok_url):
    """A tool allowed to connect directly (for a webhook, OPA…) still may not
    GET the Prometheus / Alertmanager API itself — seen through the request,
    not the socket."""
    ops = tmp_path / "scripts" / "tools" / "ops"
    ops.mkdir(parents=True)
    tool = ops / "probe.py"
    tool.write_text(f"import urllib.request\nurllib.request.urlopen({ok_url!r} + '/api/v1/query', "
                    f"timeout=5).read()\n", encoding="utf-8")
    log = tmp_path / "net.jsonl"
    env = dict(os.environ, NET_HOOK_LOG=str(log), NET_HOOK_OPS=str(ops),
               NET_HOOK_LIB=str(contract.TOOLS_DIR / "_lib_prometheus.py"))
    subprocess.run([sys.executable, str(RUNNER), str(tool)], capture_output=True, text=True,
                   encoding="utf-8", timeout=60, env=env, check=True)
    events = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    assert any("/api/v1/query" in v for v in _violations("notification_tester.py", events)), events
