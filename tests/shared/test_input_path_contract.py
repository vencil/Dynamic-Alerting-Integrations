#!/usr/bin/env python3
"""Supplied-but-unusable input paths must be EXIT_CALLER_ERROR (#1556).

dev-rules #13 files "檔案/路徑不存在" and "malformed 輸入" under
``EXIT_CALLER_ERROR`` (2). The gate this file adds is narrower than that rule
and deliberately so: it pins the one failure *shape* that #1556 found shipping
to customers —

    omitting a flag  and  supplying a value the tool cannot use
    were collapsed into the same branch, so the second silently became the first.

Measured before the fix (each with a working control):

===================================  ===========  =========================
invocation                            rc (before)  what the operator saw
===================================  ===========  =========================
validate-config --policy <not-file>   0            ``[PASS] policy — No
                                                   policy file — skipped``
validate-config --rule-packs <bad>    0            ``[PASS] custom_rules``
generate-routes --policy <not-file>   0            routes generated with the
                                                   SSRF domain check off
lint --policy <not-file> --ci         0            linted against the
                                                   built-in policy instead
===================================  ===========  =========================

The documented example is what makes this ship: ``docs/cli-reference.md:1881``
tells the operator to run ``--policy "webhook.company.com,slack.com"``. That is
a domain list, not a path, so every customer who copied it read ``[PASS]``
while the webhook allowlist never ran.

⚠️ Scope, stated so nobody reads more into a green run than it earns: this
gate covers the four (tool, flag) pairs below plus the structural scan in
``TestNoCollapsedSuppliedVsAbsent``. It does NOT establish that every
path-shaped argument in the CLI is fail-closed — that would need a classifier
for "which arguments name an input", and a wrong classifier here is worse than
none (it would read as a rule while being false for the arguments it missed).
"""
from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
OPS = REPO_ROOT / "scripts" / "tools" / "ops"
TOOLS = REPO_ROOT / "scripts" / "tools"

EXIT_CALLER_ERROR = 2
_TIMEOUT = 120

# A value that is syntactically fine and semantically unusable. Deliberately
# the shape the docs teach (a comma-separated domain list) for --policy, so a
# regression reproduces the customer's exact invocation rather than a
# synthetic "/nonexistent" that nobody would ever type.
_DOC_POLICY_VALUE = "webhook.company.com,slack.com"
_MISSING = "Z:/definitely/not/here.yaml"


def _run(script: str, argv: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-s", str(OPS / script), *argv],
        capture_output=True, timeout=_TIMEOUT,
    )


@pytest.fixture(scope="module")
def conf_d(tmp_path_factory) -> Path:
    """Smallest tenant tree the tools accept, so the controls mean something."""
    d = tmp_path_factory.mktemp("conf.d")
    (d / "db-a.yaml").write_text(
        "thresholds:\n"
        "  mysql_connections:\n"
        "    warning: 100\n"
        "    critical: 200\n",
        encoding="utf-8",
    )
    return d


# (id, script, base argv builder, offending argv fragment)
_CASES = [
    ("validate-config --policy",
     "validate_config.py",
     lambda c: ["--config-dir", str(c)],
     ["--policy", _DOC_POLICY_VALUE]),
    ("validate-config --policy missing-file",
     "validate_config.py",
     lambda c: ["--config-dir", str(c)],
     ["--policy", _MISSING]),
    ("validate-config --rule-packs",
     "validate_config.py",
     lambda c: ["--config-dir", str(c)],
     ["--rule-packs", _MISSING]),
    ("generate-routes --policy",
     "generate_alertmanager_routes.py",
     lambda c: ["--config-dir", str(c)],
     ["--policy", _DOC_POLICY_VALUE]),
    ("lint --policy",
     "lint_custom_rules.py",
     lambda c: [str(c), "--ci"],
     ["--policy", _MISSING]),
]


# Which per-check rows must carry the failure. `validate-config --policy`
# reaches TWO independent loaders (check_routes and check_policy); both are
# named so neither can quietly revert behind the other's exit code.
_EXPECTED_FAIL_ROWS = {
    "validate-config --policy": ("routes", "policy"),
    "validate-config --policy missing-file": ("routes", "policy"),
    "validate-config --rule-packs": ("custom_rules",),
}


@pytest.mark.parametrize("case_id,script,base,bad",
                         _CASES, ids=[c[0] for c in _CASES])
class TestSuppliedButUnusableIsCallerError:

    def test_control_valid_invocation_is_not_caller_error(
            self, case_id, script, base, bad, conf_d):
        """Without the offending flag the tool must RUN.

        ⛔ Without this the contract assertion is satisfiable by a tool that
        exits 2 on everything — including one broken by an unrelated change.
        """
        r = _run(script, base(conf_d))
        assert r.returncode != EXIT_CALLER_ERROR, (
            f"{case_id}: the control invocation already exits 2, so the "
            f"contract assertion below proves nothing.\n"
            f"stderr: {r.stderr.decode('utf-8', 'replace')[:400]}")

    def test_control_bad_flag_reaches_argparse(
            self, case_id, script, base, bad, conf_d):
        """A flag nobody declares must exit 2 — proves argparse is reached."""
        r = _run(script, [*base(conf_d), "--zznever-a-real-flag"])
        assert r.returncode == EXIT_CALLER_ERROR, (
            f"{case_id}: an undeclared flag did not exit 2, so this harness "
            f"is not reaching the tool's parser at all.")

    def test_unusable_value_exits_caller_error(
            self, case_id, script, base, bad, conf_d):
        r = _run(script, [*base(conf_d), *bad])
        assert r.returncode == EXIT_CALLER_ERROR, (
            f"{case_id}: supplying an unusable value exited "
            f"{r.returncode}, not {EXIT_CALLER_ERROR}.\n"
            f"⛔ This is the #1556 shape: the tool treated 'you gave me "
            f"something I cannot use' as 'you gave me nothing', and the check "
            f"the value was meant to switch ON silently did not run.\n"
            f"⛔ Do not fix this by removing the case — the check it guards "
            f"is a webhook-domain allowlist and a custom-rule lint.\n"
            f"stdout: {r.stdout.decode('utf-8', 'replace')[:300]}")

    def test_message_names_the_flag(self, case_id, script, base, bad, conf_d):
        """The diagnostic has to say which flag, or the operator cannot act."""
        r = _run(script, [*base(conf_d), *bad])
        out = (r.stdout + r.stderr).decode("utf-8", "replace")
        assert bad[0] in out, (
            f"{case_id}: exit 2 is right but the output never mentions "
            f"{bad[0]}, so the operator has to guess which argument.")

    def test_the_named_check_row_is_the_one_that_fails(
            self, case_id, script, base, bad, conf_d):
        """Exit code alone cannot say WHICH check caught it.

        ⛔ Measured: reverting `check_policy` to its #1556 behaviour left this
        module 24/24 green, because `check_routes` loads the same policy and
        was still failing — one aggregate rc, two independent producers, so
        each producer's assertion was individually vacuous. This asserts the
        row by name.
        """
        expected_rows = _EXPECTED_FAIL_ROWS.get(case_id)
        if expected_rows is None:
            pytest.skip("tool does not emit per-check rows")
        out = (r := _run(script, [*base(conf_d), *bad])).stdout.decode(
            "utf-8", "replace")
        missing = [row for row in expected_rows if f"[FAIL] {row}" not in out]
        assert not missing, (
            f"{case_id}: rc={r.returncode} but check row(s) {missing} did not "
            f"report FAIL. A sibling check failing for the same reason keeps "
            f"the exit code right while this row silently goes back to PASS.\n"
            f"stdout: {out[:600]}")


# ---------------------------------------------------------------------------
# "Not a file" was only one third of the class (CodeRabbit, round 1 on #1592).
# ---------------------------------------------------------------------------
class TestUnusableIsMoreThanMissing:
    """A file that exists but cannot be decoded or parsed is the same error.

    ⛔ Measured before these landed, each with a control: a malformed policy
    YAML and a non-UTF-8 policy file both escaped as an uncaught traceback with
    rc=1 — from the three tools whose entire subject in this PR is that this
    class must be exit 2. Fixing "not a file" and stopping there is the
    cited-instance-instead-of-the-class mistake, made inside the fix for it.
    """

    @pytest.fixture(scope="class")
    def policies(self, tmp_path_factory):
        d = tmp_path_factory.mktemp("policies")
        (d / "malformed.yaml").write_text("allowed_domains: [\n  - broken\n",
                                          encoding="utf-8")
        (d / "nonutf8.yaml").write_bytes(
            bytes([0xFF, 0xFE, 0x00]) + b"allowed_domains" + bytes([0x0A]))
        (d / "scalar.yaml").write_text("just-a-string\n", encoding="utf-8")
        (d / "good.yaml").write_text(
            'allowed_domains:\n  - "*.example.com"\n', encoding="utf-8")
        return {p.stem: p for p in d.iterdir()}

    @pytest.mark.parametrize("script,base", [
        ("validate_config.py", lambda c: ["--config-dir", str(c)]),
        ("generate_alertmanager_routes.py", lambda c: ["--config-dir", str(c)]),
        ("lint_custom_rules.py", lambda c: [str(c), "--ci"]),
    ], ids=["validate-config", "generate-routes", "lint"])
    @pytest.mark.parametrize("kind", ["malformed", "nonutf8", "scalar"])
    def test_unreadable_or_unparseable_policy_is_caller_error(
            self, script, base, kind, policies, conf_d):
        r = _run(script, [*base(conf_d), "--policy", str(policies[kind])])
        assert r.returncode == EXIT_CALLER_ERROR, (
            f"{script} with a {kind} --policy exited {r.returncode}. "
            f"dev-rules #13 files 'malformed 輸入' under EXIT_CALLER_ERROR "
            f"next to '檔案/路徑不存在'; nothing may distinguish them.\n"
            f"stderr: {r.stderr.decode('utf-8', 'replace')[-400:]}")
        assert b"Traceback" not in r.stderr, (
            f"{script} reached exit {r.returncode} via an uncaught exception. "
            f"A traceback is not a diagnostic — it names a Python frame, not "
            f"the argument the operator got wrong.")

    @pytest.mark.parametrize("script,base", [
        ("validate_config.py", lambda c: ["--config-dir", str(c)]),
        ("generate_alertmanager_routes.py", lambda c: ["--config-dir", str(c)]),
        ("lint_custom_rules.py", lambda c: [str(c), "--ci"]),
    ], ids=["validate-config", "generate-routes", "lint"])
    def test_control_a_well_formed_policy_still_runs(
            self, script, base, policies, conf_d):
        """Without this, the assertion above is satisfiable by a tool that
        exits 2 on every --policy value it is handed."""
        r = _run(script, [*base(conf_d), "--policy", str(policies["good"])])
        assert r.returncode != EXIT_CALLER_ERROR, (
            f"{script} rejects a valid policy file, so the checks above prove "
            f"nothing.\nstderr: {r.stderr.decode('utf-8', 'replace')[:400]}")


class TestArgvErrorIsNotDownstreamOfTheConfigTree:
    """An unreadable tenant file must not downgrade an argv error to exit 1.

    ⛔ Measured: with one non-UTF-8 file in conf.d/, `--policy <missing>` exited
    1 instead of 2. validate_config downgrades a caller error when the failing
    check reads a tree that had unreadable files — right for a check that could
    not do its job because of the tree, wrong for one that never got that far
    because a path in argv was unusable.
    """

    @pytest.fixture(scope="class")
    def dirty_tree(self, tmp_path_factory):
        d = tmp_path_factory.mktemp("dirty-conf.d")
        (d / "db-a.yaml").write_text(
            "thresholds:\n  mysql_connections:\n    warning: 100\n"
            "    critical: 200\n", encoding="utf-8")
        (d / "db-bad.yaml").write_bytes(
            bytes([0xFF, 0xFE, 0x00]) + b"broken" + bytes([0x0A]))
        return d

    def test_control_unreadable_config_alone_is_a_finding(self, dirty_tree):
        """Exit 1 here is correct and must stay — the fix must not widen."""
        r = _run("validate_config.py", ["--config-dir", str(dirty_tree)])
        assert r.returncode == 1, (
            f"an unreadable tenant file alone exited {r.returncode}; that is a "
            f"finding about the customer's tree, not a caller error.")

    def test_control_clean_tree_plus_bad_policy_is_caller_error(self, conf_d):
        r = _run("validate_config.py",
                 ["--config-dir", str(conf_d), "--policy", _MISSING])
        assert r.returncode == EXIT_CALLER_ERROR

    @pytest.mark.parametrize("flag", ["--policy", "--rule-packs"])
    def test_argv_error_survives_an_unreadable_tenant_file(
            self, flag, dirty_tree):
        r = _run("validate_config.py",
                 ["--config-dir", str(dirty_tree), flag, _MISSING])
        assert r.returncode == EXIT_CALLER_ERROR, (
            f"{flag} was unusable but the run exited {r.returncode}. An "
            f"unrelated unreadable tenant file silently re-attributed an argv "
            f"mistake to the customer's config tree.")


# ---------------------------------------------------------------------------
# Structural scan: the defect shape itself, not the four instances above.
# ---------------------------------------------------------------------------
_FS_PREDICATES = ("is_file", "is_dir", "exists", "isfile", "isdir")
_BENIGN_RETURNS = {"[]", "{}", "None", "False", "''", '""', "()",
                   "set()", "list()", "dict()"}

# ⛔ Known and deliberately unchanged. `_lib_io.read_onboard_hints` collapses
# the two cases, but its ONE call site compensates
# (`scaffold_tenant.run_from_onboard`, which exits 2 on a falsy return — the
# `# #452` comment there shows that was deliberate). Measured:
# `scaffold --from-onboard <missing>` already exits 2. It stays on this list
# rather than being "fixed" because changing a shared helper with a working
# caller buys nothing and risks its round-trip tests; the call-site count is
# asserted below so a SECOND caller cannot inherit the collapse silently.
_KNOWN_COLLAPSED = {("scripts/tools/_lib_io.py", "read_onboard_hints")}


def _enclosing_function(tree: ast.AST, lineno: int) -> str:
    best = None
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            end = getattr(node, "end_lineno", node.lineno)
            if node.lineno <= lineno <= end:
                if best is None or node.lineno > best[0]:
                    best = (node.lineno, node.name)
    return best[1] if best else "<module>"


def _scanned_files() -> list[str]:
    """The scan surface, exposed so it can be asserted separately.

    ⛔ Measured: with only the `_KNOWN_COLLAPSED` witness, swapping `rglob`
    for `glob` SURVIVED the whole battery — because the witness happens to sit
    at the top level of scripts/tools/, so a change that stops visiting
    ops/, dx/ and lint/ entirely does not move it. An anti-vacuity floor
    anchored on something that cannot move measures the wrong thing.
    """
    return [p.relative_to(REPO_ROOT).as_posix() for p in TOOLS.rglob("*.py")]


def _collapsed_sites(files: list[str] | None = None) -> set[tuple[str, str]]:
    """Every `if not X or <filesystem predicate>: return <benign>`.

    Derived from the STRUCTURE of the defect (two distinct outcomes OR-ed into
    one branch), not from a list of path-ish argument names — a name list would
    have to grow every time somebody adds a flag, and would miss the first one
    it did not anticipate.

    ``files`` is injectable so the detector can be pointed at planted defects.
    ⛔ Without that, "the repo is clean" is the only assertion available, and it
    is satisfied by a detector that has stopped detecting: measured, narrowing
    the operand match back to bare `ast.Name` left all 44 tests green.
    """
    found: set[tuple[str, str]] = set()
    for rel in (files if files is not None else _scanned_files()):
        path = Path(rel) if Path(rel).is_absolute() else REPO_ROOT / rel
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (OSError, SyntaxError):
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.If) or not isinstance(node.test, ast.BoolOp):
                continue
            if not isinstance(node.test.op, ast.Or):
                continue
            operands = node.test.values
            # ⛔ Name AND Attribute. Restricting to bare names made the
            # detector blind to `if not args.policy or not
            # os.path.isfile(args.policy)` — which is how this defect is
            # actually written, because argparse hands values back as
            # attributes on a Namespace. The scan reported clean for the
            # single most likely shape of the thing it exists to find.
            absent = any(
                isinstance(v, ast.UnaryOp) and isinstance(v.op, ast.Not)
                and isinstance(v.operand, (ast.Name, ast.Attribute))
                for v in operands)
            unusable = any(
                any(p in ast.unparse(v) for p in _FS_PREDICATES) for v in operands)
            if not (absent and unusable) or len(node.body) != 1:
                continue
            stmt = node.body[0]
            if not isinstance(stmt, ast.Return):
                continue
            value = ast.unparse(stmt.value) if stmt.value is not None else "None"
            if value.strip() not in _BENIGN_RETURNS:
                continue
            try:
                rel = path.relative_to(REPO_ROOT).as_posix()
            except ValueError:
                rel = path.as_posix()   # planted file outside the repo
            found.add((rel, _enclosing_function(tree, node.lineno)))
    return found


class TestTheDetectorDetects:
    """Planted defects, in both the shapes this collapse is written in.

    ⛔ `assert scan() == known` says nothing about whether the scanner still
    works. Measured: narrowing the operand match back to bare `ast.Name` — the
    exact regression this guards — left every other test in this file green.
    """

    _NAME_FORM = (
        "def load(policy):\n"
        "    if not policy or not Path(policy).is_file():\n"
        "        return []\n"
        "    return [1]\n")
    # How argparse values are actually spelled at the call site.
    _ATTRIBUTE_FORM = (
        "def load(args):\n"
        "    if not args.policy or not os.path.isfile(args.policy):\n"
        "        return []\n"
        "    return [1]\n")
    _INNOCENT = (
        "def load(args):\n"
        "    if not args.policy:\n"
        "        return []\n"
        "    if not os.path.isfile(args.policy):\n"
        "        raise ValueError('unusable')\n"
        "    return [1]\n")

    @pytest.mark.parametrize("src,name", [(_NAME_FORM, "bare name"),
                                          (_ATTRIBUTE_FORM, "attribute")])
    def test_a_planted_collapse_is_found(self, tmp_path, src, name):
        f = tmp_path / "planted.py"
        f.write_text(src, encoding="utf-8")
        found = _collapsed_sites([str(f)])
        assert {fn for _f, fn in found} == {"load"}, (
            f"the {name} form of the collapse was not detected. This is the "
            f"shape the scan exists to find; a scan that cannot see it reports "
            f"the repo clean for the same reason an unplugged detector does.")

    def test_the_split_form_is_not_reported(self, tmp_path):
        """The correct code must not be flagged, or the scan gets narrowed."""
        f = tmp_path / "innocent.py"
        f.write_text(self._INNOCENT, encoding="utf-8")
        assert _collapsed_sites([str(f)]) == set(), (
            "code that separates 'absent' from 'unusable' — exactly what this "
            "PR changed four call sites to do — was reported as a defect.")


class TestNoCollapsedSuppliedVsAbsent:

    def test_scan_surface_covers_the_tool_subdirectories(self):
        """The scan must actually visit ops/, dx/ and lint/, not just the root."""
        scanned = _scanned_files()
        for sub in ("scripts/tools/ops/", "scripts/tools/dx/",
                    "scripts/tools/lint/"):
            assert any(f.startswith(sub) for f in scanned), (
                f"the structural scan visits no file under {sub}. Every tool "
                "that could grow this defect lives in those directories; a "
                "surface that only covers the shared-lib root reports clean "
                "for the same reason an unplugged detector does.")
        assert len(scanned) >= 100, (
            f"scan surface collapsed to {len(scanned)} files.")

    def test_scan_finds_the_known_site(self):
        """Anti-vacuity: the scanner must still be able to see anything.

        ⛔ A bare `assert scan() == set()` would be satisfied by a scanner that
        stopped matching — which is how this class survived in the first place.
        The known site is the witness that the detector is alive.
        """
        assert _KNOWN_COLLAPSED <= _collapsed_sites(), (
            "the structural scan no longer finds the site it is known to "
            "find, so it is not detecting anything. Fix the scanner, do not "
            "shrink _KNOWN_COLLAPSED.")

    def test_no_new_collapsed_sites(self):
        new = _collapsed_sites() - _KNOWN_COLLAPSED
        assert not new, (
            f"new supplied-vs-absent collapse: {sorted(new)}\n"
            "⛔ `if not x or not <path check>: return <empty>` makes 'you gave "
            "me an unusable value' indistinguishable from 'you gave me "
            "nothing'. Split the branch: absent is a skip, unusable is "
            "EXIT_CALLER_ERROR (dev-rules #13).\n"
            "⛔ Do not add the new site to _KNOWN_COLLAPSED to clear this — "
            "that list exists for one site whose single caller compensates, "
            "and that compensation is asserted separately.")

    def test_known_site_still_has_exactly_one_caller(self):
        """The exemption's premise, as an assertion rather than a sentence.

        ⛔ `_lib_io.read_onboard_hints` is tolerated only because its one
        caller turns the falsy return into exit 2. A second caller would
        inherit the collapse with nothing checking it, so the premise is
        pinned here — see feedback: an exemption's reachability argument has
        to be executable, because prose has no reader.
        """
        callers = []
        for path in sorted(TOOLS.rglob("*.py")):
            if path.name == "_lib_io.py":
                continue
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"))
            except (OSError, SyntaxError):
                continue
            for node in ast.walk(tree):
                # Same blind spot on the exemption's own premise: a second
                # caller written `_lib_io.read_onboard_hints(...)` would not be
                # counted, so this assertion would keep passing after the
                # premise it guards stopped being true.
                if isinstance(node, ast.Call) and (
                        (isinstance(node.func, ast.Name)
                         and node.func.id == "read_onboard_hints")
                        or (isinstance(node.func, ast.Attribute)
                            and node.func.attr == "read_onboard_hints")):
                    callers.append(
                        f"{path.relative_to(REPO_ROOT).as_posix()}:{node.lineno}")
        assert len(callers) == 1, (
            f"read_onboard_hints now has {len(callers)} call sites "
            f"({callers}). The exemption in _KNOWN_COLLAPSED rests on the "
            "single caller checking the falsy return itself; a second caller "
            "must either do the same or the helper must stop collapsing.")


def test_case_list_is_not_empty():
    """The parametrize list is the quantifier; an empty one passes silently."""
    assert len(_CASES) >= 5, (
        "the contract case list shrank below the set #1556 measured; "
        "removing a case removes the only thing asserting that flag's value "
        "is honoured.")
