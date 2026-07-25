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
