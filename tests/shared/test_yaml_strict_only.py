"""One YAML reading in Python: the strict one (#4111, owner decision D1).

WHAT IT GUARDS. YAML forbids a mapping from holding the same key twice;
PyYAML does not enforce it — `yaml.safe_load` keeps the LAST value and says
nothing. The repo's only reading is the specification's:
`scripts/tools/_lib_yaml_strict.py` (`strict_safe_load`,
`strict_safe_load_all`, `StrictSafeLoader`), which refuses a repeated key
(`DuplicateKeyError`, a `yaml.YAMLError`). There is no list of exceptions:
not for a customer file "read the way its consumer reads it", not as debt
that only shrinks. A file that repeats a key is reported, never read.

THE RULE. Outside `_lib_yaml_strict.py`, no tracked `*.py` file names
* a PyYAML reading entry (`ENTRIES`: `yaml.safe_load*`, `yaml.load*`,
  `yaml.full_load*`, `yaml.unsafe_load*`, `yaml.compose*`, `yaml.parse`,
  `yaml.scan`, the Loader classes and the components they are built from);
* a ruamel.yaml reading entry (`RUAMEL_ENTRIES`);
* `construct_object` — driving a loader's constructor by hand is how a
  composed tree is turned into values without the duplicate check.
A reference counts, not only a call (`f = yaml.safe_load`,
`map(yaml.safe_load, …)`), under any spelling the shared resolver follows
(`tests/shared/_ast_refs.py`: `import yaml as y`, `from yaml import
safe_load as sl`, a constant `getattr`, a star import, a rebound module).
`yaml.dump` / `yaml.safe_dump`, `yaml.YAMLError`, node classes are not
readers. The files are the repository's (`tests/_tree.py` `repo_files`),
whatever directory they are in — `scripts/`, `helm/`, `components/`,
`docs/`, `.claude/`, anything added later. Python embedded in other files
(a workflow's `python3 -c`, a shell heredoc, a doc snippet) is held by a
token check at the end of this file, with its own allow-list for prose.

WHY `tests/` IS NOT SCANNED. A test is not a reader of anyone's data: it
reads fixtures it wrote, and the strict loader's own tests need the lax
reading as their control (the same document through `yaml.safe_load` and
through `strict_safe_load`, to show what changes). Measured when this gate
was written: 241 test files hold 810 such references, so including them
would be a migration of its own with nothing gained for a user.

This is a different question from `test_yaml_reader_boundary.py`, which asks
whether a file with a Go consumer is read through that consumer (its
ALLOWED / DEBT baseline). A point here can be green there and the reverse.

⚠️ WHAT IT CANNOT CATCH (it reads names, not values): a name built at run
time (`getattr(yaml, "safe_" + "load")`, `importlib`, `__import__`); a module
reached through a mapping (`sys.modules["yaml"]`); a module or loader handed
in as an argument and used as an attribute of the parameter; and a strict
loader driven around its check — `StrictSafeLoader(t).get_single_node()`
composes without constructing, and a subclass overriding
`construct_document` drops the check (the class is named, so it passes
here). Those shapes are left to review.
"""
from __future__ import annotations

import ast
import pathlib
import re
import sys

import pytest

HERE = pathlib.Path(__file__).resolve().parent
for _p in (HERE, HERE.parent):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))
from _ast_refs import Ref, module_is, refs  # noqa: E402
from _tree import repo_files  # noqa: E402

REPO = HERE.parents[1]

#: The one home of the YAML reading.
HOME = "scripts/tools/_lib_yaml_strict.py"
#: PyYAML's reading entries: load / compose / parse / scan, the Loader classes
#: and the reading components a loader is built from (also by their submodule
#: path, `yaml.constructor.SafeConstructor`).
ENTRIES = frozenset({
    "safe_load", "safe_load_all", "load", "load_all", "full_load", "full_load_all",
    "unsafe_load", "unsafe_load_all", "compose", "compose_all", "parse", "scan",
    "SafeLoader", "CSafeLoader", "Loader", "CLoader", "FullLoader", "CFullLoader",
    "UnsafeLoader", "CUnsafeLoader", "BaseLoader", "CBaseLoader",
    "Reader", "Scanner", "Parser", "Composer", "CParser",
    "BaseConstructor", "SafeConstructor", "FullConstructor", "UnsafeConstructor",
    "Constructor",
})
#: ruamel.yaml's: the `YAML` class (whose instance reads) and its load functions.
RUAMEL_ENTRIES = frozenset({
    "YAML", "load", "load_all", "safe_load", "safe_load_all", "round_trip_load",
    "round_trip_load_all", "compose", "compose_all", "parse", "scan",
})
#: A loader method that builds values from a node without the duplicate check.
CONSTRUCT = "construct_object"


def _forbidden(ref: Ref) -> bool:
    if ref.via_import:
        return False
    if ref.name == CONSTRUCT:
        return True
    mods = (ref.module,) if ref.module is not None else ref.star
    return any((module_is(m, package="yaml") and ref.name in ENTRIES)
               or (module_is(m, package="ruamel.yaml") and ref.name in RUAMEL_ENTRIES)
               for m in mods)


def violations(source: str) -> "list[tuple[int, str, str]]":
    """`(line, scope, name)` of every lax-reading reference in *source*."""
    return sorted((r.lineno, r.scope, f"{r.module or '?'}.{r.name}")
                  for r in refs(ast.parse(source)) if _forbidden(r))


def _sources() -> "dict[str, str]":
    out = {}
    for p in repo_files(".py"):
        rel = p.relative_to(REPO).as_posix()
        if rel.split("/", 1)[0] == "tests":
            continue
        out[rel] = p.read_text(encoding="utf-8")
    return out


@pytest.fixture(scope="module")
def sources() -> "dict[str, str]":
    return _sources()


# ── the gate ─────────────────────────────────────────────────────────

def test_the_scan_sees_the_repository(sources) -> None:
    """Anti-vacuity: a gate is as wide as the files it walks."""
    assert len(sources) >= 300, len(sources)
    tops = {rel.split("/", 1)[0] for rel in sources}
    assert {"scripts", "helm", "components", "docs"} <= tops, sorted(tops)
    for must in (HOME, "scripts/tools/ops/init_project.py",
                 "helm/vector/projection-gate/verify_tenant_projections.py"):
        assert must in sources, must
    # the detector fires on real code: the home builds its loader on
    # yaml.SafeLoader and drives construct_object
    names = {v[2] for v in violations(sources[HOME])}
    assert {"yaml.SafeLoader", f"?.{CONSTRUCT}"} <= names, names


def test_no_file_reads_yaml_laxly(sources) -> None:
    bad = {rel: violations(text) for rel, text in sorted(sources.items())
           if rel != HOME}
    bad = {rel: v for rel, v in bad.items() if v}
    assert not bad, (
        "lax YAML reading outside _lib_yaml_strict (a repeated key would keep "
        "its last value, silently — #4111):\n"
        + "".join(f"  {rel}:{line} ({scope}) {name}\n"
                  for rel, v in bad.items() for line, scope, name in v)
        + "Read with _lib_yaml_strict.strict_safe_load / strict_safe_load_all "
          "(or _lib_io.load_yaml_file / _lib_input). There is no exception list: "
          "a file that repeats a key is reported to its owner, not read.")


# ── the detector (synthetic sources) ─────────────────────────────────

@pytest.mark.parametrize("src", [
    "import yaml\nd = yaml.safe_load('a: 1')\n",
    "import yaml as y\ndef f(t):\n    return y.safe_load_all(t)\n",
    "from yaml import safe_load as sl\nsl('a: 1')\n",
    "import yaml\nf = yaml.safe_load\n",
    "import yaml\nlist(map(yaml.safe_load, ['a']))\n",
    "import yaml\nf = getattr(yaml, 'full_load')\n",
    "from yaml import *\nsafe_load('a')\n",
    "import yaml\nyaml.load('a', Loader=yaml.BaseLoader)\n",
    "import yaml\nyaml.unsafe_load('a')\n",
    "import yaml\nn = yaml.compose('a: 1')\n",
    "import yaml\nclass L(yaml.SafeLoader):\n    pass\n",
    "import yaml\nL = yaml.CSafeLoader\n",
    "from yaml.constructor import SafeConstructor\nSafeConstructor()\n",
    "def f(loader, node):\n    return loader.construct_object(node)\n",
    "from ruamel.yaml import YAML\nYAML().load('a')\n",
    "import yaml as _y\nY = _y\nY.safe_load('a')\n",
])
def test_each_lax_reading_is_caught(src) -> None:
    assert violations(src), src


@pytest.mark.parametrize("src", [
    "import yaml\ns = yaml.safe_dump({'a': 1})\n",
    "import yaml\ntry:\n    pass\nexcept yaml.YAMLError:\n    pass\n",
    "import json\njson.load(open('x'))\n",
    "from _lib_yaml_strict import strict_safe_load\nstrict_safe_load('a: 1')\n",
    "from _lib_yaml_strict import StrictSafeLoader\nclass L(StrictSafeLoader):\n    pass\n",
    "from _lib_io import load_yaml_file\nload_yaml_file('p')\n",
    "import yaml\nn = yaml.ScalarNode('tag', 'v')\n",
    "from yaml import safe_load\n",   # imported, never used
    "x = 'yaml.safe_load'\n",
])
def test_what_is_not_a_lax_reading(src) -> None:
    assert violations(src) == [], src


# ── Python embedded in other files (workflows, shell, docs) ──────────
#
# A `python3 -c "…"` in a workflow or a heredoc in a shell script reads YAML
# as surely as a `.py` file, and the AST above never sees it. These files
# are not Python, so the check is a token one: in a tracked file that is not
# `*.py`, not under tests/ (same reason as above) and not a symlink (its
# target is scanned), a PyYAML reading spelled `yaml.safe_load`, `yaml.load`,
# `yaml.full_load`, `yaml.unsafe_load`, `yaml.compose` (each also `_all`) or
# `safe_load_all` is red unless `TOKEN_ALLOWED` lists the file with its count
# and the reason. Embedded code reads through the lib instead:
# `sys.path.insert(0, "scripts/tools"); from _lib_yaml_strict import
# strict_safe_load`.
#
# ⚠️ A token, not a parse: `from yaml import safe_load` then `safe_load(…)`
# in embedded code is not seen; prose naming the call is (hence the list).

TOKEN = re.compile(r"\byaml\.(?:safe_load|load|full_load|unsafe_load|compose)(?:_all)?\b"
                   r"|\bsafe_load_all\b")

#: path -> (count, why the token is there and is not a reading)
TOKEN_ALLOWED: "dict[str, tuple[int, str]]" = {
    ".coderabbit.yaml": (1, "a comment quoting what PyYAML makes of a bare `off`"),
    ".github/workflows/ci.yml": (1, "a comment describing what an old test asserted"),
    "CHANGELOG.md": (1, "a released entry; history is not rewritten"),
    "changelog.d/2335-crd-loader-no-global-mutation.md": (
        1, "a changelog fragment describing a past change, not a reading"),
    "tools/portal/src/interactive/tools/cicd-setup-wizard.jsx": (
        1, "a comment on how a customer's YAML parser rejects a folded include"),
    "tools/portal/tests/cicdGenerators.test.ts": (
        1, "a comment on what the generated workflow must survive in a customer's repo"),
}


def _token_counts() -> "dict[str, int]":
    out = {}
    for p in repo_files():
        rel = p.relative_to(REPO).as_posix()
        if rel.endswith(".py") or rel.split("/", 1)[0] == "tests" or p.is_symlink():
            continue
        try:
            text = p.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        n = len(TOKEN.findall(text))
        if n:
            out[rel] = n
    return out


def test_embedded_python_reads_yaml_strictly() -> None:
    got = _token_counts()
    want = {k: v[0] for k, v in TOKEN_ALLOWED.items()}
    new = {k: n for k, n in got.items() if want.get(k) != n}
    stale = {k: n for k, n in want.items() if got.get(k) != n}
    assert not new and not stale, (
        "a PyYAML reading spelled in a non-.py file (#4111):\n"
        + "".join(f"  {k}: {n} occurrence(s), allowed {want.get(k, 0)}\n"
                  for k, n in sorted(new.items()))
        + "".join(f"  stale TOKEN_ALLOWED row: {k} (allowed {n}, found {got.get(k, 0)})\n"
                  for k, n in sorted(stale.items()))
        + "Embedded code reads through scripts/tools/_lib_yaml_strict "
          "(strict_safe_load); prose that only names the call goes in "
          "TOKEN_ALLOWED with its reason.")


def test_every_token_row_says_why() -> None:
    bad = {k: v for k, v in TOKEN_ALLOWED.items()
           if not (v[0] > 0 and len(v[1].split()) >= 5)}
    assert not bad, bad


def test_the_token_scan_sees_workflows_and_shell() -> None:
    """Anti-vacuity: the files the embedded readings lived in are scanned,
    and the token matches the shapes they had."""
    seen = {p.relative_to(REPO).as_posix() for p in repo_files()}
    for must in (".github/workflows/bench-record.yaml", ".github/workflows/validate.yaml",
                 "scripts/benchmark.sh", "scripts/ops/federation_e2e_run.sh"):
        assert must in seen, must
    for src in ("python3 -c \"import yaml; yaml.safe_load(open('a'))\"",
                "d = yaml.load(f, Loader=L)", "list(yaml.safe_load_all(t))",
                "yaml.full_load(x)"):
        assert TOKEN.search(src), src
    assert not TOKEN.search("yaml.safe_dump(d)") and not TOKEN.search("strict_safe_load(t)")
