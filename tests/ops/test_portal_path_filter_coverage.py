"""Drift guard for the Portal Tests path filter (#1215 / #1222 / #1223 follow-up).

`ci.yml`'s `detect-changes` job path-gates the Portal Tests leg on the `portal`
filter. Several portal Vitests are TWO-ENDED drift gates: they bind a
hand-maintained portal-side copy to a generated/authored SSOT that lives
OUTSIDE `tools/portal/`. The assertion lives on the portal end — so if the SSOT
end is not in the filter, a PR that only moves the SSOT never runs Portal
Tests, merges green, and leaves the red for the next unrelated portal PR. Worse,
a path-skipped required check reports `skipped`, which SATISFIES branch
protection, so nothing about the PR looks wrong.

That is not hypothetical: #1215 and #1222 both regenerated
`docs/assets/platform-data.json` without it being in the filter, the rule-pack
offline-fallback gate never ran on either PR, and main went red afterwards —
blocking every subsequent portal PR until #1223 fixed both the drift and that
one filter entry.

Invariant enforced here: **every out-of-tree path a portal test reads must be
covered by a pattern in ci.yml's `portal` filter.** Network-free pure parsing,
so it runs in the plain Python Tests job — and `.github/workflows/**` is in the
`python` filter, so editing either end re-runs this guard.

Two extraction shapes are recognised, matching the two idioms in use:

  A. a literal that climbs to the repo root inline —
     `readFileSync(resolve(__dirname, '../../../docs/assets/x.json'))`
  B. a repo-root constant plus repo-relative literals —
     `const REPO = resolve(__dirname, '../../../')` … `read('rule-packs/x.md')`

Shape B only applies in files that carry BOTH a repo-root constant and a
`readFileSync`, and only to literals that resolve to a file that actually
exists — three filters, because a bare `'docs/x.md'` string is otherwise just
as likely to be an assertion on a rendered link href.

STILL BEST-EFFORT, and deliberately so: a path assembled at runtime (glob,
f-string, variable) is invisible here. Under-detection is the safe direction —
it means no assertion, never a false red. The mirror-image check ("no filter
entry is stale") would be UNSOUND for the same reason: it would flag a
legitimately-needed entry whose read this parser cannot see, and red an
innocent PR. Don't add it.

STRONGER FIX, where it applies: for a GENERATED SSOT the durable answer is to
stop hand-maintaining the portal-side copy and emit it from the same generator,
so `platform-data-check` (which the Lint job runs `--all-files`, ungated by any
path filter) catches staleness at commit time and this whole filter question
never arises. This guard is the safety net for the gates that still work the
two-ended way.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"
PORTAL_TESTS = ROOT / "tools" / "portal" / "tests"

# Shape A: a literal that climbs at least to the repo root (three levels up
# from tools/portal/tests/) and names something below it.
CLIMBING_LITERAL = re.compile(r"""['"]((?:\.\./){3,}[^'"]+)['"]""")

# Shape B: a bare repo-relative literal, used together with a repo-root
# constant. Anchored on the repo's real top-level directories so it cannot
# match arbitrary prose.
REPO_RELATIVE_LITERAL = re.compile(
    r"""['"]((?:docs|rule-packs|components|scripts|k8s|helm|try-local"""
    r"""|operator-manifests|environments|tools)/[\w./-]+)['"]"""
)

# Shape B only EXISTS as an idiom because of a repo-root constant, so require
# one — plus an actual reader — before treating bare literals in a file as
# paths. Without both, a repo-relative string is far more likely to be an
# assertion on a rendered link href than a file the test opens (that exact
# false positive: YamlValidatorTab.test.tsx asserting a docs/ href).
ROOT_CONST = re.compile(r"""['"](?:\.\./){3,}['"]""")
READER = re.compile(r"\breadFileSync\b")

TEST_SUFFIXES = {".ts", ".tsx", ".js", ".jsx"}


def _filter_patterns(name: str) -> list[str]:
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    steps = workflow["jobs"]["detect-changes"]["steps"]
    filter_step = next(s for s in steps if s.get("id") == "filter")
    filters = yaml.safe_load(filter_step["with"]["filters"])
    return filters[name]


def _portal_filter_patterns() -> list[str]:
    return _filter_patterns("portal")


# ── the mirror image: pytest reading OUT of the Python tree ─────────────────
#
# ⛔ This half was missing, and the asymmetry bit immediately. #1176 added a
# pytest that reads portal SOURCE (`tools/portal/src/**`) to assert a component
# still routes through the shared renderer — the pytest half of a deliberate
# cross-language pair. `tools/portal/tests/**` was in the `python` filter but
# `tools/portal/src/**` was not, so a PR that only edited the component matched
# `portal`, missed `python`, and skipped the very check written to catch it
# (blind review, #1344). Same failure shape as the portal side, same fix.

PY_TEST_DIRS = ("tests",)
PY_ROOTS = {"REPO_ROOT", "ROOT", "_REPO_ROOT", "REPO", "_ROOT"}


def _python_filter_patterns() -> list[str]:
    return _filter_patterns("python")


def _python_out_of_tree_reads() -> dict[str, list[str]]:
    """Map repo-relative path -> the pytest files that reference it.

    Same best-effort posture as the portal scanner: only literal, root-anchored
    joins that resolve to a file that actually exists. Under-detection is the
    safe direction (no assertion), over-detection would red an innocent PR.

    ⛔ FILES only. A bare directory read (`REPO_ROOT / "docs"`) is a coverage
    question about a whole subtree and answering it mechanically would mean
    demanding `docs/**` in the `python` filter — a real CI-cost decision, not
    something a drift guard gets to make. Recorded here as a known blind spot
    rather than silently conflated with the file case.

    ⛔ Case-SENSITIVE existence. `Path.exists()` on Windows matches
    `"readme.md"` against `README.md`, and `tests/dx/test_verify_diff.py`
    contains exactly that string as a FIXTURE for the same trap — so a naive
    check reports a nonexistent read, on one OS only.
    """
    found: dict[str, list[str]] = {}
    for top in PY_TEST_DIRS:
        for test_file in sorted((ROOT / top).rglob("test_*.py")):
            src = test_file.read_text(encoding="utf-8")
            try:
                tree = ast.parse(src)
            except SyntaxError:
                continue
            for rel in _module_repo_paths(tree):
                if _is_tracked_file(rel):
                    found.setdefault(rel, []).append(
                        test_file.relative_to(ROOT).as_posix())
    return found


def _module_repo_paths(tree: ast.AST) -> set[str]:
    """Repo-relative paths a module names, via AST rather than regex.

    Two shapes, because both are in use:

      A. a join off a repo-root constant — `REPO_ROOT / "a/b"`, and the
         multi-segment `REPO_ROOT / "a" / "b"`.
      B. ⛔ ANY module-level string literal that resolves to a real repo file,
         in a module that also has a repo-root constant. The regex-only version
         saw shape A only, and `tests/ops/test_registry_lib.py` writes
         `tuple(REPO_ROOT / rel for rel in _TENANT_DOC_RELS)` — the join target
         is a NAME, so the two files it reads
         (`docs/getting-started/for-tenants.*`) were invisible, and they really
         were missing from the filter. A scanner whose blind spot is "someone
         put the paths in a constant" is blind to the tidiest code in the repo
         (blind review, #1344).

    The `_is_tracked_file` check upstream is what keeps shape B honest: a
    string that is not a real file in the tree is not treated as a path.
    """
    has_root = any(
        isinstance(n, ast.Name) and n.id in PY_ROOTS for n in ast.walk(tree))
    if not has_root:
        return set()
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
            parts = _div_literals(node)
            if parts:
                out.add("/".join(parts))
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            v = node.value.strip()
            if _looks_like_a_path(v):
                out.add(v)
    return out


# ⛔ Bound the candidate BEFORE it reaches the filesystem. Shape B accepts any
# module-level string, and `tests/dx/**` embeds whole Python programs as
# literals (the fake-`gh` script). On Linux `Path("<1.5 KB of source>").is_file()`
# raises `OSError: [Errno 36] File name too long`; on Windows it just returns
# False. So the un-bounded version passed on the host and ERRORED in the
# container — i.e. in CI (#1344). A "path" has no whitespace and is short.
_MAX_PATH_LEN = 200


def _looks_like_a_path(v: str) -> bool:
    return (
        "/" in v
        and len(v) <= _MAX_PATH_LEN
        and not v.startswith(("/", "http", "."))
        and not any(c.isspace() for c in v)
    )


def _div_literals(node: ast.AST) -> list[str]:
    """Flatten `ROOT / "a" / "b"` to ['a', 'b']; [] if any part is not literal."""
    if isinstance(node, ast.Name):
        return [] if node.id not in PY_ROOTS else ["\0ROOT"]
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [node.value.strip("/")]
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
        left, right = _div_literals(node.left), _div_literals(node.right)
        if not left or not right:
            return []
        parts = left + right
        return [p for p in parts if p != "\0ROOT"] if "\0ROOT" in parts else []
    return []


def _is_tracked_file(rel: str) -> bool:
    """Exists, is a file, and is spelled exactly as on disk.

    Every filesystem call is guarded: a candidate that the OS refuses to even
    stat is simply not a path, and must not become an error in a drift guard.
    """
    try:
        target = ROOT / rel
        if not target.is_file():
            return False
    except OSError:
        return False
    probe = ROOT
    try:
        for part in rel.split("/"):
            names = {p.name for p in probe.iterdir()} if probe.is_dir() else set()
            if part not in names:
                return False
            probe = probe / part
    except OSError:
        return False
    return True


def test_python_filter_covers_every_out_of_tree_pytest_input() -> None:
    patterns = _python_filter_patterns()
    uncovered = {
        path: sources
        for path, sources in _python_out_of_tree_reads().items()
        if not any(_covers(p, path) for p in patterns)
    }
    assert not uncovered, (
        "pytest files read repo paths that are NOT in ci.yml's `python` path "
        "filter — a PR touching only those paths would skip Python Tests "
        "(reporting `skipped`, which satisfies branch protection). Add each to "
        "the filter:\n"
        + "\n".join(f"    - {p!r}  (read by {', '.join(s)})"
                    for p, s in sorted(uncovered.items()))
    )


def test_python_scanner_actually_finds_something() -> None:
    """Empty-run guard. If the two regexes stop matching the idioms in use,
    the assertion above passes over nothing and reads as full coverage."""
    reads = _python_out_of_tree_reads()
    assert len(reads) >= 5, (
        f"only {len(reads)} out-of-tree pytest reads detected — the scanner "
        "has almost certainly stopped recognising the idiom")


def _out_of_tree_reads() -> dict[str, list[str]]:
    """Map repo-relative path -> the portal test files that reference it."""
    found: dict[str, list[str]] = {}

    def record(rel: str, test_file: Path) -> None:
        found.setdefault(rel, []).append(test_file.relative_to(ROOT).as_posix())

    for test_file in sorted(PORTAL_TESTS.rglob("*")):
        if test_file.suffix not in TEST_SUFFIXES:
            continue
        text = test_file.read_text(encoding="utf-8")

        for literal in CLIMBING_LITERAL.findall(text):
            target = (test_file.parent / literal).resolve()
            try:
                record(target.relative_to(ROOT).as_posix(), test_file)
            except ValueError:
                continue  # escapes the repo — not a CI-gateable path

        if ROOT_CONST.search(text) and READER.search(text):
            for literal in REPO_RELATIVE_LITERAL.findall(text):
                # Existence check drops near-misses (renamed//typo'd paths);
                # nothing to gate on a path that isn't in the tree anyway.
                if (ROOT / literal).exists():
                    record(literal, test_file)

    return found


def _covers(pattern: str, path: str) -> bool:
    """Does a paths-filter pattern cover `path` (a file OR a directory)?

    Deliberately handles only the two literal shapes the portal filter uses —
    an exact path and a `dir/**` prefix. A new glob shape fails coverage loudly
    rather than being silently mis-matched by a half-built glob engine.
    """
    if pattern == path:
        return True
    if pattern.endswith("/**"):
        prefix = pattern[: -len("/**")]
        return path == prefix or path.startswith(prefix + "/")
    return False


def test_portal_filter_covers_every_out_of_tree_test_input() -> None:
    patterns = _portal_filter_patterns()
    uncovered = {
        path: sources
        for path, sources in _out_of_tree_reads().items()
        if not any(_covers(p, path) for p in patterns)
    }
    assert not uncovered, (
        "portal Vitests read repo paths that are NOT in ci.yml's `portal` path "
        "filter — a PR touching only those paths would skip Portal Tests "
        "(reporting `skipped`, which satisfies branch protection) and leave the "
        "drift red for the next portal PR. Add each to the filter:\n"
        + "\n".join(
            f"  - {path!r}  (read by {', '.join(sorted(set(srcs)))})"
            for path, srcs in sorted(uncovered.items())
        )
    )


def test_this_guard_is_not_itself_path_skippable() -> None:
    """Turn the invariant on this guard.

    This guard is a pytest, so it only runs when `detect-changes` says the
    `python` filter matched. Its own inputs are the workflow file and the
    portal test tree — and the failure it exists to catch (a PR adding a
    portal Vitest with a new out-of-tree read) touches ONLY the latter. If
    `tools/portal/tests/**` is not in the `python` filter, such a PR skips
    Python Tests, this guard never runs, and the missing `portal` entry
    merges: the guard committing the exact omission it guards against.
    Caught by CodeRabbit on PR #1229 — the first version had this hole.
    """
    python_patterns = _filter_patterns("python")
    own_inputs = [
        PORTAL_TESTS.relative_to(ROOT).as_posix(),
        WORKFLOW.relative_to(ROOT).as_posix(),
    ]
    uncovered = [
        path
        for path in own_inputs
        if not any(_covers(p, path) for p in python_patterns)
    ]
    assert not uncovered, (
        "this guard reads paths that are NOT in ci.yml's `python` filter, so "
        "a PR touching only those paths would skip Python Tests and silently "
        f"disarm the guard: {uncovered}. Add them to the `python` filter."
    )


def test_guard_actually_sees_the_known_two_ended_gates() -> None:
    """Vacuous-pass protection.

    A regex that silently stops matching would make the gate above pass on an
    empty set forever — the exact fail-quiet mode this guard exists to kill.
    Pin the inputs we KNOW are read today (one per extraction shape's home
    gate), so a broken parser fails here instead of going quietly green.
    """
    reads = _out_of_tree_reads()
    expected = {
        "docs/assets/platform-data.json",
        "docs/schemas/tenant-config.schema.json",
        "components/tenant-api/internal/rbac/testdata/wizard",
    }
    missing = sorted(e for e in expected if e not in reads)
    assert not missing, (
        "extraction stopped seeing known out-of-tree test inputs "
        f"{missing} — the coverage assertion above is now vacuous. Fix the "
        "parser, or update this pin if the test genuinely stopped reading them."
    )
