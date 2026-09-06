"""Which `scripts/tools` files read a tenant conf.d — ONE answer, shared.

Two gates need this population: `test_confd_enumeration_contract.py`
(flat readers must say so) and `test_confd_case_parity_across_tools.py`
(readers must give one answer per spelling). Until #1761 each kept its
own discovery — one by substring (`config_dir` / `conf-d` / `conf_d`),
one by argparse flag — and a tool naming its knob any other way sat
outside BOTH without anything turning red: `gitops_check` (`dir_path`)
and `drift_detect` (`--dirs`) each enumerated a conf.d flat and were
never parametrized.

Membership here is the UNION of three structurally different keys, each
derived from the file rather than from a list of tools:

  K1  the file binds `_lib_confd` (an `Import` / `ImportFrom` node, not a
      substring — a comment saying "see _lib_confd" is not a reader);
  K2  the file declares one of `CONFD_FLAGS` literally in `add_argument`;
  K3  the source mentions one of `CONFD_TEXT_HINTS` (the historical net,
      kept because it is the only key that reaches readers taking the
      directory as a plain function parameter).

None of the three is sufficient alone — that is the point of a union —
and the enumeration contract's reconciliation pin asks the complementary
question ("what enumerates a directory and is in NONE of these?") so a
fourth naming convention has to be reviewed rather than slipping by.
"""

from __future__ import annotations

import ast
import pathlib

# Every spelling a Vibe tool uses for "the tenant conf.d directory" as a
# CLI flag. ⛔ This is a legitimate enumeration only because argparse is
# the authority on how a flag is declared — it is not a list of tools.
CONFD_FLAGS = ("--config-dir", "--conf-d", "--confd", "--config-base",
               "--source-dir")

# Substrings that mark a file as talking about a tenant config dir.
# `conf.d` (the literal directory name) joined the historical three in
# #1761: `drift_detect` documents `--dirs cluster-a/conf.d` and nothing
# else about it matched.
CONFD_TEXT_HINTS = ("config_dir", "conf-d", "conf_d", "conf.d")

_LIB = "_lib_confd"


def parse(path: pathlib.Path) -> ast.AST | None:
    """Parse or `None` — a file the walk cannot read is not silently 'no'.

    Callers that need to distinguish "unparseable" from "not a reader"
    check for `None` themselves; the population functions below treat an
    unparseable file as outside every AST-derived key, which is the
    honest answer (nothing was derived).
    """
    try:
        return ast.parse(path.read_text(encoding="utf-8", errors="replace"))
    except SyntaxError:
        return None


def imports_lib_confd(tree: ast.AST) -> bool:
    """K1 — does this module bind `_lib_confd` through an import node?"""
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.module == _LIB or (node.module or "").endswith("." + _LIB):
                return True
        elif isinstance(node, ast.Import):
            if any(a.name == _LIB or a.name.endswith("." + _LIB)
                   for a in node.names):
                return True
    return False


def confd_flag(path: pathlib.Path) -> str | None:
    """K2 — which flag this tool takes a conf.d through, from its argparse AST.

    ⛔ Derived from the `add_argument` calls rather than from a substring
    search, because the two are not the same question: `"--config-dir"`
    appears in help text, comments and docstrings of tools that never
    accept it, and a tool that accepts `--conf-d` (`describe_tenant`)
    contains neither spelling of the other.

    ⚠️ It reads LITERAL declarations only. A tool that passes the flag
    name as a variable, an f-string or `*flags` is invisible to this walk
    — blind review measured that with a real fake tool. That hole is
    closed by `test_no_tool_declares_flags_the_population_walk_cannot_read`
    in the parity test, which is what actually makes the claim true; this
    function alone does not.
    """
    tree = parse(path)
    if tree is None:
        return None
    found: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        if not (isinstance(fn, ast.Attribute) and fn.attr == "add_argument"):
            continue
        for arg in node.args:
            if isinstance(arg, ast.Constant) and arg.value in CONFD_FLAGS:
                found.add(arg.value)
    # A tool declaring several: prefer the explicitly conf.d-named one.
    for preferred in CONFD_FLAGS:
        if preferred in found:
            return preferred
    return None


def mentions_confd(src: str) -> bool:
    """K3 — the historical substring net."""
    return any(h in src for h in CONFD_TEXT_HINTS)


def population_keys(path: pathlib.Path) -> set[str]:
    """Which of {"import", "flag", "hint"} put this file in the population.

    Empty set ⇒ outside the population. The three keys are computed
    independently so a test can prove each one admits a file on its own.
    """
    keys: set[str] = set()
    src = path.read_text(encoding="utf-8", errors="replace")
    if mentions_confd(src):
        keys.add("hint")
    tree = parse(path)
    if tree is not None and imports_lib_confd(tree):
        keys.add("import")
    if confd_flag(path) is not None:
        keys.add("flag")
    return keys
