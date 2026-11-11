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


def _wrapper_calls(text: str, param: str, at: int) -> list[str] | None:
    """When *param* is a parameter of the function enclosing position *at*,
    the first arguments of every call of that function in *text*."""
    head = text[:at]
    defs = list(re.finditer(
        rf"(?:const|let|var)\s+(\w+)\s*=\s*(?:async\s*)?\(\s*{re.escape(param)}\b"
        rf"|function\s+(\w+)\s*\(\s*{re.escape(param)}\b", head))
    if not defs:
        return None
    fn = defs[-1].group(1) or defs[-1].group(2)
    calls = [_first_arg(text, m.end()) for m in re.finditer(rf"(?<![\w.]){fn}\s*\(", text)
             if not re.match(r"\s*\(?\s*" + re.escape(param), text[m.end():])]
    return calls


def _check(text: str, expr: str, at: int) -> str | None:
    """None when *expr* is accepted, else why not."""
    lead = re.match(r"^`\$\{\s*([A-Za-z_$][\w$]*)\s*\}", expr)
    if lead:
        # `${BASE}/…`: the path's start is BASE's.
        return _check(text, lead.group(1), at)
    ok = _literal_ok(expr)
    if ok is not None:
        return None if ok else f"{expr[:60]!r} is not under /api/"
    if _IDENT.match(expr):
        values = _assignments(text, expr)
        if values:
            bad = [v for v in values if _literal_ok(v) is not True]
            return None if not bad else f"{expr} = {bad[0][:60]!r} is not a literal under /api/"
        calls = _wrapper_calls(text, expr, at)
        if calls:
            bad = [c for c in calls if _literal_ok(c) is not True]
            return None if not bad else f"{expr} is passed {bad[0][:60]!r}, not a literal under /api/"
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
    return sorted(p for ext in ("js", "jsx", "ts", "tsx") for p in _SRC.rglob(f"*.{ext}")
                  if ".test." not in p.name)


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
])
def test_the_check_itself(src: str, ok: bool) -> None:
    n, bad = violations(src)
    assert n >= (0 if src.startswith("x.") else 1)
    assert (not bad) is ok, bad
