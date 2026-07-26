#!/usr/bin/env python3
"""check_portal_asset_shipping.py — runtime-loaded portal assets must ship in the da-portal image.

Why this exists
---------------
`components/da-portal/Dockerfile` copies `docs/assets/` **file by file** (an
allowlist), while mkdocs publishes `docs/` **wholesale** to GitHub Pages. The
two surfaces therefore disagree silently: a tool that fetches a new
`docs/assets/*.json` works on the public Pages site, in `try-local` (compose
bind-mounts the repo) and in every test — and 404s only inside the built,
self-hosted image, which no CI job renders.

That is not hypothetical. Before this lint THREE already-shipped assets were
missing from the Dockerfile:

  * `recommendation-data.json` — self-service-portal's Alert Preview tab
  * `template-data.json`       — Config Template Gallery
  * `design-tokens.css`        — the `<link>` of BOTH portal HTML shells

nginx makes it worse than a plain 404: `location ~* \\.(json|yaml)$` has no
`try_files`, so the SPA fallback does not apply and the fetch fails outright;
the tools fall through to their offline-fallback branch (or render untokenised)
with nothing in CI to notice.

The contract
------------
Every asset the portal loads at runtime from the `/assets/` URL namespace must
be reachable in the image — either COPYed by name, or under a whole-directory
COPY — **and land at the same URL** the browser asks for (a file copied to the
wrong destination is as broken as one not copied at all).

Scan surfaces
-------------
  * `tools/portal/src/**` + `tools/portal/entries/**`
    (`.js` / `.jsx` / `.ts` / `.tsx`)
      — bundled by esbuild into `docs/assets/dist/<tool>.js`, which
        `/assets/jsx-loader.html` loads. `fetch()` resolves relative URLs
        against the DOCUMENT, not the script, so a bare `'x.json'` in a
        component is `/assets/x.json` — NOT a path relative to the source file.
  * `docs/assets/*.html`   — served at `/assets/…`   → relative base `/assets/`
  * `docs/interactive/**/*.html` — served at `/interactive/…` → base
        `/interactive/`, which is why the Hub writes `'../assets/flows.json'`
        for the very same file the components call `'flows.json'`.

Recognised load sites: `fetch(…)`, `XHR.open(method, …)`, `.src = …`
assignments, and static `<link>` / `<script>` / `<img>` tags in any of the
three HTML attribute-value forms (`"…"`, `'…'`, unquoted). `.href = …` is
deliberately NOT a load site: every occurrence in this tree is navigation
(`window.location.href`) or a blob download anchor (`a.href = URL.create…`),
so scanning it produced only false positives. Static `<link href>` — the one
shape that IS a resource load — is covered by the tag scanner instead.

Fail-closed
-----------
A missing/unparseable Dockerfile, zero parsed `COPY` instructions, or an empty
scan corpus is a CALLER ERROR (exit 2), never a silent pass. A load site whose
URL cannot be resolved statically (a bare function parameter, a member
expression) is a VIOLATION unless registered in `EXEMPTIONS` below — so a new
dynamic asset path must be reasoned about, not merely ignored. `EXEMPTIONS`
entries that stop matching are reported too, keeping the registry honest.

Deliberately NOT covered (honest boundaries):
  * A reference to a `/assets/` path that does not exist in `docs/assets/` is
    skipped — a dangling reference is a different defect (and a different
    lint's jurisdiction); this gate only asserts SHIPPING of real files.
  * A path assembled entirely at runtime whose static prefix is a directory is
    checked at DIRECTORY granularity (e.g. `'../assets/dist/' + name + '.js'`
    only proves `dist/` is copied wholesale, not that the bundle exists).
  * Identifier tracing is by NAME within one file, not by scope: a `fetch(url)`
    whose `url` is a parameter binds to an unrelated `const url = …` elsewhere
    in the same file if one exists. Over-checking (the common case, a stricter
    assertion) is harmless; the residual under-check needs a file that fetches
    an asset through a wrapper AND reuses the name for an API URL. No such
    file exists today, and an AST/scope analyser is not worth the weight —
    prefer a literal at the call site.
  * DYNAMIC element construction is invisible. Only `.src = …` is scanned, so
    `link.href = '…'` on a constructed `<link>`, `el.setAttribute('src', …)`,
    and `new Image().src`-style indirection through a helper all pass unseen.
    `.href = …` stays unscanned by choice (see above — navigation-only in this
    tree); `setAttribute` is simply not implemented. Prefer a static tag or a
    direct `.src =` so this gate can see the load.
  * TAG COVERAGE is an allowlist of `<link>` / `<script>` / `<img>`. Other
    resource-loading elements — `<iframe src>`, `<audio>`/`<video>`/`<source>`,
    `<embed>`, `<object data>`, `<track>`, `<use href>` — are not matched. None
    exist in the tree today; adding one silently escapes the gate.
  * DYNAMIC `import()` is not a recognised load site. esbuild resolves static
    `import` at build time into the bundle (which `dist/` COPYs wholesale), but
    a runtime `import('/assets/…')` would be neither bundled nor checked.
  * `docs/assets/*.html` is scanned NON-recursively (`glob`, not `rglob`) —
    only `docs/interactive/**` recurses. There are no HTML files in
    subdirectories of `docs/assets/` today; one added there would be skipped.
  * `.ts`/`.tsx` ARE in the corpus but have never been exercised — no
    TypeScript exists under `tools/portal/src`/`entries` yet, so the extractor
    regexes are unproven against TS-only syntax (e.g. `fetch(url as string)`,
    which would resolve to nothing and therefore be reported as unresolvable —
    fail-closed, but noisy).

Lint class (lint-policy.md §2): (a) enumerable — the SOT is the Dockerfile's
COPY set on one side and the source tree's load sites on the other.

Usage
-----
  python3 scripts/tools/lint/check_portal_asset_shipping.py

Exit codes:
  0 = every runtime-loaded /assets/ path is shipped by the image
  1 = an asset is not COPYed, or a load site is unresolvable and unregistered
  2 = caller/environment error (Dockerfile missing, unparseable, empty corpus)
"""
from __future__ import annotations

import argparse
import os
import posixpath
import re
import sys
from pathlib import Path

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _THIS_DIR)  # Docker flat layout
sys.path.insert(0, os.path.join(_THIS_DIR, ".."))  # Repo subdir layout
from _lib_exitcodes import EXIT_OK, EXIT_VIOLATION, EXIT_CALLER_ERROR  # noqa: E402
from _lib_compat import try_utf8_stdout  # noqa: E402
from _lib_validation import i18n_text  # noqa: E402

# Was a hand-rolled sys.stdout.reconfigure block; `_lib_compat` is the shared
# implementation of the same guard (legacy Windows consoles are cp950/cp936 and
# cannot encode the CJK this tool's --help now carries).
try_utf8_stdout()

REPO_ROOT = Path(__file__).resolve().parents[3]
DOCKERFILE = REPO_ROOT / "components" / "da-portal" / "Dockerfile"

# Where the image serves its document root from (nginx.conf `root`).
HTML_ROOT = "/usr/share/nginx/html"
# The URL namespace this gate polices. Everything else a portal file loads
# (`/api/v1/…` proxied to tenant-api, `/preview`, CDN vendor URLs) is not a
# file the image is supposed to carry.
ASSETS_URL = "/assets/"

# Load sites whose URL cannot be resolved statically. Key: "<repo path>::<expr>"
# — stable under line moves, and invalidated the moment the expression itself
# changes. Each MUST carry a reason establishing it is not an /assets/ path.
EXEMPTIONS: dict[str, str] = {
    "tools/portal/src/interactive/tools/tenant-manager.jsx::url":
        "apiCall(url, options) wrapper — url is a function PARAMETER; both "
        "call sites (lines 427/449) pass a /api/v1/groups/… template, never "
        "an asset",
    "docs/assets/jsx-loader.html::local.react":
        "offline-vendor probe — member of the `local` map whose values are all "
        "`vendorBase + …` under /assets/vendor/, copied wholesale (line 84)",
}


class DockerfileParseError(RuntimeError):
    """Raised when the COPY set cannot be established — never fail open."""


# ── Reference extraction ────────────────────────────────────────────

_FETCH_RE = re.compile(r"\bfetch\s*\(")
_XHR_OPEN_RE = re.compile(
    r"\.open\s*\(\s*['\"](?:GET|POST|HEAD|PUT|DELETE|PATCH)['\"]\s*,\s*", re.I
)
_URL_ASSIGN_RE = re.compile(r"\.src\s*=\s*(?!=)")
# Static markup only: a resource-loading tag whose attribute is a complete
# literal. Tags assembled inside JS strings (`'<scr'+'ipt src="'+v+'">'`) never
# match — the tag name itself is split, which is exactly the discrimination we
# want. `<a href>` is navigation, not a resource load, and is excluded.
#
# All three HTML attribute-value forms are accepted. Matching only `"…"` was a
# silent fail-open: `<link rel='stylesheet' href='design-tokens.css'>` is the
# same load, in the same tag, in the same file as the double-quoted shape this
# gate was written for — swapping the quote character must not disarm it. The
# unquoted form excludes quotes/backticks, so a tag being concatenated in JS
# (`'<img src=' + url + '>'`, next char `'`) still fails to match rather than
# yielding a fragment.
_TAG_RE = re.compile(
    r"<(?:link|script|img)\b[^>]*?\b(?:href|src)\s*=\s*"
    r"(?:\"([^\"]*)\"|'([^']*)'|([^\s\"'=<>`]+))",
    re.I,
)
_IDENT_RE = re.compile(r"^[A-Za-z_$][A-Za-z0-9_$]*$")
_TEMPLATE_LEAD_IDENT_RE = re.compile(r"^`\$\{\s*([A-Za-z_$][A-Za-z0-9_$]*)\s*\}")
_NON_FILE_SCHEME_RE = re.compile(r"^(?:[a-z][a-z0-9+.-]*:|//|#|\?)", re.I)

_MAX_EXPR = 500  # a URL expression longer than this is not a path we can read


def _read_expression(text: str, start: int) -> str:
    """Return the first argument expression beginning at `start`.

    Stops at the top-level ``,`` or the closing ``)``/``;``, honouring nesting
    and quoting so that ``fetch(`a${f(x, y)}b`, {…})`` yields the template, not
    a fragment of it.
    """
    out: list[str] = []
    depth = 0
    quote: str | None = None
    i, n = start, min(len(text), start + _MAX_EXPR)
    while i < n:
        ch = text[i]
        if quote is not None:
            out.append(ch)
            if ch == "\\" and i + 1 < n:
                out.append(text[i + 1])
                i += 2
                continue
            if ch == quote:
                quote = None
            i += 1
            continue
        if ch in "'\"`":
            quote = ch
            out.append(ch)
            i += 1
            continue
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            if depth == 0:
                break
            depth -= 1
        elif depth == 0 and (ch == "," or ch == ";"):
            break
        out.append(ch)
        i += 1
    return "".join(out).strip()


def _leading_literal(expr: str) -> tuple[str, bool] | None:
    """Split a literal-led expression into ``(static prefix, is_whole_url)``.

    ``None`` when the expression does not start with a string literal, or when
    a template starts with an interpolation (no static prefix to anchor on).
    """
    if not expr or expr[0] not in "'\"`":
        return None
    quote, buf, i = expr[0], [], 1
    while i < len(expr):
        ch = expr[i]
        if ch == "\\":
            i += 2
            continue
        if quote == "`" and ch == "$" and expr[i:i + 2] == "${":
            return ("".join(buf), False) if buf else None
        if ch == quote:
            return "".join(buf), expr[i + 1:].strip() == ""
        buf.append(ch)
        i += 1
    return None


def _assignment_rhs(text: str, name: str) -> str | None:
    """Find ``const|let|var <name> = <rhs>`` in the same file and return the rhs."""
    match = re.search(
        r"\b(?:const|let|var)\s+" + re.escape(name) + r"\s*=\s*", text
    )
    return None if match is None else _read_expression(text, match.end())


def _resolve(expr: str, text: str, depth: int = 0) -> tuple[str, bool] | None:
    """Resolve a load-site expression to ``(static prefix, is_whole_url)``.

    Single-hop identifier tracing only: enough for the two idioms in the tree
    (a `const URL = '…'` hoisted above the call, and a `` `${CONST}/…` ``
    template), and deliberately short of a call-graph analysis — a wrapper whose
    URL arrives as a parameter stays unresolvable, i.e. reported.
    """
    literal = _leading_literal(expr)
    if literal is not None:
        return literal
    if depth >= 2:
        return None
    name = None
    if _IDENT_RE.match(expr):
        name, has_tail = expr, False
    else:
        lead = _TEMPLATE_LEAD_IDENT_RE.match(expr)
        if lead is not None:
            name, has_tail = lead.group(1), True
    if name is None:
        return None
    rhs = _assignment_rhs(text, name)
    if rhs is None:
        return None
    inner = _resolve(rhs, text, depth + 1)
    if inner is None:
        return None
    return inner[0], (inner[1] and not has_tail)


def page_base(rel_path: str) -> str:
    """The URL directory a relative reference in `rel_path` resolves against."""
    if rel_path.startswith("tools/portal/"):
        # Bundled into /assets/dist/*.js and loaded by /assets/jsx-loader.html;
        # relative fetch() resolves against that DOCUMENT.
        return ASSETS_URL
    if rel_path.startswith("docs/"):
        parent = posixpath.dirname(rel_path[len("docs/"):])
        return f"/{parent}/" if parent else "/"
    raise DockerfileParseError(f"no page base defined for {rel_path}")


def _to_url(base: str, literal: str) -> str | None:
    if not literal or _NON_FILE_SCHEME_RE.match(literal):
        return None
    joined = literal if literal.startswith("/") else base + literal
    url = posixpath.normpath(joined)
    return url + "/" if joined.endswith("/") and not url.endswith("/") else url


def extract_references(rel_path: str, text: str) -> list[tuple[str, str]]:
    """Return ``(expression, url-or-empty)`` pairs for every load site.

    An empty url marks an unresolvable expression (EXEMPTIONS territory). A url
    ending in ``/`` is a directory requirement from a runtime-assembled path.
    """
    base = page_base(rel_path)
    found: list[tuple[str, str]] = []
    seen: set[str] = set()

    def add(expr: str, resolved: tuple[str, bool] | None) -> None:
        if not expr or expr in seen:
            return
        seen.add(expr)
        if resolved is None:
            found.append((expr, ""))
            return
        prefix, whole = resolved
        url = _to_url(base, prefix)
        if url is None:
            return
        # `ASSETS_URL` carries a trailing slash, so a site resolving to exactly
        # `/assets` failed the `startswith` test and vanished — neither a
        # violation nor a directory requirement. That silently disarmed the
        # more natural of the two equivalent idioms:
        #     const BASE = '/assets';   fetch(`${BASE}/x.json`)   ← was missed
        #     const BASE = '/assets/';  fetch(`${BASE}x.json`)    ← was caught
        # Fold the bare directory form onto the canonical one; either way all a
        # runtime-assembled tail proves is that the directory itself must ship.
        # Exact equality, not a prefix test, so `/assetsx` stays out.
        if url == ASSETS_URL.rstrip("/"):
            url = ASSETS_URL
        if not url.startswith(ASSETS_URL):
            return
        if not whole:  # runtime tail → only the containing directory is proven
            url = url[: url.rfind("/") + 1]
        found.append((expr, url))

    for pattern in (_FETCH_RE, _XHR_OPEN_RE, _URL_ASSIGN_RE):
        for match in pattern.finditer(text):
            expr = _read_expression(text, match.end())
            add(expr, _resolve(expr, text))
    for match in _TAG_RE.finditer(text):
        # Exactly one of the three quoting alternatives captured.
        value = next(g for g in match.groups() if g is not None)
        add(f'"{value}"', (value, True))
    return found


# ── Dockerfile COPY parsing ─────────────────────────────────────────

def parse_copies(text: str) -> list[tuple[tuple[str, ...], str]]:
    """Return ``(sources, destination)`` for every COPY, joining continuations."""
    copies: list[tuple[tuple[str, ...], str]] = []
    buffer = ""
    for raw in text.splitlines():
        line = raw.strip()
        if not buffer and (not line or line.startswith("#")):
            continue
        if line.endswith("\\"):
            buffer += line[:-1] + " "
            continue
        line, buffer = buffer + line, ""
        if not re.match(r"^COPY\b", line, re.I):
            continue
        tokens = [t for t in line.split()[1:] if not t.startswith("--")]
        if len(tokens) < 2:
            raise DockerfileParseError(f"malformed COPY instruction: {line!r}")
        copies.append((tuple(tokens[:-1]), tokens[-1]))
    if not copies:
        raise DockerfileParseError("no COPY instructions found")
    return copies


def _dest_url(dest: str) -> str | None:
    if dest == HTML_ROOT:
        return "/"
    return dest[len(HTML_ROOT):] if dest.startswith(HTML_ROOT + "/") else None


def is_shipped(copies: list[tuple[tuple[str, ...], str]], url: str) -> bool:
    """Does some COPY put `url` (a file, or a directory when it ends in /) in place?"""
    wants_dir = url.endswith("/")
    for sources, dest in copies:
        dest_url = _dest_url(dest)
        if dest_url is None:
            continue
        for src in sources:
            if src.endswith("/"):  # whole-directory COPY
                prefix = dest_url if dest_url.endswith("/") else dest_url + "/"
                if url.startswith(prefix):
                    return True
            elif not wants_dir:
                landed = (
                    dest_url + posixpath.basename(src)
                    if dest_url.endswith("/")
                    else dest_url
                )
                if landed == url:
                    return True
    return False


# ── Scan corpus ─────────────────────────────────────────────────────

# `.ts`/`.tsx` match nothing under src/entries today (every TypeScript file in
# tools/portal lives under tests/, which the image never ships). They are here
# so that a gradual TypeScript migration cannot narrow this gate in silence:
# tools/portal/build.mjs already carries `ts`/`tsx` esbuild loaders, so a
# `.ts` module imported by an entry would be bundled into docs/assets/dist/ —
# shipped and scanned by esbuild, but invisible to a `.js`/`.jsx`-only corpus.
# Two extra globs now beats discovering the blind spot after the migration.
_SOURCE_EXTS = ("*.js", "*.jsx", "*.ts", "*.tsx")


def scan_files(root: Path) -> list[Path]:
    files: list[Path] = []
    for sub in ("tools/portal/src", "tools/portal/entries"):
        base = root / sub
        if base.is_dir():
            for ext in _SOURCE_EXTS:
                files += [p for p in base.rglob(ext) if p.is_file()]
    assets = root / "docs" / "assets"
    if assets.is_dir():
        files += sorted(assets.glob("*.html"))
    interactive = root / "docs" / "interactive"
    if interactive.is_dir():
        files += sorted(interactive.rglob("*.html"))
    return sorted(set(files))


def audit(root: Path) -> tuple[list[str], set[str]]:
    """Return ``(violations, exemption keys actually used)``."""
    dockerfile = root / "components" / "da-portal" / "Dockerfile"
    if not dockerfile.is_file():
        raise DockerfileParseError(f"Dockerfile not found: {dockerfile}")
    copies = parse_copies(dockerfile.read_text(encoding="utf-8"))

    files = scan_files(root)
    if not files:
        raise DockerfileParseError(
            "scan corpus is empty — the portal source layout moved; "
            "fix scan_files() rather than letting this pass silently"
        )

    violations: list[str] = []
    used: set[str] = set()
    for path in files:
        rel = path.relative_to(root).as_posix()
        text = path.read_text(encoding="utf-8", errors="replace")
        for expr, url in extract_references(rel, text):
            if not url:
                key = f"{rel}::{expr}"
                if key in EXEMPTIONS:
                    used.add(key)
                else:
                    violations.append(
                        f"{rel}: cannot statically resolve the URL of "
                        f"`{expr}` — if it is not an /assets/ path, register "
                        f'it in EXEMPTIONS as "{key}"'
                    )
                continue
            if not url.endswith("/") and not (
                root / "docs" / "assets" / url[len(ASSETS_URL):]
            ).is_file():
                continue  # dangling reference — out of this gate's scope
            if not is_shipped(copies, url):
                kind = "directory" if url.endswith("/") else "file"
                violations.append(
                    f"{rel}: loads {url} ({kind}) but no COPY in "
                    f"components/da-portal/Dockerfile puts it there"
                )
    return violations, used


def main(argv: list[str] | None = None) -> int:
    # argparse with no flags — enforces the repo-wide CLI contract
    # (test_help_exits_zero / test_invalid_args_exits_nonzero): `--help`
    # exits 0; unknown flags exit 2 (argparse default). argv defaults to []
    # (not sys.argv) so importers/tests calling main() are not handed pytest's
    # own argv → argparse SystemExit(2).
    argparse.ArgumentParser(
        description=i18n_text(
            "驗證 portal 在 runtime 從 /assets/ URL 命名空間載入的每個資產，"
            "都確實被 da-portal image 出貨（Dockerfile 的 COPY 涵蓋集合）。",
            "Validate that every asset the portal loads at runtime from the "
            "/assets/ URL namespace is shipped by the da-portal image.",
        ),
    ).parse_args([] if argv is None else argv)

    try:
        violations, used = audit(REPO_ROOT)
    except (DockerfileParseError, OSError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return EXIT_CALLER_ERROR

    stale = sorted(set(EXEMPTIONS) - used)
    if not violations and not stale:
        print(
            "OK: every runtime-loaded /assets/ path is shipped by "
            "components/da-portal/Dockerfile "
            f"({len(EXEMPTIONS)} registered unresolvable load site(s))."
        )
        return EXIT_OK

    if violations:
        print(f"FAIL: {len(violations)} portal asset-shipping violation(s):")
        for issue in violations:
            print(f"  - {issue}")
        print(
            "\nAn asset missing from the Dockerfile still works on GitHub "
            "Pages (mkdocs publishes docs/ wholesale) and in try-local (bind "
            "mount), so ONLY self-hosted image users see the failure. Fix by "
            "adding a COPY line to the shared-assets block, mirroring the "
            "existing ones (--chown=nginx:nginx, destination under "
            f"{HTML_ROOT}/assets/)."
        )
    if stale:
        print("\nFAIL: EXEMPTIONS entries no longer match any load site (remove them):")
        for key in stale:
            print(f"  - {key}")
    return EXIT_VIOLATION


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
