"""A request that got no HTTP answer is a failed request — everywhere (#3519).

``_lib_prometheus.TRANSPORT_ERRORS`` is the one list of what a request
raises when it got no usable answer (#3477). A peer that answers with bytes
that are not HTTP raises ``http.client.HTTPException`` (``BadStatusLine``,
``IncompleteRead``…), which is **not** an ``OSError``: a handler that lists
``URLError`` / ``OSError`` itself lets it through, and the tool ends as a
crash instead of its own "could not ask" path.

Two halves:

* **Behaviour** — each shared helper, each tool-side fetch that was moved
  onto the list, and every ``--json`` recipe that takes a URL, is run
  against a real socket that misbehaves. The failure must be the one the
  contract names, and its text one bounded line with no control character:
  the peer chose those bytes.
* **Static** — every ``try`` around a request whose handlers catch a failed
  connection must also catch ``http.client.HTTPException``. What a handler
  catches is *resolved*, not matched by name: a tuple alias, a ``+`` of
  tuples, ``TRANSPORT_ERRORS`` itself and a star expression are followed to
  the classes they hold, so taking ``HTTPException`` out of the list turns
  every user of it red. ``ALLOWED`` is the ledger of the tries that may
  stay as they are, each with its reason; an entry that no longer matches
  fails too.

What the static half takes for a request: ``urlopen`` / ``urlretrieve``
(under any import alias, through ``getattr`` with a literal name, or a name
bound to one — directly or by ``functools.partial``), an opener's ``open``,
a method of an ``HTTPConnection`` / ``HTTPSConnection``, and a function a
non-HTTP reply can escape from — in the same file, imported (through a
re-exporting module too), a method of the same class or of an object built
from a class of the file — whether it is called or handed on (``map(f, …)``).

⛔ Known limits of the static half — each is a spelling it does NOT judge.
The behaviour half is the authority for the sites it runs.

* A handler name it cannot resolve — a class of a third-party package, a
  name bound more than once, a tuple built by anything but a literal, ``+``
  or a star — counts as catching **nothing**: neither a claim to handle a
  failed connection nor a cover. So ``except (OSError, *ERRS)`` with an
  ``ERRS`` it cannot resolve is a finding even if ``ERRS`` holds
  ``HTTPException``: name the list where it can be read, or enter the try
  in ``ALLOWED`` (pinned by ``unresolved-…`` below).
* A handler that only re-raises (``except Exception: raise``) is not there:
  it neither covers nor claims. One that raises something else (``raise
  X(...) from exc``) counts as handling what it catches.
* "Expects a failed connection" means: catches (a parent or a child of)
  ``URLError``, ``ConnectionError``, ``TimeoutError``, ``socket.gaierror``
  / ``herror`` or ``ssl.SSLError``, or a class of the same file derived
  from ``OSError`` — ``HTTPError`` and its children apart (a handler for an
  HTTP status). ``FileNotFoundError`` and the other file errors are not a
  claim about the request.
* Not seen as a request: a lambda called where it is written, ``getattr``
  with a computed name, a method of an object whose class the file does not
  show (a parameter, a returned object), a response read in another ``try``
  than the one that opened it (``IncompleteRead`` from ``r.read()``), a
  third-party client (``requests``, ``httpx``…), ``curl`` in a subprocess.
* Functions nested in another are looked up by their bare name; two of the
  same name in one file are judged together (over-approximates).
"""
from __future__ import annotations

import ast
import builtins
import http.client
import importlib
import socket
import ssl
import sys
import threading
import urllib.error
from pathlib import Path
from typing import Iterator, Optional

import pytest

from _tree import REPO_ROOT, repo_files

TOOLS_DIR = REPO_ROOT / "scripts" / "tools"
for _p in (TOOLS_DIR, TOOLS_DIR / "ops", TOOLS_DIR / "lint", TOOLS_DIR / "dx"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import _lib_prometheus as lp  # noqa: E402
from _lib_label import safe_label  # noqa: E402

import test_json_stdout_contract as contract  # noqa: E402  (tests/shared on sys.path)
from test_json_stdout_contract import fake_kubectl_dir, sandbox_repo  # noqa: E402,F401  (fixtures)

LIB = "scripts/tools/_lib_prometheus.py"
SOAK = "scripts/tools/dx/run_chaos_soak.py"

#: (file, function) of a ``try`` around a request that catches a failed
#: connection and not a non-HTTP reply → why it may stay. Both directions are
#: checked: a new one fails, and so does an entry that no longer matches.
ALLOWED: dict[tuple[str, str], str] = {
    (SOAK, "fetch_metrics"):
        "the poll in the middle of a soak. A closed connection or a timeout there is "
        "already a blank CSV row of a soak that exits 0 (its own ticket); a non-HTTP "
        "reply is deliberately NOT added to that — it leaves fetch_metrics and ends the "
        "run non-zero. The first probe catches it in main() and ends at exit 2 "
        "(tests/dx/test_run_chaos_soak.py)",
}


# ═══════════════════════════════════════════════════════════════════════════
# Behaviour: peers that misbehave
# ═══════════════════════════════════════════════════════════════════════════
_GARBAGE = b"GARBAGE NOT HTTP\r\n\r\n"
_ESC, _BEL = "\x1b", "\x07"

#: Peers whose bytes end up in the error text. Each carries ESC and BEL, a
#: line break, or more than the text may keep.
_HOSTILE = {
    # not HTTP at all: ESC, a second line, a NUL, 5000 characters
    "not-http": b"\x1b[31mEVIL\x1b[0m\x07\r\nline2\x00 " + b"A" * 5000 + b"\r\n\r\n",
    # an HTTP error whose reason phrase is the payload (HTTPError: an OSError)
    "reason-500": b"HTTP/1.1 500 \x1b[31mPWN\x1b[0m\x07 " + b"R" * 3000
                  + b"\r\nContent-Length: 0\r\n\r\n",
    # a redirect urllib refuses, naming the Location
    "redirect-scheme": b"HTTP/1.1 302 Found\r\nLocation: gopher://h/\x1b]0;PWN\x07"
                       + b"Z" * 2000 + b"\r\nContent-Length: 0\r\n\r\n",
    # urllib's own three-line message
    "redirect-loop": b"HTTP/1.1 302 Found\r\nLocation: /x\r\nContent-Length: 0\r\n\r\n",
}


def _ok(body: bytes) -> bytes:
    return b"HTTP/1.1 200 OK\r\nContent-Length: %d\r\n\r\n" % len(body) + body


@pytest.fixture()
def peer():
    """``peer(reply) -> (url, hits)``: a TCP server that answers every
    connection with *reply*; *hits* counts the connections it took."""
    servers: list[socket.socket] = []

    def start(reply: bytes) -> tuple[str, list[int]]:
        srv = socket.socket()
        srv.bind(("127.0.0.1", 0))
        srv.listen(64)
        servers.append(srv)
        hits: list[int] = []

        def answer(conn: socket.socket) -> None:
            try:
                conn.settimeout(5)
                conn.recv(65536)
                hits.append(1)
                conn.sendall(reply)
            except OSError:
                pass
            finally:
                conn.close()

        def serve() -> None:
            while True:
                try:
                    conn, _ = srv.accept()
                except OSError:
                    return
                threading.Thread(target=answer, args=(conn,), daemon=True).start()

        threading.Thread(target=serve, daemon=True).start()
        return f"http://127.0.0.1:{srv.getsockname()[1]}", hits

    yield start
    for srv in servers:
        srv.close()


def _one_bounded_line(text: str) -> None:
    assert text == safe_label(text), repr(text)            # no control character
    assert not any(c in text for c in "\r\n" + _ESC + _BEL), repr(text)
    assert len(text) <= lp.ERROR_TEXT_MAX + 40, len(text)  # the cut, "…", a type name


def test_the_list_holds_a_reply_that_is_not_http():
    assert issubclass(http.client.BadStatusLine, lp.TRANSPORT_ERRORS)
    assert issubclass(http.client.IncompleteRead, lp.TRANSPORT_ERRORS)
    assert issubclass(urllib.error.URLError, lp.TRANSPORT_ERRORS)


@pytest.mark.parametrize("helper", ["http_get_json", "probe_health"])
def test_a_tuple_helper_returns_the_error_of_a_non_http_reply(helper, peer):
    url, hits = peer(_GARBAGE)
    answer, err = getattr(lp, helper)(url + "/x", timeout=5)
    assert answer is None
    assert err == "BadStatusLine: GARBAGE NOT HTTP", err   # the type, one line
    assert hits


@pytest.mark.parametrize("helper", ["http_get_json", "probe_health"])
@pytest.mark.parametrize("hostile", sorted(_HOSTILE))
def test_a_helpers_error_is_one_bounded_line_whatever_the_peer_sent(helper, hostile, peer):
    url, hits = peer(_HOSTILE[hostile])
    answer, err = getattr(lp, helper)(url + "/x", timeout=5)
    assert answer is None and hits
    _one_bounded_line(err)


@pytest.mark.parametrize("exc,text", [
    (urllib.error.URLError("refused"), "<urlopen error refused>"),
    # also an HTTPException, but a connection error first: its text, no type
    (http.client.RemoteDisconnected("Remote end closed connection without response"),
     "Remote end closed connection without response"),
    (http.client.BadStatusLine("x" * lp.ERROR_TEXT_MAX), "BadStatusLine: " + "x" * lp.ERROR_TEXT_MAX),
    (http.client.BadStatusLine("x" * (lp.ERROR_TEXT_MAX + 1)),
     "BadStatusLine: " + "x" * lp.ERROR_TEXT_MAX + "…"),
    (http.client.IncompleteRead(b"ab", 7), "IncompleteRead: IncompleteRead(2 bytes read, 7 more expected)"),
    (http.client.BadStatusLine("a\r\nb\x1b[31m\x85c"), "BadStatusLine: a??b?[31m?c"),
    (OSError("a\nb\x07" + "y" * 300), "a?b?" + "y" * (lp.ERROR_TEXT_MAX - 4) + "…"),
    (urllib.error.HTTPError("http://h/", 500, "\x1b[0mR", None, None), "HTTP Error 500: ?[0mR"),
], ids=["urlerror", "remote-disconnected", "at-the-cut", "past-the-cut", "incomplete-read",
        "control-characters", "oserror-cleaned-and-cut", "http-error-reason"])
def test_the_error_text(exc, text):
    assert lp.ERROR_TEXT_MAX == 200
    assert lp.transport_error_text(exc) == text


def test_the_error_text_names_the_type_when_asked():
    assert lp.transport_error_text(OSError("boom"), with_type=True) == "OSError: boom"
    assert lp.transport_error_text(http.client.LineTooLong("status line"), with_type=True) == (
        "LineTooLong: got more than 65536 bytes when reading status line")


# Every --json recipe that is given the stub URL, against each hostile peer:
# whatever the tool prints, the peer's bytes do not reach the terminal raw.
#: (recipe id, hostile peer) whose output is NOT held to the rule, and why: a
#: text this lib does not write. Checked both ways — the pair must still
#: break the rule, or the entry goes.
_NOT_THIS_LIBS_TEXT: dict[tuple[str, str], str] = {
    ("policy_opa_bridge[opa-url]", "redirect-scheme"):
        "policy_opa_bridge writes its own OPA error line (call_opa_rest): the Location of a "
        "refused redirect is printed uncut (control characters are already shown as `?`). "
        "Found by this test in #3519; its own ticket",
}


def _takes_the_url(recipe, tmp_path: Path) -> bool:
    probe = tmp_path / "probe"
    probe.mkdir()
    return any("http://stub.invalid:1" in a for a in recipe.build(probe, "http://stub.invalid:1"))


def _raw_peer_bytes(text: str) -> Optional[str]:
    if _ESC in text or _BEL in text:
        return "an ESC or BEL reached the output"
    if "R" * (lp.ERROR_TEXT_MAX + 1) in text:
        return "the reason phrase was not cut"
    if "Z" * (lp.ERROR_TEXT_MAX + 1) in text:
        return "the Location was not cut"
    # urllib's redirect-loop message is three lines; its 2nd must not start
    # a line of the tool's output.
    if any(line.startswith("The last 30x error message was:") for line in text.splitlines()):
        return "urllib's message is on more than one line"
    return None


@pytest.mark.parametrize("recipe", contract.RECIPES, ids=lambda r: r.id)
@pytest.mark.parametrize("hostile", ["reason-500", "redirect-scheme", "redirect-loop"])
def test_a_tools_output_holds_no_raw_bytes_of_the_peer(recipe, hostile, tmp_path, peer,
                                                      sandbox_repo, fake_kubectl_dir):
    """Every --json recipe that is given the stub URL, against each hostile
    peer: whatever the tool prints, the peer's bytes do not reach the
    terminal raw."""
    if not _takes_the_url(recipe, tmp_path):
        pytest.skip("the recipe is not given a URL")
    url, _hits = peer(_HOSTILE[hostile])
    work = tmp_path / "run"
    work.mkdir()
    proc = contract._run(recipe, work, url, sandbox_repo, fake_kubectl_dir)
    faults = [fault for stream in (proc.stdout, proc.stderr)
              if (fault := _raw_peer_bytes(stream.decode("utf-8", "replace")))]
    excuse = _NOT_THIS_LIBS_TEXT.get((recipe.id, hostile))
    if excuse is not None:
        assert faults, f"{recipe.id} / {hostile} is clean now — remove it from _NOT_THIS_LIBS_TEXT"
        return
    assert not faults, (faults, proc.stderr[-400:])


def test_the_url_recipes_are_there(tmp_path):
    """The test above skips a recipe that takes no URL: it must not skip all."""
    taking = {r.tool for r in contract.RECIPES if _takes_the_url(r, _fresh(tmp_path))}
    assert {"byo_check", "federation_check", "shadow_verify", "check_alert", "alert_correlate",
            "blind_spot_discovery", "discover_instance_mappings"} <= taking, sorted(taking)
    assert {rid for rid, _hostile in _NOT_THIS_LIBS_TEXT} <= {r.id for r in contract.RECIPES}


def _fresh(tmp_path: Path) -> Path:
    n = len(list(tmp_path.iterdir()))
    out = tmp_path / f"p{n}"
    out.mkdir()
    return out


def test_discover_instance_mappings_scrape_reports_a_non_http_reply(peer):
    import discover_instance_mappings as dim
    url, _ = peer(_GARBAGE)
    assert dim.scrape_metrics_endpoint(url + "/metrics", timeout=5) == (
        None, "BadStatusLine: GARBAGE NOT HTTP")


@pytest.fixture()
def trk_at(monkeypatch):
    """check_trk_index_coverage with its GitHub host pointed at a peer."""
    import check_trk_index_coverage as trk

    def point(url: str):
        monkeypatch.setattr(trk, "_API_HOST", url + "/")
        monkeypatch.setattr(trk, "_API", url + "/repos/{owner}/{repo}/issues")
        monkeypatch.setenv("GH_TOKEN", "x")
        return trk
    return point


def test_trk_index_title_scan_is_not_measured_on_a_non_http_reply(peer, trk_at, capsys):
    from _lib_exitcodes import EXIT_CALLER_ERROR
    url, hits = peer(_GARBAGE)
    rc = trk_at(url).main(["--all-titles", "--ci", "--repo", str(REPO_ROOT)])
    assert rc == EXIT_CALLER_ERROR and hits
    assert "BadStatusLine: GARBAGE NOT HTTP" in capsys.readouterr().err


@pytest.mark.parametrize("body", [b"null", b'{"a": 1}', b"[1, 2]", b'"x"', b'[{"title": 5}]',
                                  b"<html>\xff\xfe"],
                         ids=["null", "object", "numbers", "string", "title-not-a-string", "not-utf8"])
def test_trk_index_title_scan_is_not_measured_on_a_200_of_the_wrong_shape(
        body, peer, trk_at, capsys):
    """#3519 (F4): the answer is JSON (or not even that) but not a list of
    issue objects — "could not measure", never a crash and never "no TRK"."""
    from _lib_exitcodes import EXIT_CALLER_ERROR
    url, hits = peer(_ok(body))
    rc = trk_at(url).main(["--all-titles", "--ci", "--repo", str(REPO_ROOT)])
    assert rc == EXIT_CALLER_ERROR and hits
    assert "量不到：issue 標題抓不到" in capsys.readouterr().err


@pytest.fixture()
def crd_at(monkeypatch, tmp_path):
    """generate_crd_schemas with one source, served by a peer over http."""
    import generate_crd_schemas as crd

    def point(url: str):
        sources = tmp_path / "SOURCES.yaml"
        sources.write_text(
            f"sources:\n  - id: s1\n    url: {url}/crd.yaml\n"
            "    kinds: [{group: g, kind: K, version: v1}]\n", encoding="utf-8")
        monkeypatch.setattr(crd, "SOURCES", sources)
        monkeypatch.setattr(crd, "_validate_source_url", lambda u: None)  # https-only otherwise
        return crd
    return point


def test_crd_schema_fetch_fails_named_on_a_non_http_reply(peer, crd_at, capsys):
    from _lib_exitcodes import EXIT_CALLER_ERROR
    url, hits = peer(_GARBAGE)
    assert crd_at(url).run(check_only=True) == EXIT_CALLER_ERROR and hits
    assert "s1: fetch failed (BadStatusLine: GARBAGE NOT HTTP)" in capsys.readouterr().err


@pytest.mark.parametrize("body,said", [
    (b"kind: \xff\xfe", "not UTF-8"),
    (b"apiVersion: v1\nkind: CustomResourceDefinition\n", "KeyError: 'spec'"),
    (b"apiVersion: v1\nkind: CustomResourceDefinition\nspec: 3\n", "TypeError"),
    (b"apiVersion: v1\nkind: CustomResourceDefinition\nspec: {group: g, names: {kind: K}, versions: [1]}\n",
     "TypeError"),
], ids=["not-utf8", "no-spec", "spec-not-an-object", "version-not-an-object"])
def test_crd_schema_source_that_is_not_a_crd_fails_named(body, said, peer, crd_at, capsys):
    """#3519 (F5): the source answered 200 with something that is not a CRD
    file — a named exit 2, not a crash."""
    from _lib_exitcodes import EXIT_CALLER_ERROR
    url, hits = peer(_ok(body))
    assert crd_at(url).run(check_only=True) == EXIT_CALLER_ERROR and hits
    err = capsys.readouterr().err
    assert "s1: the source did not send a CRD file" in err and said in err, err


# ═══════════════════════════════════════════════════════════════════════════
# Static: what the handlers around a request catch
# ═══════════════════════════════════════════════════════════════════════════
_MODULE = "<module>"
_DEFS = (ast.FunctionDef, ast.AsyncFunctionDef)
_TRIES = (ast.Try, getattr(ast, "TryStar", ast.Try))
_REQUEST_FUNCS = ("urlopen", "urlretrieve")
_CONNECTIONS = ("HTTPConnection", "HTTPSConnection")
#: The methods of an HTTP(S)Connection that talk to the peer.
_CONNECTION_IO = ("request", "getresponse", "connect", "endheaders", "send", "putrequest")
#: What a failed connection raises (see "Expects a failed connection" above).
_CONNECTION_FAILURES = (urllib.error.URLError, ConnectionError, TimeoutError,
                        socket.gaierror, socket.herror, ssl.SSLError)


def _repo_sources() -> dict[str, str]:
    """Every Python file of the repository that is not a test, by its
    repo-relative path (``git ls-files``: an untracked ``venv/`` is not it)."""
    out = {}
    for path in repo_files(".py"):
        rel = path.relative_to(REPO_ROOT).as_posix()
        if not rel.startswith("tests/"):
            out[rel] = path.read_text(encoding="utf-8")
    return out


def _dotted(node: ast.AST) -> Optional[str]:
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        return ".".join([node.id, *reversed(parts)])
    return None


def _calls(node: ast.AST, names: tuple[str, ...]) -> bool:
    """*node* is a call of something whose last name is one of *names*."""
    return (isinstance(node, ast.Call)
            and (_dotted(node.func) or "").rsplit(".", 1)[-1] in names)


class _File:
    """One parsed file: its imports, what its names are bound to, its scopes."""

    def __init__(self, rel: str, source: str) -> None:
        self.rel = rel
        self.tree = ast.parse(source)
        self.modules: dict[str, str] = {}             # local name -> module
        self.names: dict[str, tuple[str, str]] = {}   # local name -> (module, attr)
        self.values: dict[str, list[ast.AST]] = {}    # NAME = <expr>, anywhere
        self.attr_values: dict[str, list[ast.AST]] = {}   # <obj>.NAME = <expr>
        self.classes: dict[str, ast.ClassDef] = {}
        #: scope -> its root nodes. "<module>", "f", "Class.method"; a lambda
        #: bound to a name is the scope of that name.
        self.functions: dict[str, list[ast.AST]] = {_MODULE: [self.tree]}
        self._collect(self.tree, None)
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Import):
                for a in node.names:
                    self.modules[a.asname or a.name.split(".")[0]] = (
                        a.name if a.asname else a.name.split(".")[0])
            elif isinstance(node, ast.ImportFrom) and node.module:
                for a in node.names:
                    self.names[a.asname or a.name] = (node.module, a.name)
            targets: list[ast.AST] = []
            if isinstance(node, ast.Assign):
                targets = node.targets
            elif isinstance(node, ast.AnnAssign) and node.value is not None:
                targets = [node.target]
            for target in targets:
                if isinstance(target, ast.Name):
                    self.values.setdefault(target.id, []).append(node.value)
                    if isinstance(node.value, ast.Lambda):
                        self.functions.setdefault(target.id, []).append(node.value)
                elif isinstance(target, ast.Attribute):
                    self.attr_values.setdefault(target.attr, []).append(node.value)

    def _collect(self, node: ast.AST, cls: Optional[str]) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.ClassDef):
                self.classes[child.name] = child
                self._collect(child, child.name)
            elif isinstance(child, _DEFS):
                name = f"{cls}.{child.name}" if cls else child.name
                self.functions.setdefault(name, []).append(child)
                self._collect(child, None)
            else:
                self._collect(child, cls)

    def bound_to(self, node: ast.AST, names: tuple[str, ...]) -> bool:
        """*node* (a name or ``obj.attr``) is bound somewhere in the file to
        a call of one of *names* — an opener, a connection."""
        if isinstance(node, ast.Name):
            return any(_calls(v, names) for v in self.values.get(node.id, []))
        if isinstance(node, ast.Attribute):
            return any(_calls(v, names) for v in self.attr_values.get(node.attr, []))
        return False


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
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            return self._classes(f, node.left, seen) + self._classes(f, node.right, seen)
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
        if name in f.values:                          # bound once: read it; else unknown
            return self._classes(f, f.values[name][0], seen) if len(f.values[name]) == 1 else []
        if name in f.classes:                         # the file's own class: a child of its bases
            bases = tuple(c for b in f.classes[name].bases for c in self._classes(f, b, seen))
            try:
                return [type(name, bases, {"_own_class": True})] if bases else []
            except TypeError:
                return []
        if name in f.names:
            module, attr = f.names[name]
            return self._attr(module, attr, seen)
        obj = getattr(builtins, name, None)
        return [obj] if isinstance(obj, type) else []

    def _attr(self, module: str, attr: str, seen: frozenset) -> list[type]:
        """``module.attr`` (attr may be dotted): a stdlib object, or a name of
        a scanned file — its own, or one it imports in turn (a facade)."""
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
        """The classes the handlers of *try_node* take care of — a handler
        that only re-raises takes care of nothing."""
        return [c for h in try_node.handlers
                if not (len(h.body) == 1 and isinstance(h.body[0], ast.Raise) and h.body[0].exc is None)
                for c in self._classes(f, h.type)]

    def _covers(self, f: _File, try_node: ast.AST) -> bool:
        return any(issubclass(http.client.HTTPException, c) for c in self._caught(f, try_node))

    def _expects_failure(self, f: _File, try_node: ast.AST) -> bool:
        for c in self._caught(f, try_node):
            if issubclass(c, urllib.error.HTTPError):
                continue                              # a handler for an HTTP status
            if any(issubclass(failure, c) or issubclass(c, failure) for failure in _CONNECTION_FAILURES):
                return True
            if getattr(c, "_own_class", False) and issubclass(c, OSError):
                return True
        return False

    # ── where a request is made ────────────────────────────────────────
    def _exported(self, f: _File, name: str, seen: frozenset) -> bool:
        """The name *name* of file *f* is a request (see ``_is_request``)."""
        key = (f.rel, name)
        if key in seen:
            return False
        seen = seen | {key}
        if name in f.functions:                       # a def (or a bound lambda) of the file
            return (f.rel, name) in self.leaky
        if name in f.names:
            module, attr = f.names[name]
            if module == "urllib.request" and attr in _REQUEST_FUNCS:
                return True
            other = self.by_stem.get(module.split(".")[-1])
            return other is not None and self._exported(other, attr, seen)
        if name in f.values:                          # go = urlopen / partial(urlopen, …)
            return any(self._is_request(f, v.args[0] if _calls(v, ("partial",)) and v.args else v,
                                        None, seen)
                       for v in f.values[name] if isinstance(v, (ast.Name, ast.Attribute, ast.Call)))
        return name in _REQUEST_FUNCS

    def _is_request(self, f: _File, node: ast.AST, cls: Optional[str],
                    seen: frozenset = frozenset()) -> bool:
        """*node* names something that makes a request a non-HTTP reply can
        escape from — called or handed on."""
        if isinstance(node, ast.Call):                # getattr(urllib.request, "urlopen")
            return (_calls(node, ("getattr",)) and len(node.args) >= 2
                    and isinstance(node.args[1], ast.Constant) and node.args[1].value in _REQUEST_FUNCS)
        if isinstance(node, ast.Name):
            return self._exported(f, node.id, seen)
        if not isinstance(node, ast.Attribute):
            return False
        receiver = node.value
        if node.attr in _REQUEST_FUNCS:
            return True
        if node.attr == "open" and (_calls(receiver, ("build_opener",))
                                    or f.bound_to(receiver, ("build_opener",))):
            return True
        if node.attr in _CONNECTION_IO and (_calls(receiver, _CONNECTIONS)
                                            or f.bound_to(receiver, _CONNECTIONS)):
            return True
        if isinstance(receiver, ast.Name):
            if receiver.id in f.modules:               # mod.func
                other = self.by_stem.get(f.modules[receiver.id].split(".")[-1])
                return other is not None and self._exported(other, node.attr, seen)
            if receiver.id in ("self", "cls") and cls:
                return (f.rel, f"{cls}.{node.attr}") in self.leaky
            for value in f.values.get(receiver.id, []):   # c = C(); c.get
                if isinstance(value, ast.Call) and isinstance(value.func, ast.Name) \
                        and value.func.id in f.classes:
                    if (f.rel, f"{value.func.id}.{node.attr}") in self.leaky:
                        return True
        return False

    def _requests(self, f: _File, name: str) -> Iterator[tuple[ast.AST, list[ast.AST]]]:
        """Each request in *name*'s own body (not in a function nested in
        it), with the ``try`` statements whose body holds it, outermost first."""
        cls = name.rpartition(".")[0] or None

        def walk(node: ast.AST, tries: list[ast.AST]) -> Iterator[tuple[ast.AST, list[ast.AST]]]:
            if isinstance(node, (ast.Name, ast.Attribute)) and not isinstance(
                    getattr(node, "ctx", None), ast.Store):
                if self._is_request(f, node, cls):
                    yield node, tries
                    return
            elif isinstance(node, ast.Call) and self._is_request(f, node, cls):
                yield node, tries
            for field, value in ast.iter_fields(node):
                inner = tries + [node] if isinstance(node, _TRIES) and field == "body" else tries
                for child in value if isinstance(value, list) else [value]:
                    if isinstance(child, ast.AST) and not isinstance(child, (*_DEFS, ast.Lambda)):
                        yield from walk(child, inner)

        for root in f.functions[name]:
            if isinstance(root, ast.Lambda):
                yield from walk(root.body, [])
                continue
            for child in ast.iter_child_nodes(root):
                if not isinstance(child, (*_DEFS, ast.Lambda)) and (
                        name == _MODULE or not isinstance(child, ast.ClassDef)):
                    yield from walk(child, [])

    def _escapes(self, f: _File, name: str) -> bool:
        """Can a non-HTTP reply to a request made in *name* leave it?"""
        return any(not any(self._covers(f, t) for t in tries)
                   for _node, tries in self._requests(f, name))

    def judged(self) -> set[tuple[str, str]]:
        """(file, function) of every ``try`` around a request."""
        return {(f.rel, name) for f in self.files.values() for name in f.functions
                for _node, tries in self._requests(f, name) if tries}

    def uncovered(self) -> set[tuple[str, str]]:
        """(file, function) of each ``try`` a non-HTTP reply reaches whose
        handlers expect a failed connection and do not catch it."""
        out: set[tuple[str, str]] = set()
        for f in self.files.values():
            for name in f.functions:
                for _node, tries in self._requests(f, name):
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
    """No finding must not mean nothing was looked at: the population is the
    repository's (a literal floor, not one derived from the scan), the lib's
    helpers and the tool-side fetches are found as requests inside a
    ``try``, and the functions a reply can escape from are found as such."""
    assert len(repo_scan.files) >= 250, len(repo_scan.files)
    assert not any(rel.startswith(("tests/", ".claude/")) or "node_modules" in rel
                   for rel in repo_scan.files)
    judged = repo_scan.judged()
    for function in ("http_get_json", "probe_health"):
        assert (LIB, function) in judged, function
    assert ("scripts/tools/ops/discover_instance_mappings.py", "scrape_metrics_endpoint") in judged
    assert ("scripts/tools/lint/check_trk_index_coverage.py", "main") in judged   # through title_trks
    assert (SOAK, "main") in judged                                               # through fetch_metrics
    assert ("scripts/tools/lint/check_trk_index_coverage.py", "title_trks") in repo_scan.leaky
    assert ("scripts/tools/dx/generate_crd_schemas.py", "_fetch") in repo_scan.leaky
    assert (SOAK, "fetch_metrics") in repo_scan.leaky      # the mid-soak poll: see ALLOWED
    assert (LIB, "http_get_json") not in repo_scan.leaky


def test_the_list_without_httpexception_turns_its_users_red():
    """The handlers are resolved through ``TRANSPORT_ERRORS`` itself: with
    ``HTTPException`` taken out of the real list, every helper that catches
    the list is a finding — the ones that reach the lib through its facade
    (``_lib_python``) among them."""
    sources = _repo_sources()
    whole = "OSError, http.client.HTTPException, ValueError)"
    assert sources[LIB].count(whole) == 1
    sources[LIB] = sources[LIB].replace(whole, "OSError, ValueError)")
    found = _Scan(sources).uncovered()
    assert {(LIB, "http_get_json"), (LIB, "probe_health"),
            ("scripts/tools/ops/discover_instance_mappings.py", "scrape_metrics_endpoint"),
            ("scripts/tools/lint/check_trk_index_coverage.py", "main"),
            ("scripts/tools/dx/generate_crd_schemas.py", "run")} <= found, sorted(found)


_LIB_STUB = ("import http.client\n"
             "TRANSPORT_ERRORS: tuple = (OSError, http.client.HTTPException, ValueError)\n")
_FACADE = "from leaklib import leak\nfrom _lib_prometheus import TRANSPORT_ERRORS\n"
_LEAKLIB = "import urllib.request\ndef leak(u):\n    return urllib.request.urlopen(u).read()\n"
_HEAD = "import urllib.request, urllib.error, http.client, socket, ssl, functools\n"
_OWN = "    except (urllib.error.URLError, OSError):\n        pass\n"   # the shape #3519 found
_OWN8 = "        except (urllib.error.URLError, OSError):\n            pass\n"


def _f(body: str, handlers: str = _OWN, head: str = "") -> str:
    """``def f(u)`` trying *body* (one statement per line) under *handlers*."""
    lines = "".join(f"        {line}\n" for line in body.split("\n"))
    return f"{_HEAD}{head}def f(u):\n    try:\n{lines}{handlers}"


_RED = {
    # ── how the handlers are spelled ──
    "own-enumeration": _f("urllib.request.urlopen(u)",
                          "    except (urllib.error.URLError, OSError, ValueError):\n        return None\n"),
    "timeout-only": _f("urllib.request.urlopen(u)", "    except TimeoutError:\n        pass\n"),
    "socket-error": _f("urllib.request.urlopen(u)", "    except socket.error:\n        pass\n"),
    "socket-timeout-from": _f("urllib.request.urlopen(u)", "    except timeout:\n        pass\n",
                              head="from socket import timeout\n"),
    "environment-error": _f("urllib.request.urlopen(u)", "    except EnvironmentError:\n        pass\n"),
    "ioerror": _f("urllib.request.urlopen(u)", "    except IOError:\n        pass\n"),
    "connection-refused-only": _f("urllib.request.urlopen(u)",
                                  "    except ConnectionRefusedError:\n        pass\n"),
    "ssl-error": _f("urllib.request.urlopen(u)", "    except ssl.SSLError:\n        pass\n"),
    "gaierror": _f("urllib.request.urlopen(u)", "    except socket.gaierror:\n        pass\n"),
    "own-oserror-subclass": _f("urllib.request.urlopen(u)", "    except NetErr:\n        pass\n",
                               head="class NetErr(OSError):\n    pass\n"),
    "own-urlerror-subclass": _f("urllib.request.urlopen(u)", "    except Gone:\n        pass\n",
                                head="class Gone(urllib.error.URLError):\n    pass\n"),
    "alias-starred": _f("urllib.request.urlopen(u)", "    except (*NET, KeyError):\n        pass\n",
                        head="NET = (urllib.error.URLError, OSError)\n"),
    "alias-under-if": _f("urllib.request.urlopen(u)", "    except ERRS:\n        pass\n",
                         head="if True:\n    ERRS = (urllib.error.URLError, OSError)\n"),
    "alias-concatenated": _f("urllib.request.urlopen(u)", "    except ERRS:\n        pass\n",
                             head="ERRS = (urllib.error.URLError,) + (OSError,)\n"),
    "alias-local": (_HEAD + "def f(u):\n    errs = (urllib.error.URLError, OSError)\n    try:\n"
                    "        urllib.request.urlopen(u)\n    except errs:\n        pass\n"),
    # an unresolvable name catches nothing: it does not cover
    "unresolved-is-no-cover": _f("urllib.request.urlopen(u)", "    except (OSError, *ERRS):\n        pass\n",
                                 head="from thirdparty import ERRS\n"),
    "unresolved-bare-name-is-no-cover": _f("urllib.request.urlopen(u)",
                                           "    except (OSError, NOWHERE):\n        pass\n"),
    "unresolved-call-is-no-cover": _f("urllib.request.urlopen(u)", "    except (OSError, *ERRS):\n        pass\n",
                                      head="ERRS = tuple(make())\n"),
    "reraise-is-no-cover": _f("urllib.request.urlopen(u)",
                              "    except urllib.error.URLError:\n        pass\n"
                              "    except Exception:\n        raise\n"),
    "inner-reraise-outer-oserror": (
        _HEAD + "def f(u):\n    try:\n        try:\n            urllib.request.urlopen(u)\n"
        "        except Exception:\n            raise\n    except OSError:\n        pass\n"),
    # ── how the request is spelled ──
    "from-import": _f("urlopen(u)", "    except URLError:\n        pass\n",
                      head="from urllib.error import URLError\nfrom urllib.request import urlopen\n"),
    "import-as": _f("fetch(u)", head="from urllib.request import urlopen as fetch\n"),
    "import-as-open": _f("open(u)", head="from urllib.request import urlopen as open\n"),
    "module-as": _f("ur.urlopen(u)", head="import urllib.request as ur\n"),
    "name-bound-to-it": _f("go(u)", head="go = urllib.request.urlopen\n"),
    "name-bound-locally": (_HEAD + "def f(u):\n    go = urllib.request.urlopen\n    try:\n"
                           "        go(u)\n" + _OWN),
    "partial": _f("go(u)", head="go = functools.partial(urllib.request.urlopen, timeout=3)\n"),
    "bound-lambda": _f("go(u)", head="go = lambda u: urllib.request.urlopen(u)\n"),
    "getattr-literal": _f("getattr(urllib.request, 'urlopen')(u)"),
    "urlretrieve": _f("urllib.request.urlretrieve(u, '/tmp/x')"),
    "with": _f("with urllib.request.urlopen(u) as r:\n    r.read()"),
    "async": (_HEAD + "async def f(u):\n    try:\n        urllib.request.urlopen(u)\n" + _OWN),
    "module-level": _HEAD + "try:\n    urllib.request.urlopen('http://x')\nexcept OSError:\n    pass\n",
    "class-body": _HEAD + "class C:\n    try:\n        urllib.request.urlopen('http://x')\n" + _OWN,
    "opener": _f("O.open(u)", head="O = urllib.request.build_opener()\n"),
    "opener-local": (_HEAD + "def f(u):\n    o = urllib.request.build_opener()\n    try:\n"
                     "        o.open(u)\n" + _OWN),
    "opener-on-self": (_HEAD + "class C:\n    def __init__(self):\n        self.o = urllib.request.build_opener()\n"
                       "    def f(self, u):\n        try:\n            self.o.open(u)\n" + _OWN8),
    "connection-chained": _f("http.client.HTTPConnection(u).request('GET', '/')",
                             "    except ConnectionError:\n        pass\n"),
    "connection-built-outside": (
        _HEAD + "def f(h):\n    c = http.client.HTTPConnection(h)\n    try:\n"
        "        c.request('GET', '/')\n        c.getresponse().read()\n" + _OWN),
    "https-connection-built-outside": (
        _HEAD + "def f(h):\n    c = http.client.HTTPSConnection(h)\n    try:\n"
        "        c.request('GET', '/')\n        r = c.getresponse()\n"
        "    except ConnectionError:\n        pass\n"),
    # ── the request is somewhere else ──
    "through-a-helper": _f("get(u)", "    except OSError:\n        pass\n",
                           head="def get(u):\n    return urllib.request.urlopen(u).read()\n"),
    "helper-in-with": _f("with get(u) as r:\n    r.read()",
                         head="def get(u):\n    return urllib.request.urlopen(u)\n"),
    "helper-handed-on": (_HEAD + "def get(u):\n    return urllib.request.urlopen(u)\n"
                         "def f(us):\n    try:\n        list(map(get, us))\n" + _OWN),
    "nested-def": (_HEAD + "def f(u):\n    def inner():\n        return urllib.request.urlopen(u)\n"
                   "    try:\n        inner()\n" + _OWN),
    "method-of-self": (_HEAD + "class C:\n    def get(self, u):\n        return urllib.request.urlopen(u)\n"
                       "    def f(self, u):\n        try:\n            self.get(u)\n" + _OWN8),
    "method-of-an-object-built-here": (
        _HEAD + "class C:\n    def get(self, u):\n        return urllib.request.urlopen(u)\n"
        "def f(u):\n    c = C()\n    try:\n        c.get(u)\n" + _OWN),
    "imported-helper": _f("leak(u)", head="from leaklib import leak\n"),
    "helper-through-a-facade": _f("leak(u)", head="from facade import leak\n"),
    "helper-through-a-facade-module": _f("facade.leak(u)", head="import facade\n"),
}

_GREEN = {
    "the-list": _f("urllib.request.urlopen(u)", "    except TRANSPORT_ERRORS:\n        pass\n",
                   head="from _lib_prometheus import TRANSPORT_ERRORS\n"),
    "the-list-starred": _f("urllib.request.urlopen(u)",
                           "    except (*_lib_prometheus.TRANSPORT_ERRORS, KeyError):\n        pass\n",
                           head="import _lib_prometheus\n"),
    "the-list-through-a-facade": _f("urllib.request.urlopen(u)", "    except TRANSPORT_ERRORS:\n        pass\n",
                                    head="from facade import TRANSPORT_ERRORS\n"),
    # the types named, HTTPException among them — over two handlers too
    "httpexception-named": _f("urllib.request.urlopen(u)",
                              "    except urllib.error.HTTPError:\n        pass\n"
                              "    except (OSError, http.client.HTTPException):\n        pass\n"),
    "httpexception-from-import": _f("urllib.request.urlopen(u)",
                                    "    except (OSError, HTTPException):\n        pass\n",
                                    head="from http.client import HTTPException\n"),
    "exception": _f("urllib.request.urlopen(u)", "    except Exception:\n        pass\n"),
    "bare": _f("urllib.request.urlopen(u)", "    except:\n        pass\n"),
    "cover-alias-under-if": _f("urllib.request.urlopen(u)", "    except (OSError, *ERRS):\n        pass\n",
                               head="if True:\n    ERRS = (OSError, http.client.HTTPException)\n"),
    "cover-alias-concatenated": _f("urllib.request.urlopen(u)", "    except (OSError, *ERRS):\n        pass\n",
                                   head="ERRS = (OSError,) + (http.client.HTTPException,)\n"),
    "cover-alias-local": (_HEAD + "def f(u):\n    errs = (OSError, http.client.HTTPException)\n    try:\n"
                          "        urllib.request.urlopen(u)\n    except errs:\n        pass\n"),
    # a handler that makes no claim about the connection
    "http-status-only": _f("urllib.request.urlopen(u)", "    except urllib.error.HTTPError:\n        pass\n"),
    "file-error-only": _f("urllib.request.urlopen(u)", "    except FileNotFoundError:\n        pass\n"),
    "unresolved-is-no-claim": _f("urllib.request.urlopen(u)", "    except NetErrors:\n        pass\n",
                                 head="from thirdparty import NetErrors\n"),
    "unresolved-bare-name-is-no-claim": _f("urllib.request.urlopen(u)", "    except NOWHERE:\n        pass\n"),
    # caught where it is made: the outer try never sees it
    "caught-inside": _f("get(u)", "    except OSError:\n        pass\n",
                        head="from _lib_prometheus import TRANSPORT_ERRORS\n"
                             "def get(u):\n    try:\n        return urllib.request.urlopen(u).read()\n"
                             "    except TRANSPORT_ERRORS:\n        return None\n"),
    "own-urlopen-that-catches": _f("urlopen(u)",
                                   head="from _lib_prometheus import TRANSPORT_ERRORS\n"
                                        "def urlopen(u):\n    try:\n"
                                        "        return urllib.request.build_opener().open(u)\n"
                                        "    except TRANSPORT_ERRORS:\n        return None\n"),
    # not a request
    "builtin-open": "def f(p):\n    try:\n        open(p).read()\n    except OSError:\n        pass\n",
    "request-in-handler": (_HEAD + "def f(u):\n    try:\n        pass\n    except OSError:\n"
                           "        urllib.request.urlopen(u)\n"),
    "same-name-method-of-another-class": (
        _HEAD + "class A:\n    def get(self, u):\n        return urllib.request.urlopen(u)\n"
        "class B:\n    def get(self, p):\n        return open(p).read()\n"
        "    def f(self, p):\n        try:\n            self.get(p)\n"
        "        except OSError:\n            pass\n"),
}

# The spellings the header lists as known limits: each IS a leak the scan
# does not see. Pinned so that a change which starts to see one moves it to
# _RED (and takes it off the header) instead of passing unnoticed.
_NOT_SEEN = {
    "lambda-called-in-place": _f("(lambda: urllib.request.urlopen(u))()"),
    "getattr-computed": _f("getattr(urllib.request, 'url' + 'open')(u)"),
    "method-of-a-parameter": (_HEAD + "class C:\n    def get(self, u):\n        return urllib.request.urlopen(u)\n"
                              "def f(c, u):\n    try:\n        c.get(u)\n" + _OWN),
    "read-in-another-try": (_HEAD + "def f(u):\n    try:\n        r = urllib.request.urlopen(u)\n"
                            "    except Exception:\n        return\n    try:\n        r.read()\n" + _OWN),
    "third-party-client": ("import requests\ndef f(u):\n    try:\n        requests.get(u)\n"
                           "    except requests.ConnectionError:\n        pass\n"),
    "name-bound-twice": (_HEAD + "ERRS = ()\nERRS = (urllib.error.URLError, OSError)\n"
                         "def f(u):\n    try:\n        urllib.request.urlopen(u)\n"
                         "    except ERRS:\n        pass\n"),
}


def _found(source: str) -> list[str]:
    scan = _Scan({LIB: _LIB_STUB, "scripts/tools/facade.py": _FACADE,
                  "scripts/tools/leaklib.py": _LEAKLIB, "scripts/tools/t.py": source})
    return sorted(fn for rel, fn in scan.uncovered() if rel.endswith("/t.py"))


@pytest.mark.parametrize("shape", sorted(_RED))
def test_the_predicate_finds(shape):
    found = _found(_RED[shape])
    assert found and all(fn.rpartition(".")[2] == "f" or fn == _MODULE for fn in found), found


@pytest.mark.parametrize("shape", sorted(_GREEN))
def test_the_predicate_passes(shape):
    assert _found(_GREEN[shape]) == []


@pytest.mark.parametrize("shape", sorted(_NOT_SEEN))
def test_the_predicate_does_not_see(shape):
    assert _found(_NOT_SEEN[shape]) == [], (
        f"{shape} is found now: move it to _RED and take it off the known limits in the header")
