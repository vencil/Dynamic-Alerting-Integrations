"""A request that got no HTTP answer is a failed request — everywhere (#3519).

``_lib_prometheus.TRANSPORT_ERRORS`` is the one list of what a request
raises when it got no usable answer (#3477). A peer that answers with bytes
that are not HTTP raises ``http.client.HTTPException`` (``BadStatusLine``,
``IncompleteRead``…), which is **not** an ``OSError``: a handler that lists
``URLError`` / ``OSError`` itself lets it through, and the tool ends as a
crash instead of its own "could not ask" path.

Two halves:

* **Behaviour** — each shared helper, and each tool-side fetch that was
  moved onto the list, is run against a real socket that answers with
  garbage, and must fail the way its contract says.
* **Static** — every ``try`` around a request (``urlopen``, an opener's
  ``open``, an ``HTTPConnection``, or a function a non-HTTP reply can escape
  from) whose handlers catch a failed connection must also catch
  ``http.client.HTTPException``. What a handler catches is *resolved*, not
  matched by name: a tuple alias, ``TRANSPORT_ERRORS`` itself and a star
  expression are followed to the classes they hold, so taking
  ``HTTPException`` out of the list turns every user of it red.
  ``ALLOWED`` is the ledger of the tries that may stay as they are, each
  with its reason; an entry that no longer matches fails too.

Known limit of the static half: a handler name it cannot resolve (a class
from a third-party package, a name built at run time) counts as catching
nothing — it is neither a claim to handle a failed connection nor a cover.
The behaviour half is the authority for the sites it runs.
"""
from __future__ import annotations

import ast
import builtins
import http.client
import importlib
import os
import socket
import sys
import threading
import urllib.error
from pathlib import Path
from typing import Iterator, Optional

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
TOOLS_DIR = REPO_ROOT / "scripts" / "tools"
for _p in (TOOLS_DIR, TOOLS_DIR / "ops", TOOLS_DIR / "lint", TOOLS_DIR / "dx"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import _lib_prometheus as lp  # noqa: E402

LIB = "scripts/tools/_lib_prometheus.py"

#: (file, function) of a ``try`` around a request that catches a failed
#: connection and not a non-HTTP reply → why it may stay. Both directions are
#: checked: a new one fails, and so does an entry that no longer matches.
ALLOWED: dict[tuple[str, str], str] = {
    ("scripts/tools/dx/run_chaos_soak.py", "fetch_metrics"):
        "#3519, waiting for the owner: on the list, a poll in the middle of a soak that "
        "gets a non-HTTP reply would be a blank CSV row and exit 0 where it is a crash "
        "(exit 2) today — a failed measurement read as a finished soak",
}


# ═══════════════════════════════════════════════════════════════════════════
# Behaviour: a peer that does not speak HTTP
# ═══════════════════════════════════════════════════════════════════════════
_GARBAGE = b"GARBAGE NOT HTTP\r\n\r\n"


@pytest.fixture()
def garbage_peer():
    """``(url, hits)``: a TCP server that answers every connection with
    bytes that are not HTTP; *hits* counts the connections it took."""
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(16)
    hits: list[int] = []

    def serve() -> None:
        while True:
            try:
                conn, _ = srv.accept()
            except OSError:
                return
            try:
                conn.recv(65536)
                hits.append(1)
                conn.sendall(_GARBAGE)
            except OSError:
                pass
            finally:
                conn.close()

    threading.Thread(target=serve, daemon=True).start()
    yield f"http://127.0.0.1:{srv.getsockname()[1]}", hits
    srv.close()


def test_the_list_holds_a_reply_that_is_not_http():
    assert issubclass(http.client.BadStatusLine, lp.TRANSPORT_ERRORS)
    assert issubclass(http.client.IncompleteRead, lp.TRANSPORT_ERRORS)
    assert issubclass(urllib.error.URLError, lp.TRANSPORT_ERRORS)


@pytest.mark.parametrize("helper", ["http_get_json", "http_post_json", "probe_health"])
def test_a_tuple_helper_returns_the_error_of_a_non_http_reply(helper, garbage_peer):
    url, hits = garbage_peer
    answer, err = getattr(lp, helper)(url + "/x", timeout=5)
    assert answer is None
    assert err == "BadStatusLine: GARBAGE NOT HTTP", err   # the type, one line
    assert hits


def test_the_retrying_helper_retries_a_non_http_reply_and_raises_urlerror(
        garbage_peer, monkeypatch):
    url, hits = garbage_peer
    slept: list[float] = []
    monkeypatch.setattr(lp.time, "sleep", slept.append)
    with pytest.raises(urllib.error.URLError) as raised:
        lp.http_request_with_retry(url + "/x", timeout=5, max_retries=3)
    assert len(hits) == 3 and slept == [1, 2]
    assert raised.value.reason == "BadStatusLine: GARBAGE NOT HTTP"
    assert isinstance(raised.value.__cause__, http.client.BadStatusLine)


def test_the_retrying_helper_does_not_retry_a_body_that_is_not_json(monkeypatch):
    """A 2xx whose body is not JSON was read: the same request gives the same
    body. It was never retried, and moving onto the list (which holds
    ``ValueError``) must not start to."""
    calls: list[int] = []

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return b"<html>not json</html>"

    def urlopen(req, data=None, timeout=None):
        calls.append(1)
        return _Resp()

    monkeypatch.setattr(lp.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(lp.time, "sleep", lambda s: pytest.fail("slept: it retried"))
    with pytest.raises(ValueError):
        lp.http_request_with_retry("http://127.0.0.1:1/x")
    assert calls == [1]


def test_a_connection_error_keeps_its_text():
    assert lp.transport_error_text(urllib.error.URLError("refused")) == "<urlopen error refused>"
    assert lp.transport_error_text(OSError("boom"), with_type=True) == "OSError: boom"


def test_the_text_of_a_non_http_reply_is_one_bounded_line():
    text = lp.transport_error_text(http.client.BadStatusLine("a\r\nb\x1b[31m" + "x" * 500))
    assert text.startswith("BadStatusLine: a??b?[31m")
    assert "\n" not in text and "\x1b" not in text and len(text) < 230


def test_discover_instance_mappings_scrape_reports_a_non_http_reply(garbage_peer):
    import discover_instance_mappings as dim
    url, _ = garbage_peer
    assert dim.scrape_metrics_endpoint(url + "/metrics", timeout=5) == (
        None, "BadStatusLine: GARBAGE NOT HTTP")


def test_trk_index_title_scan_is_not_measured_on_a_non_http_reply(
        garbage_peer, monkeypatch, capsys):
    import check_trk_index_coverage as trk
    from _lib_exitcodes import EXIT_CALLER_ERROR
    url, hits = garbage_peer
    monkeypatch.setattr(trk, "_API_HOST", url + "/")
    monkeypatch.setattr(trk, "_API", url + "/repos/{owner}/{repo}/issues")
    monkeypatch.setenv("GH_TOKEN", "x")
    rc = trk.main(["--all-titles", "--ci", "--repo", str(REPO_ROOT)])
    assert rc == EXIT_CALLER_ERROR and hits
    assert "BadStatusLine: GARBAGE NOT HTTP" in capsys.readouterr().err


def test_crd_schema_fetch_fails_named_on_a_non_http_reply(
        garbage_peer, monkeypatch, tmp_path, capsys):
    import generate_crd_schemas as crd
    from _lib_exitcodes import EXIT_CALLER_ERROR
    url, hits = garbage_peer
    sources = tmp_path / "SOURCES.yaml"
    sources.write_text(f"sources:\n  - id: s1\n    url: {url}/crd.yaml\n    kinds: []\n",
                       encoding="utf-8")
    monkeypatch.setattr(crd, "SOURCES", sources)
    monkeypatch.setattr(crd, "_validate_source_url", lambda u: None)  # https-only otherwise
    assert crd.run(check_only=True) == EXIT_CALLER_ERROR and hits
    assert "s1: fetch failed (BadStatusLine: GARBAGE NOT HTTP)" in capsys.readouterr().err


# ═══════════════════════════════════════════════════════════════════════════
# Static: what the handlers around a request catch
# ═══════════════════════════════════════════════════════════════════════════
_SKIP_DIRS = {"node_modules", "tests", "__pycache__"}
_MODULE = "<module>"
_DEFS = (ast.FunctionDef, ast.AsyncFunctionDef)
_TRIES = (ast.Try, getattr(ast, "TryStar", ast.Try))
#: What a failed connection raises: a handler that catches one of these says
#: "the request may fail here".
_CONNECTION_FAILURES = (urllib.error.URLError, ConnectionError, TimeoutError)


def _repo_sources() -> dict[str, str]:
    """Every Python file of the repo that is not a test, by repo-relative path."""
    out: dict[str, str] = {}
    for dirpath, dirnames, filenames in os.walk(REPO_ROOT):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS
                       and (not d.startswith(".") or d == ".github")]
        for name in filenames:
            if name.endswith(".py"):
                path = Path(dirpath, name)
                out[path.relative_to(REPO_ROOT).as_posix()] = path.read_text(encoding="utf-8")
    return out


def _dotted(node: ast.AST) -> Optional[str]:
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        return ".".join([node.id, *reversed(parts)])
    return None


class _File:
    """One parsed file: its imports, module-level names and functions."""

    def __init__(self, rel: str, source: str) -> None:
        self.rel = rel
        self.tree = ast.parse(source)
        self.modules: dict[str, str] = {}             # local name -> module
        self.names: dict[str, tuple[str, str]] = {}   # local name -> (module, attr)
        self.values: dict[str, ast.AST] = {}          # module-level NAME = <expr>
        self.classes: set[str] = set()
        self.openers: set[str] = set()
        self.functions: dict[str, list[ast.AST]] = {_MODULE: [self.tree]}
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Import):
                for a in node.names:
                    self.modules[a.asname or a.name.split(".")[0]] = (
                        a.name if a.asname else a.name.split(".")[0])
            elif isinstance(node, ast.ImportFrom) and node.module:
                for a in node.names:
                    self.names[a.asname or a.name] = (node.module, a.name)
            elif isinstance(node, ast.ClassDef):
                self.classes.add(node.name)
            elif isinstance(node, _DEFS):
                self.functions.setdefault(node.name, []).append(node)
            targets: list[ast.AST] = []
            if isinstance(node, ast.Assign):
                targets = node.targets
            elif isinstance(node, ast.AnnAssign) and node.value is not None:
                targets = [node.target]
            for target in targets:
                if not isinstance(target, ast.Name):
                    continue
                value = node.value
                if isinstance(value, ast.Call) and (_dotted(value.func) or "").endswith("build_opener"):
                    self.openers.add(target.id)
                if node in self.tree.body:
                    self.values[target.id] = value


class _Scan:
    """The judgement over a set of sources (the repo, or a test's own)."""

    def __init__(self, sources: dict[str, str]) -> None:
        self.files = {rel: _File(rel, src) for rel, src in sources.items()}
        self.by_stem: dict[str, _File] = {}
        for rel, f in self.files.items():
            self.by_stem.setdefault(Path(rel).stem, f)
        self.leaky: set[tuple[str, str]] = set()
        while True:                                   # a leak reaches its callers
            found = {(f.rel, name) for f in self.files.values() for name in f.functions
                     if name != _MODULE and self._escapes(f, name)}
            if found == self.leaky:
                break
            self.leaky = found

    # ── what a handler catches ─────────────────────────────────────────
    def _classes(self, f: _File, node: Optional[ast.AST], seen: frozenset = frozenset()) -> list[type]:
        if node is None:                              # bare `except:`
            return [BaseException]
        if isinstance(node, (ast.Tuple, ast.List)):
            return [c for e in node.elts for c in self._classes(f, e, seen)]
        if isinstance(node, ast.Starred):
            return self._classes(f, node.value, seen)
        dotted = _dotted(node)
        if dotted is None:
            return []
        head, _, rest = dotted.partition(".")
        if not rest:
            return self._name(f, head, seen)
        if head in f.modules:                         # urllib.error.URLError, mod.NAME
            return self._attr(f.modules[head], rest, seen)
        if head in f.names:                           # from urllib import error
            module, attr = f.names[head]
            return self._attr(f"{module}.{attr}", rest, seen)
        return []

    def _name(self, f: _File, name: str, seen: frozenset) -> list[type]:
        key = (f.rel, name)
        if key in seen:
            return []
        seen = seen | {key}
        if name in f.values:
            return self._classes(f, f.values[name], seen)
        if name in f.classes:                         # the file's own class
            return []
        if name in f.names:
            module, attr = f.names[name]
            return self._attr(module, attr, seen)
        obj = getattr(builtins, name, None)
        return [obj] if isinstance(obj, type) else []

    def _attr(self, module: str, attr: str, seen: frozenset) -> list[type]:
        """``module.attr`` (attr may be dotted): a stdlib object, or a
        module-level name of a scanned file."""
        parts = attr.split(".")
        other = self.by_stem.get(module.split(".")[-1])
        if other is not None and len(parts) == 1:
            return self._name(other, parts[0], seen)
        if module.split(".")[0] not in sys.stdlib_module_names:
            return []
        obj: object = None
        for split in range(len(parts)):               # urllib + error.URLError
            try:
                obj = importlib.import_module(".".join([module, *parts[:split]]))
            except ImportError:
                break
            for part in parts[split:]:
                obj = getattr(obj, part, None)
            if obj is not None:
                break
        if isinstance(obj, type):
            return [obj]
        if isinstance(obj, tuple):
            return [c for c in obj if isinstance(c, type)]
        return []

    def _caught(self, f: _File, try_node: ast.AST) -> list[type]:
        return [c for h in try_node.handlers for c in self._classes(f, h.type)]

    def _covers(self, f: _File, try_node: ast.AST) -> bool:
        return any(issubclass(http.client.HTTPException, c) for c in self._caught(f, try_node))

    def _expects_failure(self, f: _File, try_node: ast.AST) -> bool:
        caught = self._caught(f, try_node)
        return any(issubclass(failure, c) for failure in _CONNECTION_FAILURES for c in caught)

    # ── where a request is made ────────────────────────────────────────
    def _is_request(self, f: _File, call: ast.Call) -> bool:
        func = call.func
        dotted = _dotted(func) or ""
        last = dotted.rsplit(".", 1)[-1] if dotted else (
            func.attr if isinstance(func, ast.Attribute) else "")
        if last in ("HTTPConnection", "HTTPSConnection"):
            return True
        if isinstance(func, ast.Name):
            if func.id in f.functions:
                return (f.rel, func.id) in self.leaky
            if func.id in f.names:
                module, attr = f.names[func.id]
                other = self.by_stem.get(module.split(".")[-1])
                if other is not None:
                    return (other.rel, attr) in self.leaky
            return func.id == "urlopen"
        if not isinstance(func, ast.Attribute):
            return False
        if func.attr == "urlopen":
            return True
        receiver = func.value
        if func.attr == "open":
            return ((isinstance(receiver, ast.Name) and receiver.id in f.openers)
                    or (isinstance(receiver, ast.Call)
                        and (_dotted(receiver.func) or "").endswith("build_opener")))
        if isinstance(receiver, ast.Name):
            if receiver.id in f.modules:               # mod.func(...)
                other = self.by_stem.get(f.modules[receiver.id].split(".")[-1])
                return other is not None and (other.rel, func.attr) in self.leaky
            if receiver.id in ("self", "cls"):
                return (f.rel, func.attr) in self.leaky
        return False

    def _requests(self, f: _File, name: str) -> Iterator[tuple[ast.Call, list[ast.AST]]]:
        """Each request made in *name*'s own body (not in a function nested in
        it), with the ``try`` statements whose body holds it, outermost first."""
        def walk(node: ast.AST, tries: list[ast.AST]) -> Iterator[tuple[ast.Call, list[ast.AST]]]:
            if isinstance(node, ast.Call) and self._is_request(f, node):
                yield node, tries
            for field, value in ast.iter_fields(node):
                inner = tries + [node] if isinstance(node, _TRIES) and field == "body" else tries
                for child in value if isinstance(value, list) else [value]:
                    if isinstance(child, ast.AST) and not isinstance(
                            child, (*_DEFS, ast.Lambda, ast.ClassDef)):
                        yield from walk(child, inner)

        for root in f.functions[name]:
            for child in ast.iter_child_nodes(root):
                if not isinstance(child, (*_DEFS, ast.ClassDef)):
                    yield from walk(child, [])

    def _escapes(self, f: _File, name: str) -> bool:
        """Can a non-HTTP reply to a request made in *name* leave it?"""
        return any(not any(self._covers(f, t) for t in tries)
                   for _call, tries in self._requests(f, name))

    def judged(self) -> set[tuple[str, str]]:
        """(file, function) of every ``try`` around a request."""
        return {(f.rel, name) for f in self.files.values() for name in f.functions
                for _call, tries in self._requests(f, name) if tries}

    def uncovered(self) -> set[tuple[str, str]]:
        """(file, function) of each ``try`` a non-HTTP reply reaches whose
        handlers expect a failed connection and do not catch it."""
        out: set[tuple[str, str]] = set()
        for f in self.files.values():
            for name in f.functions:
                for _call, tries in self._requests(f, name):
                    for try_node in reversed(tries):   # innermost first
                        if self._covers(f, try_node):
                            break
                        if self._expects_failure(f, try_node):
                            out.add((f.rel, name))
        return out


@pytest.fixture(scope="module")
def repo_scan() -> _Scan:
    return _Scan(_repo_sources())


def test_every_try_around_a_request_catches_a_non_http_reply(repo_scan):
    measured = repo_scan.uncovered()
    new = sorted(measured - set(ALLOWED))
    gone = sorted(set(ALLOWED) - measured)
    assert not new, (
        "a try around a request catches a failed connection but not "
        "http.client.HTTPException (a reply that is not HTTP) — catch "
        f"_lib_prometheus.TRANSPORT_ERRORS instead of listing the types: {new}")
    assert not gone, f"ALLOWED entry that no longer matches — remove it: {gone}"


def test_the_scan_sees_the_requests_it_is_about(repo_scan):
    """No finding must not mean nothing was looked at: the lib's helpers and
    the tool-side fetches are found as requests inside a ``try``, and the
    functions a reply can escape from are found as such."""
    judged = repo_scan.judged()
    for function in ("http_get_json", "http_post_json", "http_request_with_retry", "probe_health"):
        assert (LIB, function) in judged, function
    assert ("scripts/tools/ops/discover_instance_mappings.py", "scrape_metrics_endpoint") in judged
    assert ("scripts/tools/lint/check_trk_index_coverage.py", "main") in judged   # through title_trks
    assert ("scripts/tools/lint/check_trk_index_coverage.py", "title_trks") in repo_scan.leaky
    assert ("scripts/tools/dx/generate_crd_schemas.py", "_fetch") in repo_scan.leaky
    assert (LIB, "http_get_json") not in repo_scan.leaky


def test_the_list_without_httpexception_turns_its_users_red():
    """The handlers are resolved through ``TRANSPORT_ERRORS`` itself: with
    ``HTTPException`` taken out of the real list, every helper that catches
    the list is a finding."""
    sources = _repo_sources()
    whole = "OSError, http.client.HTTPException, ValueError)"
    assert sources[LIB].count(whole) == 1
    sources[LIB] = sources[LIB].replace(whole, "OSError, ValueError)")
    found = _Scan(sources).uncovered()
    assert {(LIB, "http_get_json"), (LIB, "http_post_json"), (LIB, "probe_health"),
            (LIB, "http_request_with_retry"),
            ("scripts/tools/ops/discover_instance_mappings.py", "scrape_metrics_endpoint"),
            ("scripts/tools/lint/check_trk_index_coverage.py", "main"),
            ("scripts/tools/dx/generate_crd_schemas.py", "run")} <= found, sorted(found)


_LIB_STUB = ("import http.client\n"
             "TRANSPORT_ERRORS: tuple = (OSError, http.client.HTTPException, ValueError)\n")
_HEAD = "import urllib.request, urllib.error, http.client\n"


@pytest.mark.parametrize("source,flagged", [
    # its own enumeration, the shape #3519 found
    (_HEAD + "def f(u):\n    try:\n        urllib.request.urlopen(u)\n"
             "    except (urllib.error.URLError, OSError, ValueError):\n        return None\n", ["f"]),
    (_HEAD + "from urllib.error import URLError\nfrom urllib.request import urlopen\n"
             "def f(u):\n    try:\n        urlopen(u)\n    except URLError:\n        pass\n", ["f"]),
    (_HEAD + "def f(u):\n    try:\n        urllib.request.urlopen(u)\n"
             "    except TimeoutError:\n        pass\n", ["f"]),
    # an alias of its own, and a star expression of it
    (_HEAD + "NET = (urllib.error.URLError, OSError)\n"
             "def f(u):\n    try:\n        urllib.request.urlopen(u)\n"
             "    except (*NET, KeyError):\n        pass\n", ["f"]),
    # the request is in a helper the reply escapes from
    (_HEAD + "def get(u):\n    return urllib.request.urlopen(u).read()\n"
             "def f(u):\n    try:\n        get(u)\n    except OSError:\n        pass\n", ["f"]),
    # an opener, and an HTTPConnection
    (_HEAD + "O = urllib.request.build_opener()\n"
             "def f(u):\n    try:\n        O.open(u)\n    except OSError:\n        pass\n", ["f"]),
    (_HEAD + "def f(h):\n    try:\n        http.client.HTTPConnection(h).request('GET', '/')\n"
             "    except ConnectionError:\n        pass\n", ["f"]),
    # at module level
    (_HEAD + "try:\n    urllib.request.urlopen('http://x')\nexcept OSError:\n    pass\n", [_MODULE]),
    # the list
    (_HEAD + "from _lib_prometheus import TRANSPORT_ERRORS\n"
             "def f(u):\n    try:\n        urllib.request.urlopen(u)\n"
             "    except TRANSPORT_ERRORS:\n        pass\n", []),
    (_HEAD + "import _lib_prometheus\n"
             "def f(u):\n    try:\n        urllib.request.urlopen(u)\n"
             "    except (*_lib_prometheus.TRANSPORT_ERRORS, KeyError):\n        pass\n", []),
    # the types named, HTTPException among them — over two handlers too
    (_HEAD + "def f(u):\n    try:\n        urllib.request.urlopen(u)\n"
             "    except urllib.error.HTTPError:\n        pass\n"
             "    except (OSError, http.client.HTTPException):\n        pass\n", []),
    (_HEAD + "def f(u):\n    try:\n        urllib.request.urlopen(u)\n"
             "    except Exception:\n        pass\n", []),
    (_HEAD + "def f(u):\n    try:\n        urllib.request.urlopen(u)\n    except:\n        pass\n", []),
    # a handler for an HTTP status only makes no claim about the connection
    (_HEAD + "def f(u):\n    try:\n        urllib.request.urlopen(u)\n"
             "    except urllib.error.HTTPError:\n        pass\n", []),
    # caught where it is made: the outer try never sees it
    (_HEAD + "from _lib_prometheus import TRANSPORT_ERRORS\n"
             "def get(u):\n    try:\n        return urllib.request.urlopen(u).read()\n"
             "    except TRANSPORT_ERRORS:\n        return None\n"
             "def f(u):\n    try:\n        get(u)\n    except OSError:\n        pass\n", []),
    # not a request: the built-in open, and a request in the handler
    ("def f(p):\n    try:\n        open(p).read()\n    except OSError:\n        pass\n", []),
    (_HEAD + "def f(u):\n    try:\n        pass\n    except OSError:\n"
             "        urllib.request.urlopen(u)\n", []),
], ids=["own-enumeration", "from-import", "timeout-only", "alias-starred", "through-a-helper",
        "opener", "http-connection", "module-level", "the-list", "the-list-starred",
        "httpexception-named", "exception", "bare", "http-status-only", "caught-inside",
        "builtin-open", "request-in-handler"])
def test_the_predicate_on_known_shapes(source, flagged):
    scan = _Scan({"scripts/tools/_lib_prometheus.py": _LIB_STUB, "scripts/tools/t.py": source})
    assert sorted(fn for _rel, fn in scan.uncovered()) == flagged
