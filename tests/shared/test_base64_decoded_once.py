"""A ConfigMap ``binaryData`` value is decoded in one place (#3461), and no
other base64 decode ships unless it is listed here with why.

``_lib_consumer_read.binary_data_bytes`` decodes a ``binaryData`` value the
way the API server decodes a ``[]byte`` field (Go ``encoding/json``:
``base64.StdEncoding``, which skips ``\\r`` / ``\\n`` and refuses everything
else outside the alphabet). Before #3461 three tools (patch-config's
``configmap_files``, batch-diagnose's ``read_configmap_files``,
configmap-assemble's ``measure_artifact``) each called
``base64.b64decode(value, validate=True)`` themselves, which refuses a value
folded over lines with a YAML ``|`` block — a value the API server takes.

The guard does not judge whether a decode is "about a ConfigMap": that is a
question of meaning a scan cannot answer, and a guess is how the three copies
got in. Every base64 decode in shipped Python (every tracked ``*.py`` outside
``tests/``) is a hit; ``CANONICAL`` is the one sanctioned decode and
``EXEMPT`` lists the others, each with why. An entry of either whose count
no longer matches is red, so the list cannot rot into a blanket pass.

A hit is any reference (a call, or the function taken as a value:
``f = base64.b64decode``, ``map(base64.b64decode, ...)``) to:

* ``base64``: ``b64decode``, ``standard_b64decode``, ``urlsafe_b64decode``,
  ``decodebytes``, ``decodestring``, ``decode``;
* ``binascii.a2b_base64``;

through ``import base64`` / ``import base64 as b`` (attribute) or
``from base64 import b64decode [as d]`` (bare name), plus
``from base64 import *`` / ``from binascii import *`` themselves (the bare
calls after it cannot be told apart), plus ``codecs.decode`` / ``codecs.lookup``
/ ``codecs.getdecoder`` (attribute or ``from codecs import ... [as x]``) given
the base64 codec's name as a string constant.

Each allow-list entry carries the exact number of decode references in its
function: a second decode added inside an exempted function is red too.

⛔ What it cannot see: a module reached through ``importlib`` /
``__import__`` / ``getattr`` with a computed name, a codec name not spelled
as a constant, a decode written by hand (a lookup table), and any language
other than Python (Go decodes ``[]byte`` itself through ``encoding/json``;
JS/JSX is held only by review). Its job is to stop the plain spelling from
coming back, not to prove no decode exists.
"""
from __future__ import annotations

import ast
import functools

import pytest

from _pysource import parse_py
from _tree import REPO_ROOT, repo_files

#: The decoders, by module.
DECODERS = {
    "base64": frozenset({"b64decode", "standard_b64decode", "urlsafe_b64decode",
                         "decodebytes", "decodestring", "decode"}),
    "binascii": frozenset({"a2b_base64"}),
}
_CODECS_FNS = frozenset({"decode", "lookup", "getdecoder", "getincrementaldecoder",
                         "getreader"})
_BASE64_CODEC_NAMES = frozenset({"base64", "base_64", "base64_codec"})

#: The one sanctioned decode: a ConfigMap `binaryData` value, read as the API
#: server reads it. (path, enclosing qualified name, decode references in it)
CANONICAL = ("scripts/tools/_lib_consumer_read.py", "binary_data_bytes", 1)

#: (path, enclosing qualified name, decode references in it, why) — every
#: other decode in shipped Python. The count is exact: a second decode added
#: inside an exempted function is red, as is an entry that matches nothing.
EXEMPT: "tuple[tuple[str, str, int, str], ...]" = (
    ("scripts/tools/ops/_gsvc_proto.py", "Write.content", 1,
     "generator-service wire protocol: `content_b64` is a JSON field this "
     "repo's own client encodes (`base64.b64encode`), not a ConfigMap "
     "`binaryData` value; one-line strict base64 is that protocol's contract"),
)


def _enclosing_names(tree: ast.Module) -> "dict[int, str]":
    """{id(node): dotted name of the def/class it sits in, or '<module>'}."""
    out: dict[int, str] = {}

    def walk(node: ast.AST, scope: "tuple[str, ...]") -> None:
        for child in ast.iter_child_nodes(node):
            out[id(child)] = ".".join(scope) or "<module>"
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                walk(child, scope + (child.name,))
            else:
                walk(child, scope)

    walk(tree, ())
    return out


def decode_hits(tree: ast.Module) -> "list[tuple[int, str, str]]":
    """(line, enclosing qualified name, what) for every base64 decode reference
    in *tree*. Import bindings are read file-wide (a function-local import is
    as much a binding as a top-level one)."""
    module_alias: dict[str, str] = {}    # local name -> "base64" / "binascii" / "codecs"
    bare: dict[str, str] = {}            # local name -> "base64.b64decode"
    codecs_bare: dict[str, str] = {}     # local name -> "decode" (from codecs import ...)
    star: list[ast.ImportFrom] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.name in DECODERS or a.name == "codecs":
                    module_alias[a.asname or a.name] = a.name
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module in DECODERS:
            for a in node.names:
                if a.name == "*":
                    star.append(node)
                elif a.name in DECODERS[node.module]:
                    bare[a.asname or a.name] = f"{node.module}.{a.name}"
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module == "codecs":
            for a in node.names:
                if a.name in _CODECS_FNS:
                    codecs_bare[a.asname or a.name] = a.name

    def names_base64_codec(call: ast.Call) -> bool:
        args = list(call.args) + [k.value for k in call.keywords]
        return any(isinstance(a, ast.Constant) and isinstance(a.value, str)
                   and a.value.lower().replace("-", "_") in _BASE64_CODEC_NAMES
                   for a in args)

    where = _enclosing_names(tree)
    hits = [(n.lineno, where[id(n)], f"from {n.module} import *") for n in star]
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            mod = module_alias.get(node.value.id)
            if mod in DECODERS and node.attr in DECODERS[mod]:
                hits.append((node.lineno, where[id(node)], f"{mod}.{node.attr}"))
        elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load) and node.id in bare:
            hits.append((node.lineno, where[id(node)], bare[node.id]))
        elif isinstance(node, ast.Call) and names_base64_codec(node):
            f = node.func
            if (isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name)
                    and module_alias.get(f.value.id) == "codecs" and f.attr in _CODECS_FNS):
                hits.append((node.lineno, where[id(node)], f"codecs.{f.attr}(base64)"))
            elif isinstance(f, ast.Name) and f.id in codecs_bare:
                hits.append((node.lineno, where[id(node)],
                             f"codecs.{codecs_bare[f.id]}(base64)"))
    return sorted(hits)


def _shipped_python() -> "list[str]":
    return sorted(p.relative_to(REPO_ROOT).as_posix() for p in repo_files(".py")
                  if not p.relative_to(REPO_ROOT).as_posix().startswith("tests/"))


@functools.lru_cache(maxsize=1)
def _all_hits() -> "list[tuple[str, int, str, str]]":
    files = _shipped_python()
    assert len(files) > 250, f"scan found {len(files)} files — the walk itself is broken"
    assert CANONICAL[0] in files and "scripts/tools/ops/patch_config.py" in files
    hits, unreadable = [], []
    for rel in files:
        try:
            tree = parse_py(REPO_ROOT / rel)
        except (SyntaxError, UnicodeDecodeError) as exc:   # not a skip: named
            unreadable.append(f"{rel}: {exc}")
            continue
        hits += [(rel, line, scope, what) for line, scope, what in decode_hits(tree)]
    assert not unreadable, "could not read, so could not scan:\n" + "\n".join(unreadable)
    return hits


def _allow_list() -> "list[tuple[str, str, int]]":
    return [CANONICAL] + [(p, s, n) for p, s, n, _why in EXEMPT]


def unlisted(hits, allow) -> "list[tuple[str, int, str, str]]":
    """The hits whose (path, scope) no allow-list entry names."""
    keys = {(p, s) for p, s, _n in allow}
    return [h for h in hits if (h[0], h[2]) not in keys]


def count_mismatches(hits, allow) -> "list[tuple[str, str, int, int]]":
    """(path, scope, expected, found) for every allow-list entry whose decode
    count is not what it says — 0 found is a stale entry."""
    found: dict[tuple[str, str], int] = {}
    for rel, _line, scope, _what in hits:
        found[(rel, scope)] = found.get((rel, scope), 0) + 1
    return [(p, s, n, found.get((p, s), 0)) for p, s, n in allow
            if found.get((p, s), 0) != n]


def test_no_base64_decode_ships_outside_the_canonical_one_and_the_exemptions():
    bad = unlisted(_all_hits(), _allow_list())
    assert not bad, (
        "decode a ConfigMap binaryData value through "
        "_lib_consumer_read.binary_data_bytes; any other base64 decode goes in "
        "EXEMPT with why:\n" + "\n".join(f"{r}:{n} in {s}: {w}" for r, n, s, w in bad))


def test_the_canonical_decode_and_every_exemption_match_their_count():
    wrong = count_mismatches(_all_hits(), _allow_list())
    assert not wrong, ("allow-list entries whose decode count changed (0 found: "
                       "stale, delete it; more: a new decode needs its own review) — "
                       "(path, scope, expected, found): " + repr(wrong))


def test_every_exemption_says_why():
    assert all(why.strip() for _p, _s, _n, why in EXEMPT)
    assert all(n >= 1 for _p, _s, n in _allow_list())
    assert len({(p, s) for p, s, _n in _allow_list()}) == len(_allow_list())


# --- the allow-list comparison itself: it must see rot ------------------------

_HITS = [("a.py", 3, "f", "base64.b64decode"), ("b.py", 9, "C.m", "binascii.a2b_base64")]
_ALLOW = [("a.py", "f", 1), ("b.py", "C.m", 1)]


def test_the_comparison_passes_a_matching_allow_list():
    assert unlisted(_HITS, _ALLOW) == [] and count_mismatches(_HITS, _ALLOW) == []


def test_the_comparison_sees_an_entry_that_matches_nothing():
    assert count_mismatches(_HITS, _ALLOW + [("gone.py", "g", 1)]) == [("gone.py", "g", 1, 0)]


def test_the_comparison_sees_a_count_that_is_off():
    extra = _HITS + [("a.py", 4, "f", "base64.b64decode")]
    assert count_mismatches(extra, _ALLOW) == [("a.py", "f", 1, 2)]
    assert count_mismatches(_HITS, [("a.py", "f", 2), ("b.py", "C.m", 1)]) == [("a.py", "f", 2, 1)]


def test_the_comparison_sees_an_unlisted_decode():
    assert unlisted(_HITS, _ALLOW[:1]) == [_HITS[1]]


# --- the scanner itself: must fire / must not fire ----------------------------


@pytest.mark.parametrize("snippet", [
    "import base64\nbase64.b64decode(x)",
    "import base64\nbase64.b64decode(x, validate=True)",
    "import base64\nbase64.standard_b64decode(x)",
    "import base64\nbase64.urlsafe_b64decode(x)",
    "import base64\nbase64.decodebytes(x)",
    "import base64\nbase64.decodestring(x)",
    "import base64\nbase64.decode(fin, fout)",
    "import binascii\nbinascii.a2b_base64(x)",
    "import base64 as b\nb.b64decode(x)",
    "import binascii as ba\nba.a2b_base64(x)",
    "from base64 import b64decode\nb64decode(x)",
    "from base64 import b64decode as d\nd(x)",
    "from binascii import a2b_base64\na2b_base64(x)",
    "from base64 import *\nb64decode(x)",
    "import base64\nf = base64.b64decode",
    "import base64\nlist(map(base64.b64decode, xs))",
    "def g():\n    import base64\n    return base64.b64decode(x)",
    "import codecs\ncodecs.decode(x, 'base64')",
    "import codecs\ncodecs.decode(x, encoding='base64_codec')",
    "import codecs\ncodecs.getdecoder('base-64')",
    "from codecs import decode\ndecode(x, 'base64')",
    "from codecs import decode as dec\ndec(x, encoding='base64_codec')",
    "from codecs import lookup\nlookup('base64')",
    "from codecs import getdecoder as gd\ngd('base64')",
])
def test_the_scanner_sees_each_spelling(snippet):
    assert decode_hits(ast.parse(snippet)), snippet


@pytest.mark.parametrize("snippet", [
    "import base64\nbase64.b64encode(x)",
    "import base64\nbase64.urlsafe_b64encode(x)",
    "import binascii\nbinascii.b2a_base64(x)",
    "import binascii\nbinascii.hexlify(x)",
    "x.b64decode(y)",                       # not the base64 module
    "def b64decode(v):\n    return v\nb64decode(x)",
    "import codecs\ncodecs.decode(x, 'utf-8')",
    "from codecs import decode\ndecode(x, 'utf-8')",
    "decode(x, 'base64')",                  # not codecs' decode
    "s = 'base64.b64decode(x)'",
])
def test_the_scanner_passes_over_what_is_not_a_decode(snippet):
    assert not decode_hits(ast.parse(snippet)), snippet


def test_the_scanner_names_the_enclosing_def():
    tree = ast.parse("import base64\nclass W:\n    @property\n    def content(self):\n"
                     "        return base64.b64decode(self.c)\n")
    assert decode_hits(tree) == [(5, "W.content", "base64.b64decode")]
    assert decode_hits(ast.parse("import base64\nbase64.b64decode(x)\n"))[0][1] == "<module>"
