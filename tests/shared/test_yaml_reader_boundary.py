"""Who under `scripts/` reads YAML with PyYAML — an AST ratchet on the
boundary of ADR-036 §12 (step 12, #3206).

WHAT IT GUARDS. A YAML file means what the program that reads it and acts on
it says it means. When a file has a Go consumer, Python does not read it with
PyYAML; it asks that consumer's reading: a Prometheus rule file
`da-read prometheus-rules`, `prometheus.yml` `da-read prometheus`, an
Alertmanager config `da-read alertmanager`, a Kubernetes manifest or Helm
values `da-crdecode`, conf.d-shaped text `da-guard parse-tree`. Files only
Python reads (a GitHub workflow, the pre-commit config, mkdocs, the repo's
own data) are read as before, each place listed with its reason.

WHAT COUNTS AS ONE POINT. A reference to a PyYAML parsing entry (`ENTRIES`:
`yaml.safe_load` … `yaml.scan`, the Loader classes and their reading
components) — not only a call:
`f = yaml.safe_load` and `map(yaml.safe_load, …)` count too. An aliased import
(`from yaml import safe_load as sl`), a module alias (`import yaml as y`) and a
constant `getattr(yaml, "safe_load")` are counted under the canonical name
(`tests/shared/_ast_refs.py`, shared with `test_rewrite_layer_is_gone.py`).
`except yaml.YAMLError` and `yaml.dump` / `yaml.safe_dump` are not points (a
writer is not a reader). The readers in `_lib_io` and `_lib_yaml_keys`
(`WRAPPER_HOMES`) — every top-level function, class or assignment there whose
definition reaches an entry, directly or through another one — are entries
themselves, computed from the AST (`wrappers()`): where they are defined is
not a point, where they are used is. A module that re-exports an entry
(`_lib_python` re-exports `_lib_io.load_yaml_file`) passes it on under the
name it binds. A point is identified by (repo path, qualname of the enclosing
def/class or `"<module>"`) with a count — no line numbers, which drift with
every edit.

THE RATCHET. The migration is not finished, so every point found today is
registered in `_yaml_reader_baseline.py`: `ALLOWED` (only Python reads it, with
the reason) or `DEBT` (it has a Go consumer, with the reading it moves to).
Every key the scan finds must be in exactly one of them with an equal count.
More, or a new key, is red: ask the consumer's reading, or — if only Python
reads the file — add the point to `ALLOWED` with its reason. Fewer, or a key
gone, is red too: shrink or drop the row (DEBT only shrinks). ⛔ Nothing
mechanical stops a new row in `DEBT`; a new DEBT row is refused in review.

A star import (`from yaml import *`, `from _lib_io import *`) binds every
entry of that module; a plain rebinding of a module (`Y = yaml`, `x = io`) is
followed. ruamel.yaml counts too (`RUAMEL_ENTRIES`: its `YAML` class and load
functions).

⚠️ WHAT IT CANNOT CATCH (it reads names, not values): a name built at run time
(`getattr(yaml, "safe_" + "load")`, `importlib`, `__import__`); a module or an
entry reached through a mapping (`yaml.__dict__["safe_load"]`,
`sys.modules["yaml"]`); a module or parser handed in as an argument and used as
an attribute of the parameter (`def f(m): m.safe_load(...)`), including from a
module outside `scripts/`; and the callers of a helper inside `scripts/` other
than `WRAPPER_HOMES` — only the helper's own point counts, so an `ALLOWED`
helper later called on a file with a Go consumer stays green.
"""
from __future__ import annotations

import ast
import collections
import pathlib
import sys
from dataclasses import dataclass

import pytest

HERE = pathlib.Path(__file__).resolve().parent
for _p in (HERE, HERE.parent):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))
from _ast_refs import Ref, module_is, refs  # noqa: E402
from _tree import repo_files  # noqa: E402
from _yaml_reader_baseline import ALLOWED, DEBT, Reading, debt_by_class  # noqa: E402

REPO = HERE.parents[1]

#: PyYAML's parsing entries: the load / compose / parse / scan functions, the
#: Loader classes and the reading components a loader is built from (also by
#: their submodule path, `yaml.reader.Reader`). `dump`, `safe_dump`,
#: `YAMLError`, node classes are not here.
ENTRIES = frozenset({
    "safe_load", "safe_load_all", "load", "load_all", "full_load", "full_load_all",
    "unsafe_load", "unsafe_load_all", "compose", "compose_all", "parse", "scan",
    "SafeLoader", "CSafeLoader", "Loader", "CLoader", "FullLoader", "CFullLoader",
    "UnsafeLoader", "CUnsafeLoader", "BaseLoader", "CBaseLoader",
    "Reader", "Scanner", "Parser", "Composer", "CParser",
})

#: ruamel.yaml's: the `YAML` class (whose instance does the reading) and the
#: module-level load functions.
RUAMEL_ENTRIES = frozenset({
    "YAML", "load", "load_all", "safe_load", "safe_load_all", "round_trip_load",
    "round_trip_load_all", "compose", "compose_all", "parse", "scan",
})

#: The shared readers whose definitions reaching an entry make them entries
#: (`_lib_yaml_keys` is deleted in step 13).
WRAPPER_HOMES = ("scripts/tools/_lib_io.py", "scripts/tools/_lib_yaml_keys.py")

Key = "tuple[str, str]"


def _stem(path: str) -> str:
    return path.rsplit("/", 1)[-1].removesuffix(".py")


@dataclass
class Scan:
    points: "collections.Counter[tuple[str, str]]"
    #: home module -> its top-level names that are entries
    wrappers: "dict[str, frozenset[str]]"


def _entries_of(module: str, known: "dict[str, set[str]]") -> "frozenset[str] | set[str]":
    """The names that reach a parser when taken from *module*."""
    if module_is(module, package="yaml"):
        return ENTRIES
    if module_is(module, package="ruamel.yaml"):
        return RUAMEL_ENTRIES
    return known.get(module.rsplit(".", 1)[-1], set())


def _reaches(ref: Ref, known: "dict[str, set[str]]", own: "set[str]") -> bool:
    """Whether *ref* reaches a parser: an entry, or a wrapper or re-export of
    one by its module (`_lib_io.load_yaml_file`, or `load_yaml_file` imported
    from `_lib_python`); a bare name a star import may have bound
    (`from yaml import *`); or — in a wrapper home — one of the home's own
    wrappers (*own*) by bare name."""
    if ref.via_import:
        return False
    if ref.module is not None:
        return ref.name in _entries_of(ref.module, known)
    return ref.name in own or any(ref.name in _entries_of(m, known) for m in ref.star)


def scan(sources: "dict[str, str]") -> Scan:
    """The points in *sources* (repo path -> source text)."""
    trees = {path: list(refs(ast.parse(text))) for path, text in sources.items()}
    homes = {_stem(p): p for p in WRAPPER_HOMES if p in trees}
    known: "dict[str, set[str]]" = collections.defaultdict(set)   # module -> names
    defined: "dict[str, set[str]]" = {h: set() for h in homes}
    changed = True
    while changed:
        changed = False
        for path, rs in trees.items():
            stem = _stem(path)
            own = defined.get(stem, set())
            for r in rs:
                if stem in homes and r.top and not r.via_import \
                        and r.top not in own and _reaches(r, known, own):
                    defined[stem].add(r.top)
                    known[stem].add(r.top)
                    changed = True
                # a module-level import of an entry is a re-export under its
                # local name; a star import re-exports all of them
                if r.via_import and r.scope == "<module>" and r.module is not None:
                    names = _entries_of(r.module, known)
                    new = (set(names) if r.name == "*" else
                           {r.local} if r.name in names else set()) - known[stem]
                    if new:
                        known[stem] |= new
                        changed = True
    points: "collections.Counter[tuple[str, str]]" = collections.Counter()
    for path, rs in trees.items():
        own = defined.get(_stem(path), set())
        for r in rs:
            if _reaches(r, known, own) and r.top not in own:
                points[(path, r.scope)] += 1
    return Scan(points, {h: frozenset(v) for h, v in defined.items()})


def _scripts() -> "dict[str, str]":
    return {p.relative_to(REPO).as_posix(): p.read_text(encoding="utf-8")
            for p in repo_files(".py") if p.relative_to(REPO).parts[0] == "scripts"}


@pytest.fixture(scope="module")
def found() -> Scan:
    return scan(_scripts())


# ── the gate ─────────────────────────────────────────────────────────

def test_the_files_are_read(found: Scan) -> None:
    """Tripwire: the gate is only as wide as the files it walks."""
    paths = set(_scripts())
    for must in (*WRAPPER_HOMES, "scripts/tools/_lib_python.py",
                 "scripts/ops/ci_flake_retry.py", "scripts/dx/generate_adr_index.py"):
        assert must in paths, must
    # the wrappers are derived, not listed: the two everyone uses must be among them
    assert {"load_yaml_file", "safe_load", "strict_safe_load"} <= found.wrappers["_lib_io"]
    assert "load_exporter_keys" in found.wrappers["_lib_yaml_keys"]


def test_the_baseline_has_no_key_twice() -> None:
    both = set(ALLOWED) & set(DEBT)
    assert not both, f"in both ALLOWED and DEBT: {sorted(both)}"


def test_every_point_is_registered_with_its_count(found: Scan) -> None:
    registered = {k: v[0] for k, v in {**ALLOWED, **DEBT}.items()}
    new = {k: n for k, n in found.points.items() if k not in registered}
    more = {k: (registered[k], n) for k, n in found.points.items()
            if k in registered and n > registered[k]}
    fewer = {k: (n, found.points.get(k, 0)) for k, n in registered.items()
             if found.points.get(k, 0) < n}
    msg = []
    if new or more:
        msg.append(
            "PyYAML reads YAML at a point not in the baseline (ADR-036 §12):\n"
            + "".join(f"  {p} :: {q}  ({n} point(s))\n" for (p, q), n in sorted(new.items()))
            + "".join(f"  {p} :: {q}  registered {a}, found {b}\n"
                      for (p, q), (a, b) in sorted(more.items()))
            + "If the file has a Go consumer, ask that consumer's reading instead "
              "(rule file: da-read prometheus-rules; prometheus.yml: da-read prometheus; "
              "Alertmanager: da-read alertmanager; Kubernetes / Helm: da-crdecode; "
              "conf.d-shaped text: da-guard parse-tree). If only Python reads it, add the "
              "point to ALLOWED in tests/shared/_yaml_reader_baseline.py with the reason.")
    if fewer:
        msg.append(
            "fewer points than the baseline registers — the migration moved on:\n"
            + "".join(f"  {p} :: {q}  registered {a}, found {b}\n"
                      for (p, q), (a, b) in sorted(fewer.items()))
            + "Lower the count or drop the row in tests/shared/_yaml_reader_baseline.py "
              "(DEBT only shrinks).")
    assert not msg, "\n".join(msg)


def test_every_debt_row_names_the_reading_it_moves_to() -> None:
    bad = {k: v for k, v in DEBT.items()
           if not (isinstance(v, tuple) and len(v) == 2 and isinstance(v[0], int)
                   and v[0] > 0 and isinstance(v[1], Reading))}
    assert not bad, f"DEBT rows must be (count > 0, Reading): {bad}"


_PLACEHOLDERS = frozenset({"", "todo", "tbd", "fixme", "xxx", "reason", "?", "-", "n/a"})


def test_every_allowed_row_says_why() -> None:
    bad = {k: v for k, v in ALLOWED.items()
           if not (isinstance(v, tuple) and len(v) == 2 and isinstance(v[0], int)
                   and v[0] > 0 and isinstance(v[1], str)
                   and v[1].strip().lower().rstrip(".") not in _PLACEHOLDERS
                   and len(v[1].split()) >= 4)}
    assert not bad, f"ALLOWED rows must be (count > 0, a reason in words): {bad}"


def test_debt_lists_by_reading() -> None:
    """`debt_by_class()` partitions DEBT exactly — the later steps ask it
    "is there any A / B left"."""
    by = debt_by_class()
    assert set(by) == set(Reading)
    flat = [k for keys in by.values() for k in keys]
    assert sorted(flat) == sorted(DEBT) and len(flat) == len(set(flat))
    for cls, keys in by.items():
        assert all(DEBT[k][1] is cls for k in keys)


#: The readings whose migration is done: no DEBT row may move to them again.
#: A later step adds its letter here when its last row goes (it is a set so two
#: steps landing in either order merge by union). A: every reader of a
#: Prometheus rule file asks `da-read prometheus-rules`
#: (`_lib_consumer_read.prometheus_rule_files` / `prometheus_rule_text_results`
#: / `manifest_rule_file_results`); B: every reader of a `prometheus.yml` asks
#: `da-read prometheus`; C: every reader of an Alertmanager config asks
#: `da-read alertmanager`; D: every reader of a Kubernetes manifest, Helm
#: values file or rendered chart asks `da-crdecode`.
CLEARED: "frozenset[Reading]" = frozenset({Reading.A, Reading.B, Reading.C, Reading.D})
#: A class not cleared yet but down to these rows only — the rows an owner
#: decision holds back. Empty for a class once it moves to CLEARED.
REMAINING: "dict[Reading, frozenset[tuple[str, str]]]" = {}


def test_cleared_readings_have_no_debt() -> None:
    """ADR-036 §12 step 12: a reading whose migration is done keeps no DEBT
    row, and a class held back by an owner decision keeps only those rows."""
    by = debt_by_class()
    left = {cls.name: by[cls] for cls in CLEARED if by[cls]}
    assert not left, f"DEBT rows in a cleared reading: {left}"
    for cls, allowed in REMAINING.items():
        extra = sorted(set(by[cls]) - allowed)
        assert not extra, f"DEBT {cls.name} rows beyond the held-back ones: {extra}"
    assert not CLEARED & set(REMAINING), "a cleared reading cannot hold rows back"


# ── the detector (synthetic sources, no repo file) ───────────────────

_LIB_IO = (
    "import yaml\n"
    "SAFE = yaml.SafeLoader\n"
    "def load_yaml_file(path):\n"
    "    with open(path) as f:\n"
    "        return _parse(f)\n"
    "def _parse(f):\n"
    "    return yaml.safe_load(f)\n"
    "def write(d):\n"
    "    return yaml.safe_dump(d)\n"
    "class Strict(yaml.SafeLoader):\n"
    "    pass\n"
)
_LIB_PY = "from _lib_io import load_yaml_file\n"


def _points(src: str) -> "dict[tuple[str, str], int]":
    s = scan({"scripts/tools/_lib_io.py": _LIB_IO, "scripts/tools/_lib_python.py": _LIB_PY,
              "scripts/t.py": src})
    return {k: n for k, n in s.points.items() if k[0] == "scripts/t.py"}


def _at(*scopes: str) -> "dict[tuple[str, str], int]":
    return dict(collections.Counter(("scripts/t.py", q) for q in scopes))


@pytest.mark.parametrize("src, want", [
    ("import yaml\nd = yaml.safe_load('a: 1')\n", _at("<module>")),
    ("import yaml as y\ndef f(t):\n    return y.safe_load_all(t)\n", _at("f")),
    ("from yaml import safe_load as sl\ndef f(t):\n    return sl(t)\n", _at("f")),
    ("from _lib_io import load_yaml_file as lf\ndef f(p):\n    return lf(p)\n", _at("f")),
    ("import _lib_io as io\ndef f(p):\n    return io.load_yaml_file(p)\n", _at("f")),
    ("from tools import _lib_io\ndef f(p):\n    return _lib_io.load_yaml_file(p)\n", _at("f")),
    ("import yaml\nf = getattr(yaml, 'safe_load')\n", _at("<module>")),
    ("import yaml\ndef f():\n    g = yaml.safe_load\n    return g\n", _at("f")),
    ("import yaml\ndef f(ts):\n    return list(map(yaml.safe_load, ts))\n", _at("f")),
    ("import yaml\nclass C:\n    def m(self, t):\n        def inner():\n"
     "            return yaml.compose(t)\n        return inner()\n", _at("C.m.inner")),
    ("import yaml\nclass L(yaml.SafeLoader):\n    pass\n", _at("L")),
    ("import yaml\ndef f(t):\n    return yaml.load(t, Loader=yaml.CSafeLoader)\n", _at("f", "f")),
    ("from yaml import loader as pure\nL = pure.SafeLoader\n", _at("<module>")),
    ("from _lib_io import Strict\nclass Mine(Strict):\n    pass\n", _at("Mine")),
    ("from _lib_python import load_yaml_file\ndef f(p):\n    return load_yaml_file(p)\n",
     _at("f")),
    ("def f():\n    import yaml\n    return yaml.full_load('x')\n", _at("f")),
    # star imports bind every entry of the module
    ("from yaml import *\ndef f(t):\n    return safe_load(t)\n", _at("f")),
    ("from _lib_io import *\ndef f(p):\n    return load_yaml_file(p)\n", _at("f")),
    # a module rebound by plain assignment, at module level and in a function
    ("import yaml as _y\nY = _y\ndef f(p):\n    return Y.safe_load(p)\n", _at("f")),
    ("import _lib_io as io\ndef f(p):\n    x = io\n    return x.load_yaml_file(p)\n",
     _at("f")),
    # PyYAML's reading components, also by submodule path
    ("import yaml\nclass R(yaml.reader.Reader, yaml.Scanner):\n    pass\n", _at("R", "R")),
    ("from yaml.parser import Parser\nP = Parser\n", _at("<module>")),
    # ruamel.yaml
    ("from ruamel.yaml import YAML\ndef f(t):\n    return YAML().load(t)\n", _at("f")),
    ("import ruamel.yaml\ndef f(t):\n    return ruamel.yaml.YAML(typ='safe').load(t)\n",
     _at("f")),
    ("from ruamel import yaml\nd = yaml.round_trip_load('a: 1')\n", _at("<module>")),
])
def test_each_reading_is_caught(src, want) -> None:
    assert _points(src) == want


@pytest.mark.parametrize("src", [
    "import yaml\ns = yaml.safe_dump({'a': 1})\n",                     # a writer
    "import yaml\ntry:\n    pass\nexcept yaml.YAMLError:\n    pass\n",  # an error class
    "import json\nd = json.load(open('x'))\n",                         # another `load`
    "import yaml\nfrom _lib_io import write\nwrite({})\n",            # not a wrapper
    "from yaml import safe_load\n",                                    # imported, never used
    "def safe_load(t):\n    return t\nsafe_load('x')\n",             # own function, same name
    "x = 'yaml.safe_load'\n",                                          # a string
    "from json import *\nd = load(open('x'))\n",                       # another star
    "import json as j\nJ = j\nd = J.load(open('x'))\n",                # another rebinding
    "import yaml\nn = yaml.ScalarNode('tag', 'v')\n",                 # a node, not a reader
])
def test_what_is_not_a_reading(src) -> None:
    assert _points(src) == {}


def test_wrappers_are_derived_and_their_definitions_are_not_points() -> None:
    s = scan({"scripts/tools/_lib_io.py": _LIB_IO})
    # `load_yaml_file` reaches the parser only through `_parse`: transitive
    assert s.wrappers["_lib_io"] == {"SAFE", "load_yaml_file", "_parse", "Strict"}
    assert not s.points
