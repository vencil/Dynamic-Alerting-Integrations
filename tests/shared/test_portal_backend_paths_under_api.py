"""test_portal_backend_paths_under_api.py — ADR-027 §5, §9 item 4.

The portal is served behind one oauth2-proxy that routes ``/api/`` to
tenant-api's Unix socket and everything else to the portal's static files.
A backend path outside ``/api/`` would therefore reach the static file server
(which answers its SPA fallback, ``index.html`` with 200), never a backend —
and before ADR-027 such a path (``/preview``) was a reverse proxy that carried
the caller's identity across the network. So every request the portal source
makes to an absolute path must be under ``/api/``.

What is checked: the first argument of every ``fetch(`` call in
``tools/portal/src`` (tests excluded). It is accepted when it is

  * a string or template literal whose static start is ``/api/``, or a
    relative data file next to the page (a whole ``.json`` / ``.yaml`` file
    name, no ``/`` — e.g. ``'platform-data.json'``); dot segments
    (``/api/../x``) are refused, since the browser normalises them away;
  * an identifier that names, in the same file, a ``const``/``let``/``var``
    whose every assignment is such a literal (also as the leading
    ``${NAME}`` of a template); or
  * a parameter of the enclosing wrapper function (``const apiCall = async
    (url, …) =>`` / ``function apiCall(url, …)``) whose every call site in the
    file passes such a literal — at least one call site must exist.

Anything else — an absolute non-``/api/`` path, a scheme, an expression this
check cannot resolve — fails: write the path so it can be read.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
from _tree import repo_files

_REPO = Path(__file__).resolve().parents[2]
_SRC = _REPO / "tools" / "portal" / "src"

_FETCH = re.compile(r"(?<![\w.])fetch\s*\(")
_IDENT = re.compile(r"^[A-Za-z_$][\w$]*$")


def _first_arg(text: str, start: int) -> str:
    """The first argument of the call whose '(' is at text[start-1]: up to the
    first top-level ',' or ')', skipping over strings, templates and brackets."""
    depth, i, quote = 0, start, None
    while i < len(text):
        c = text[i]
        if quote:
            if c == "\\":
                i += 2
                continue
            if c == quote:
                quote = None
            elif quote == "`" and text.startswith("${", i):
                # a template expression: skip to its matching '}'
                j, d = i + 2, 1
                while j < len(text) and d:
                    d += {"{": 1, "}": -1}.get(text[j], 0)
                    j += 1
                i = j
                continue
        elif c in "'\"`":
            quote = c
        elif c in "([{":
            depth += 1
        elif c in ")]}":
            if depth == 0:
                return text[start:i].strip()
            depth -= 1
        elif c == "," and depth == 0:
            return text[start:i].strip()
        i += 1
    return text[start:].strip()


def _literal_ok(expr: str) -> bool | None:
    """True/False for a string or template literal (or a '+' concatenation
    starting with one); None when expr is not one."""
    m = re.match(r"""^(['"`])(.*)""", expr, re.S)
    if not m:
        return None
    quote, rest = m.groups()
    static = rest.split("${", 1)[0] if quote == "`" else rest
    static = static.split(quote, 1)[0]
    # A browser normalises dot segments: /api/../preview is /preview.
    if re.search(r"(^|/)\.\.?(/|$)", static):
        return False
    if static.startswith("/api/"):
        return True
    if static.startswith("/") or re.match(r"^[a-z][a-z0-9+.-]*:", static, re.I):
        return False
    # A relative data file next to the page, read with GET: a whole literal
    # naming a .json / .yaml file, nothing built after it.
    whole = quote != "`" or "${" not in rest.split(quote, 1)[0]
    return whole and bool(re.fullmatch(r"[\w.-]+\.(json|ya?ml)", static))


def _assignments(text: str, name: str) -> list[str]:
    pat = re.compile(rf"(?:\b(?:const|let|var)\s+{re.escape(name)}\s*=|(?<![\w.]){re.escape(name)}\s*=(?!=))")
    out = []
    for m in pat.finditer(text):
        out.append(_first_arg(text, m.end()).rstrip(";").strip())
    return out


_DOT_SEGMENT = re.compile(r"(^|/)\.\.?(/|$)")


def _static_text(expr: str) -> str:
    """The literal text of every string and template piece in *expr*, in
    order (template ${...} parts dropped). A dot segment anywhere in it is
    one the browser would apply: '/api/' + '../x' and `${E}/../x` both end
    up outside /api/."""
    out, i, quote = [], 0, None
    while i < len(expr):
        c = expr[i]
        if quote:
            if c == "\\":
                out.append(expr[i + 1:i + 2])
                i += 2
                continue
            if c == quote:
                quote = None
                out.append("\0")
            elif quote == "`" and expr.startswith("${", i):
                j, d = i + 2, 1
                while j < len(expr) and d:
                    d += {"{": 1, "}": -1}.get(expr[j], 0)
                    j += 1
                out.append("\0")
                i = j
                continue
            else:
                out.append(c)
        elif c in "'\"`":
            quote = c
        i += 1
    # \0 marks a boundary an expression may fill; a segment is checked on
    # the text as written, so join pieces without it.
    return "".join(out).replace("\0", "")


_HEADS = (
    # function name(a, b) {
    re.compile(r"function\s*(?P<name>\w*)\s*\((?P<params>[^)]*)\)\s*(?P<open>\{)"),
    # const name = async (a, b) =>   /   (a, b) =>
    re.compile(r"(?:(?:const|let|var)\s+(?P<name>\w+)\s*=\s*)?(?:async\s*)?"
               r"\((?P<params>[^)]*)\)\s*=>"),
    # const name = a =>   /   a =>
    re.compile(r"(?:(?:const|let|var)\s+(?P<name>\w+)\s*=\s*)?(?:async\s+)?"
               r"(?<![\w.])(?P<params>[A-Za-z_$][\w$]*)\s*=>"),
)


def _body_end(text: str, start: int) -> int:
    """End of a function body that starts at *start*: a braced block, or an
    arrow's expression up to the first top-level ';' or newline."""
    i = start
    while i < len(text) and text[i] in " \t":
        i += 1
    braced = i < len(text) and text[i] == "{"
    depth, quote = 0, None
    while i < len(text):
        c = text[i]
        if quote:
            if c == "\\":
                i += 2
                continue
            if c == quote:
                quote = None
        elif c in "'\"`":
            quote = c
        elif c in "([{":
            depth += 1
        elif c in ")]}":
            depth -= 1
            if braced and depth == 0:
                return i + 1
            if depth < 0:
                return i
        elif not braced and depth == 0 and c in ";\n":
            return i
        i += 1
    return len(text)


def _enclosing_param(text: str, name: str, at: int) -> tuple[str, int] | None:
    """When *name* is a parameter of a function whose body holds position
    *at*: (that function's name, '' if anonymous; the parameter's index).
    The innermost such function wins: it shadows any outer binding."""
    best = None
    for head in _HEADS:
        for m in head.finditer(text, 0, at):
            params = [p.strip().split("=")[0].strip() for p in m.group("params").split(",")]
            if name not in params:
                continue
            body = m.end("open") - 1 if "open" in m.groupdict() and m.group("open") else m.end()
            if not body <= at < _body_end(text, body):
                continue
            if best is None or m.start() > best[0]:
                best = (m.start(), m.group("name") or "", params.index(name))
    return None if best is None else best[1:]


def _call_args(text: str, fn: str) -> list[tuple[str, int]]:
    """(first argument, position) of every call of *fn*, its own definition
    excluded."""
    return [(_first_arg(text, m.end()), m.start())
            for m in re.finditer(rf"(?<![\w.]){re.escape(fn)}\s*\(", text)
            if not re.search(r"function\s+$", text[:m.start()])]


def _check(text: str, expr: str, at: int, depth: int = 0) -> str | None:
    """None when *expr* is accepted, else why not."""
    if depth > 4:
        return f"{expr[:60]!r} is passed through too many calls to read"
    if _DOT_SEGMENT.search(_static_text(expr)):
        return f"{expr[:60]!r} has a dot segment (the browser resolves it)"
    lead = re.match(r"^`\$\{\s*([A-Za-z_$][\w$]*)\s*\}", expr)
    if lead:
        # `${BASE}/…`: the path's start is BASE's.
        return _check(text, lead.group(1), at, depth)
    ok = _literal_ok(expr)
    if ok is not None:
        return None if ok else f"{expr[:60]!r} is not under /api/"
    if _IDENT.match(expr):
        # A parameter of the enclosing function shadows any outer binding of
        # the same name: resolve it from the call sites, or not at all.
        param = _enclosing_param(text, expr, at)
        if param is not None:
            fn, index = param
            calls = _call_args(text, fn) if fn and index == 0 else []
            if not calls:
                return f"{expr} is a parameter whose calls cannot be read here"
            # Each argument is read where it is written, in that scope.
            bad = [(c, why) for c, pos in calls if (why := _check(text, c, pos, depth + 1))]
            return None if not bad else f"{expr} is passed {bad[0][0][:60]!r}: {bad[0][1]}"
        values = _assignments(text, expr)
        if values:
            bad = [v for v in values if _literal_ok(v) is not True]
            return None if not bad else f"{expr} = {bad[0][:60]!r} is not a literal under /api/"
        return f"cannot resolve {expr!r} in this file"
    return f"cannot read {expr[:60]!r} as a path"


def violations(text: str) -> tuple[int, list[str]]:
    sites, out = 0, []
    for m in _FETCH.finditer(text):
        sites += 1
        expr = _first_arg(text, m.end())
        why = _check(text, expr, m.start())
        if why:
            line = text.count("\n", 0, m.start()) + 1
            out.append(f"line {line}: {why}")
    return sites, out


def _sources() -> list[Path]:
    return sorted(p for p in repo_files(".js", ".jsx", ".ts", ".tsx")
                  if p.is_relative_to(_SRC) and ".test." not in p.name)


def test_every_portal_fetch_is_under_api() -> None:
    total, bad = 0, []
    for p in _sources():
        n, v = violations(p.read_text(encoding="utf-8"))
        total += n
        bad += [f"{p.relative_to(_REPO)} {x}" for x in v]
    # Vacuity guard: the portal makes more than a handful of requests.
    assert total >= 10, total
    assert not bad, "portal requests outside /api/ (ADR-027 §5, §9 item 4):\n" + "\n".join(bad)


@pytest.mark.parametrize("src,ok", [
    ("fetch('/api/v1/me')", True),
    ("fetch(`/api/v1/tenants/${id}/recipe-preview`, {method: 'POST'})", True),
    ("fetch('platform-data.json')", True),
    ("fetch('/preview', {method: 'POST'})", False),
    ("fetch(`/preview?x=${y}`)", False),
    ("fetch('https://evil.example/api/v1/me')", False),
    ("fetch(\n  `/api/v1/x`,\n  {}\n)", True),
    ("const E = '/api/v1/views';\nfetch(E)", True),
    ("const E = '/views';\nfetch(E)", False),
    ("fetch(`${E}/x`)", False),
    ("const E = '/api/v1/views';\nfetch(`${E}/${encodeURIComponent(id)}`)", True),
    ("const E = '/views';\nfetch(`${E}/x`)", False),
    ("const apiCall = async (url, o) => { return fetch(url, o); };\napiCall('/api/v1/groups', {});", True),
    ("const apiCall = async (url, o) => { return fetch(url, o); };\napiCall('/groups', {});", False),
    ("function get(url) { return fetch(url); }", False),
    ("fetch(makeUrl())", False),
    ("x.fetch('/preview')", True),  # not the global fetch
    ("fetch('preview', {method: 'POST'})", False),
    ("fetch('../preview')", False),
    ("fetch('/api/../preview')", False),
    ("fetch('/api/v1/./me')", False),
    ("fetch(`data-${x}.json`)", False),
    ("fetch('template-data.json')", True),
    # CodeRabbit on #3749: a suffix that walks back out of /api/
    ("fetch('/api/' + '../preview')", False),
    ("const E = '/api/v1/views';\nfetch(`${E}/../../../preview`)", False),
    ("fetch('/api/v1/tenants/' + encodeURIComponent(id) + '/views')", True),
    # ... and a wrapper parameter shadowing an outer constant of the same name
    ("const url = '/api/v1/me';\nfunction apiCall(url) { return fetch(url); }\napiCall('/preview');", False),
    ("const url = '/api/v1/me';\nfunction apiCall(url) { return fetch(url); }\napiCall('/api/v1/x');", True),
    ("const url = '/api/v1/me';\nfunction apiCall(o, url) { return fetch(url, o); }\napiCall({}, '/preview');", False),
    ("const url = '/api/v1/me';\nconst f = (url) => fetch(url);\nf('/preview');", False),
    # a call-site argument is read in its own scope
    ("function g() { const u = '/api/v1/x'; return w(u); }\nfunction w(u) { return fetch(u); }", True),
    ("function g() { const u = '/preview'; return w(u); }\nfunction w(u) { return fetch(u); }", False),
])
def test_the_check_itself(src: str, ok: bool) -> None:
    n, bad = violations(src)
    assert n >= (0 if src.startswith("x.") else 1)
    assert (not bad) is ok, bad
