"""actionlint pin parity — the linter that judges OUR workflows must be the
linter that judges the workflows we SHIP (#1347).

Two independent copies of "which actionlint" now exist:

  * ``.pre-commit-config.yaml`` pins the upstream ``rhysd/actionlint`` hook by
    ``rev:``. That hook only ever sees ``^\\.github/workflows/`` — the repo's own
    workflow tree.
  * ``.github/workflows/ci.yml`` installs a pinned CLI into the
    ``python-tests-run`` job, where ``tests/ops/test_generated_ci_artifacts.py``
    runs it over the workflow ``da-tools init`` generates for CUSTOMER repos and
    over the portal wizard's preview copy.

Nothing bound them. actionlint's rule set moves between releases (new checks,
tightened expression typing), so a skew means the artifacts we ship to customers
are judged by a different — possibly weaker — engine than the one gating this
repo, while both checks stay green. Worse, the skew is invisible: neither file
mentions the other.

Same shape as ``tests/shared/test_vector_pin_parity.py``; separate file rather
than an extension of it because that one is a Vector-specific narrative and its
helpers are wired to Vector's four pins.

Pure file parsing — no actionlint, no network — so it runs in the standard
Python Tests job rather than only on binary-gated runs.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest
import yaml

_REPO = Path(__file__).resolve().parents[2]
_CI_YML = _REPO / ".github" / "workflows" / "ci.yml"
_PRE_COMMIT = _REPO / ".pre-commit-config.yaml"
# ⛔ ONE literal string: scripts/tools/dx/verify_diff.py maps source→test by
# scanning for literal path strings (the #1313 lesson).
_GENERATED_CI_TEST = _REPO / "tests/ops/test_generated_ci_artifacts.py"

_ACTIONLINT_REPO = "https://github.com/rhysd/actionlint"


def _one(pattern: str, text: str, what: str) -> str:
    matches = re.findall(pattern, text, re.MULTILINE)
    assert len(matches) == 1, (
        f"expected exactly one {what} match for /{pattern}/, found "
        f"{len(matches)}: {matches}"
    )
    return matches[0]


def _hook_entry() -> dict:
    """The rhysd/actionlint repo entry, read via YAML parse (not grep).

    ``.pre-commit-config.yaml`` is structured data with ~100 repo entries; a
    text match for ``rev:`` is ambiguous by construction.
    """
    cfg = yaml.safe_load(_PRE_COMMIT.read_text(encoding="utf-8"))
    entries = [r for r in cfg["repos"]
               if str(r.get("repo", "")).rstrip("/") == _ACTIONLINT_REPO]
    assert len(entries) == 1, (
        f"expected exactly one {_ACTIONLINT_REPO} entry in "
        f".pre-commit-config.yaml, found {len(entries)}"
    )
    return entries[0]


def _ci_version() -> str:
    return _one(r"""^\s*ACTIONLINT_VERSION=["']?([^\s"'#]+)""",
                _CI_YML.read_text(encoding="utf-8"), "ci.yml ACTIONLINT_VERSION")


_BINARY = "actionlint"


def _which_callables(tree: ast.AST) -> tuple[set[str], set[str]]:
    """(module aliases whose ``.which`` is shutil's, bare names bound to it).

    Read from the file's own imports rather than assumed to be ``shutil.which``:
    ``from shutil import which as _w`` is the same call spelled differently, and
    a gate that knows only one spelling is a gate with a documented workaround.
    """
    modules: set[str] = set()
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(a.asname or a.name for a in node.names if a.name == "shutil")
        elif isinstance(node, ast.ImportFrom) and node.module == "shutil":
            names.update(a.asname or a.name for a in node.names if a.name == "which")
    return modules, names


def _is_which_call(node, modules: set[str], names: set[str]) -> bool:
    """``shutil.which(_BINARY)``, in any import spelling."""
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    resolves = (
        (isinstance(func, ast.Attribute) and func.attr == "which"
         and isinstance(func.value, ast.Name) and func.value.id in modules)
        or (isinstance(func, ast.Name) and func.id in names)
    )
    return bool(
        resolves and node.args
        and isinstance(node.args[0], ast.Constant)
        and node.args[0].value == _BINARY
    )


def _actionlint_bindings(tree: ast.AST) -> set[str]:
    """Every name bound to a ``which("actionlint")`` lookup.

    ⛔ The SUBJECT is the binary, not a chosen identifier and not a chosen
    statement type. Two earlier forms of this helper were beaten, both by
    anchoring on the wrong thing:

      * keying on the literal ``"_ACTIONLINT"`` classified *that name*, so a
        second binding invoked with no flags stayed invisible;
      * replacing it with "exactly one ``ast.Assign`` of ``shutil.which``" still
        enumerated a spelling. Adversarial review named four ways past it, and
        the three that matter share one property — they reach the binary
        WITHOUT producing the syntax being counted: an inline
        ``shutil.which("actionlint")`` sitting directly in an argv list, an
        aliased import, and a bare ``"actionlint"`` string trusting PATH. (The
        fourth, ``AnnAssign``, only ever failed closed.) All three were silent
        whenever a normal binding also existed, because that one alone satisfied
        the exactly-one count.

    So the anchor is the string ``"actionlint"`` itself: no route to running the
    binary exists without naming it as a constant somewhere in the file, and
    ``_actionlint_invocations`` requires EVERY such constant to sit in a shape it
    recognises. Several bindings are fine now — each one's loads get
    classified, so an alias produces a named red line instead of a blind spot.
    """
    modules, names = _which_callables(tree)
    bound: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            targets = list(node.targets)
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets = [node.target]
        else:
            continue
        if _is_which_call(node.value, modules, names):
            bound.update(t.id for t in targets if isinstance(t, ast.Name))
    return bound


def _actionlint_invocations() -> tuple[list[tuple[int, set, str]], list[str]]:
    """The real generated-artifact gate, classified.

    Thin wrapper so the classifier itself can be exercised against synthetic
    sources — the bypass shapes below must be provable without editing the file
    the gate actually protects.
    """
    return _classify_actionlint_uses(_GENERATED_CI_TEST.read_text(encoding="utf-8"))


def _classify_actionlint_uses(src: str) -> tuple[list[tuple[int, set, str]], list[str]]:
    """Classify every route to the actionlint binary in a source file.

    Returns ``(invocations, unclassified)``. Each invocation is
    ``(lineno, string_args, source_text)`` for an argv list whose FIRST element
    resolves to the binary — a bound name, an inline ``which("actionlint")``,
    or the bare string. Only *string constant* elements go into ``string_args``:
    a flag passed via a variable is not a literal we can compare, so it counts as
    absent (fail-closed); ``source_text`` is carried so the failure message can
    show the real argv instead of a list of Nones.

    ``unclassified`` is the fail-closed half, and it covers TWO populations now:
    every occurrence of the constant ``"actionlint"`` and every load of a bound
    name that this function did not account for. Anything the classifier cannot
    read becomes a named red line rather than dropping out of the sample.

    ⛔ Parsed, not grepped. The counted thing is the CALL SITE; text search
    counts the file's own English. Same rule as
    [[feedback_count_structured_via_parse_not_grep]].
    """
    tree = ast.parse(src, filename=str(_GENERATED_CI_TEST))
    modules, which_names = _which_callables(tree)
    bindings = _actionlint_bindings(tree)

    invocations: list[tuple[int, set, str]] = []
    classified: set[int] = set()

    def _account_for(node) -> None:
        """Mark a node and everything under it as read by this classifier."""
        for inner in ast.walk(node):
            classified.add(id(inner))

    def _is_binary(node) -> bool:
        return (
            (isinstance(node, ast.Name) and node.id in bindings)
            or (isinstance(node, ast.Constant) and node.value == _BINARY)
            or _is_which_call(node, modules, which_names)
        )

    for node in ast.walk(tree):
        # (a) the binding statement itself is a recognised shape
        if isinstance(node, (ast.Assign, ast.AnnAssign)) and _is_which_call(
            getattr(node, "value", None), modules, which_names
        ):
            _account_for(node.value)
        # (b) argv list literal whose head resolves to the binary
        elif isinstance(node, ast.List) and node.elts and _is_binary(node.elts[0]):
            invocations.append((
                node.lineno,
                {e.value for e in node.elts
                 if isinstance(e, ast.Constant) and isinstance(e.value, str)},
                ast.get_source_segment(src, node) or "<unavailable>",
            ))
            _account_for(node.elts[0])
        # (c) presence probe: `<binary> is None` / `is not None`
        elif isinstance(node, ast.Compare) and all(
            isinstance(op, (ast.Is, ast.IsNot)) for op in node.ops
        ):
            for operand in (node.left, *node.comparators):
                if _is_binary(operand):
                    _account_for(operand)

    unclassified = [
        f"line {n.lineno}: "
        + (f"name {n.id}" if isinstance(n, ast.Name) else f'constant "{_BINARY}"')
        for n in ast.walk(tree)
        if id(n) not in classified
        and (
            (isinstance(n, ast.Constant) and n.value == _BINARY)
            or (isinstance(n, ast.Name) and n.id in bindings
                and isinstance(n.ctx, ast.Load))
        )
    ]
    return invocations, sorted(unclassified)


def test_files_exist() -> None:
    for p in (_CI_YML, _PRE_COMMIT, _GENERATED_CI_TEST):
        assert p.is_file(), f"missing {p}"


def test_ci_install_matches_the_pre_commit_rev() -> None:
    """⛔ The load-bearing one."""
    rev = _hook_entry()["rev"]
    assert rev.startswith("v"), (
        f"actionlint hook rev {rev!r} is not a v-prefixed tag — this gate "
        f"compares it to ci.yml's bare ACTIONLINT_VERSION"
    )
    assert rev.lstrip("v") == _ci_version(), (
        f"actionlint VERSION skew: .pre-commit-config.yaml pins {rev!r} but "
        f"ci.yml installs {_ci_version()!r}. The repo's own workflows and the "
        f"workflows `da-tools init` ships to customers would then be judged by "
        f"different rule sets. Bump BOTH together (and refresh the SHA-256 and "
        f"the download-cache key in ci.yml)."
    )


def test_ci_install_pins_a_sha256() -> None:
    """The install must be checksum-verified, like the Vector / mtail siblings."""
    ci = _CI_YML.read_text(encoding="utf-8")
    # _one() already proves there is exactly one 64-hex ACTIONLINT_SHA256.
    _one(r"^\s*ACTIONLINT_SHA256=([0-9a-f]{64})", ci, "ci.yml ACTIONLINT_SHA256")
    # ⛔ Anchored to the start of a line, like the three checks above it. This
    # one was a whole-file substring test, and the file it searches is full of
    # prose: measured, replacing the real `bash scripts/ops/_verify_download.sh
    # …` line with a `# TODO(...)` comment containing the same literal left this
    # gate at 10 passed while an unverified binary went onto the runner. Same
    # class as the prose-satisfies-a-behaviour-check bug fixed in the sibling
    # guard — a class fixed in one file has to be swept in the others, and this
    # is the sweep.
    _one(
        r"""^\s*bash\s+scripts/ops/_verify_download\.sh\s+"\$DL/actionlint-\$\{ACTIONLINT_VERSION\}\.tgz\"""",
        ci,
        "ci.yml actionlint checksum-verification command",
    )


def test_download_cache_key_carries_the_pinned_version() -> None:
    """A stale cache key exact-hits the old entry and the new tarball never gets
    cached (ci.yml says so in its own comment about the Vector/mtail pins)."""
    ci = _CI_YML.read_text(encoding="utf-8")
    key = _one(r"^\s*key:\s*(\$\{\{ runner\.os \}\}-vibe-dl-pytests-\S+)", ci,
               "ci.yml python-tests download-cache key")
    assert f"-actionlint-{_ci_version()}" in key, (
        f"download-cache key {key!r} does not carry the pinned actionlint "
        f"version {_ci_version()!r}"
    )


def test_engine_args_match_the_pre_commit_hook() -> None:
    """The generated-artifact gate must invoke actionlint with the SAME optional-
    integration pinning the repo uses on itself.

    ``-shellcheck=`` / ``-pyflakes=`` turn actionlint's "run it if it happens to
    be installed" integrations OFF. Leaving them implicit in one place and
    explicit in the other means the two callers can report different findings on
    the same YAML depending on what else the machine has installed — exactly the
    host-green/CI-red drift .pre-commit-config.yaml's own comment warns about.

    ⛔ Asserted by PARSING the call sites, not by searching the source text for
    the flag. The substring form this replaced (``f'"{arg}"' in test_src``) is
    not per-call-site and cannot be: it asks whether the characters appear
    ANYWHERE in the file. Two measurements, both taken here:

    * Strip the flags from ONE of the two actionlint invocations → the
      substring form stays GREEN (the other invocation still carries the
      text), while this AST form fails and names the offending line.
    * Strip BOTH and leave a single comment mentioning ``"-shellcheck=",
      "-pyflakes="`` → substring form GREEN with **zero** flagged invocations
      left in the file; AST form still red.

    (With both stripped and no comment left behind, the substring form does go
    red today — but only because the quoted spelling happens to appear nowhere
    else in that file right now. One sentence of prose written with straight
    quotes silently restores the tautology, which is why the shape, not the
    current text, is the problem.)
    """
    hook = next(h for h in _hook_entry()["hooks"] if h["id"] == "actionlint")
    hook_args = set(hook.get("args", []))
    assert {"-shellcheck=", "-pyflakes="} <= hook_args, (
        f"the actionlint pre-commit hook no longer pins its optional "
        f"integrations off (args={sorted(hook_args)}); update this gate and "
        f"tests/ops/test_generated_ci_artifacts.py together"
    )

    invocations, unclassified = _actionlint_invocations()
    # ⛔ The classifier's own failure mode gets an assertion, because the worst
    # outcome is not misjudging a call site — it is not treating one as subject
    # to the check at all. Every mention of the BINARY (the string "actionlint",
    # and every load of a name bound to it) must be an invocation, a presence
    # probe, or the binding itself; anything else reds here instead of vanishing
    # from the sample.
    assert not unclassified, (
        f"tests/ops/test_generated_ci_artifacts.py reaches the actionlint binary "
        f"in {len(unclassified)} way(s) this parity gate cannot classify:\n  "
        + "\n  ".join(unclassified)
        + "\nEither it is a new way of invoking actionlint — write it as a list "
        "literal whose first element is the binary, so the flags below are "
        "checkable — or extend the classifier. Do NOT delete this assertion: an "
        "unclassified call site is one that silently drops out of the argument "
        "check."
    )
    assert invocations, (
        "no actionlint invocation found in "
        "tests/ops/test_generated_ci_artifacts.py — the check below would pass "
        "vacuously (see the anti-vacuity reasoning in that file's matrix test)"
    )

    # ⛔ EQUALITY against the hook's own flags, in BOTH directions — not "are the
    # two flags present". Two holes lived in the subset form, and the second is
    # the one that matters:
    #   * missing flag → caught by either form;
    #   * EXTRA flag → caught by neither. Measured: appending `-ignore '.*'` to
    #     both call sites makes actionlint exit 0 on a workflow it otherwise
    #     rejects (`unexpected key "apply" for "workflow" section`), and this
    #     gate stayed at 10 passed. The engine is then nominally the same
    #     version and effectively disarmed — precisely the "two callers, two
    #     verdicts" drift this file exists to prevent.
    # Deriving the expected set from `hook_args` closes the reverse direction
    # too: a flag added to the pre-commit hook and not to the CI caller now reds
    # here instead of silently re-opening the skew.
    expected_flags = {a for a in hook_args if a.startswith("-")}
    for lineno, args, source in sorted(invocations):
        flags = {a for a in args if a.startswith("-")}
        assert flags == expected_flags, (
            f"tests/ops/test_generated_ci_artifacts.py line {lineno} invokes "
            f"actionlint with flags {sorted(flags)}, but the pre-commit hook "
            f"passes {sorted(expected_flags)}.\n"
            f"  missing here: {sorted(expected_flags - flags)}\n"
            f"  extra here:   {sorted(flags - expected_flags)}\n"
            "The same workflow must not pass one caller and fail the other. An "
            "EXTRA flag is not harmless — `-ignore` disarms the engine while "
            "leaving the version pin intact. If a flag genuinely belongs on "
            "both sides, add it to .pre-commit-config.yaml too.\n"
            f"  argv: {source}"
        )


# ── the classifier's own counter-examples ───────────────────────────────────
#
# Every entry runs actionlint WITHOUT the pinned flags while a normal, correct
# binding also exists — that pairing is the point. Each one was silent against
# the previous "exactly one ast.Assign of shutil.which" form, because the normal
# binding alone satisfied its count. A guard for the class has to catch them
# whether or not the well-behaved spelling is also present.
_NORMAL_BINDING = (
    "import shutil\n"
    "_ACTIONLINT = shutil.which('actionlint')\n"
    "def good(p):\n"
    "    return _run([_ACTIONLINT, '-shellcheck=', '-pyflakes=', str(p)])\n"
)

_BYPASSES = {
    "inline which() in argv": "def sneak(p):\n    return _run([shutil.which('actionlint'), str(p)])\n",
    "aliased import": (
        "from shutil import which as _w\n"
        "_AL2 = _w('actionlint')\n"
        "def sneak(p):\n"
        "    return _run([_AL2, str(p)])\n"
    ),
    "bare string trusting PATH": "def sneak(p):\n    return _run(['actionlint', str(p)])\n",
    "AnnAssign binding": (
        "_AL3: str | None = shutil.which('actionlint')\n"
        "def sneak(p):\n"
        "    return _run([_AL3, str(p)])\n"
    ),
}


def test_the_bypass_corpus_is_not_empty() -> None:
    """⛔ Anti-vacuity floor — an empty parametrize is a SKIP that exits 0.

    This corpus is the only thing proving the classifier sees every route to the
    binary. Measured: emptying it (and the sibling `_UNSUPPORTED_IFS`) left the
    run green with two silent skips.
    """
    assert len(_BYPASSES) >= 4, (
        f"only {len(_BYPASSES)} bypass shapes remain — this corpus is the "
        "classifier's only safety proof. Do not shrink it to make a change pass."
    )


@pytest.mark.parametrize("shape", sorted(_BYPASSES))
def test_classifier_sees_every_route_to_the_binary(shape: str) -> None:
    """⛔ Counter-example half — an unflagged invocation must never be invisible.

    Invisible means BOTH lists miss it: not reported as an invocation (so its
    argv is never compared against the hook's flags) and not reported as
    unclassified (so nothing reds). That combination is the failure this whole
    helper exists to prevent, and it is what all four shapes below produced
    before the anchor moved to the binary itself.
    """
    invocations, unclassified = _classify_actionlint_uses(
        _NORMAL_BINDING + _BYPASSES[shape]
    )
    flagless = [
        (lineno, source)
        for lineno, args, source in invocations
        if {"-shellcheck=", "-pyflakes="} - args
    ]
    assert flagless or unclassified, (
        f"{shape!r} reached the actionlint binary invisibly: it produced no "
        f"flagless invocation and no unclassified entry, so the argument check "
        f"below would never have looked at it.\n"
        f"  invocations: {invocations}\n"
        f"  unclassified: {unclassified}"
    )


def test_classifier_still_accepts_the_correct_shape() -> None:
    """Paired positive: the well-behaved spelling must classify cleanly.

    Without this, "catch every bypass" is satisfiable by flagging everything —
    which reds the gate permanently and teaches the next reader to delete it.
    """
    invocations, unclassified = _classify_actionlint_uses(_NORMAL_BINDING)
    assert not unclassified, f"the correct shape was flagged: {unclassified}"
    assert len(invocations) == 1, f"expected one invocation, got {invocations}"
    assert {"-shellcheck=", "-pyflakes="} <= invocations[0][1]

    # …and a presence probe is still a recognised, silent shape.
    probe = _NORMAL_BINDING + "def skip_if_absent():\n    return _ACTIONLINT is None\n"
    _, unclassified = _classify_actionlint_uses(probe)
    assert not unclassified, f"a presence probe was flagged: {unclassified}"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
