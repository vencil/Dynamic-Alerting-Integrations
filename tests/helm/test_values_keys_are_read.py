"""test_values_keys_are_read.py — every values.yaml key is read by a template (#2532)

Helm accepts any key in a values file, so a key a chart declares but no
template reads is a silent no-op: an operator overrides it and nothing
changes. #2027 (`ingress:` with no Ingress template) and #2073
(`rules.operator.*` the templates never read) were both this class, and the
two README guards cannot see it — they only check that README and values.yaml
agree with each other.

This is a STATIC scan of each chart's templates (plus the `files/` a template
pulls in with `tpl (.Files.Get "files/x") .`). It resolves references the way
Go templates bind them:

  * `.Values.a.b`, `$.Values.a.b`, `index .Values "a" "b"`      -> a.b
  * inside `with X` / `range X`, `.` is X: `.c` -> X.c, bare `.` -> X
  * `$v := X` and `range $k, $v := X` bind `$v` to X
  * inside `define`, `.` is assumed to be the root context (helpers here are
    called with `.` or `$`; a helper handed a subtree is covered at the call
    site, where the subtree reference itself is counted)

A reference that is the head of `if` / `with` / `range` is a GUARD and covers
only that node. Any other reference covers the node AND its subtree —
`toYaml .Values.resources` really does consume every child. A leaf counts as
read when a reference covers it, or when a reference reaches BELOW it (a leaf
that is an empty map / null the template indexes into). Lists are leaves:
their element schema is not the values contract.

Known lenient spots (they can only hide a finding, never invent one):
  * `(default dict .Values.x).y` counts as reading all of `x`, not just `x.y`.
  * a subtree handed to `include` / `dict` counts as fully read, whatever the
    helper does with it.

Fail-closed: a template whose `if/with/range/define … end` does not balance,
or that hands the whole `.Values` somewhere, fails the scan instead of being
skipped — either would silently widen what counts as read.

Overlay values files are held to the same rule: a key misspelled in
`values-tier1.yaml` is as silent a no-op as one in `values.yaml`. Every
`values*.yaml` under `helm/` is scanned against the chart it overrides:

  * inside a chart directory (next to `Chart.yaml`) -> that chart;
  * anywhere else -> the chart named for it in `_REPO_OVERLAYS`.

An overlay with no owner fails instead of being skipped, and so does an
`_REPO_OVERLAYS` entry naming a file or chart that does not exist.

A key that is legitimately declared but not read by a template goes in
`_UNREAD_ALLOWED` with a reason. An entry whose key is now read, or no longer
declared, fails as stale.
"""
from __future__ import annotations

import functools
import re
from pathlib import Path

import pytest
import yaml

from _tree import repo_files

_REPO = Path(__file__).resolve().parent.parent.parent

# (values file relative to the repo, dotted key) -> why that file declares the
# key although no template of its chart reads it.
_UNREAD_ALLOWED: dict[tuple[str, str], str] = {}

# Overlay values files outside a chart directory -> the chart they are passed to.
_REPO_OVERLAYS: dict[str, str] = {
    # scripts/setup.sh: `helm upgrade --install ... helm/mariadb-instance -f helm/values-${inst}.yaml`.
    "helm/values-db-a.yaml": "mariadb-instance",
    "helm/values-db-b.yaml": "mariadb-instance",
}

_ACTION = re.compile(r"\{\{-?(.*?)-?\}\}", re.S)
_STRING = re.compile(r'"(?:[^"\\]|\\.)*"|`[^`]*`')
# `$`, `$var`, or a leading `.` not preceded by an identifier / `)` / `]`
# (so `(x).y` and `foo.bar` are not read as a fresh reference), then fields.
_REF = re.compile(
    r"(\$[A-Za-z0-9_]*|(?<![\w)\]])\.)"
    r"((?:\.?[A-Za-z_][A-Za-z0-9_]*)(?:\.[A-Za-z_][A-Za-z0-9_]*)*)?"
)
_KEYWORD = re.compile(r"^\s*(if|with|range|else\s+if|else\s+with|else|end|define|block|template)\b")
_INDEX = re.compile(r'index\s+\(?\s*\$?\.Values\s*((?:"[^"]+"\s*)+)')
_DECL = re.compile(r"\s*(\$[A-Za-z0-9_]+)(?:\s*,\s*(\$[A-Za-z0-9_]+))?\s*:?=\s*(.*)", re.S)
_WHOLE_VALUES = re.compile(r"(^|[\s(])\$?\.Values\s*($|[\s)|])")
_TPL_FILE = re.compile(r'tpl\s+\(\s*\.Files\.Get\s+"([^"]+)"')
_ROOT = ("<root>",)


class ScanError(Exception):
    pass


def _leaves(node, path=()):
    if isinstance(node, dict) and node:
        for key, child in node.items():
            yield from _leaves(child, path + (str(key),))
    elif path:
        yield path


def _resolve(tok: str, rest: str | None, scope, bound: dict):
    parts = tuple(p for p in (rest or "").lstrip(".").split(".") if p)
    if tok == "$":
        base = _ROOT
    elif tok.startswith("$"):
        base = bound.get(tok)
    else:
        base = scope
    if base is None:
        return None
    full = base + parts
    if full[:1] == _ROOT:
        return full[2:] if full[1:2] == ("Values",) else None
    return full


def _refs(expr: str, scope, bound: dict) -> list[tuple]:
    out = [tuple(re.findall(r'"([^"]+)"', m.group(1))) for m in _INDEX.finditer(expr)]
    for m in _REF.finditer(_STRING.sub('""', expr)):
        path = _resolve(m.group(1), m.group(2), scope, bound)
        if path is not None:
            out.append(path)
    return out


def scan(text: str, where: str = "<template>") -> tuple[set, set]:
    """Return (subtree reads, guard-only reads) for one template text."""
    reads: set = set()
    guards: set = set()
    stack: list = []  # (keyword, scope before it, bindings before it)
    scope, bound = _ROOT, {}
    for m in _ACTION.finditer(text):
        action = m.group(1).strip()
        if action.startswith("/*"):
            continue
        kw_match = _KEYWORD.match(action)
        kw = kw_match.group(1).split()[-1] if kw_match else None
        body = action[kw_match.end():] if kw_match else action
        if kw_match and kw_match.group(1).startswith("else"):
            if not stack:
                raise ScanError(f"{where}: `else` outside any block")
            scope, bound = stack[-1][1], dict(stack[-1][2])
            if kw == "else":
                continue
        if kw == "end":
            if not stack:
                raise ScanError(f"{where}: `end` without an open block")
            _, scope, bound = stack.pop()
            continue
        if kw == "define":
            stack.append((kw, scope, dict(bound)))
            scope, bound = _ROOT, {}
            continue
        decl = _DECL.match(body)
        rhs = decl.group(3) if decl else body
        if scope == _ROOT and _WHOLE_VALUES.search(_STRING.sub('""', rhs)):
            raise ScanError(f"{where}: hands the whole .Values somewhere: {{{{ {action[:80]} }}}}")
        found = _refs(rhs, scope, bound)
        head = found[0] if found else None
        if kw in ("if", "with", "range") and head is not None:
            guards.add(head)
            reads.update(found[1:])
        else:
            reads.update(found)
        if decl and head is not None:
            bound[decl.group(2) or decl.group(1)] = head
        if kw in ("if", "with", "range", "block") and not (kw_match and kw_match.group(1).startswith("else")):
            stack.append((kw, scope, dict(bound)))
        if kw in ("with", "range"):
            scope = head
    if stack:
        raise ScanError(f"{where}: {len(stack)} block(s) never closed by `end`")
    return reads, guards


def _is_read(leaf: tuple, reads: set, guards: set) -> bool:
    if leaf in guards:
        return True
    if any(r and leaf[: len(r)] == r for r in reads):
        return True
    return any(len(r) > len(leaf) and r[: len(leaf)] == leaf for r in reads | guards)


def _chart_sources(chart: Path) -> list[Path]:
    files = sorted(p for p in (chart / "templates").rglob("*") if p.is_file())
    pulled = []
    for f in files:
        for rel in _TPL_FILE.findall(f.read_text(encoding="utf-8")):
            target = chart / rel
            if not target.is_file():
                raise ScanError(f"{f.relative_to(_REPO)}: tpl'd file {rel} does not exist")
            pulled.append(target)
    return files + sorted(set(pulled))


@functools.lru_cache(maxsize=None)
def _chart_reads(chart: Path) -> tuple[frozenset, frozenset]:
    reads: set = set()
    guards: set = set()
    for src in _chart_sources(chart):
        r, g = scan(src.read_text(encoding="utf-8"), str(src.relative_to(_REPO)))
        reads |= r
        guards |= g
    return frozenset(reads), frozenset(guards)


def chart_report(chart: Path, values_file: Path | None = None) -> tuple[list[tuple], list[str], set]:
    """Return (all leaves, unread dotted keys, subtree reads) for one values file of a chart."""
    values_file = values_file or chart / "values.yaml"
    values = yaml.safe_load(values_file.read_text(encoding="utf-8")) or {}
    reads, guards = _chart_reads(chart)
    leaves = list(_leaves(values))
    unread = [".".join(leaf) for leaf in leaves if not _is_read(leaf, reads, guards)]
    return leaves, unread, reads | guards


def _rel(path: Path) -> str:
    return path.relative_to(_REPO).as_posix()


_CHARTS = sorted(p.parent for p in (_REPO / "helm").glob("*/Chart.yaml") if (p.parent / "values.yaml").exists())


def _values_files() -> list[Path]:
    return sorted(
        p for p in repo_files(".yaml", ".yml")
        if _rel(p).startswith("helm/") and p.name.startswith("values")
    )


def overlay_owners() -> tuple[dict[str, Path], list[str]]:
    """Return ({overlay path: chart dir}, problems) for every non-default values file under helm/."""
    by_name = {c.name: c for c in _CHARTS}
    owners: dict[str, Path] = {}
    problems: list[str] = []
    for path in _values_files():
        rel = _rel(path)
        if (path.parent / "Chart.yaml").is_file():
            if path.name != "values.yaml":
                owners[rel] = path.parent
        elif rel in _REPO_OVERLAYS:
            chart = by_name.get(_REPO_OVERLAYS[rel])
            if chart is None:
                problems.append(f"{rel}: _REPO_OVERLAYS names chart {_REPO_OVERLAYS[rel]!r}, which does not exist")
            else:
                owners[rel] = chart
        else:
            problems.append(
                f"{rel}: not in a chart directory and not in _REPO_OVERLAYS — which chart is it passed to?")
    for rel in _REPO_OVERLAYS:
        if not (_REPO / rel).is_file():
            problems.append(f"_REPO_OVERLAYS entry {rel}: no such file")
    return owners, problems


_OVERLAYS, _OVERLAY_PROBLEMS = overlay_owners()


# ── resolver controls ────────────────────────────────────────────────────────

def _unread(template: str, values: dict) -> list[str]:
    reads, guards = scan(template)
    return [".".join(leaf) for leaf in _leaves(values) if not _is_read(leaf, reads, guards)]


def test_resolver_follows_go_template_binding():
    values = {"a": {"x": 1, "y": 2}, "b": {"z": 1}, "c": {"w": 1}, "d": {"v": 1}, "e": {}}
    tmpl = (
        "{{ with .Values.a }}{{ .x }}{{ end }}"             # with rebinds `.`
        "{{ $v := .Values.b }}{{ $v.z }}"                     # variable binding
        '{{ define "h" }}{{ .Values.c.w }}{{ end }}'          # define = root context
        "{{ toYaml $.Values.d }}"                             # $ root, subtree read
        "{{ .Values.e.deep }}"                                # reference below an empty-map leaf
    )
    assert _unread(tmpl, values) == ["a.y"]


def test_a_guard_does_not_read_the_children():
    values = {"x": {"enabled": True, "port": 1}, "y": {"k": 1}}
    assert _unread("{{ if .Values.x.enabled }}{{ end }}{{ with .Values.y }}ok{{ end }}", values) == [
        "x.port", "y.k"]


def test_else_restores_the_outer_context():
    values = {"a": {"x": 1}, "top": 1}
    assert _unread("{{ with .Values.a }}{{ .x }}{{ else }}{{ .Values.top }}{{ end }}", values) == []


def test_strings_are_not_references():
    assert _unread('{{ printf ".Values.a" }}', {"a": 1}) == ["a"]


def test_unbalanced_blocks_fail_closed():
    with pytest.raises(ScanError, match="never closed"):
        scan("{{ if .Values.a }}")
    with pytest.raises(ScanError, match="without an open block"):
        scan("{{ end }}")


def test_handing_over_the_whole_values_fails_closed():
    with pytest.raises(ScanError, match="whole .Values"):
        scan("{{ toYaml .Values }}")


# ── the charts ───────────────────────────────────────────────────────────────

def test_the_scan_covers_every_chart():
    # Vacuity: a broken glob or resolver must not turn the check below into
    # zero cases, or into charts with no reads at all.
    assert len(_CHARTS) >= 10, [c.name for c in _CHARTS]
    for chart in _CHARTS:
        leaves, _, refs = chart_report(chart)
        assert leaves, f"{chart.name}: values.yaml has no leaves"
        assert refs, f"{chart.name}: no .Values reference resolved in any template"


def test_every_overlay_has_an_owning_chart():
    assert not _OVERLAY_PROBLEMS, _OVERLAY_PROBLEMS


def test_the_scan_covers_the_overlays():
    # Vacuity: the overlays this repo ships today. A discovery that stops
    # seeing them must fail here, not quietly shrink the parametrize below.
    known = {
        "helm/da-portal/values-tier1.yaml", "helm/da-portal/values-tier2.yaml",
        "helm/tenant-api/values-scope-enforce.yaml", "helm/vector/values-projection.yaml",
        "helm/values-db-a.yaml", "helm/values-db-b.yaml",
    }
    assert known <= set(_OVERLAYS), sorted(known - set(_OVERLAYS))
    for rel, chart in _OVERLAYS.items():
        leaves, _, _ = chart_report(chart, _REPO / rel)
        assert leaves, f"{rel}: no leaves"


_VALUES_FILES = [(_rel(c / "values.yaml"), c) for c in _CHARTS] + sorted(_OVERLAYS.items())


@pytest.mark.parametrize("rel,chart", _VALUES_FILES, ids=[rel for rel, _ in _VALUES_FILES])
def test_every_declared_key_is_read(rel, chart):
    _, unread, _ = chart_report(chart, _REPO / rel)
    unexpected = [k for k in unread if (rel, k) not in _UNREAD_ALLOWED]
    assert not unexpected, (
        f"{rel} declares {unexpected} but no template of helm/{chart.name} reads them — "
        "overriding them is a silent no-op. Read them in a template, delete them, or list "
        "them in _UNREAD_ALLOWED with the reason."
    )


def test_exemptions_are_not_stale():
    owners = dict(_VALUES_FILES)
    stale = []
    for (rel, key), _reason in _UNREAD_ALLOWED.items():
        chart = owners.get(rel)
        if chart is None:
            stale.append(f"{rel}: not a scanned values file")
            continue
        leaves, unread, _ = chart_report(chart, _REPO / rel)
        if tuple(key.split(".")) not in leaves:
            stale.append(f"{rel}: {key} is no longer declared")
        elif key not in unread:
            stale.append(f"{rel}: {key} is read now")
    assert not stale, stale
