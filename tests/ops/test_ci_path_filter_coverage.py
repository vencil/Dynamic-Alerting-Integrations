"""Drift guards for CI path filters — three invariants, one failure mode.

Renamed from `test_portal_path_filter_coverage.py` (#1373), because the old name
needed a ⛔ note apologising that only one of its three halves was portal-
specific — and a comment explaining why a filename lies is a signal to rename
the file, not a fix.

⛔ Chasing a reference written before the rename: grepping the old name is NOT
enough, and that is the whole lesson of #1373. A reference wrapped mid-token
across two comment lines is invisible to a whole-name grep — one survived the
rename sweep exactly that way. Rejoin comment continuations (drop the newline,
its indent, and any `#`/`//`/`*` prefix) BEFORE matching.

Read this list before assuming what is (and is not) enforced here:

  1. `portal` filter vs portal Vitests — every out-of-tree file a portal test
     READS must be in ci.yml's `portal` filter. (The original guard, below.)
  2. `python` filter vs pytest — the mirror image, for files a pytest reads.
     Added in #1344 after the asymmetry bit immediately.
  3. gating condition vs the job's own steps — every repo file a path-gated
     job EXECUTES or INSTALLS FROM must trigger the condition that gates it,
     across ci.yml, docs-ci.yaml and validate.yaml. Unlike 1 and 2 this asks
     nothing about tests: `requirements/ci-constraints.txt` is not an input to
     the Python suite, it is the suite's ENVIRONMENT, so both scanners above
     were structurally blind to it.

⛔ What invariant 3 does NOT model, stated once here because every one of these
was found by someone constructing a workflow that stayed green (blind review),
not by reading the code. The rule applied throughout is that an unmodelled
shape must be REFUSED, never absorbed — every gap below either raises or is a
disclosed boundary, because all of this module's assertions are negative
("nothing uncovered") and a silent mis-model turns the file green and quiet.

  * REFUSED (raise): dorny's `predicate-quantifier`; a `!`-prefixed exclusion
    pattern; a gate expressed through the detect job's outputs in a shape
    `GATE_OUTPUT` cannot parse; a step-level `if:` consulting a gate output.
  * MODELLED since blind review: `working-directory:` on a step; the
    `${{ github.workspace }}` spelling of the checkout root; gate inheritance
    through `needs:` without `always()`.
  * DISCLOSED, still invisible: a file reached INDIRECTLY, through a script the
    job merely invokes (the filters themselves carry the entries this costs,
    found by tracing what those scripts read — see the ⛔ blocks in docs-ci.yaml
    and validate.yaml); a directory-shaped dependency (`pytest tests/`); a
    workflow-level `on.<event>.paths:`, which both scanned workflows' banners
    forbid but nothing enforces; and everything `_command_verb` lists as a gap.

The shared failure mode all three exist to prevent: a path-skipped required
check reports `skipped`, which SATISFIES branch protection. Nothing about such
a PR looks wrong, so the omission merges and the red lands on someone else.

The original portal rationale, still accurate for invariant 1:

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
from functools import lru_cache
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"
PORTAL_TESTS = ROOT / "tools" / "portal" / "tests"
# The config whose `include:` is what makes the line above true. Hard-coding a
# directory that another file defines is a two-ended binding of exactly the kind
# this module exists to guard, so it gets the same treatment: pinned by
# `test_scanned_portal_tree_matches_the_vitest_include` and listed among this
# guard's own inputs.
VITEST_CONFIG = ROOT / "tools" / "portal" / "vitest.config.ts"
VITEST_INCLUDE = re.compile(r"include:\s*\[([^\]]*)\]")

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


@lru_cache(maxsize=None)
def _load_workflow(path: Path) -> dict:
    """Parse a workflow once. ci.yml is ~1700 lines and every scanner below
    wants it per job; without this the module re-parsed it a dozen times."""
    return yaml.safe_load(path.read_text(encoding="utf-8"))


@lru_cache(maxsize=None)
def _workflow_filters(path: Path) -> dict[str, list[str]]:
    """Every `dorny/paths-filter` filter defined in a workflow, by name.

    ⛔ Two of dorny's inputs change what "this filter matched" MEANS, and this
    module models neither — it treats a filter as a plain OR over positive
    patterns (`any(_covers(...))`). Both are rejected loudly rather than
    silently mis-modelled, because both fail in the direction that turns the
    whole file green: an unread exclusion makes an uncovered path look covered.
      * `predicate-quantifier` — `every` turns the OR into an AND, and
        `some-with-excludes` makes a negated match FINAL, overriding positives.
      * a `!`-prefixed pattern — `_covers` would treat `!x` as the literal
        segment `"!x"`, return False, and `any()` would absorb that False while
        the other patterns still report "covered". Note this is exactly the case
        `_covers`' docstring does NOT cover when it calls False "the loud
        direction": False is loud for a POSITIVE pattern the caller is asking
        about, and mute for a negation, which is why the rejection lives here
        instead. Found by blind review; neither shape occurs in the repo today,
        which is precisely when a silent mis-model would be cheapest to add.
    """
    filters: dict[str, list[str]] = {}
    for job in _load_workflow(path)["jobs"].values():
        for step in job.get("steps") or []:
            if "dorny/paths-filter" not in str(step.get("uses", "")):
                continue
            with_block = step.get("with") or {}
            if "predicate-quantifier" in with_block:
                raise AssertionError(
                    f"{path.name}: the dorny step sets predicate-quantifier="
                    f"{with_block['predicate-quantifier']!r}, which changes how "
                    "a filter's patterns combine. This module assumes the "
                    "default (`some` = OR over positive patterns). Model the "
                    "quantifier in `_covers`' callers before using it.")
            parsed = yaml.safe_load(step["with"]["filters"])
            negated = sorted(
                p for patterns in parsed.values() for p in patterns
                if isinstance(p, str) and p.startswith("!"))
            if negated:
                raise AssertionError(
                    f"{path.name}: exclusion patterns {negated} cannot be "
                    "expressed by this module's OR-over-patterns model — an "
                    "excluded path would still report as covered. Model "
                    "exclusion (and set predicate-quantifier accordingly) "
                    "before adding one.")
            _merge_filter_block(filters, parsed, path.name)
    return filters


def _merge_filter_block(into: dict[str, list[str]], parsed: dict,
                        where: str) -> None:
    """Merge one paths-filter block, refusing to overwrite a name.

    ⛔ A second step redefining a name would silently win, and
    `_filter_patterns("python")` would quietly stop meaning the detect job's
    python filter. One step per workflow today — split out as a pure function
    purely so the refusal can be pinned: it was the module's one fail-loud path
    with no test, because the caller needs a workflow on disk to exercise it.
    """
    clash = sorted(set(parsed) & set(into))
    if clash:
        raise AssertionError(
            f"{where} defines filter name(s) {clash} in more than one "
            "`dorny/paths-filter` step. This module assumes one namespace per "
            "workflow — decide which step owns each name before merging them.")
    into.update(parsed)


def _filter_patterns(name: str) -> list[str]:
    return _workflow_filters(WORKFLOW)[name]


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

# The pytest tree this module scans. A single value, indexed rather than
# iterated — it was a tuple in case a second root appeared and none has, so
# it stays a named constant (two call sites) without pretending to be a list.
PY_TEST_ROOT = "tests"

# Repo-root constants, recognised two ways — the UNION, because each way alone
# is strictly worse:
#
# ⛔ Deliberately NO counts here. Earlier revisions carried three ("28
# assignments", "35 modules", "16 modules"); every one drifted within two days
# as `tests/` grew, and re-measuring them produced a DIFFERENT number again
# depending on whether you count modules or detections — two blind reviews
# disagreed on the same figure for that reason. A number that needs a stated
# metric and a stated date to mean anything is not evidence, it is decoration
# that reads like evidence. What actually holds both halves honest is
# `test_compat_root_names_are_all_load_bearing`, which re-measures on every run.
#
#   * The hand-written list on its own missed `_REPO`, so every shape-A join in
#     those modules was invisible — and because shape B is gated on a root too,
#     those modules were invisible in their entirety: a missing root name costs
#     the module its ENTIRE contribution.
#   * ⛔ The derivation on its own is NOT a superset: some modules yield
#     shape-A paths only the derivation reaches, and some yield paths only the
#     list reaches, because the repo
#     binds its roots four different ways — `parents[N]`, `HERE.parent.parent`,
#     `os.path.dirname(os.path.dirname(...))`, `Path(os.path.abspath(join(...)))`
#     — and only the first is a `parents[N]` Subscript. Replacing the list with
#     the derivation alone was a net regression that no test caught (blind
#     review); the union is what strictly improves on both.
#
# So this is deliberately not a pure "derive, never enumerate" — the list is a
# compatibility floor for binding idioms the derivation cannot see, and the
# derivation removes the floor's blind spot for names nobody thought to add.
#
# ⛔ Every name here must be LOAD-BEARING, and
# `test_compat_root_names_are_all_load_bearing` asserts it. A name the
# derivation already covers reads like a second line of defence while being
# inert, and pinning the two halves against each other is not enough — the
# names INSIDE this half need pinning too. `REPO` and `ROOT` were here and
# contributed exactly zero (the derivation sees both everywhere they appear).
#
# ⛔ `_ROOT` was here and is gone for a different reason: it admitted exactly
# one module, and all four of that module's "reads" are `_write(tmp_path, "…")`
# FIXTURE WRITES, not reads of the repo file. So the name bought only false
# positives — and the anchor added to pin it was itself anchored on one, which
# is how the false-positive nature surfaced at all (blind review). ⚠️ Shape B
# cannot tell a fixture write from a read in general; the rest are covered by
# the filter today, so that is an accepted limitation, not a closed one.
_CONVENTIONAL_ROOT_NAMES = {"REPO_ROOT"}


def _repo_root_names(tree: ast.AST) -> set[str]:
    """Names bound to `<something>.parents[N]`, plus the conventional names.

    `TOOLS_DIR = ….parents[2] / "scripts" / "tools"` is a BinOp, not a
    Subscript, so subdirectory constants are correctly excluded — joining a
    literal onto one of those would yield a path that is not repo-relative.
    """
    derived, present = _root_name_parts(tree)
    return derived | (present & _CONVENTIONAL_ROOT_NAMES)


def _root_name_parts(tree: ast.AST) -> tuple[set[str], set[str]]:
    """(names bound to `parents[N]`, every Name in the module).

    The two halves `_repo_root_names` combines, exposed so the compat-list
    measurement can vary the conventional set without re-deriving either — a
    hand-rolled copy there would drift the moment the derivation changes.
    """
    derived = {
        target.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign) and _is_parents_subscript(node.value)
        for target in node.targets
        if isinstance(target, ast.Name)
    }
    present = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    return derived, present


def _is_parents_subscript(node: ast.AST) -> bool:
    return (isinstance(node, ast.Subscript)
            and isinstance(node.value, ast.Attribute)
            and node.value.attr == "parents")


def _python_filter_patterns() -> list[str]:
    return _filter_patterns("python")


@lru_cache(maxsize=1)
def _parsed_test_modules() -> tuple[tuple[str, ast.AST], ...]:
    """Every `tests/**/test_*.py` parsed once, as (repo-relative path, tree).

    Two independent scans walked and re-parsed the whole tree separately; on a
    ~300-module suite that traversal, not the analysis, was the cost. Nothing
    mutates the trees, so one parse serves both.
    """
    parsed = []
    for test_file in sorted((ROOT / PY_TEST_ROOT).rglob("test_*.py")):
        try:
            tree = ast.parse(test_file.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        parsed.append((test_file.relative_to(ROOT).as_posix(), tree))
    return tuple(parsed)


@lru_cache(maxsize=1)
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
    `"readme.md"` against `README.md`, so a naive check reports a read that
    does not exist — on one OS only, which is the worst kind of divergence.
    (The `readme.md` literal in `tests/dx/test_verify_diff.py` was once cited
    here as the live example; it is not — one occurrence has whitespace and the
    other has no `/`, so `_looks_like_a_path` rejects both before this function
    sees them. The trap is real but currently only reachable through a shape-A
    join like `ROOT / "Docs" / "x.md"`, which is why
    `test_is_tracked_file_is_case_exact_and_stays_inside_the_repo` pins it
    synthetically.)
    """
    found: dict[str, list[str]] = {}
    for module, tree in _parsed_test_modules():
        for rel in _module_repo_paths(tree):
            if _is_tracked_file(rel):
                found.setdefault(rel, []).append(module)
    return found


def _module_repo_paths(tree: ast.AST) -> set[str]:
    """Repo-relative paths a module names, via AST rather than regex.

    Two shapes, because both are in use:

      A. a join off a repo-root constant — `REPO_ROOT / "a/b"`, and the
         multi-segment `REPO_ROOT / "a" / "b"`.
      B. ⛔ ANY string literal ANYWHERE in the module that resolves to a real
         repo file — including inside function bodies; the walk is over the
         whole tree. Restricting it to module level costs roughly a tenth of
         all detections, and is pinned by the
         `helm/federation-reconciler/values.yaml` anchor in
         `test_python_scanner_actually_finds_something` — twice this comment
         claimed a protection that did not exist, so check that anchor still
         has exactly one producer before trusting this sentence. Gated on the
         module also having a repo-root constant, which is shape A's
         precondition borrowed as a proxy for "this module deals in repo
         paths"; see the REJECTED note in
         `test_compat_root_names_are_all_load_bearing` for why lifting it was
         measured and reverted.

         Shape B exists because the regex-only version saw shape A only, and
         `tests/ops/test_registry_lib.py` writes
         `tuple(REPO_ROOT / rel for rel in _TENANT_DOC_RELS)` — the join target
         is a NAME, so the two files it reads
         (`docs/getting-started/for-tenants.*`) were invisible, and they really
         were missing from the filter. A scanner whose blind spot is "someone
         put the paths in a constant" is blind to the tidiest code in the repo
         (blind review, #1344).

    The `_is_tracked_file` check upstream is what keeps shape B honest: a
    string that is not a real file in the tree is not treated as a path.
    """
    roots = _repo_root_names(tree)
    if not roots:
        return set()
    return _module_repo_paths_with(tree, roots)


def _module_repo_paths_with(tree: ast.AST, roots: set[str],
                            shapes: "set[str]" = frozenset({"A", "B"})) -> set[str]:
    """The extraction itself, with the root set supplied.

    Split out so the compat-list measurement can ask "what would this module
    yield under THIS root set", and `shapes` so `_shape_a_paths` can ask for
    joins only — both without re-implementing either shape. An earlier
    duplicate of this body drifted against the original, and a second one had
    grown back before review caught it.
    """
    out: set[str] = set()
    for node in ast.walk(tree):
        if ("A" in shapes and isinstance(node, ast.BinOp)
                and isinstance(node.op, ast.Div)):
            parts = _div_literals(node, roots)
            if parts:
                out.add("/".join(parts))
        elif ("B" in shapes and isinstance(node, ast.Constant)
                and isinstance(node.value, str)):
            v = node.value.strip()
            if _looks_like_a_path(v):
                out.add(v)
    return out


# ⛔ Bound the candidate BEFORE it reaches the filesystem. Shape B accepts any
# module-level string, and `tests/dx/**` embeds whole Python programs as
# literals (the fake-`gh` script). On Linux `Path("<1.5 KB of source>").is_file()`
# raises `OSError: [Errno 36] File name too long`; on Windows it just returns
# False, so the un-bounded version passed on the host and ERRORED in the
# container (#1344).
#
# ⚠️ That original crash is no longer the reason: `_is_tracked_file` now catches
# OSError itself, so an over-long candidate would just be judged "not a path".
# These bounds are kept because they are the only throttle on shape B's
# breadth, and they carry real volume — over the ~8.4k unique string
# literals in admitted modules, the length bound drops ~250
# slash-containing candidates, the prefix bound ~135, the whitespace bound
# ~670. ⚠️ Approximate on purpose: this module's own literals are part of
# the corpus, so an exact figure here is falsified by the next edit to this
# file — which happened twice while it was being written.
#
# The bounds are cheap and they keep the filesystem out of the hot path.
_MAX_PATH_LEN = 200


def _looks_like_a_path(v: str) -> bool:
    return (
        "/" in v
        and len(v) <= _MAX_PATH_LEN
        and not v.startswith(("/", "http", "."))
        and not any(c.isspace() for c in v)
    )


_ROOT_MARK = "\0ROOT"


def _modules_needing_compat(
    modules: "list[tuple[str, ast.AST]] | None" = None,
) -> list[tuple[str, set[str]]]:
    """(module, candidate root names) for modules that rely on the compat list.

    `modules` is injectable ONLY so the behaviour can be pinned: its real-tree
    answer is the empty list — correct, and therefore useless as evidence that
    the function works. Replacing the whole body with `return []` was green
    until a synthetic module could be fed through it.

    A module qualifies when recognising one of its `__file__`-bound names would
    add a shape-A join the scanner does not already make. That is the only
    thing a root name can buy, so it is the only question worth asking — and it
    is self-filtering: `_TOOLS_DIR` and friends are bound to subdirectory
    handles or plain `str`s that cannot `/`-join, so they never qualify.

    ⚠️ The flip side, since `_shape_a_paths`' own docstring warns that joins
    are the wrong unit for measuring worth: this question is blind to a missing
    root name whose module would contribute only shape-B literals. Live
    example — `tests/lint/test_check_admin_config_schema.py` binds `_REPO` by
    an underivable idiom and would add 5 shape-B detections and 0 joins, so it
    is not reported here. Under-detection, and all 5 fall under `helm/**` /
    `k8s/**` which the filter already carries, but the gap is real.
    """
    out: list[tuple[str, set[str]]] = []
    for module, tree in modules if modules is not None else _parsed_test_modules():
        candidates: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign) and "__file__" in ast.dump(node.value):
                candidates |= {t.id for t in node.targets
                               if isinstance(t, ast.Name)}
        if not candidates:
            continue
        recognised = _repo_root_names(tree)
        if _shape_a_paths(tree, recognised | candidates) - _shape_a_paths(
                tree, recognised):
            out.append((module, candidates))
    return out


def _shape_a_paths(tree: ast.AST, roots: set[str]) -> set[str]:
    """Shape-A joins only — the sole thing recognising a root name buys.

    Used for the COMPLETENESS question only — "would recognising this name add
    a join the scanner does not already make". That framing is what keeps
    subdirectory handles out: `_TOOLS_DIR` is bound to a `str`, which can never
    `/`-join, so it excludes itself instead of being demanded as a repo root.

    ⛔ Not the right unit for measuring whether a listed name is load-bearing —
    a recognised root also ADMITS the module to the scan at all, so its bare
    literals depend on it too. `test_compat_root_names_are_all_load_bearing`
    measures in full detections for that reason.
    """
    # ⛔ Derived from the real extractor, not a second copy of its shape-A
    # branch. This module already records what a duplicated body costs (see
    # `_module_repo_paths_with`), and a copy here would silently stop measuring
    # the function it names the moment shape A gains an idiom.
    return {
        path for path in _module_repo_paths_with(tree, roots, shapes={"A"})
        if _is_tracked_file(path)
    }


def _div_literals(node: ast.AST, roots: set[str]) -> list[str]:
    """Flatten `ROOT / "a" / "b"` to ['a', 'b']; [] if any part is not literal.

    ⛔ The marker must survive nested flattening. The previous version stripped
    it inside the recursion, so for a THREE-operand join the outer call saw no
    marker and returned `[]` — i.e. only the two-operand form worked, silently.
    Measured cost of that: 97 tracked files reached by a `ROOT / "a" / "b"`
    join yielded nothing, against 38 total detections (blind review). Strip the
    marker once, at the top.
    """
    parts = _flatten_div(node, roots)
    return parts[1:] if parts[:1] == [_ROOT_MARK] else []


def _flatten_div(node: ast.AST, roots: set[str]) -> list[str]:
    if isinstance(node, ast.Name):
        return [_ROOT_MARK] if node.id in roots else []
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [node.value.strip("/")]
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
        left = _flatten_div(node.left, roots)
        right = _flatten_div(node.right, roots)
        return left + right if left and right else []
    return []


@lru_cache(maxsize=None)
def _is_tracked_file(rel: str) -> bool:
    """Exists, is a file, and is spelled exactly as on disk.

    Every filesystem call is guarded: a candidate that the OS refuses to even
    stat is simply not a path, and must not become an error in a drift guard.

    ⛔ Absolute and `..` candidates are rejected UP FRONT, not left to the
    segment walk. `ROOT / "/tmp/x.sh"` is `/tmp/x.sh` on POSIX — pathlib drops
    the left side — so `validate.yaml`'s real `sh /tmp/golangci-install.sh`
    step would `is_file()` a live SYSTEM path. Today the walk happens to reject
    it (`"".split("/")` yields an empty first segment that matches no entry),
    i.e. it is correct by accident of ordering. Make it correct on purpose.
    """
    if rel.startswith("/") or ".." in rel.split("/"):
        return False
    try:
        target = ROOT / rel
        if not target.is_file():
            return False
    except OSError:
        return False
    probe = ROOT
    for part in rel.split("/"):
        if part not in _dir_entries(probe):
            return False
        probe = probe / part
    return True


@lru_cache(maxsize=None)
def _dir_entries(directory: Path) -> frozenset[str]:
    """Cached, case-exact listing. The walk above asks for the same ancestor
    directories thousands of times — once per segment per candidate across the
    whole `tests/` tree — and each ask was a fresh `iterdir()`."""
    try:
        return frozenset(p.name for p in directory.iterdir())
    except OSError:
        return frozenset()


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
        "(reporting `skipped`, which satisfies branch protection).\n"
        "⛔ CHECK WHICH FIX APPLIES before editing the filter. Shape B "
        "harvests ANY literal anywhere in the module that resolves to a real "
        "file, so a "
        "path used purely as a test FIXTURE lands here too — and widening the "
        "filter for a fixture makes CI worse for no safety. If the named file "
        "is not actually opened by the named test, change the fixture to a "
        "fictional path instead. Otherwise, add each to the filter:\n"
        + "\n".join(f"    - {p!r}  (read by {', '.join(s)})"
                    for p, s in sorted(uncovered.items()))
    )


# The `python` filter entries this scanner currently justifies — i.e. entries
# it can point at a concrete pytest read for. Pinned as an EXACT set.
#
# ⛔ This replaces "add one more anchor path" as the answer to a scanner going
# blind, and it is the only formulation that scales. Anchors pin whatever axis
# you thought of: the shape anchors did not notice a whole NAMESPACE going
# dark, and adding namespace anchors would just be a second enumeration to
# forget to extend. Measured before this existed: blinding `helm/`, `k8s/`,
# `environments/` and `try-local/` cost 22 detections and every test stayed
# green, with the floor's headroom absorbing the loss — the entire namespace
# axis sat within a couple of detections of being unguarded, by luck rather
# than by assertion.
# (The floor is no longer what guards that axis — this exact-set assertion is,
# which is why no headroom figure is quoted here.)
#
# Entries absent from this set are justified by something other than this
# scanner (a portal Vitest, a workflow step, plain conservatism); their being
# unlisted is not a claim that they are unnecessary.
PYTHON_ENTRIES_THIS_SCANNER_JUSTIFIES = {
    "**/*.py", "tests/**", "scripts/**", "components/**", "helm/**", "k8s/**",
    "try-local/**", "Makefile", ".pre-commit-config.yaml", "docs/**",
    "renovate.json", ".devcontainer/**", ".github/workflows/**",
    "environments/**", "tools/portal/tests/**", "tools/portal/src/**",
}


def test_scanner_still_justifies_every_filter_entry_it_used_to() -> None:
    """Exact-set accounting over WHICH entries the scanner can vouch for.

    A per-namespace anchor list answers "did this one path survive"; this
    answers "did any part of the repo stop being seen", which is the question
    that actually matters — an entry losing its last justifier means either the
    scanner went blind there, or the entry is now unjustified and someone
    should say so out loud.
    """
    reads = {
        path for path, sources in _python_out_of_tree_reads().items()
        if sources != [Path(__file__).resolve().relative_to(ROOT).as_posix()]
    }
    justified = {
        pattern for pattern in _python_filter_patterns()
        if any(_covers(pattern, path) for path in reads)
    }
    lost = sorted(PYTHON_ENTRIES_THIS_SCANNER_JUSTIFIES - justified)
    gained = sorted(justified - PYTHON_ENTRIES_THIS_SCANNER_JUSTIFIES)
    assert not lost and not gained, (
        "the set of `python` filter entries this scanner can point at a real "
        "pytest read for changed.\n"
        f"  no longer justified: {lost}\n"
        f"  newly justified:     {gained}\n"
        "⛔ A LOST entry usually means the scanner went blind to that part of "
        "the tree — check the extractor before concluding the filter entry is "
        "unnecessary, and do NOT delete the entry to make this pass. A GAINED "
        "entry is normally fine: add it here.")


def test_python_scanner_actually_finds_something() -> None:
    """Empty-run guard for the pytest half.

    ⛔ The floor EXCLUDES reads sourced only from this module. Shape B harvests
    any module-level literal that resolves to a real file, so this file's own
    pins (`PINNED_STEP_INPUTS`, the shape anchors) are counted as "reads" — a
    handful of them, and the count moves every time a pin is added, which is
    itself the reason not to let them near the floor. Against the original
    floor of 5 that meant the scanner could go blind to every other file in
    `tests/` and still pass on its own literals alone: an anti-vacuity floor
    propped up by the very thing it is supposed to be independent of. Measured,
    not theorised — deleting shape B dropped detections 38 -> 11 at the time
    and the old floor stayed green (blind review).

    ⛔ Plus exact pins, one per extraction shape, so the SHAPE going blind reds
    rather than being absorbed by the other shape's yield.
    """
    reads = _python_out_of_tree_reads()
    own = Path(__file__).resolve().relative_to(ROOT).as_posix()
    independent = {p: s for p, s in reads.items() if s != [own]}
    assert len(independent) >= 100, (
        f"only {len(independent)} out-of-tree pytest reads detected outside "
        f"this file's own literals (total {len(reads)}) — the scanner has "
        "almost certainly stopped recognising an idiom")
    # ⛔ Each anchor must be EXCLUSIVE to its shape and read by ANOTHER module.
    # Both halves of that mattered: the first version pinned
    # `docs/schemas/tenant-config.schema.json` as the shape-B anchor, but no
    # pytest opens it — the only module naming it is THIS one, so the pin was
    # satisfied by the literal inside the assertion (the same self-propping the
    # floor above rejects). The other pin was labelled shape A but is reached
    # by shape B too, so deleting shape A entirely left it green.
    # ⛔ The two ROOT-half anchors must be shape-A JOINS. A bare literal pins a
    # root half only via shape B's gate — an incidental coupling — so a change
    # to that gate silently unpins them. (Demonstrated: an ungating experiment
    # did exactly that, with every test still green.)
    expected = {
        # shape A only: a join off a repo-root constant
        # (tests/lint/test_check_devcontainer_dep_parity.py)
        ".devcontainer/devcontainer.json",
        # shape B only: a bare literal in a module that has a repo-root
        # constant (tests/ops/test_registry_lib.py)
        "docs/getting-started/for-tenants.en.md",
        # ⛔ a shape-A JOIN reachable only through the conventional-name half of
        # `_repo_root_names` — its module binds the root with
        # `.parent.parent.parent`, which the `parents[N]` derivation cannot see.
        # Without this anchor the compat list is unenforced.
        "scripts/tools/dx/tenant_verify.py",
        # ⛔ …and the mirror anchor, a shape-A join reachable only through the
        # `parents[N]` derivation. Without it the two halves absorb each other:
        # killing the derivation leaves the other pins green, and only this one
        # names WHICH half broke.
        #
        # ⚠️ Both root-half anchors are deliberately shape-A JOINS, not bare
        # literals. A literal pins a root half only because shape B is gated on
        # a root being recognised — an incidental coupling, not the mechanism.
        # An experiment that ungated shape B made that concrete: the two
        # literal anchors this pair replaced stopped pinning anything, silently
        # and immediately. Anchor on the mechanism, not on a side effect of it.
        "docs/schemas/waveform-pack.schema.json",
        # ⛔ NAMESPACE, not shape. Every anchor above pins an extraction shape
        # or a root name, and all of them happen to be read from
        # `tests/{ops,lint,dx}` — so blinding the scanner to a whole subtree, or
        # to every `tools/`-prefixed detection, stayed green: the floor's
        # headroom absorbed it (blind review). This one is the #1176 case the
        # python half was ADDED for — a pytest reading portal SOURCE — so
        # losing it means losing the half's own motivating example.
        "tools/portal/src/interactive/tools/template-gallery.jsx",
        # ⛔ …and a SOURCE-tree anchor, because the one above is still read
        # from `tests/lint`. Restricting the scan to `tests/{ops,lint,dx}` —
        # where all the other anchors happen to live — stayed green until this
        # one, which only `tests/shared` produces. Two axes, two anchors: the
        # path's namespace and the reading module's.
        ".devcontainer/install-vector.sh",
        # ⛔ A third axis: shape B walks the WHOLE tree, so a literal inside a
        # function body counts. Nothing pinned that — restricting shape B to
        # module level dropped ~13% of detections with every test still green,
        # while a comment claimed the restriction "reds two tests".
        #
        # ⛔ And the FIRST anchor chosen for it did not work either, for a
        # reason worth keeping: `helm/federation-gateway/values.yaml` is a
        # function-body literal in `test_renovate_config.py`, but ANOTHER
        # module reaches the same path as a shape-A join
        # (`test_federation_e2e_mirrors_deploy_sot.py`), so narrowing shape B
        # never lost it. Anchor exclusivity has to be checked across every
        # producer in the tree, not just inside the module you had in mind —
        # the same "a second path absorbs the first one's death" trap this very
        # anchor list exists to close. This path has exactly one producer.
        "helm/federation-reconciler/values.yaml",
    }
    missing = sorted(
        e for e in expected
        if e not in independent  # not `reads`: self-sourced would re-prop it
    )
    assert not missing, (
        f"extraction stopped seeing known pytest reads {missing} — one of the "
        "two shapes has gone blind. Fix the scanner rather than the pin.")


# ── the third half: what a gated job's OWN STEPS name ───────────────────────
#
# ⛔ Both halves above ask the same question — "which files do the TESTS read?"
# — and therefore share one blind spot: a file that decides what a gated job
# DOES without any test reading it. `requirements/ci-constraints.txt` is that
# file. Every `pip install … -c requirements/ci-constraints.txt` step resolves
# versions through it (#1158), so it is not an input to the Python suite, it is
# the suite's ENVIRONMENT — and it was in no filter. A pin bump touches ONLY
# `requirements/**`, so python_changed=false, Python Tests is skipped, and the
# skip satisfies branch protection. Precisely: the ungated `lint-rule-packs`
# job does install these pins and run some pytest modules, and the full suite
# does run on the push-to-main AFTER the merge (`python_changed` is hard-coded
# 'true' off a PR) — so the defect is not "never tested", it is "no pre-merge
# signal, and main goes red for everyone instead of the PR going red for its
# author". Which is exactly what a required check is for. (Such a bump is
# hand-authored today — `renovate.json` sets `enabledManagers:
# ["custom.regex"]` and its managers match only image refs.)
#
# Invariant enforced here: **every repo file a path-gated job EXECUTES or
# INSTALLS FROM must TRIGGER the condition that gates that job.** Not "be
# covered by one of its filters" — the whole `if:` is evaluated, so a job
# gated `A && B` would require the file to match BOTH. Not
# every file it mentions — see the `INTERPRETERS` note below for why that
# weaker predicate is actively harmful. Mechanically checkable from the
# workflow alone: no reviewer has to notice that a `-c <file>` flag is a CI
# dependency.
#
# Soundness: this direction is safe to assert. If a gated job's commands name a
# tracked file, changing that file can change what the job does, so the job
# should run — exactly the CONSERVATIVE philosophy ci.yml's own banner states
# ("skip only what is provably unrelated; when in doubt, run"). The mirror
# ("no filter entry is unused") stays forbidden for the reason the module
# docstring gives.
#
# Same best-effort posture as the other two: only literal, existing paths are
# seen. A path built at runtime, or one reached through a script this job
# merely invokes, is invisible — under-detection, the safe direction.

GATE_OUTPUT = re.compile(r"needs\.[\w-]+\.outputs\.(\w+)_changed")

# ⛔ EXECUTED or INSTALLED-FROM, never merely MENTIONED. The first version of
# this half took any tracked-file token anywhere in the job, and that predicate
# is wrong in both directions:
#   * `run: echo "::error::see docs/internal/foo.md for the fix"` would demand
#     a runbook in the `portal` filter — a false red on a PR that only improved
#     an error message, with a failure message telling the maintainer to make
#     the filter WORSE. (`#` lines inside a `run: |` block are string content,
#     not YAML comments, so every prose comment was a candidate too.)
#   * `pytest --ignore=<some test file>` names files the job
#     deliberately does NOT run — an exclusion read as a dependency.
# So match only the idioms that actually make a file decide what a job does.
# There are THREE, and `_command_files` yields from each: an interpreter's
# script argument, pip's constraints/requirements file, and a `./script` run
# directly in command position. ⛔ Do not read "the two that motivated this
# half" as the whole list — an earlier version of this comment said "two", and
# the third is the one no gated job exercises today, so a maintainer trusting
# the count would find nothing using it and delete it.
INTERPRETERS = {"python", "python3", "py", "bash", "sh", "node", "ruby", "perl"}
DEPENDENCY_FLAGS = {"-c", "--constraint", "-r", "--requirement"}
PIP_COMMAND = re.compile(r"pip3?")
# `python3 "$GITHUB_WORKSPACE/scripts/ops/ci_flake_retry.py"` is a literal path
# behind a fixed prefix. Without this strip, `go-tests-threshold-exporter`
# yielded ZERO files and its coverage assertion was entirely vacuous.
# ⛔ Named variables only. Stripping ANY `$VAR/` turns `"$RUNNER_TEMP/Makefile"`
# — a scratch file that merely shares a name with a repo file — into a demanded
# filter entry. These two are the ENV-VAR spellings of the checkout root.
VAR_PREFIX = re.compile(r"^\$\{?(?:GITHUB_WORKSPACE|CI_PROJECT_DIR)\}?/")

# ⛔ The same root has a THIRD spelling, and an earlier version of the comment
# above claimed the two env vars were "the ones that actually denote the
# checkout" — false: `${{ github.workspace }}` is the GitHub-expression form and
# this repo already uses it (config-diff.yaml, nightly-race.yaml ×2). It cannot
# be handled by VAR_PREFIX, because `${{ github.workspace }}/x` contains spaces:
# tokenising splits it into `${{`, `github.workspace`, `}}/x`, the interpreter
# branch takes `${{` as the script argument and stops — the real path never
# becomes a candidate at all. So it is normalised out of the RAW TEXT, before
# any splitting. Blind review, and not hypothetical for long: the moment a gated
# leg spells its script that way, its coverage assertion goes vacuous silently.
# ⚠️ Only this one expression is modelled. Any other `${{ … }}` around a path is
# still invisible; there is no general answer without evaluating the expression.
WORKSPACE_EXPR = re.compile(r"\$\{\{\s*github\.workspace\s*\}\}/")


def _paths_filter_workflows() -> list[Path]:
    """Every workflow that path-gates jobs with `dorny/paths-filter`.

    Discovered, not listed: a new WORKFLOW that adopts the action is scanned the
    day it lands.

    ⛔ That is discovery at the workflow level only, and an earlier version of
    this docstring let it be read as more — "covered the day it lands", full
    stop. A workflow being scanned says nothing about its JOBS being seen:
    `_gated_jobs` recognises one gating idiom, so a leg written any other way
    lands inside a scanned workflow and is still invisible. Blind review built
    three that stayed green. `_gated_jobs` now rejects the ones it can detect
    but not parse; the ones it cannot even detect are named in its docstring.
    """
    return sorted(
        p for p in (ROOT / ".github" / "workflows").glob("*.y*ml")
        if "dorny/paths-filter" in p.read_text(encoding="utf-8")
    )


def _detect_jobs(workflow_path: Path) -> set[str]:
    """Job ids that RUN the dorny step, i.e. whose outputs are the gate."""
    return {
        job_id
        for job_id, job in _load_workflow(workflow_path)["jobs"].items()
        if any("dorny/paths-filter" in str(s.get("uses", ""))
               for s in (job.get("steps") or []))
    }


def _gated_jobs(workflow_path: Path) -> dict[str, list[str]]:
    """Map job id -> the filter names that decide whether it runs.

    Two ways in, and the second one exists because blind review walked through
    the first:

      1. the job's OWN `if:` consults a detect job's `*_changed` output;
      2. the job `needs:` a gated job and does not neutralise the dependency
         with `always()`. GitHub SKIPS a job whose dependency was skipped, so
         such a job is path-gated in fact while carrying no `if:` of its own.
         This workflow tree already relies on the escape hatch — every
         aggregate gate (`python-tests`, `all-checks`, …) writes `if: always()`
         precisely to opt out — so the first contributor who forgets it creates
         a silently gated leg. Today that changes nothing: all four such jobs
         carry `always()`, which is what makes this cheap to add now.

    ⛔ Still an idiom, not a semantics. What is DETECTED but NOT PARSED now
    raises below. What is not even detected: a gate expressed without touching
    the detect job's outputs at all (a separate `if:` on a step — see
    `_job_step_files` — or a workflow-level `on.<event>.paths:`, which both
    scanned workflows' banners forbid but nothing enforces).
    """
    known = _workflow_filters(workflow_path)
    jobs = _load_workflow(workflow_path)["jobs"]
    detect = _detect_jobs(workflow_path)
    gated: dict[str, list[str]] = {}
    for job_id, job in jobs.items():
        condition = str(job.get("if", ""))
        names = sorted(set(GATE_OUTPUT.findall(condition)) & set(known))
        if names:
            gated[job_id] = names
        elif any(f"needs.{d}.outputs." in condition for d in detect):
            raise AssertionError(
                f"{workflow_path.name}::{job_id} gates on a detect job's "
                f"outputs in a shape this module does not parse: {condition!r}. "
                "It is path-gated in reality and invisible to every coverage "
                "assertion here (dorny's own "
                "`contains(fromJSON(...outputs.changes), 'name')` array idiom "
                "is the likely spelling). Teach GATE_OUTPUT the shape, or move "
                "the leg out of the gate deliberately.")

    # Fixpoint over `needs:`, so a chain (gated -> A -> B) propagates too.
    changed = True
    while changed:
        changed = False
        for job_id, job in jobs.items():
            if job_id in gated or "always()" in str(job.get("if", "")):
                continue
            needs = job.get("needs") or []
            needs = [needs] if isinstance(needs, str) else needs
            inherited = sorted({n for dep in needs if dep in gated
                                for n in gated[dep]})
            if inherited:
                gated[job_id] = inherited
                changed = True
    return gated


# ── deciding whether a file actually triggers a gated job ───────────────────
#
# ⛔ Do NOT classify the condition and then pick union-or-intersection. Two
# attempts at that heuristic were defeated in review, in OPPOSITE directions:
# `"&&" in condition` fired on `evt && (A || B)` where the union is correct
# (and its message told the maintainer to intersect `go` with `docs`, which is
# nearly the empty set — a guard whose error message walks you into disarming
# it); the fix, "look between adjacent gate references", then stayed SILENT on
# `(A || evt) && B`, where the `||` sits between the gates and the `&&` binds
# outside them, so a file in `go` only would be accepted for a job that runs
# only when `docs` matched.
#
# ⭐ The two-heuristics-two-holes pattern is the signal: classifying was the
# wrong layer. EVALUATE the condition instead. A gate reference is true exactly
# when the file matches that filter, so the job runs for that file iff the
# whole boolean expression evaluates true — union, intersection and every mixed
# shape fall out of ordinary `and`/`or` with no special cases at all.
#
# Atoms outside the modelled set RAISE rather than defaulting either way: the
# safe default depends on the atom (assuming true under-reports, assuming false
# reds innocent PRs), so guessing is exactly what must not happen.
#
# ⚠️ `_covers` fails loud the OTHER way for an unmodelled glob — it returns
# False, so the path reports as uncovered. Both are loud; they are not the same
# stance, and an earlier version of this comment claimed they were. The
# difference is that a glob shape has a safe default (uncovered ⇒ someone
# looks) while a condition atom does not.

# `github.event_name` comparisons are evaluated for the PULL REQUEST case,
# which is the only one path filters govern (push-to-main runs everything —
# see the `detect-changes` outputs, which hard-code 'true' off a PR).
EVENT_NAME = re.compile(
    r"github\.event_name\s*(==|!=)\s*'([^']*)'")
GATE_ATOM = re.compile(
    r"needs\.[\w-]+\.outputs\.(\w+)_changed\s*==\s*'true'")
ALWAYS = re.compile(r"always\(\)")


def _condition_triggers(condition: str, path: str,
                        filters: dict[str, list[str]]) -> bool:
    """Would `path` changing make a job with this `if:` run, on a PR?"""
    tokens = _tokenize_condition(condition)
    value, rest = _parse_or(tokens, path, filters, condition)
    if rest:
        raise AssertionError(
            f"could not fully parse job condition {condition!r} (stopped at "
            f"{rest[0]!r}). Model the new syntax before using it.")
    return value


def _tokenize_condition(condition: str) -> list[str]:
    # `if: ${{ … }}` is as legal as the bare form and is what GitHub's own docs
    # show; strip the wrapper rather than reporting its contents as unmodelled.
    stripped = condition.strip()
    if stripped.startswith("${{") and stripped.endswith("}}"):
        stripped = stripped[3:-2]
    # `always()` / `success()` must survive as ONE token — splitting on the
    # parens turned `always()` into `['always', '(', ')']`, so the atom matcher
    # never saw it and a job written `if: always() && <gate>` hard-failed with
    # "unmodelled atom 'always'", telling the maintainer to model something
    # that already appeared to be modelled.
    protected = re.sub(r"\b(\w+)\(\)", r"\1\0CALL", stripped)
    tokens = re.findall(r"\(|\)|&&|\|\||[^()&|]+", protected)
    return [t.replace("\0CALL", "()") for t in tokens if t.strip()]


def _parse_or(tokens, path, filters, whole):
    value, tokens = _parse_and(tokens, path, filters, whole)
    while tokens and tokens[0] == "||":
        right, tokens = _parse_and(tokens[1:], path, filters, whole)
        value = value or right
    return value, tokens


def _parse_and(tokens, path, filters, whole):
    value, tokens = _parse_atom(tokens, path, filters, whole)
    while tokens and tokens[0] == "&&":
        right, tokens = _parse_atom(tokens[1:], path, filters, whole)
        value = value and right
    return value, tokens


def _parse_atom(tokens, path, filters, whole):
    if not tokens:
        raise AssertionError(f"job condition {whole!r} ends unexpectedly.")
    head, tokens = tokens[0], tokens[1:]
    if head == "(":
        value, tokens = _parse_or(tokens, path, filters, whole)
        if not tokens or tokens[0] != ")":
            raise AssertionError(f"unbalanced parentheses in {whole!r}.")
        return value, tokens[1:]
    return _atom_value(head.strip(), whole, path, filters), tokens


def _atom_value(atom: str, whole: str, path: str,
                filters: dict[str, list[str]]) -> bool:
    gate = GATE_ATOM.fullmatch(atom)
    if gate and gate.group(1) in filters:
        return any(_covers(p, path) for p in filters[gate.group(1)])
    event = EVENT_NAME.fullmatch(atom)
    if event:
        equals = event.group(1) == "=="
        is_pr = event.group(2) == "pull_request"
        return equals == is_pr
    if ALWAYS.fullmatch(atom):
        return True
    raise AssertionError(
        f"unmodelled atom {atom!r} in job condition {whole!r}. This guard "
        "decides whether a changed file triggers the job by EVALUATING the "
        "condition; an atom it cannot evaluate has no safe default (assuming "
        "true under-reports, assuming false reds innocent PRs). Model it "
        "here — do not widen a filter to silence this.")


def _job_step_files(workflow_path: Path, job_id: str) -> set[str]:
    """Repo-relative files a job's `run:` steps EXECUTE or INSTALL FROM.

    ⛔ FILES only, the same deliberate blind spot `_python_out_of_tree_reads`
    documents: a job that names a DIRECTORY (`pytest tests/`) raises a
    whole-subtree coverage question, and answering it mechanically would have
    this guard demand `tests/**` in the `go` filter off a `pytest tests/`
    mention — a real CI-cost decision a drift guard does not get to make.

    ⛔ `run:` only. A file referenced through `with:` (a `hashFiles()` cache
    key, an action input) is not read here: a cache key is not a correctness
    dependency, and the under-detection is the safe direction.

    ⛔ `working-directory:` IS honoured, and was not until blind review: a
    relative script argument resolves against the step's directory, not the
    repo root, so `working-directory: tools/portal` + `node ci/gen.js` was
    tested as `<root>/ci/gen.js`, found missing, and dropped — the dependency
    became invisible with every test green. Ten steps across the gated legs
    already set `working-directory`; none of them names a relative script
    TODAY, which is the only reason this was latent rather than live. The
    step directory is tried FIRST and the repo root second, which is right for
    both spellings: a genuinely relative path resolves where the shell would,
    and a root-anchored one (VAR_PREFIX / WORKSPACE_EXPR already stripped) does
    not exist under the step directory and falls through.
    """
    workflow = _load_workflow(workflow_path)
    if job_id not in workflow["jobs"]:
        raise AssertionError(
            f"job {job_id!r} no longer exists in {workflow_path.name}. If it "
            "was renamed, update the pins in this module; do not drop them.")
    found: set[str] = set()
    for step in workflow["jobs"][job_id].get("steps") or []:
        if GATE_OUTPUT.search(str(step.get("if", ""))):
            raise AssertionError(
                f"{workflow_path.name}::{job_id} has a STEP whose `if:` "
                f"consults a gate output ({step.get('if')!r}). The effective "
                "condition for that step's files is the CONJUNCTION of the "
                "job's `if:` and the step's, and this module evaluates only "
                "the job's — so a file the step runs would be checked against "
                "the wrong gate, and the job reports `success` (not `skipped`) "
                "while never executing it. Model the conjunction before using "
                "this shape.")
        work_dir = str(step.get("working-directory") or "").strip("/")
        for candidate in _run_script_files(step.get("run") or ""):
            for resolved in (f"{work_dir}/{candidate}" if work_dir else None,
                             candidate):
                if resolved and _is_tracked_file(resolved):
                    found.add(resolved)
                    break
    return found


SHELL_SEPARATORS = re.compile(r"\s(?:&&|\|\||[;|])\s|\s*[;|]\s*")


def _shell_commands(line: str):
    """Split a logical line into individual commands.

    ⛔ Scanning per LINE instead of per COMMAND is what let two narrowings leak:
    `./x` was harvested from any argument position (so
    `echo "see ./docs/foo.md"` demanded a runbook — the exact prose
    false-positive this predicate exists to avoid), and a `-c` flag was
    harvested from any line merely CONTAINING the word `pip`
    (`pip --version && tox -c reqs.txt`). Both are "which command owns this
    token" questions, and neither is answerable from the line.

    Over-splitting is safe: a repo path never spans a command separator, so the
    worst case is under-detection, this module's stated safe direction.
    """
    for part in SHELL_SEPARATORS.split(line):
        tokens = part.split()
        if tokens:
            yield tokens


def _run_script_files(script: str):
    """Yield candidate repo paths from a shell script's command arguments."""
    script = WORKSPACE_EXPR.sub("", script)
    for line in _logical_shell_lines(script):
        for tokens in _shell_commands(line):
            yield from _command_files(tokens)


# ⛔ SKIP-LISTS, so the failure directions are the opposite of a root-name
# list: over-inclusion costs nothing (skipping a token that was really a
# command loses at most that command), under-inclusion silently blinds the
# scanner. That is why inert members are tolerated here and forbidden in
# `_CONVENTIONAL_ROOT_NAMES`. ⚠️ Measured per member (remove one, re-run):
# against the three scanned workflows NOTHING here is load-bearing; against
# this module's own fixtures exactly `then`, `do`, `sudo`, `env` and `xargs`
# are. Two earlier versions of this comment got that list wrong in both
# directions — state it as a measurement or not at all.
#
# ⭐ The structure behind it is more useful than the list: only a keyword that
# IMMEDIATELY PRECEDES a command matters. `if`/`while`/`until`/`for` are
# followed by a condition or a word list, so skipping them changes nothing —
# the unrecognised head is not an interpreter either way. `then` and `do` are
# the ones a command actually follows.
#
# ⛔ And this IS an enumeration, with known gaps — none live in the three
# scanned workflows (measured). Mostly under-detection: a wrapper flag that
# takes a SEPARATE value (`sudo -u runner bash x.sh`, `xargs -n 1 bash x.sh`),
# a wrapper with a mandatory POSITIONAL argument (`timeout 600 bash x.sh` —
# which is why `timeout`/`nice`/`stdbuf` are not listed: every real spelling
# carries the argument, so listing them would help in no case at all, the same
# argument that removed the `[` branch), `source`/`.`, an interpreter flag with
# a separate value (`bash -eo pipefail x.sh`), `-m pip` anywhere but
# immediately after the interpreter, `uv pip`, `case … in` bodies, a subshell
# with no inner separator (`(bash x.sh)` — the pinned fixture uses
# `(cd x && bash x.sh)`, which `SHELL_SEPARATORS` splits first, so the plain
# form is untested), a lone `&` background operator, a nested command context
# (`bash -c "bash x.sh"`), and a variable verb (`$PYTHON x.py`).
#
# ⚠️ One gap runs the OTHER way, so "all under-detection" would be wrong: the
# flag-skipping loop takes a flag's VALUE as the script, so
# `bash --rcfile scripts/ops/x.sh -c true` yields the rcfile and loses the real
# argument. Weak in practice — a file handed to a flag is usually a real
# dependency — but it is over-detection and belongs on the list.
#
# Resolving these properly means parsing shell, which is not worth it here;
# what matters is that the list does not pretend to be complete.
ENV_PREFIXES = {"sudo", "env", "exec", "time", "xargs", "nohup", "command"}
# Shell keywords that can stand where a command does, once `_shell_commands`
# has split on `;`. A `run: |` step wrapped in `if … then … fi` is an ordinary
# refactor, and without these the wrapped command becomes invisible.
SHELL_KEYWORDS = {"if", "then", "else", "elif", "fi", "do", "done", "while",
                  "for", "until", "case", "esac", "{", "}", "(", ")", "!"}
ASSIGNMENT = re.compile(r"^\w+=")


def _command_verb(tokens: list[str]) -> tuple[int, str | None]:
    """(index, basename) of the word this command actually runs.

    ⛔ Skips everything that can legitimately precede the verb: wrappers
    (`sudo`, `env`, `nohup`, …), THEIR flags (`sudo -E bash x.sh` — four of the
    five wrappers were blind in their flagged spelling), `VAR=value`
    assignments, and shell keywords left at the head of a command by the
    `;`-split (`if [ -f x ]; then bash x.sh; fi`). Each of those was a separate
    member of the same "which token is the command" class; the class is
    enumerated here rather than discovered one review round at a time.

    ⚠️ An unrecognised head is returned AS the verb — it is not skipped, and
    `(0, None)` happens only if every token was skipped. That matters because
    `_command_files` uses `tokens[verb_index]` for the `./` branch whether or
    not the verb was recognised. The safe behaviour comes from the verb then
    failing every downstream test, not from refusing to name one.
    """
    skipping_wrapper = False
    for index, token in enumerate(tokens):
        bare = token.strip("'\"")
        if bare in SHELL_KEYWORDS or ASSIGNMENT.match(bare):
            skipping_wrapper = False
            continue
        if bare in ENV_PREFIXES:
            skipping_wrapper = True
            continue
        # a flag belonging to the wrapper we just skipped, not to the verb
        if skipping_wrapper and bare.startswith("-"):
            continue
        # ⛔ No `[` branch here. It was dead for every real spelling — after
        # `[` the next token is a test operator, which becomes the verb and is
        # neither an interpreter nor `./` — and its ONE reachable effect was
        # wrong: `[ ./x.sh ]` is a `test`, not an execution, and it made the
        # path a dependency. An inert branch whose only live behaviour is a
        # false positive is worse than no branch.
        return index, bare.rsplit("/", 1)[-1]
    return 0, None


def _command_files(tokens: list[str]):
    """Candidate paths from ONE command's tokens."""
    # ⛔ Direct execution, COMMAND POSITION ONLY. `./scripts/ops/x.sh` runs the
    # file; the same token as an argument is being passed, not run — and
    # accepting it anywhere made `echo "::error::see ./docs/foo.md"` demand a
    # runbook, the precise prose false-positive this predicate exists to avoid.
    verb_index, verb = _command_verb(tokens)

    # ⛔ The verb, not `tokens[0]` — otherwise `sudo ./x.sh` and
    # `env FOO=1 ./x.sh` miss, while the two sibling predicates below handle
    # them. Third member of the same "which token is the command" class.
    head = tokens[verb_index].strip("'\"")
    if head.startswith("./"):
        yield _normalise_token(head[2:])

    # ⛔ `pip` as a WORD, and as THIS command's word — not merely present
    # somewhere on the line. `pipefail`/`pipeline`/`pipx` satisfy a substring
    # test, and `pip --version && tox -c reqs.txt` satisfies a line-scoped one.
    #
    # ⛔ …and `python -m pip` IS pip. Keying only on the verb missed it, and
    # `ci.yml` uses that spelling (`python3 -m pip install --quiet pyyaml -c
    # requirements/ci-constraints.txt`) — the constraints file vanished from a
    # form the surrounding comment claims to handle. Of the four spellings in
    # practice (`pip`, `pip3`, `python -m pip`, `python3 -m pip`) two were
    # unhandled and one of those is already in the tree.
    is_pip = (verb is not None and PIP_COMMAND.fullmatch(verb) is not None) or (
        verb in INTERPRETERS
        and tokens[verb_index + 1:verb_index + 3] == ["-m", "pip"])

    # ⛔ An interpreter counts in COMMAND POSITION ONLY. Scanning every token
    # for one meant a message that SHOWS the fix became a dependency:
    #   echo "::error::spec drift. Run python3 scripts/tools/dx/bump_docs.py"
    # yielded `bump_docs.py`. Naming the fix command in `::error::` is this
    # repo's convention (ci.yml does it twice), so this was live, not
    # hypothetical — safe only by the accident that those two say `make`,
    # which is not an interpreter. Third instance of the same position bug in
    # this one function, after `./` and `pip`.
    if verb is not None and verb in INTERPRETERS:
        # The first NON-FLAG argument, and only that one. Flags are skipped
        # rather than stopping the scan, so `python3 -u x.py` and `bash -x
        # x.sh` still resolve; stopping at the first flag lost those silently.
        # Taking only the first non-flag argument is what keeps `bash deploy.sh
        # docs/runbook.md` from demanding the runbook — a false red whose
        # remedy would be to widen the filter. (`python3 -m pytest tests/`
        # yields `pytest`, not a tracked file.)
        for following in tokens[verb_index + 1:]:
            if following.startswith("-"):
                continue
            yield _normalise_token(following)
            break

    if not is_pip:
        return
    for index, token in enumerate(tokens):
        if token in DEPENDENCY_FLAGS and index + 1 < len(tokens):
            yield _normalise_token(tokens[index + 1])
        elif "=" in token and token.split("=", 1)[0] in DEPENDENCY_FLAGS:
            yield _normalise_token(token.split("=", 1)[1])


def _logical_shell_lines(script: str) -> list[str]:
    """Join trailing-backslash continuations, as `check_unpinned_deps` does,
    then drop comment lines.

    `pip install pyyaml \\`⏎`  -c requirements/ci-constraints.txt` is ONE
    command; scanning the physical lines separately loses the `-c` file,
    because the continuation line does not contain the word `pip`.

    Three shell rules this has to respect, because getting any of them wrong
    silently DELETES a command from the scan (and the legs that would notice
    are the ones with the fewest pins):

      * a `#` comment ends at its newline — a trailing `\\` does NOT continue
        it. Joining first and testing `#` afterwards let one prose line eat the
        real command below it.
      * an EVEN number of trailing backslashes is an escaped backslash, not a
        continuation (`echo end\\\\`).
      * whitespace AFTER the backslash means bash does not continue either, so
        strip only the line terminator before testing.
    """
    lines, buffer, joining = [], "", False
    for raw in script.splitlines():
        stripped = raw.rstrip("\r")
        is_comment = not joining and stripped.lstrip().startswith("#")
        continues = (not is_comment
                     and (len(stripped) - len(stripped.rstrip("\\"))) % 2 == 1)
        if is_comment:
            buffer, joining = "", False
            continue
        buffer += stripped[:-1] + " " if continues else stripped
        if continues:
            joining = True
            continue
        lines.append(buffer)
        buffer, joining = "", False
    if buffer:
        lines.append(buffer)
    return lines


def _normalise_token(token: str) -> str:
    # Trailing shell punctuation is not part of the path: `python3 x.py; echo`
    # tokenizes to `x.py;`, which is not a tracked file, so the dependency
    # vanished silently. `validate.yaml::go-lint` extracts exactly ONE file, so
    # one stray `;` there would empty the leg.
    return VAR_PREFIX.sub("", token.strip().strip("'\"").strip(";|&()"))


def test_gating_filter_covers_every_file_its_job_runs() -> None:
    uncovered: dict[str, list[str]] = {}
    for workflow_path in _paths_filter_workflows():
        filters = _workflow_filters(workflow_path)
        jobs = _load_workflow(workflow_path)["jobs"]
        for job_id, filter_names in _gated_jobs(workflow_path).items():
            condition = str(jobs[job_id].get("if", ""))
            missing = sorted(
                path
                for path in _job_step_files(workflow_path, job_id)
                if not _condition_triggers(condition, path, filters)
            )
            if missing:
                key = (f"{workflow_path.name} :: {job_id} "
                       f"(gated on {'/'.join(filter_names)})")
                uncovered[key] = missing
    assert not uncovered, (
        "path-gated jobs run or install from repo files that are NOT covered "
        "by the filter gating them — a PR touching only those paths skips the "
        "job (reporting `skipped`, which satisfies branch protection) even "
        "though the file decides what that job does.\n"
        "⛔ If you just GENERALISED a filter entry (e.g. an exact path to "
        "`dir/pre_*.ext`), the entry is probably fine and `_covers` simply "
        "does not model that glob shape — extend its alphabet instead of "
        "un-generalising the filter. Otherwise, add each to the job's "
        "filter:\n"
        + "\n".join(
            f"    {job}:\n" + "\n".join(f"      - {p!r}" for p in paths)
            for job, paths in sorted(uncovered.items())
        )
    )


# Workflows this half is known to scan. Pinned as an EXACT set because
# discovery is a substring test on the file's text: swap `dorny/paths-filter`
# for another action (or hoist the detect job into a reusable workflow) and
# that workflow silently leaves the guard's world, taking its legs' coverage
# assertions with it. A threshold like `>= 5` did not catch that — ci.yml
# alone yields exactly 5.
SCANNED_WORKFLOWS = {"ci.yml", "docs-ci.yaml", "validate.yaml"}

# ⛔ And the gated legs themselves, as an exact set. Pinning only the workflow
# set is not enough: a leg can stay path-gated in reality while leaving THIS
# guard's view, and the most likely way is the refactor these very workflows
# invite — moving the `if:` from the job down onto its steps, so the required
# check reports `success` instead of `skipped`. Blind review demonstrated it:
# doing that to any of six legs (including `mkdocs-build`) removed the leg from
# `_gated_jobs` with every test still green. dorny's documented
# `contains(fromJSON(...outputs.changes), 'docs')` array idiom drops out the
# same way. A leg leaving this set must be a decision, not an accident.
GATED_LEGS = {
    ("ci.yml", "python-tests-run"),
    ("ci.yml", "portal-tests-run"),
    ("ci.yml", "go-tests-threshold-exporter"),
    ("ci.yml", "go-tests-tenant-api"),
    ("ci.yml", "go-tests-am-inhibit"),
    ("docs-ci.yaml", "validate-mermaid"),
    ("docs-ci.yaml", "check-links"),
    ("docs-ci.yaml", "check-frontmatter"),
    ("docs-ci.yaml", "check-coverage"),
    ("docs-ci.yaml", "mkdocs-build"),
    ("docs-ci.yaml", "doc-line-count"),
    ("docs-ci.yaml", "drift-checks"),
    ("docs-ci.yaml", "i4-runbook-smoke-test"),
    ("validate.yaml", "go-lint"),
    ("validate.yaml", "validate-config"),
    ("validate.yaml", "version-check"),
}

# Gated legs that legitimately yield NO file, with the reason. Asserted as an
# exact set, so a newly-vacuous leg must be justified here rather than joining
# a silent majority — and a leg that STOPS being vacuous reds too, keeping this
# list from rotting into a stale excuse.
KNOWN_VACUOUS_LEGS = {
    ("ci.yml", "go-tests-am-inhibit"):
        "only `go test ./...` under working-directory: tests/alertmanager-"
        "inhibit — its dependency is directory-shaped",
    ("docs-ci.yaml", "doc-line-count"):
        "only `find docs rule-packs -name '*.md' | wc -l` — directory-shaped",
}

# One concrete path per motivating leg. Weaker than the accounting above (that
# is what actually catches a shrink), but it names the exact files this change
# was written about, so a regression says WHICH dependency went blind.
PINNED_STEP_INPUTS = {
    ("ci.yml", "python-tests-run"): "requirements/ci-constraints.txt",
    ("ci.yml", "portal-tests-run"):
        "scripts/tools/lint/check_portal_bundle_size.py",
    ("ci.yml", "go-tests-tenant-api"): "tests/contract/requirements.txt",
    ("ci.yml", "go-tests-threshold-exporter"): "scripts/ops/ci_flake_retry.py",
    ("docs-ci.yaml", "mkdocs-build"): "requirements/ci-constraints.txt",
    ("validate.yaml", "go-lint"): "scripts/ops/_verify_download.sh",
    ("validate.yaml", "version-check"): "requirements/ci-constraints.txt",
    # ⛔ At least one SCRIPT pin per workflow, not just `-c` pins. docs-ci and
    # validate were anchored solely by the constraints file, so deleting
    # `python`/`python3`/`node`/… from INTERPRETERS dropped 10 script files
    # across 7 legs in those two workflows. That WAS green when the anchors
    # were added; it is not any more (the exact-set accounting above now
    # catches it), so the pins are belt-and-braces rather than the only net —
    # kept because they name WHICH dependency went blind, which the set
    # assertions cannot. ⛔ THREE idioms feed this scanner, not two: an earlier
    # version of this very comment said "two", which is the same slip the
    # INTERPRETERS block warns about — see its ⛔ note on why a maintainer who
    # trusts that count deletes the `./script` branch.
    ("docs-ci.yaml", "check-links"): "scripts/tools/lint/check_doc_links.py",
    ("validate.yaml", "validate-config"):
        "scripts/tools/ops/generate_alertmanager_routes.py",
}

# ⛔ Filter entries this module CANNOT justify and must still not lose.
#
# Each is a file a gated leg reads INDIRECTLY — through a script it merely
# invokes — which is the boundary `_job_step_files` documents. The scanner will
# never point at them, so without this set they are entries no assertion
# protects: deletable in silence, which is exactly how a filter entry rots into
# decoration. The mutation sweep proved it — removing three of them was green.
#
# ⚠️ What this set is and is not. It PINS a measurement; it does not REPRODUCE
# one. The measurement was: replay every `python … .py` command of every gated
# leg in docs-ci.yaml and validate.yaml under an open()/read_text recorder, keep
# the tracked files no pattern covered. Re-run that when a leg's commands
# change — a NEW indirect read still arrives unannounced, and no assertion here
# will say so. (`drift-checks` was excluded: validate_all.py fans out through
# subprocess, so an in-process recorder under-reports it.)
TRACED_INDIRECT_INPUTS = {
    # check-links: the waiver list decides what that required check ENFORCES.
    ("docs-ci.yaml", "docs", ".doclinkignore"),
    # mkdocs-build: pass/fail is a two-way `comm` against this ledger, so
    # deleting a line is as fatal as adding one.
    ("docs-ci.yaml", "docs", "scripts/tools/lint/mkdocs-anchor-debt.txt"),
    ("docs-ci.yaml", "docs", "components/threshold-exporter/README.md"),
    # version-check: bump_docs.py's version and count sources. CHANGELOG.md is
    # deliberately ABSENT — the trace shows it reads docs/CHANGELOG.md, and
    # adding the root one would drag nearly every PR into three required checks.
    ("validate.yaml", "validate", "README.md"),
    ("validate.yaml", "validate", "README.en.md"),
    ("validate.yaml", "validate", "CLAUDE.md"),
    ("validate.yaml", "validate", "mkdocs.yml"),
    ("validate.yaml", "validate", "components/da-portal/README.md"),
    ("validate.yaml", "validate", "components/da-tools/README.md"),
    ("validate.yaml", "validate", "components/da-tools/app/VERSION"),
    ("validate.yaml", "validate", "components/threshold-exporter/README.md"),
    ("validate.yaml", "validate", ".github/workflows/config-diff.yaml"),
}


def test_traced_indirect_inputs_remain_covered() -> None:
    """Both ends, because either one rotting makes the entry a no-op.

    Coverage: dropping the filter entry is the regression this exists for.
    Trackedness: if the input is renamed or deleted, the entry silently stops
    naming anything and the next reader trusts a dead line.

    ⛔ Do not rename this to anything EXACTLY 40 characters long. This module
    is full of `glob`, and trufflehog's Lob detector fires on a 40-character
    identifier with `lob` nearby — the previous name
    (`…_stay_covered`) was exactly 40 and CI reported it as a VERIFIED live
    credential, blocking the PR. Nothing local catches this shape (#1364).
    """
    problems = []
    for workflow, filter_name, path in sorted(TRACED_INDIRECT_INPUTS):
        patterns = _workflow_filters(
            ROOT / ".github" / "workflows" / workflow).get(filter_name)
        if patterns is None:
            problems.append(f"{workflow}: no `{filter_name}` filter any more")
            continue
        if not _is_tracked_file(path):
            problems.append(f"{path} is no longer a tracked file")
        elif not any(_covers(p, path) for p in patterns):
            problems.append(
                f"{workflow} `{filter_name}` no longer covers {path}")
    assert not problems, (
        "a gated leg reads these INDIRECTLY, so no scanner in this module can "
        "re-derive them — they exist only because someone measured what those "
        "scripts read. Losing one restores the exact skip-as-green gap this "
        "file is about, silently:\n  " + "\n  ".join(problems))


def _all_gated_legs() -> dict[tuple[str, str], Path]:
    return {
        (path.name, job): path
        for path in _paths_filter_workflows()
        for job in _gated_jobs(path)
    }


def test_every_gated_leg_is_accounted_for() -> None:
    """Vacuous-pass protection — by COMPLETE accounting, not by sampling.

    The previous version pinned a sample and asserted a floor, and both were
    satisfied by `ci.yml` alone. Two demonstrated consequences: (a) drop the
    other two workflows out of discovery and every assertion still passed
    while eleven legs stopped being checked; (b) tightening `INTERPRETERS`
    (removing `"bash"`, a plausible precision edit) cost four legs a file and
    left two of them extracting NOTHING — including `validate.yaml::go-lint`,
    whose only extracted file is the `scripts/ops/_verify_download.sh` entry
    this module claims to enforce. (Only `python`/`python3`/`bash` carry that
    weight today; `py`/`sh`/`node`/`ruby`/`perl` currently extract nothing, so
    removing THOSE is green — they are forward-looking, not load-bearing.)

    So: assert the scanned-workflow set exactly, and assert exactly WHICH legs
    yield nothing. Every leg is then either checked or named with a reason.
    """
    assert {p.name for p in _paths_filter_workflows()} == SCANNED_WORKFLOWS, (
        "the set of workflows this guard scans changed. Discovery is a "
        "substring test for `dorny/paths-filter`, so a workflow that switches "
        "action — or moves its detect job into a reusable workflow — leaves "
        "the guard silently. Update SCANNED_WORKFLOWS *and* make sure the "
        "departing workflow's legs are still covered some other way.")

    legs = set(_all_gated_legs())
    assert legs == GATED_LEGS, (
        "the set of path-gated jobs changed.\n"
        f"  no longer seen as gated: {sorted(GATED_LEGS - legs)}\n"
        f"  newly gated:             {sorted(legs - GATED_LEGS)}\n"
        "A leg leaving this set does NOT mean it stopped being path-gated — "
        "moving the `if:` onto its steps, or switching to the "
        "`contains(fromJSON(...changes), ...)` idiom, keeps the gating and "
        "only hides it from this guard. Confirm which happened before "
        "updating GATED_LEGS.")

    vacuous = {
        leg for leg, path in _all_gated_legs().items()
        if not _job_step_files(path, leg[1])
    }
    newly_vacuous = sorted(vacuous - set(KNOWN_VACUOUS_LEGS))
    no_longer_vacuous = sorted(set(KNOWN_VACUOUS_LEGS) - vacuous)
    assert not newly_vacuous and not no_longer_vacuous, (
        "the set of gated legs this scanner extracts NOTHING from changed.\n"
        "⛔ Before adding anything to KNOWN_VACUOUS_LEGS: if the leg's `run:`\n"
        "  steps moved behind a composite action or a reusable workflow, this\n"
        "  scanner is `run:`-only BY DESIGN and cannot follow them — the leg\n"
        "  is NOT genuinely dependency-free, and its filter entry must stay.\n"
        "  Exempting it here would silently drop the very coverage this\n"
        "  module exists to hold. Prefer keeping one `run:` line that names\n"
        "  the dependency, or record the exemption WITH that caveat.\n"
        f"  newly vacuous (their coverage assertion is now empty — either the\n"
        f"  extractor lost an idiom, or justify it in KNOWN_VACUOUS_LEGS):\n"
        f"    {newly_vacuous}\n"
        f"  no longer vacuous (drop them from KNOWN_VACUOUS_LEGS):\n"
        f"    {no_longer_vacuous}")


def test_gated_job_scanner_still_sees_the_motivating_inputs() -> None:
    """Named pins for the dependencies this change is about.

    ⛔ If a step stops naming a pinned file because the invocation moved behind
    a Makefile target, the filter entry is still needed — the job still runs
    that file, one indirection away, and the scanner just cannot see it. Fix
    the pin, KEEP the filter entry. Deleting either is a silent shrink.

    Side effect worth knowing before editing `PINNED_STEP_INPUTS`: this module
    has a repo-root constant, so `_module_repo_paths`' shape B sees these
    literals and the python-half assertion then demands them in the `python`
    filter too. Harmless — it demands the right thing — but a failure there can
    name THIS file as the "reader" of a path it never opens.
    """
    legs = _all_gated_legs()
    absent = sorted(leg for leg in PINNED_STEP_INPUTS if leg not in legs)
    assert not absent, (
        f"pinned jobs no longer exist as gated legs: {absent}. If one was "
        "renamed, re-point the pin; if its gate moved onto its steps, the leg "
        "is still gated and still needs its filter entry. Do NOT drop the pin "
        "or the filter entry (see docstring).")
    missing = sorted(
        f"{workflow}::{job} -> {path}"
        for (workflow, job), path in PINNED_STEP_INPUTS.items()
        if path not in _job_step_files(legs[(workflow, job)], job)
    )
    assert not missing, (
        f"extraction stopped seeing known step inputs {missing} — fix the "
        "scanner. If a step genuinely stopped naming the file, re-point the "
        "pin; do NOT drop the corresponding filter entry (see docstring).")


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


SUFFIX_SEGMENT = re.compile(r"\*\.\w+$")


def _covers(pattern: str, path: str) -> bool:
    """Does a paths-filter pattern cover `path` (a file OR a directory)?

    Segment matcher over the CLOSED alphabet the repo's filters actually use:
    a literal, `**` (any run of segments, including none), and `*.ext`. That
    set was not guessed — every pattern in every `dorny/paths-filter` block in
    `.github/workflows/` was enumerated, and nothing else occurs. Within that
    alphabet this is complete, not a half-built glob engine.

    Anything OUTSIDE the alphabet (a bare `*`, `a*b`, `?`) returns False, i.e.
    reports the path as uncovered. That is the loud direction: a new glob shape
    surfaces as a filter-coverage failure a human must look at, rather than
    being silently mis-matched.

    ⛔ "False is loud" holds only for a POSITIVE pattern, because callers ask
    `any(_covers(p, path) for p in patterns)` — one False narrows nothing. A
    NEGATED pattern (`!x`) therefore fails the opposite way: it would be read as
    the literal segment `"!x"`, return False, and be absorbed while the positive
    patterns still say "covered", making an excluded path look included. That
    is why negation is rejected in `_workflow_filters` rather than handled here;
    do not "fix" it by teaching this function about `!`, which would still leave
    the OR unable to express an override.

    ⛔ This is the single disarm surface for every assertion in this module —
    all of them are negative ("nothing uncovered"), so an over-permissive
    `_covers` turns the whole file green and quiet.
    `test_covers_matches_only_the_shapes_in_use` pins both directions; keep it
    that way.
    """
    return _match_segments(pattern.split("/"), path.split("/"))


def _match_segments(pat: list[str], parts: list[str]) -> bool:
    if not pat:
        return not parts
    head, rest = pat[0], pat[1:]
    if head == "**":
        # `**` absorbs any number of segments, including zero — so
        # `docs/**` covers `docs` itself and `a/**/*.md` covers `a/x.md`.
        return any(_match_segments(rest, parts[i:]) for i in range(len(parts) + 1))
    if not parts:
        return False
    if SUFFIX_SEGMENT.fullmatch(head):
        suffix = head[len("*"):]
        if not (parts[0].endswith(suffix) and parts[0] != suffix):
            return False
    elif "*" in head or "?" in head:
        return False  # outside the modelled alphabet — fail loud
    elif parts[0] != head:
        return False
    return _match_segments(rest, parts[1:])


def test_covers_matches_only_the_shapes_in_use() -> None:
    """`_covers` is the disarm surface for all three assertions above.

    Every one of them is a NEGATIVE assertion ("nothing uncovered"), so a
    `_covers` that says yes too often turns the whole module green and quiet —
    the opposite of a false red, and much harder to notice. Pin both
    directions: the shapes really in the filters must match, and everything
    else must NOT (an unrecognised glob is uncovered, i.e. loud).

    ⛔ Every path here is DELIBERATELY not a real repo path. `_covers` never
    touches the filesystem, so fixtures are pure strings and fake ones assert
    exactly as much — but a real one gets harvested by this module's own
    shape-B scanner as "a file this test reads", which both pollutes
    `verify_diff_map.json` and can red the python-half assertion demanding a
    fixture path be added to the `python` filter. Keep them fictional.
    """
    assert _covers("Zfile", "Zfile")
    # ⛔ An exact-path entry covers THAT path, never its subtree — dorny
    # matches the literal only. Mutating `_match_segments`' base case to
    # `return True` (so a consumed pattern accepts any remaining segments) was
    # green: every other negative fixture here is killed by the suffix or
    # literal branch, none by the base case, so the one over-permissive
    # direction that would disarm the whole module was unpinned.
    assert not _covers("zdocs", "zdocs/index.zmd")
    assert not _covers("zreq/pins.txt", "zreq/pins.txt/deeper.txt")
    assert _covers("zreq/**", "zreq/pins.txt")
    assert _covers("zreq/**", "zreq")
    assert not _covers("zreq/**", "zreq-dev/x.txt")
    assert not _covers("zreq/**", "other/zreq/x.txt")

    # `**/`-rooted, the `go`/`python` idiom: basename match at any depth.
    assert _covers("**/zmod.txt", "zsrc/zpkg/zmod.txt")
    assert _covers("**/zmod.txt", "zmod.txt")
    assert not _covers("**/zmod.txt", "zsrc/zpkg/zsum.txt")
    assert not _covers("**/zmod.txt", "zmod.txt.bak")
    assert _covers("**/*.zpy", "zsrc/zlint/ztool.zpy")
    assert not _covers("**/*.zpy", "zreq/pins.txt")
    # A bare ".zpy" is a filename, not a file matched by `*.zpy`.
    assert not _covers("**/*.zpy", ".zpy")

    # `prefix/**/*.ext`, the shape docs-ci.yaml's filter uses. `**` must also
    # absorb ZERO segments, or `zsrc/ztop.zpy` reads as uncovered.
    assert _covers("zsrc/**/*.zpy", "zsrc/zlint/ztool.zpy")
    assert _covers("zsrc/**/*.zpy", "zsrc/ztop.zpy")
    assert not _covers("zsrc/**/*.zpy", "zsrc/zlint/ztool.zsh")
    assert not _covers("zsrc/**/*.zpy", "zother/ztool.zpy")

    # `*.ext` is a SINGLE segment: it matches a file beside the prefix, never
    # one nested below it (picomatch's `*` does not cross a separator).
    assert _covers("zdocs/*.zmd", "zdocs/index.zmd")
    assert not _covers("zdocs/*.zmd", "zdocs/zsub/index.zmd")

    # Unrecognised shapes must fail loud (report uncovered), never be guessed.
    assert not _covers("z*x/**", "zyx/a.txt")
    assert not _covers("zdocs/z?.zmd", "zdocs/za.zmd")
    assert not _covers("zdocs/*", "zdocs/index.zmd")


def test_condition_evaluator_handles_the_shapes_that_defeated_heuristics() -> None:
    """Both directions, including the two shapes that broke classification.

    A guard that decides union-vs-intersection by inspecting the condition was
    wrong twice, once too loud and once too quiet. These pin that evaluating is
    right where classifying was not — and that an unmodelled atom raises rather
    than defaulting to either answer.
    """
    filters = {"za": ["zsrc/**"], "zb": ["zdocs/**"]}
    a = "needs.d.outputs.za_changed == 'true'"
    b = "needs.d.outputs.zb_changed == 'true'"
    hit, miss = "zsrc/x.txt", "zother/x.txt"

    def triggers(cond, path):
        return _condition_triggers(cond, path, filters)

    assert triggers(a, hit) and not triggers(a, miss)
    # union: either gate is enough
    assert triggers(f"{a} || {b}", hit)
    assert triggers(f"{a} || {b}", "zdocs/y.md")
    # intersection: a file in only one filter does NOT trigger it
    assert not triggers(f"{a} && {b}", hit)
    assert triggers(f"{a} && {b}", "zsrc/x.txt") is False
    # `evt && (A || B)` — the shape the first heuristic wrongly rejected
    assert triggers(f"github.event_name != 'schedule' && ({a} || {b})", hit)
    # `(A || evt) && B` — the shape the second heuristic silently accepted.
    # The file is in `za` only, and the job runs only when `zb` matched.
    assert not triggers(f"({a} || github.event_name == 'push') && {b}", hit)
    # docs-ci's real idiom: non-PR runs unconditionally, PRs consult the gate.
    assert not triggers(f"github.event_name != 'pull_request' || {b}", hit)
    assert triggers(f"github.event_name == 'pull_request' && {b}", "zdocs/y.md")

    # `always()` survives tokenizing as ONE atom, and the `${{ }}` wrapper is
    # stripped rather than reported as unmodelled syntax.
    assert triggers(f"always() && {b}", "zdocs/y.md")
    assert not triggers(f"always() && {b}", hit)
    assert triggers("${{ " + b + " }}", "zdocs/y.md")

    # ⛔ A gate atom naming a filter that does not exist must RAISE, not be
    # treated as satisfied. It is the one atom shape that is syntactically a
    # gate, so it is the one an over-permissive edit would reach for; making it
    # return True leaves every other test green while turning the coverage
    # assertion vacuous for any leg whose condition mentions a second, unknown
    # gate (blind review).
    for unmodelled in ["success()", "needs.other.result == 'success'",
                       "github.actor != 'dependabot[bot]'",
                       "needs.d.outputs.zzz_changed == 'true'"]:
        try:
            triggers(f"{unmodelled} && {a}", hit)
        except AssertionError as exc:
            assert "unmodelled atom" in str(exc)
        else:  # pragma: no cover - the assertion above is the point
            raise AssertionError(
                f"{unmodelled!r} was silently evaluated instead of raising")


def test_run_script_extraction_rejects_the_documented_near_misses() -> None:
    """The two narrowings above are documented but were unenforced.

    Reverting `PIP_COMMAND` to a plain `"pip" in line` substring test, or
    widening `VAR_PREFIX` back to any `$VAR/`, both left every other test green
    (blind review) — so the hazards their comments describe had no teeth.
    """
    def files(script: str) -> set[str]:
        # the same two steps `_job_step_files` applies: extract, then keep only
        # what is really in the tree. Asserting on the raw candidates alone
        # would test the wrong layer — an unresolvable `$VAR/…` token IS
        # yielded, and it is `_is_tracked_file` that rejects it.
        return {c for c in _run_script_files(script) if _is_tracked_file(c)}

    # `pip` as a word, not a substring of pipefail / pipeline / pipx
    assert files("pip install pyyaml -c requirements/ci-constraints.txt") == {
        "requirements/ci-constraints.txt"}
    assert files("set -o pipefail; cp -r requirements/ci-constraints.txt /tmp/"
                 ) == set()
    assert files("pipx run tox -c requirements/ci-constraints.txt") == set()

    # only checkout-denoting variables are stripped; a scratch dir that happens
    # to hold a repo-shaped name must not become a demanded filter entry
    assert files('python3 "$GITHUB_WORKSPACE/scripts/ops/ci_flake_retry.py"'
                 ) == {"scripts/ops/ci_flake_retry.py"}
    assert files('python3 "$RUNNER_TEMP/scripts/ops/ci_flake_retry.py"') == set()

    # ⛔ an interpreter's first non-flag argument ONLY. This must be asserted
    # with a REAL interpreter: the `pytest tools/portal --ignore=…` case below
    # returns empty because `pytest` is not in INTERPRETERS at all, so it holds
    # identically with the extractor fully widened — it looks like it covers
    # this rule and does not.
    assert files("bash scripts/ops/_verify_download.sh docs/internal/dev-rules.md"
                 ) == {"scripts/ops/_verify_download.sh"}
    assert files("python3 -m pytest tests/") == set()
    assert files("bash scripts/ops/_verify_download.sh x y") == {
        "scripts/ops/_verify_download.sh"}
    # flags are SKIPPED, not treated as end-of-arguments
    assert files("bash -x scripts/ops/_verify_download.sh") == {
        "scripts/ops/_verify_download.sh"}
    # trailing shell punctuation is not part of the path.
    # ⛔ Use the SUBSHELL form: the `;` case below is split by
    # `SHELL_SEPARATORS` before `_normalise_token` ever sees it, so it passes
    # identically with the strip deleted — a pin that holds without the thing
    # it claims to pin (blind review). Parens are what the strip is for.
    assert files("(cd x && bash scripts/ops/_verify_download.sh)") == {
        "scripts/ops/_verify_download.sh"}
    assert files("bash scripts/ops/_verify_download.sh; echo done") == {
        "scripts/ops/_verify_download.sh"}

    # ⛔ The full "which token is the command" class, enumerated rather than
    # discovered one review round at a time: shell keywords left at a command's
    # head by the `;`-split, and a wrapper's OWN flags. Wrapping an existing
    # `run:` step in `if … then … fi` is an ordinary refactor, and four of the
    # five wrappers were blind in their flagged spelling.
    verifier = {"scripts/ops/_verify_download.sh"}
    assert files("if [ -f x ]; then bash scripts/ops/_verify_download.sh; fi"
                 ) == verifier
    assert files("for f in a b; do bash scripts/ops/_verify_download.sh; done"
                 ) == verifier
    assert files("sudo -E bash scripts/ops/_verify_download.sh") == verifier
    assert files("xargs -0 bash scripts/ops/_verify_download.sh") == verifier

    # `--flag=value` form of a dependency flag — live code, previously untested.
    assert files("pip install --constraint=requirements/ci-constraints.txt x"
                 ) == {"requirements/ci-constraints.txt"}
    # prose is not a dependency
    assert files('echo "::error::see docs/internal/dev-rules.md"') == set()
    # (spelled `--ignore=<a real doc>` rather than a real test path: `verify_diff`
    # indexes this module by TEXT, so naming a test file here would route every
    # edit of it to this guard.)
    assert files("pytest tools/portal --ignore=docs/internal/dev-rules.md"
                 ) == set()

    # ⛔ direct execution has no interpreter token to key on. No gated job uses
    # it today, so this is the only thing holding that branch honest — without
    # it, deleting the branch stays green (blind review) and the day a step is
    # written `./scripts/ops/x.sh` the dependency goes silently unseen.
    assert files("./scripts/ops/_verify_download.sh a b") == {
        "scripts/ops/_verify_download.sh"}
    # ⛔ …but ONLY in command position. Pinning just the line above is what let
    # the `./` branch harvest from any argument position for two rounds: prose
    # in an `::error::` string, and a path handed to another command, both
    # became "dependencies" — the exact false positive this whole predicate
    # was narrowed to avoid (blind review).
    assert files('echo "::error::see ./docs/internal/dev-rules.md"') == set()
    assert files("bash scripts/ops/_verify_download.sh ./docs/internal/dev-rules.md"
                 ) == {"scripts/ops/_verify_download.sh"}

    # ⛔ command-scoped, not line-scoped: `pip` must be THIS command's word.
    assert files("pip --version && tox -c requirements/ci-constraints.txt") == set()

    # ⛔ An INTERPRETER also only counts in command position. Naming the fix
    # command inside `::error::` is this repo's convention, so a message that
    # SHOWS the command must not become a dependency. The earlier pin used a
    # prose string with no interpreter token in it, so it held identically with
    # the extractor fully widened — it looked like it covered this and did not.
    assert files(
        'echo "::error::spec drift. Run python3 scripts/tools/dx/bump_docs.py --fix"'
    ) == set()
    assert files('echo "::error::run bash scripts/ops/_verify_download.sh"') == set()
    # …while the wrappers that legitimately precede a command still resolve —
    # for ALL THREE verb-keyed predicates, not just the two that had pins.
    # `./` kept using `tokens[0]` after its siblings moved to the resolved
    # verb, so `sudo ./x.sh` silently found nothing (blind review).
    assert files("sudo bash scripts/ops/_verify_download.sh") == {
        "scripts/ops/_verify_download.sh"}
    assert files("env FOO=1 python3 scripts/tools/dx/bump_docs.py") == {
        "scripts/tools/dx/bump_docs.py"}
    assert files("sudo ./scripts/ops/_verify_download.sh") == {
        "scripts/ops/_verify_download.sh"}
    assert files("env FOO=1 ./scripts/ops/_verify_download.sh") == {
        "scripts/ops/_verify_download.sh"}

    # ⛔ `python -m pip` IS pip. Keying only on the command verb missed it, and
    # ci.yml already spells it that way — so a constraints file vanished from a
    # form the comment above claims to handle. Four spellings exist in
    # practice; two were unhandled and one of those was live in the tree.
    assert files("python3 -m pip install --quiet pyyaml "
                 "-c requirements/ci-constraints.txt") == {
        "requirements/ci-constraints.txt"}
    assert files("python -m pip install pyyaml "
                 "-c requirements/ci-constraints.txt") == {
        "requirements/ci-constraints.txt"}
    # …but `-m <not pip>` must not inherit pip's flag handling.
    assert files("python3 -m pytest tests/ -c requirements/ci-constraints.txt"
                 ) == set()
    # …and a real command after a separator is still seen.
    assert files("echo hi; bash scripts/ops/_verify_download.sh") == {
        "scripts/ops/_verify_download.sh"}


def test_compat_root_names_are_all_load_bearing() -> None:
    """No inert entries in `_CONVENTIONAL_ROOT_NAMES`, and each one pinned.

    Anchoring the two halves of `_repo_root_names` against each other still
    left the names INSIDE the compat half unpinned: dropping `REPO_ROOT` used to
    be green in every other test (blind review). Asserting that each name is
    individually load-bearing pins all of them at once — and kills
    inert entries, which are worse than absent ones because they read like a
    second line of defence while doing nothing.

    ⚠️ Deliberate asymmetry with `INTERPRETERS`, which DOES carry inert entries
    (`py`/`sh`/`node`/`ruby`/`perl` extract nothing today) and says so. The
    failure modes differ: an unused interpreter name can only ever start
    matching more commands, whereas an unused ROOT name changes how every
    literal in a whole module is interpreted — and a wrong one silently
    reinterprets that module's paths. Forward-looking is cheap in the first and
    load-bearing in the second.
    """
    # Parse each module ONCE, then answer every name-subset question from the
    # extracted sets: this runs `len(names) + 1` times, and re-parsing the
    # whole `tests/` tree per call dominated the module's runtime.
    # ⛔ Reuses `_root_name_parts`, the same halves `_repo_root_names` combines.
    # An earlier version re-derived them inline here — and this module already
    # records what that costs (see `_module_repo_paths_with`: a duplicated body
    # drifted against its original). If the derivation gains a third idiom, a
    # hand-rolled copy would silently stop measuring the function it names.
    per_module = [(tree, *_root_name_parts(tree))
                  for _module, tree in _parsed_test_modules()]

    def paths_seen(names: set[str]) -> set[str]:
        # ⛔ ALL detections, not shape-A joins only. A recognised root name does
        # two things: it interprets joins, AND it admits the module to the scan
        # at all (`_module_repo_paths` returns early without one). Measuring
        # joins alone would call a name inert while its module's bare literals
        # depend on it entirely. The unit has to match what the gate actually
        # controls — and it changed once already when the gate did.
        seen: set[str] = set()
        for tree, derived, present in per_module:
            roots = derived | (present & names)
            if not roots:
                continue
            seen |= {p for p in _module_repo_paths_with(tree, roots)
                     if _is_tracked_file(p)}
        return seen

    baseline = paths_seen(_CONVENTIONAL_ROOT_NAMES)
    inert = sorted(
        name for name in _CONVENTIONAL_ROOT_NAMES
        if paths_seen(_CONVENTIONAL_ROOT_NAMES - {name}) == baseline
    )
    assert not inert, (
        f"these conventional root names contribute no module the `parents[N]` "
        f"derivation does not already see: {inert}. Remove them — an inert "
        "entry in a compatibility list is indistinguishable from a working "
        "one until someone relies on it.")

    # Completeness: "no inert entries" only inspects the names that ARE listed,
    # so on its own it cannot notice one going MISSING. The external reference
    # is now derivable, because a root name buys shape-A joins and nothing
    # else: a module needs this list exactly when recognising one of its
    # `__file__`-bound names would add a join the scanner does not already
    # make.
    #
    # ⭐ That framing is what makes the assertion writable at all. The earlier
    # attempt asked "does this module contribute anything", which swept in
    # `_TOOLS_DIR` — a SUBDIRECTORY handle bound to a `str` — and demanded it
    # be called a repo root, a fix that would have mis-resolved every join off
    # it. Asked about JOINS, `_TOOLS_DIR` excludes itself: a `str` can never
    # `/`-join, so recognising it adds nothing. The candidate set went 15 -> 0.
    orphaned = [
        (module, sorted(candidates))
        for module, candidates in _modules_needing_compat()
        if not candidates & _CONVENTIONAL_ROOT_NAMES
    ]
    assert not orphaned, (
        "these test modules bind a repo root by an idiom `_repo_root_names` "
        "cannot derive, and recognising it would add shape-A joins the scanner "
        "is currently missing:\n"
        + "\n".join(f"    {m}  (candidates: {c})" for m, c in orphaned)
        + "\nAdd the name to _CONVENTIONAL_ROOT_NAMES, or teach the derivation "
        "their binding idiom. ⛔ Do NOT add a name that is a subdirectory "
        "handle rather than the repo root — check what it is bound to first.")

    # HISTORY worth keeping, because these mistakes are easy to repeat:
    #
    #   * The first completeness attempt asked "does this module contribute
    #     anything", and its failure message therefore demanded `_TOOLS_DIR` —
    #     a subdirectory handle — be called a repo root. It was built, then
    #     deleted: an assertion whose message walks the maintainer into a wrong
    #     fix is worse than no assertion. Re-asking it about JOINS is what made
    #     it correct, and the candidate set went 15 -> 0.
    #
    #   * ⛔ REJECTED, with numbers, so nobody re-proposes it blind: ungating
    #     shape B (dropping `_module_repo_paths`' `if not roots` early return).
    #     It is tempting — shape B never uses the root name, so the gate is
    #     borrowed from shape A — and it measured well in isolation: roughly
    #     a third more contributing modules, with every newly detected path
    #     already covered by the `python` filter.
    #
    #     But zero new paths meant zero new SAFETY, and the cost was a live
    #     false-positive surface. Shape B has no evidence the literal is ever
    #     opened, and the modules the gate had been excluding are precisely the
    #     ones that test PATH CLASSIFIERS — `test_check_structure`,
    #     `test_check_hardcode_tenant`, `test_verify_diff`, `test_bump_docs` —
    #     i.e. modules whose path strings are INPUTS, not reads. Hundreds of
    #     tracked files sit outside the `python` filter, so those become false
    #     reds:
    #     adding a `tools/portal/entries/*.jsx` case to `check_structure`'s
    #     `ALLOWED_JSX_DIRS` test is enough to trip it. And for
    #     `test_check_structure`'s `_fake_tracked` names the documented remedy
    #     is IMPOSSIBLE — they are bound to `ALLOWED_TOOLS_ROOT`, so making
    #     them fictional breaks that test, leaving only "widen the filter",
    #     which is what the message itself warns against.
    #
    #     "Has a repo-root constant" turns out to be a decent proxy for "opens
    #     repo files", crude as it looks. A reader-call gate was measured as a
    #     replacement and separated only 1 of the 4 (the others do read
    #     fixtures while also classifying strings), so there is no cheap
    #     better gate. Under-detection is this module's stated safe direction;
    #     the blind spot stays, tracked as its own issue.


def test_is_tracked_file_is_case_exact_and_stays_inside_the_repo() -> None:
    """The case-exact walk, pinned; plus the repo-escape behaviour.

    ⛔ Honest about what pins what, and WHERE — this test does not mean the
    same thing in the three places it runs:

      * Windows host — the case assertions bite: `Docs/index.md` resolves, so
        deleting the per-segment `iterdir()` walk reds them.
      * Dev container — they bite HERE TOO, and not for the reason you would
        guess. Its repo mount is 9p from `C:\\` and inherits NTFS
        case-insensitivity (measured: `ls Docs/index.md` succeeds inside the
        container, while the container's own `/tmp` is case-sensitive). ⚠️ So
        "verified in the dev container" is NOT evidence about case-sensitive
        behaviour — the container is not a faithful oracle for it.
      * Real CI (ubuntu-latest, ext4) — `Docs/index.md` does not exist, so the
        case assertions pass TRIVIALLY. The walk is genuinely load-bearing
        there (it is what makes the scanner agree across platforms) but
        nothing in CI pins it. Accepted: the two environments a human actually
        edits in do pin it.

    The absolute/`..` rejection is NOT pinned by any of them, anywhere: the
    walk already rejects those (`"/etc/hosts".split("/")` starts with an empty
    segment; `..` is never an `iterdir()` entry). It is kept as defence in
    depth — it avoids `stat()`ing a live system path on POSIX, where
    `ROOT / "/etc/hosts"` IS `/etc/hosts` — but the assertions below verify the
    OUTCOME, not that particular guard.
    """
    assert _is_tracked_file("docs/index.md")
    # wrong case: real on a case-insensitive filesystem, absent on Linux
    assert not _is_tracked_file("Docs/index.md")
    assert not _is_tracked_file("docs/INDEX.md")
    # never escape the repo: `ROOT / "/tmp/x"` is `/tmp/x` on POSIX
    assert not _is_tracked_file("/etc/hosts")
    assert not _is_tracked_file("docs/../docs/index.md")
    assert not _is_tracked_file("../CHANGELOG.md")


def test_condition_parser_refuses_leftovers_and_unbalanced_parens() -> None:
    """The parser's two structural fail-loud paths, neither otherwise reached.

    `test_condition_evaluator_…` covers unmodelled ATOMS; a condition that
    parses a valid prefix and then leaves tokens behind, or that never closes a
    paren, takes different branches — and dropping either raise left every
    test green (blind review), silently evaluating only the prefix.
    """
    filters = {"za": ["zsrc/**"]}
    gate = "needs.d.outputs.za_changed == 'true'"
    for broken in [f"{gate} )", f"({gate}", f"({gate} && ({gate})"]:
        try:
            _condition_triggers(broken, "zsrc/x.txt", filters)
        except AssertionError as exc:
            assert "parse" in str(exc) or "parenthes" in str(exc), str(exc)
        else:  # pragma: no cover - the assertion above is the point
            raise AssertionError(f"{broken!r} was accepted instead of raising")


def test_logical_shell_lines_follows_the_three_shell_rules() -> None:
    """The joiner is currently invisible to the integration path — every
    continuation in the scanned workflows happens to carry no dependency — so
    without this its three documented rules have zero coverage, and a wrong
    edit fails by silently deleting a command from the scan."""
    bs = "\\"

    def tokens(script):
        # what the consumer actually sees; exact run-lengths of the joining
        # whitespace are not part of the contract.
        return [line.split() for line in _logical_shell_lines(script)]

    # continuation joins, and the joined line is scanned as one command
    assert tokens(f"pip install pyyaml {bs}\n  -c reqs/pins.txt\n") == [
        ["pip", "install", "pyyaml", "-c", "reqs/pins.txt"]]
    # a comment ends at its newline: a trailing backslash must NOT eat the
    # command below it
    assert tokens(f"# note {bs}\nbash scripts/x.sh\n") == [
        ["bash", "scripts/x.sh"]]
    # an EVEN number of trailing backslashes is an escaped backslash
    assert tokens(f"echo end{bs}{bs}\nbash scripts/x.sh\n") == [
        ["echo", f"end{bs}{bs}"], ["bash", "scripts/x.sh"]]
    # whitespace after the backslash means bash does not continue either
    assert tokens(f"echo a {bs}  \nbash scripts/x.sh\n") == [
        ["echo", "a", bs], ["bash", "scripts/x.sh"]]
    # CRLF is handled like LF
    assert tokens(f"pip install a {bs}\r\n  -c reqs/pins.txt\r\n") == [
        ["pip", "install", "a", "-c", "reqs/pins.txt"]]


def test_modules_needing_compat_detects_a_synthetic_orphan() -> None:
    """The completeness helper, pinned on a constructed case.

    Its real-tree result is `[]` — correct, and therefore useless as evidence
    that it works: replacing the whole helper with `return []` was green (blind
    review). So drive it with a module the repo does not contain: a root bound
    by an idiom the `parents[N]` derivation cannot see, whose recognition WOULD
    add a shape-A join.
    """
    real = "docs/index.md"
    parent, name = real.split("/")

    orphan = ast.parse(
        f'ZROOT = os.path.dirname(os.path.dirname(__file__))\n'
        f'P = ZROOT / "{parent}" / "{name}"\n')
    assert _shape_a_paths(orphan, {"ZROOT"}) == {real}, (
        "the synthetic orphan stopped yielding a join — this fixture no longer "
        "exercises what it claims to")
    assert not _repo_root_names(orphan), (
        "the derivation now sees `os.path.dirname(...)`; if that is deliberate, "
        "this fixture is obsolete — but check `_CONVENTIONAL_ROOT_NAMES` first")
    assert _modules_needing_compat([("zsynthetic.py", orphan)]) == [
        ("zsynthetic.py", {"ZROOT"})], (
        "the orphan detector did not report a module that binds its root by an "
        "underivable idiom and would gain a join from it — the completeness "
        "half of test_compat_root_names_are_all_load_bearing is now blind")

    # …and a name that is a subdirectory handle must NOT qualify, which is the
    # property that made this assertion writable at all.
    subdir = ast.parse(
        f'ZTOOLS = os.path.join(os.path.dirname(__file__), "scripts")\n'
        f'P = "{real}"\n')
    assert _shape_a_paths(subdir, {"ZTOOLS"}) == set(), (
        "a subdirectory handle produced a shape-A join — the completeness "
        "check would start demanding non-root names be called repo roots")


def test_looks_like_a_path_bounds_are_reference_pinned() -> None:
    """Shape B's only throttle, pinned by reference rather than by effect.

    Each bound could be deleted with every other test still green (blind
    review) — not because they do nothing (they drop hundreds of candidates
    each; the figures live at `_MAX_PATH_LEN` and are deliberately stated ONCE)
    but because what they drop is not a tracked file anyway, so nothing
    downstream notices. That makes them exactly the kind of quiet safety a
    refactor removes by accident.

    ⛔ Do not restate those counts here. Three review rounds in a row caught a
    stale measured number in this file, and every one of them was a SECOND
    copy of a figure stated correctly elsewhere. One site, or none.
    """
    assert _looks_like_a_path("docs/index.md")
    # length: `tests/dx/**` embeds whole programs as string literals
    assert not _looks_like_a_path("a/" + "x" * _MAX_PATH_LEN)
    # prefix: absolute paths, URLs, and relative `./` are not repo-relative
    assert not _looks_like_a_path("/etc/hosts")
    assert not _looks_like_a_path("https://example.com/a/b")
    assert not _looks_like_a_path("./docs/index.md")
    # whitespace: prose and multi-line fixtures are not paths
    assert not _looks_like_a_path("see docs/index.md for details")
    assert not _looks_like_a_path("docs/index.md\nmore text")
    # and the one bound that IS pinned by effect, kept here for completeness
    assert not _looks_like_a_path("index.md")

    # A NUL byte must be judged "not a path", never crash the scan. `tests/**`
    # already holds three such literals (none with a `/`, so none reaches the
    # filesystem today). Review raised this as a ValueError risk needing its
    # own `except`; measured on both platforms, `Path.is_file()` returns False
    # rather than raising (it has swallowed ValueError since 3.8), so no extra handler was added — an
    # `except` clause that can never fire reads like a second line of defence
    # while being none. This pins the OUTCOME, which is what matters if that
    # behaviour ever changes back.
    assert not _is_tracked_file("docs/\0index.md")

    # `_is_parents_subscript` must require the `parents` attribute, not just
    # any subscript: `X = SOMETHING[2]` is not a repo root, and treating it as
    # one would reinterpret every literal in that module. Adds no paths today,
    # which is exactly why it needs a reference case.
    assert _is_parents_subscript(
        ast.parse("Path(__file__).resolve().parents[2]", mode="eval").body)
    assert not _is_parents_subscript(
        ast.parse("SOMETHING[2]", mode="eval").body)
    assert not _is_parents_subscript(
        ast.parse("Path(__file__).resolve().parent[2]", mode="eval").body)


def test_shape_b_requires_the_module_to_have_a_repo_root() -> None:
    """The gate on shape B, pinned with synthetic modules.

    ⛔ This gate looks removable — shape B never uses the root name, so the
    precondition is borrowed from shape A. It was removed once, measured
    (a third more contributing modules, zero new filter gaps), and reverted: the modules it
    excludes are the ones that TEST PATH CLASSIFIERS, whose path strings are
    inputs rather than reads, and a large slice of the tree sits outside the
    `python` filter for those to turn into false reds. See the rejection note in
    `test_compat_root_names_are_all_load_bearing` for the full numbers.

    Nothing pinned it before, so removing it again was green (blind review).
    Synthetic fixtures, because the property is about a module SHAPE and no
    real module can demonstrate the negative case without also being a live
    read.
    """
    real = "docs/index.md"
    assert _is_tracked_file(real), "fixture path must exist for this to mean anything"

    rootless = ast.parse(f'X = 1\nFOO = "{real}"\n')
    assert _module_repo_paths(rootless) == set(), (
        "shape B extracted from a module with no repo-root constant — the "
        "gate that keeps path-classifier tests (whose path strings are INPUTS, "
        "not reads) out of the scanner is gone. It was removed deliberately "
        "once and reverted; do not remove it again without re-reading why.")

    with_root = ast.parse(
        f'REPO_ROOT = Path(__file__).resolve().parents[2]\nFOO = "{real}"\n')
    assert real in _module_repo_paths(with_root), (
        "shape B stopped extracting even WITH a root present — the gate is now "
        "rejecting everything, which passes the assertion above vacuously.")


def test_filter_blocks_refuse_to_overwrite_a_name() -> None:
    """The last fail-loud path that had no pin.

    Every sibling refusal in this module got a synthetic test; this one did
    not, because its caller needs a workflow on disk. Deleting the clash check
    was green (blind review), so a second `dorny/paths-filter` step could
    silently take over a filter name and `_filter_patterns("python")` would
    quietly mean something else.
    """
    merged: dict[str, list[str]] = {}
    _merge_filter_block(merged, {"zalpha": ["a/**"]}, "zwf.yaml")
    _merge_filter_block(merged, {"zbeta": ["b/**"]}, "zwf.yaml")
    assert merged == {"zalpha": ["a/**"], "zbeta": ["b/**"]}

    try:
        _merge_filter_block(merged, {"zalpha": ["c/**"]}, "zwf.yaml")
    except AssertionError as exc:
        assert "zalpha" in str(exc) and "more than one" in str(exc)
    else:  # pragma: no cover - the assertion above is the point
        raise AssertionError("a redefined filter name was merged silently")


def _synthetic_workflow(tmp_path: Path, name: str, jobs: str, *,
                        filters: tuple[str, ...] = ('python:', '  - "docs/**"'),
                        detect_with: str = "") -> Path:
    """A minimal dorny-gated workflow ON DISK.

    On disk because `_workflow_filters` / `_gated_jobs` / `_job_step_files` all
    take a Path — the same constraint that forced
    `test_filter_blocks_refuse_to_overwrite_a_name` to exercise the merge helper
    directly. ⛔ A fresh `name` per call: both loaders are `lru_cache`d by path,
    so reusing one within a test returns the previous content.
    """
    text = (
        "name: synthetic\n"
        "on: [pull_request]\n"
        "jobs:\n"
        "  detect-changes:\n"
        "    runs-on: ubuntu-latest\n"
        "    outputs:\n"
        "      python_changed: ${{ steps.filter.outputs.python }}\n"
        "    steps:\n"
        "      - id: filter\n"
        "        uses: dorny/paths-filter@v3\n"
        "        with:\n"
        + detect_with
        + "          filters: |\n"
        + "".join(f"            {line}\n" for line in filters)
        + jobs
    )
    path = tmp_path / name
    path.write_text(text, encoding="utf-8", newline="")
    return path


GATED_LEG_YAML = (
    "  leg:\n"
    "    needs: [detect-changes]\n"
    "    if: needs.detect-changes.outputs.python_changed == 'true'\n"
    "    runs-on: ubuntu-latest\n"
    "    steps:\n"
    "      - run: python3 scripts/tools/lint/check_doc_links.py --ci\n"
)


def test_gate_idiom_that_cannot_be_parsed_is_refused(tmp_path: Path) -> None:
    """A leg gated by dorny's array idiom must not vanish quietly.

    `GATE_OUTPUT` matches `outputs.<name>_changed`. dorny's own documented
    alternative — `contains(fromJSON(needs.detect.outputs.changes), 'python')` —
    matches nothing, so before this the job simply was not a leg: no coverage
    assertion, no exact-set failure, all green. Blind review built it.
    """
    wf = _synthetic_workflow(
        tmp_path, "array-idiom.yml",
        "  leg:\n"
        "    needs: [detect-changes]\n"
        "    if: contains(fromJSON(needs.detect-changes.outputs.changes), 'python')\n"
        "    runs-on: ubuntu-latest\n"
        "    steps:\n"
        "      - run: echo hi\n")
    try:
        _gated_jobs(wf)
    except AssertionError as exc:
        assert "does not parse" in str(exc)
    else:  # pragma: no cover - the raise is the point
        raise AssertionError("an unparsed gate idiom produced no leg, quietly")


def test_a_needs_dependency_propagates_the_gate(tmp_path: Path) -> None:
    """`needs:` a skipped job and you are skipped — so you are gated too.

    Without `always()` this is GitHub's documented behaviour, and the workflows
    here demonstrate it: every aggregate gate writes `if: always()` for exactly
    this reason. A job that omits it is path-gated with no `if:` of its own.
    """
    downstream = ("  report:\n"
                  "    needs: [leg]\n"
                  "    runs-on: ubuntu-latest\n"
                  "    steps:\n"
                  "      - run: echo hi\n")
    inherited = _gated_jobs(_synthetic_workflow(
        tmp_path, "needs-gate.yml", GATED_LEG_YAML + downstream))
    assert inherited.get("report") == ["python"], inherited

    opted_out = _gated_jobs(_synthetic_workflow(
        tmp_path, "needs-always.yml",
        GATED_LEG_YAML + downstream.replace(
            "    needs: [leg]\n", "    needs: [leg]\n    if: always()\n")))
    assert "report" not in opted_out, opted_out


def test_a_step_level_gate_condition_is_refused(tmp_path: Path) -> None:
    """The effective gate becomes a conjunction this module does not evaluate.

    Worse than `skipped`: the JOB reports success while the step never ran, so
    the missing coverage leaves no trace at all in the checks list.
    """
    wf = _synthetic_workflow(
        tmp_path, "step-gate.yml",
        GATED_LEG_YAML
        + "      - if: needs.detect-changes.outputs.python_changed == 'true'\n"
          "        run: bash scripts/ops/_verify_download.sh a b\n")
    try:
        _job_step_files(wf, "leg")
    except AssertionError as exc:
        assert "CONJUNCTION" in str(exc)
    else:  # pragma: no cover - the raise is the point
        raise AssertionError("a step-level gate was evaluated as if absent")


def test_working_directory_resolves_step_relative_scripts(tmp_path: Path) -> None:
    """Resolve where the shell would, then fall back to the repo root."""
    relative = _synthetic_workflow(
        tmp_path, "wd.yml",
        "  leg:\n"
        "    needs: [detect-changes]\n"
        "    if: needs.detect-changes.outputs.python_changed == 'true'\n"
        "    runs-on: ubuntu-latest\n"
        "    steps:\n"
        "      - working-directory: scripts\n"
        "        run: python3 tools/lint/check_doc_links.py --ci\n")
    assert _job_step_files(relative, "leg") == {
        "scripts/tools/lint/check_doc_links.py"}

    # Root-anchored spellings must still land at the root: nothing exists under
    # `<work_dir>/scripts/...`, so the fallback is what makes both work.
    anchored = _synthetic_workflow(
        tmp_path, "wd-anchored.yml",
        "  leg:\n"
        "    needs: [detect-changes]\n"
        "    if: needs.detect-changes.outputs.python_changed == 'true'\n"
        "    runs-on: ubuntu-latest\n"
        "    steps:\n"
        "      - working-directory: components/tenant-api\n"
        '        run: python3 "$GITHUB_WORKSPACE/scripts/ops/ci_flake_retry.py"\n')
    assert _job_step_files(anchored, "leg") == {"scripts/ops/ci_flake_retry.py"}


def test_filters_refuse_exclusions_and_a_predicate_quantifier(
        tmp_path: Path) -> None:
    """Both change what "matched" means; neither is modelled by `any()`."""
    excluding = _synthetic_workflow(
        tmp_path, "excl.yml", GATED_LEG_YAML,
        filters=('python:', '  - "docs/**"', '  - "!docs/internal/**"'))
    try:
        _workflow_filters(excluding)
    except AssertionError as exc:
        assert "exclusion" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("an exclusion pattern was silently absorbed")

    quantified = _synthetic_workflow(
        tmp_path, "quant.yml", GATED_LEG_YAML,
        detect_with="          predicate-quantifier: every\n")
    try:
        _workflow_filters(quantified)
    except AssertionError as exc:
        assert "predicate-quantifier" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("a non-default quantifier was ignored")


def _stray_globs(globs: list[str], scanned: str) -> list[str]:
    """Include globs that collect tests from outside the scanned directory."""
    return sorted(g for g in globs if not g.startswith(f"{scanned}/"))


def test_scanned_portal_tree_matches_the_vitest_include() -> None:
    """`PORTAL_TESTS` must still be where the Vitests actually are.

    The portal half scans one hard-coded directory. Vitest decides the real
    answer, and the two agree today only by coincidence of editing. Widen the
    include to `src/**/*.test.*` and every Vitest added there reads out-of-tree
    files this guard never looks at — silently, because a scanner that finds
    nothing new looks exactly like a tree with nothing new in it.
    """
    match = VITEST_INCLUDE.search(VITEST_CONFIG.read_text(encoding="utf-8"))
    assert match, (
        f"no `include:` array in {VITEST_CONFIG.name} — either it moved or the "
        "config gained a form this pin does not read. Re-point both.")
    globs = re.findall(r"['\"]([^'\"]+)['\"]", match.group(1))
    assert globs, f"empty `include:` in {VITEST_CONFIG.name}"
    scanned = PORTAL_TESTS.relative_to(VITEST_CONFIG.parent).as_posix()

    # ⛔ Both directions, because the real config has NO stray glob: asserting
    # only on it left `_stray_globs` free to `return []` with this test green —
    # a case that cannot distinguish the implementation from a stub is a test
    # design bug, not coverage (testing-playbook §v2.10.0 ⑤; mutation sweep).
    assert _stray_globs([f"{scanned}/**/*.test.ts"], scanned) == []
    assert _stray_globs(["src/**/*.test.ts"], scanned) == ["src/**/*.test.ts"]

    stray = _stray_globs(globs, scanned)
    assert not stray, (
        f"{VITEST_CONFIG.name} collects Vitests from outside "
        f"{scanned!r}: {stray}. `PORTAL_TESTS` scans only {scanned!r}, so those "
        "tests' out-of-tree reads are invisible to this guard. Widen "
        "PORTAL_TESTS (and the `python` filter entry that re-runs this guard) "
        "to match, or narrow the include.")


def test_workspace_expression_is_stripped_before_tokenising() -> None:
    """`${{ github.workspace }}/x` contains SPACES, so it must go first.

    Tokenising it first yields `${{` as the script argument and the real path
    is never a candidate — the failure is total, not partial.
    """
    found = set(_run_script_files(
        'python3 "${{ github.workspace }}/scripts/ops/ci_flake_retry.py" --'))
    assert "scripts/ops/ci_flake_retry.py" in found, found


def test_div_literals_refuses_joins_with_a_non_literal_segment() -> None:
    """`ROOT / var / "x.md"` must yield NOTHING, not `"x.md"`.

    Dropping the `left and right` requirement in `_flatten_div` makes the
    variable segment silently vanish, so an unrelated file at the repo root
    would be demanded in the `python` filter — over-detection, whose failure
    message ("Add each to the filter") widens a filter for a path nothing
    reads. Unpinned until now (blind review).
    """
    roots = {"ROOT"}
    assert _div_literals(ast.parse('ROOT / "a" / "b.md"', mode="eval").body,
                         roots) == ["a", "b.md"]
    assert _div_literals(ast.parse('ROOT / var / "b.md"', mode="eval").body,
                         roots) == []
    assert _div_literals(ast.parse('OTHER / "a" / "b.md"', mode="eval").body,
                         roots) == []


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
    `python` filter matched. The failure it exists to catch (a PR adding a
    portal Vitest with a new out-of-tree read) touches ONLY the portal test
    tree. If `tools/portal/tests/**` is not in the `python` filter, such a PR
    skips Python Tests, this guard never runs, and the missing `portal` entry
    merges: the guard committing the exact omission it guards against.
    Caught by CodeRabbit on PR #1229 — the first version had this hole.

    ⛔ Keep `own_inputs` in step with what the module ACTUALLY reads. It began
    as ci.yml + the portal tests; it now scans every discovered paths-filter
    workflow and the whole `tests/` tree, and the list did not follow (blind
    review). All are covered today, so this was a documentation drift rather
    than a hole — but the one test whose job is to turn the invariant on the
    guard has to describe the guard.
    """
    python_patterns = _filter_patterns("python")
    own_inputs = [
        PORTAL_TESTS.relative_to(ROOT).as_posix(),
        PY_TEST_ROOT,
        # ⛔ Not a file this module opens — the file that DECIDES what
        # `PORTAL_TESTS` means. Second blind-review miss of the same shape as
        # the docstring above records: "what the module reads" is the obvious
        # list and the wrong one; the invariant needs everything that changes
        # what the module WOULD read. It was uncovered by `python` (matching
        # only `portal`), so widening the Vitest include was a PR this guard
        # never ran on.
        VITEST_CONFIG.relative_to(ROOT).as_posix(),
        *(p.relative_to(ROOT).as_posix() for p in _paths_filter_workflows()),
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


# Same exact-set accounting as the python half, for the two other invariants.
# ⛔ This is the formulation that scales, and it took three attempts to see
# that: per-shape anchors miss a namespace going dark, per-namespace anchors
# are a second enumeration to forget, and both were tried here first. What
# actually matters is never "did this one path survive" but "can the scanner
# still point at a reason for each filter entry it used to justify".
PORTAL_ENTRIES_THIS_SCANNER_JUSTIFIES = {
    "tools/portal/**", "docs/assets/platform-data.json",
    "docs/assets/template-data.json", "docs/schemas/tenant-config.schema.json",
    "rule-packs/threshold-registry.yaml", "rule-packs/ALERT-REFERENCE.md",
    "components/tenant-api/internal/rbac/testdata/wizard/**",
}
GATED_ENTRIES_THIS_SCANNER_JUSTIFIES = {
    # ⛔ Keyed by (FILTER, pattern), and note which filter each falls under —
    # `ci_flake_retry.py` and `tests/contract/**` justify `go`, not `python`.
    # ci.yml's `python` entries `**/*.py` and `tests/**` are absent because
    # nothing `python-tests-run` itself executes or installs from matches them;
    # both are amply justified by the OTHER two scanners. This set is only
    # about what the gated-job half can vouch for, per filter.
    "ci.yml": {
        ("go", "scripts/ops/ci_flake_retry.py"),
        ("go", "tests/contract/**"),
        ("portal", "scripts/tools/lint/check_portal_bundle_size.py"),
        ("python", "requirements/**"),
        ("python", "scripts/**"),
    },
    "docs-ci.yaml": {
        ("docs", "requirements/**"),
        ("docs", "scripts/tools/**/*.py"),
        ("docs", "scripts/tools/lint/**/*.sh"),
    },
    "validate.yaml": {
        ("validate", "requirements/**"),
        ("validate", "scripts/ops/_verify_download.sh"),
        ("validate", "scripts/tools/**"),
    },
}


def test_portal_scanner_still_justifies_every_filter_entry_it_used_to() -> None:
    """The portal half's namespace axis — previously anchors only.

    Dropping `docs` from `REPO_RELATIVE_LITERAL`'s enumerated alphabet left
    every test green while `docs/assets/template-data.json` lost its only
    justification (blind review). That entry is one of the narrow two-ended
    gates this module exists to protect, and the alphabet is an enumeration —
    so the axis it can go blind along is exactly the one anchors do not cover.
    """
    reads = set(_out_of_tree_reads())
    justified = {
        pattern for pattern in _portal_filter_patterns()
        if any(_covers(pattern, path) for path in reads)
    }
    lost = sorted(PORTAL_ENTRIES_THIS_SCANNER_JUSTIFIES - justified)
    gained = sorted(justified - PORTAL_ENTRIES_THIS_SCANNER_JUSTIFIES)
    assert not lost and not gained, (
        "the set of `portal` filter entries this scanner can point at a real "
        f"Vitest read for changed.\n  no longer justified: {lost}\n"
        f"  newly justified:     {gained}\n"
        "⛔ A LOST entry usually means the scanner went blind to that part of "
        "the tree — check `REPO_RELATIVE_LITERAL`'s alphabet and the shape-A "
        "regex before concluding the filter entry is unnecessary, and do NOT "
        "delete the entry to make this pass.")


def test_gated_scanner_still_justifies_every_filter_entry_it_used_to() -> None:
    """Same accounting for the gated-job half.

    Pinning legs and vacuous-legs as exact sets still let individual (leg,
    file) pairs vanish: of 28 extracted pairs only 9 were pinned, so 19 could
    disappear with everything green (blind review). Pinning all 28 would be a
    third enumeration; asking "does each filter entry still have a reason"
    survives legs being renamed and steps being rewritten.

    ⛔ Honest residue: this reduces the losable pairs from 19 to 17 of 28, not
    to zero — any pair whose pattern is co-justified by a sibling can still go
    silently, and that includes `mkdocs-build → mkdocs_strict_check.sh`, the
    leg this whole change cites as the only pre-merge doc build. A realistic
    loss vector is a step rewritten into one of the documented `_command_verb`
    gaps. What this assertion guarantees is that no filter entry loses its LAST
    reason — not that every extraction survives.
    """
    lost: dict[str, list[str]] = {}
    gained: dict[str, list[str]] = {}
    for workflow_path in _paths_filter_workflows():
        filters = _workflow_filters(workflow_path)
        # ⛔ Per FILTER, not one union over the whole workflow. Unioned, an
        # entry counted as justified by a file that only some OTHER filter's
        # legs run — ci.yml's `python` entries `**/*.py` and `tests/**` were
        # both "justified" by files belonging to the go and portal legs. The
        # assertion stayed literally true while meaning something weaker than
        # it reads (blind review).
        # ⛔ A leg gated `go || docs` credits its files to BOTH names. That is
        # correct for `||`: either filter matching is enough to run the leg, so
        # either one is a real reason the file reaches CI.
        #
        # ⚠️ An earlier version filtered each file against the target filter
        # here, described as "correctness for the multi-name case". That was
        # wrong twice over: the restriction cannot change the outcome for ANY
        # input — `justified` below only admits a pattern together with a
        # witness it covers, so a file the filter does not cover was never a
        # witness — and the case it named is not the residue it would address.
        # Removed rather than kept as decoration.
        files_by_filter: dict[str, set[str]] = {name: set() for name in filters}
        for job, names in _gated_jobs(workflow_path).items():
            step_files = _job_step_files(workflow_path, job)
            for name in names:
                files_by_filter[name] |= step_files
        # ⛔ Keyed by (filter, pattern). The same pattern STRING appears in two
        # filters of one workflow five times in ci.yml (`rule-packs/**`,
        # `try-local/**`, `flaky-tests.yaml`, `.github/workflows/ci.yml`,
        # `docs/**`), and a bare-string set would call it justified when either
        # filter's legs matched — reintroducing, one level down, the exact
        # cross-filter credit this rescope removed.
        justified = {
            (name, pattern)
            for name in filters for pattern in filters[name]
            if any(_covers(pattern, path) for path in files_by_filter[name])
        }
        expected = GATED_ENTRIES_THIS_SCANNER_JUSTIFIES[workflow_path.name]
        if expected - justified:
            lost[workflow_path.name] = sorted(expected - justified)
        if justified - expected:
            gained[workflow_path.name] = sorted(justified - expected)
    assert not lost and not gained, (
        "the set of filter entries the gated-job scanner can point at a real "
        f"executed/installed file for changed.\n  no longer justified: {lost}\n"
        f"  newly justified:     {gained}\n"
        "⛔ A LOST entry usually means the extractor stopped recognising an "
        "invocation shape — check `_run_script_files` before concluding the "
        "filter entry is unnecessary, and do NOT delete the entry.")


def test_guard_actually_sees_the_known_two_ended_gates() -> None:
    """Vacuous-pass protection.

    A regex that silently stops matching would make the gate above pass on an
    empty set forever — the exact fail-quiet mode this guard exists to kill.
    Pin the inputs we KNOW are read today (one per extraction shape's home
    gate), so a broken parser fails here instead of going quietly green.
    """
    reads = _out_of_tree_reads()
    expected = {
        # shape A (CLIMBING_LITERAL)
        "docs/assets/platform-data.json",
        "docs/schemas/tenant-config.schema.json",
        "components/tenant-api/internal/rbac/testdata/wizard",
        # ⛔ shape B (ROOT_CONST + READER + REPO_RELATIVE_LITERAL) had NO pin,
        # so it could go blind while these three shape-A paths kept the test
        # green — six of the nine detections, including the two rule-pack
        # drift gates, vanish if any of its three conditions is narrowed
        # (blind review). One anchor per shape, same rule as the python half.
        "rule-packs/threshold-registry.yaml",
    }
    missing = sorted(e for e in expected if e not in reads)
    assert not missing, (
        "extraction stopped seeing known out-of-tree test inputs "
        f"{missing} — the coverage assertion above is now vacuous. Fix the "
        "parser, or update this pin if the test genuinely stopped reading them."
    )
