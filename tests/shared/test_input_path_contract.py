#!/usr/bin/env python3
"""Supplied-but-unusable input paths must be EXIT_CALLER_ERROR (#1556).

dev-rules #13 files "檔案/路徑不存在" and "malformed 輸入" under
``EXIT_CALLER_ERROR`` (2). The gate this file adds is narrower than that rule
and deliberately so: it pins the one failure *shape* that #1556 found shipping
to customers —

    omitting a flag  and  supplying a value the tool cannot use
    were collapsed into the same branch, so the second silently became the first.

Measured on ``origin/main`` before the fix, each with a working control
(rc column is what the operator got; every one of them also printed a report
that said the run was fine):

===================================  ===  =========================
invocation                            rc   what the operator saw
===================================  ===  =========================
validate-config --policy <not-file>    0   ``[PASS] policy — No
                                           policy file — skipped``
validate-config --rule-packs <bad>     0   ``[PASS] custom_rules``
generate-routes --policy <not-file>    0   routes written, allowlist off
lint --policy <not-file> --ci          0   linted, built-in policy used
validate-config --policy-dsl <bad>     0   output BYTE-IDENTICAL to
                                           passing no flag at all
===================================  ===  =========================

What made it ship is that the docs taught it: before this PR,
``docs/cli-reference.md`` line 1897 read
``da-tools validate-config --config-dir ./conf.d --policy "webhook.company.com,slack.com"``
— a comma-separated domain list where the flag takes a path. That line is
rewritten in this PR, so the citation is to the pre-fix file, not to today's.

⚠️ Scope, stated so nobody reads more into a green run than it earns:

  COVERED    the five (tool, flag) pairs in ``_CASES`` and the shapes in
             ``TestUnusableIsMoreThanMissing`` — i.e. the PATH axis: a value
             that cannot serve as the file it names.
  NOT COVERED  the CONTENT axis. Nine inputs still reach an empty allowlist
             through ``load_policy``, six of which mean "the tool could not
             tell" (see its docstring). A 0-byte file and one missing ``s`` in
             ``allowed_domains`` both switch the SSRF check off at exit 0.
  NOT COVERED  every other path-shaped argument in the CLI. Establishing that
             would need a classifier for "which arguments name an input", and
             a wrong classifier reads as a rule while being false for whatever
             it missed.
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
OPS = REPO_ROOT / "scripts" / "tools" / "ops"

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
    # ⛔ The fifth carrier had NO case here for a whole round. Measured:
    # rewriting `if not os.path.isfile(policy_dsl_file)` to `if True` — so that
    # a perfectly good DSL file is rejected as "not a file" — left all 464
    # tests green. The commit that added the flag's fix claimed in CHANGELOG
    # that every row carried both controls; this row carried neither.
    ("validate-config --policy-dsl",
     "validate_config.py",
     lambda c: ["--config-dir", str(c)],
     ["--policy-dsl", _MISSING]),
    # #1616: the SIXTH carrier. `--base-config` feeds the ConfigMap that
    # docs/integration/gitops-deployment.md Method C tells customers to
    # `kubectl apply` / let ArgoCD sync, and a typo'd path produced output
    # BYTE-FOR-BYTE identical to omitting the flag (measured with `cmp -s`) at
    # exit 0 with an empty stderr — so the operator's `global:` (SMTP
    # smarthost, Slack webhook) was silently replaced by a built-in
    # placeholder and notifications went nowhere. `--output-configmap` is in
    # the base argv because it is the only mode that reads the flag.
    # ⚠️ NOT "the only carrier whose artifact reaches a cluster" — an earlier
    # revision said that and it is false: `generate-routes --policy` changes
    # the same emitted ConfigMap (measured, 1871 vs 1915 bytes), and its old
    # behaviour was worse in kind — a receiver that should have been rejected
    # is admitted, rather than a value being replaced.
    ("generate-routes --base-config",
     "generate_alertmanager_routes.py",
     lambda c: ["--config-dir", str(c), "--output-configmap"],
     ["--base-config", _MISSING]),
]


# Which per-check rows must carry the failure. `validate-config --policy`
# reaches TWO independent loaders (check_routes and check_policy); both are
# named so neither can quietly revert behind the other's exit code.
_EXPECTED_FAIL_ROWS = {
    "validate-config --policy": ("routes", "policy"),
    "validate-config --policy missing-file": ("routes", "policy"),
    "validate-config --rule-packs": ("custom_rules",),
    "validate-config --policy-dsl": ("policy_dsl",),
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

        ⛔ Measured: reverting `check_policy` to its #1556 behaviour left
        every test in this module green, because `check_routes` loads the
        same policy and was still failing — one aggregate rc, two independent
        producers, so each producer's assertion was individually vacuous.
        (No test count here: it moved three times while this PR was open.)
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

    # -- the same four shapes for the fifth carrier ------------------------
    # ⛔ `--policy-dsl` shipped with only "not a file" closed, which is the
    # 1-of-5 that external review had already caught on `--policy` one round
    # earlier. Measured before this landed: `--policy-dsl <malformed>` and
    # `<non-UTF-8>` both exited 1 with the row reading "could not read the
    # config … If no file is named above, the fault is in one of the paths you
    # passed" — while naming the file three lines above, and attributing an
    # argv mistake to the customer's tree.

    @pytest.fixture(scope="class")
    def dsl_files(self, tmp_path_factory):
        d = tmp_path_factory.mktemp("dsl")
        (d / "malformed.yaml").write_text("policies:\n  - name: [\n",
                                          encoding="utf-8")
        (d / "nonutf8.yaml").write_bytes(
            bytes([0xFF, 0xFE, 0x00]) + b"policies" + bytes([0x0A]))
        (d / "sequence.yaml").write_text("- not-a-mapping\n", encoding="utf-8")
        (d / "good.yaml").write_text("policies: []\n", encoding="utf-8")
        return {p.stem: p for p in d.iterdir()}

    @pytest.mark.parametrize("kind", ["malformed", "nonutf8", "sequence"])
    def test_unusable_policy_dsl_is_caller_error(self, kind, dsl_files, conf_d):
        r = _run("validate_config.py",
                 ["--config-dir", str(conf_d), "--policy-dsl",
                  str(dsl_files[kind])])
        out = r.stdout.decode("utf-8", "replace")
        assert r.returncode == EXIT_CALLER_ERROR, (
            f"--policy-dsl with a {kind} file exited {r.returncode}. "
            f"'not a file' is one of five shapes of unusable, not the class.\n"
            f"stdout: {out[:500]}")
        assert b"Traceback" not in r.stderr
        assert "[FAIL] policy_dsl" in out, (
            f"the failing row is not policy_dsl — an argv mistake was "
            f"attributed elsewhere.\nstdout: {out[:500]}")

    def test_control_a_well_formed_policy_dsl_still_runs(
            self, dsl_files, conf_d):
        """Without this, the assertion above is satisfied by a --policy-dsl
        that rejects every value it is handed — measured: rewriting the
        is_file() guard to `if True` left the whole suite green."""
        r = _run("validate_config.py",
                 ["--config-dir", str(conf_d), "--policy-dsl",
                  str(dsl_files["good"])])
        assert r.returncode != EXIT_CALLER_ERROR, (
            f"a valid policy DSL file was rejected, so the checks above prove "
            f"nothing.\nstdout: {r.stdout.decode('utf-8', 'replace')[:400]}")


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
# ⛔ NOT GUARDED: writing a NEW collapse anywhere in scripts/tools/
# ---------------------------------------------------------------------------
# An AST scan for `if not X or <fs predicate>: return <benign>` lived here and
# was WITHDRAWN. What follows is what was measured, not a summary of an
# argument — a future reader deciding to rebuild it should start from these
# numbers rather than from the idea.
#
# Two-sided mutation battery, same nine mutations against the tree with the
# scan present and with it removed:
#
#     mutation                                       with scan   without
#     V1 load_policy stops raising on "not a file"   KILLED      KILLED
#     V2 lint drops `except UnicodeDecodeError`      KILLED      KILLED
#     V3 check_policy back to PASS-on-skip           KILLED      KILLED
#     V4 check_custom_rules back to PASS             KILLED      KILLED
#     V5 --policy-dsl back to the collapsed form     KILLED      KILLED
#     V6 _argv_error_row stops clearing the flag     KILLED      KILLED
#     V7 a NEW not/or collapse planted               KILLED      SURVIVED
#     V8 a NEW attribute-form collapse planted       KILLED      SURVIVED
#     V9 a second, non-compensating caller planted   KILLED      SURVIVED
#
# So removal costs exactly V7/V8/V9 — "somebody writes a new one" — and costs
# nothing on "#1556 comes back", which the contract tests above pin directly.
#
# Why it was not worth those three cells, all measured by blind review:
#   * Detection: fed 10 behaviourally equivalent spellings of the same defect,
#     the scan caught 2. It missed `p is None or ...`, `p == '' or ...`, a body
#     with one extra statement, `return 0`, a nested if, a ternary,
#     `os.access(p, os.R_OK)`, and `try: open(p) / except OSError: return []`.
#     Its own docstring criticised "a list of names that would miss the first
#     one it did not anticipate" while being three such lists.
#   * A live in-tree instance it reported CLEAN. `_grar_render.load_base_config`
#     WAS byte-for-byte the shape the scan hunts —
#     `if not path or not Path(path).is_file():` — and `--base-config <typo>`
#     silently substituted the built-in Alertmanager `global:` for the
#     operator's, at exit 0, into a ConfigMap meant for `kubectl apply`. The
#     scan skipped it because the body returned `dict(_DEFAULT_BASE_CONFIG)`, a
#     Call rather than a benign literal. Re-measured after the withdrawal with
#     the detector restored from git and a passing control on `_lib_io`: an
#     11th spelling, and the only one of the eleven that was a real defect
#     standing in the tree it was scanning.
#     ⇒ ✅ FIXED in #1616; that carrier now has a row in `_CASES` above, so the
#     evidence for the withdrawal no longer doubles as an open defect. ⚠️ The
#     three cells the withdrawal costs (V7/V8/V9 — "somebody writes a NEW
#     collapse") are unchanged: nothing here detects a fresh one.
#   * False reds: the `and`-positive mirror rule — the obvious way to cover the
#     forms above — was never committed, so its count cannot be re-derived from
#     this repo. ⛔ An earlier revision of this comment said "6 sites"; blind
#     review's faithful reconstruction found 10 (11 before de-duplicating by
#     enclosing function), and 6 is not reachable under any rule either of us
#     could write. What DID reproduce, on both counts, is the shape of the
#     result: exactly 1 of them was a real defect, and 2 of the others were the
#     CORRECT split being flagged.
#   * Its exemption's premise held for 1 of 3 unusable inputs
#     (`scaffold --from-onboard` exits 2 on a missing file, but raises an
#     uncaught traceback at rc=1 on malformed JSON and on non-UTF-8).
#   * Its anti-vacuity witness punished FIXING the defect it exempted: after a
#     correct split of `_lib_io.read_onboard_hints`, the assertion went red
#     with a message forbidding the only correct response.
#
# ⚠️ `_lib_io.read_onboard_hints` still collapses the two cases. Its one call
# site (`scaffold_tenant.run_from_onboard`) turns a falsy return into exit 2,
# which is why the missing-file path is fine and the malformed/non-UTF-8 paths
# are not. Nothing here watches either fact.
#
# ⛔ Nor the call-site count itself. `test_known_site_still_has_exactly_one_
# caller` went out with the scan, and it did NOT depend on the scan: it pinned
# that a second, non-compensating caller cannot appear (V9 above). Re-measured
# by hand today — still exactly one, `scaffold_tenant.py` inside
# `run_from_onboard`. That is a snapshot, not a guard; the withdrawal traded
# this assertion away and the first version of this block failed to say so.
#
# ⚠️ And the compensation is itself of the family: `scaffold --from-onboard`
# on a readable file whose content is `{}` also exits 2 saying "Cannot read
# onboard hints". A falsy return cannot distinguish "unreadable" from "read
# fine, empty".
# ---------------------------------------------------------------------------


def _fail_row_actions(stdout: str) -> dict[str, str]:
    """Map each `[FAIL] <row>` to the `Suggested action` text under it.

    ⛔ Per row, not joined. The first cut of the test below searched the whole
    report for the flag name and SURVIVED the mutation it existed to catch:
    `validate-config --policy` fails TWO rows, so `check_routes` keeping its
    hint satisfied the assertion while `check_policy` lost hers. That is the
    same aggregate-vacuity CodeRabbit found in the exit-code assertion one
    round earlier, rebuilt by hand in the fix for it.
    """
    actions: dict[str, str] = {}
    current: str | None = None
    for line in stdout.splitlines():
        stripped = line.strip()
        if stripped.startswith("[FAIL] "):
            current = stripped[len("[FAIL] "):].strip()
        elif stripped.startswith("[") and "]" in stripped:
            current = None          # any other row closes the previous one
        elif current and "Suggested action" in stripped:
            actions[current] = stripped
    return actions


@pytest.mark.parametrize("argv,marker,rows", [
    (["--policy", _MISSING], "--policy", ("policy", "routes")),
    (["--rule-packs", _MISSING], "--rule-packs", ("custom_rules",)),
    (["--policy-dsl", _MISSING], "--policy-dsl", ("policy_dsl",)),
], ids=["policy", "rule-packs", "policy-dsl"])
def test_each_argv_error_row_carries_its_own_remediation(
        argv, marker, rows, conf_d):
    """EVERY failing row must name the flag the operator actually got wrong.

    ⛔ Blind review measured that two of these three fixes had NO test at all:
    deleting `hint=_RULE_PACKS_INPUT_HINT` and deleting
    `hint=_POLICY_DSL_INPUT_HINT` both left the suite green, while the commit
    message claimed every fix carried a reverting mutation. Without a hint the
    row inherits the generic per-check advice, which sends the operator to read
    tenant YAML for a mistake that is entirely in their own argv — measured for
    --rule-packs: "Validate rule pack YAML syntax … run rule-pack-split
    --check", about a tree the tool never opened.
    """
    r = _run("validate_config.py", ["--config-dir", str(conf_d), *argv])
    out = r.stdout.decode("utf-8", "replace")
    assert r.returncode == EXIT_CALLER_ERROR, out[:400]
    actions = _fail_row_actions(out)
    for row in rows:
        assert row in actions, (
            f"{marker}: row [FAIL] {row} carried no suggested action at all "
            f"(rows seen: {sorted(actions)})")
        # ⛔ Whole token, not substring. `marker in actions[row]` was the first
        # cut and it is satisfied by the WRONG flag: `"--policy"` is a
        # substring of `"--policy-dsl"`, so swapping the policy row's hint for
        # the policy-dsl one left this green — measured, with the control
        # (swapping in the --rule-packs hint, which shares no prefix) going red
        # as it should. The failure mode this test exists to catch is exactly
        # "the hint names a neighbouring artefact", and the neighbour whose
        # name starts with this one's is the likeliest neighbour of all.
        assert re.search(rf"(?<![\w-]){re.escape(marker)}(?![\w-])",
                         actions[row]), (
            f"{marker}: the remediation on row [FAIL] {row} never names the "
            f"flag as a whole token — it reads {actions[row][:200]!r}. A hint "
            f"that points at the wrong artefact is worse than none: it sends "
            f"the operator to edit files that are not the problem.")


def test_every_measured_carrier_has_a_case():
    """The parametrize list is the quantifier; an empty one passes silently.

    ⛔ Count CARRIERS, not rows. The first version asserted ``len(_CASES) >= 5``
    while `_CASES` held five rows covering only FOUR (tool, flag) pairs —
    `validate-config --policy` appeared twice — and the fifth carrier #1556
    actually shipped a fix for, `--policy-dsl`, had no case at all. A count
    that can be satisfied by duplicating an existing row is not a quantifier
    over the thing being claimed.
    """
    carriers = {(script, tuple(bad[:1])) for _, script, _, bad in _CASES}
    measured = {
        ("validate_config.py", ("--policy",)),
        ("validate_config.py", ("--rule-packs",)),
        ("validate_config.py", ("--policy-dsl",)),
        ("generate_alertmanager_routes.py", ("--policy",)),
        ("lint_custom_rules.py", ("--policy",)),
        # #1616 — sixth carrier.
        ("generate_alertmanager_routes.py", ("--base-config",)),
    }
    assert measured <= carriers, (
        f"a (tool, flag) carrier #1556/#1616 measured and fixed has no case "
        f"here: {sorted(measured - carriers)}. Removing a carrier removes the "
        f"only thing asserting that flag's value is honoured.")


# ---------------------------------------------------------------------------
# The EMPTY STRING, which is a supplied value and was read as an omitted one.
# ---------------------------------------------------------------------------
# ⛔ This axis existed for a whole release and no test could see it. The reason
# is structural and worth naming: `test_every_measured_carrier_has_a_case`
# above quantifies over CARRIERS — (tool, flag) pairs — and nothing quantified
# over SHAPES of unusable value. `_CASES` only ever fed `_MISSING` (a path that
# does not exist) and a comma-separated domain list, so adding a carrier was
# guarded while adding a shape was not, and `""` was never in the vocabulary.
#
# Measured before the fix, on every carrier below: the tool took the
# flag-omitted branch. For `generate-routes --base-config` the emitted ConfigMap
# was byte-identical to omitting the flag at exit 0 with an empty stderr; for
# `--policy` the webhook domain allowlist was simply off.
#
# `--base-config "$BASE_CFG"` with the variable unset is exactly this.
_EMPTY_CARRIERS = [
    ("validate-config --policy", "validate_config.py",
     lambda c: ["--config-dir", str(c)], "--policy"),
    ("validate-config --rule-packs", "validate_config.py",
     lambda c: ["--config-dir", str(c)], "--rule-packs"),
    ("validate-config --policy-dsl", "validate_config.py",
     lambda c: ["--config-dir", str(c)], "--policy-dsl"),
    ("generate-routes --policy", "generate_alertmanager_routes.py",
     lambda c: ["--config-dir", str(c)], "--policy"),
    ("generate-routes --base-config", "generate_alertmanager_routes.py",
     lambda c: ["--config-dir", str(c), "--output-configmap"], "--base-config"),
    ("lint --policy", "lint_custom_rules.py",
     lambda c: [str(c), "--ci"], "--policy"),
]


@pytest.mark.parametrize("case_id,script,base,flag", _EMPTY_CARRIERS,
                         ids=[c[0] for c in _EMPTY_CARRIERS])
def test_an_empty_value_is_supplied_not_omitted(case_id, script, base, flag, conf_d):
    r = _run(script, [*base(conf_d), flag, ""])
    out = (r.stdout + r.stderr).decode("utf-8", "replace")
    assert r.returncode == EXIT_CALLER_ERROR, (
        f"{case_id}: `{flag} \"\"` exited {r.returncode}, not "
        f"{EXIT_CALLER_ERROR}. An empty string is a SUPPLIED value — it is what "
        f"an unset shell variable expands to — and treating it as omitted is "
        f"the #1556/#1616 collapse arriving through a falsy test.\n"
        f"⛔ Do not fix this by dropping the case.\nstdout: {out[:400]}")
    assert flag in out, (
        f"{case_id}: exit 2 is right but the output never names {flag}.")
    # ⛔ And the VALUE has to be visible. Without `!r` the diagnostic for an
    # empty value stops after the colon — `--rule-packs: not a directory: ` —
    # which is the one shape this axis exists to test. Measured: removing the
    # `!r` was free until this line existed.
    assert "''" in out, (
        f"{case_id}: the message does not show the offending value, so an "
        f"empty one is indistinguishable from a truncated message.\n{out[:400]}")


@pytest.mark.parametrize("case_id,script,base,flag", _EMPTY_CARRIERS,
                         ids=[c[0] for c in _EMPTY_CARRIERS])
def test_control_omitting_the_flag_entirely_is_not_a_caller_error(
        case_id, script, base, flag, conf_d):
    """⛔ Pairs with the test above. Without it, "" going red is satisfied by a
    tool that rejects the flag unconditionally — which would break every
    legitimate omission."""
    r = _run(script, base(conf_d))
    assert r.returncode != EXIT_CALLER_ERROR, (
        f"{case_id}: omitting {flag} must stay a legitimate invocation.\n"
        f"stderr: {r.stderr.decode('utf-8', 'replace')[:400]}")


def test_every_path_interpolation_in_the_loaders_shows_the_value():
    """⛔ Structural, because the per-branch messages are not all reachable.

    The empty-value cases above pin `!r` on exactly one branch — "not a file" —
    because an empty path fails `is_file()` before any other branch can run. So
    the UTF-8 / YAML / OSError / not-a-mapping messages had `!r` that no test
    could reach, and dropping it was measured to be free.

    The property is "every message that shows this path shows it quoted", which
    is checkable on the source without reaching each branch. It is the same
    reason the value has to be quoted at all: a bare interpolation of `''`
    renders as a message that stops after the colon.
    """
    import ast as _ast
    targets = [
        ("scripts/tools/ops/_grar_render.py", "load_base_config", "path"),
        ("scripts/tools/ops/_grar_validate.py", "load_policy", "policy_path"),
        ("scripts/tools/ops/validate_config.py", "check_policy_dsl",
         "policy_dsl_file"),
    ]
    offenders = []
    for rel, fname, var in targets:
        src = (REPO_ROOT / rel).read_text(encoding="utf-8")
        tree = _ast.parse(src, filename=rel)
        fn = next(n for n in _ast.walk(tree)
                  if isinstance(n, _ast.FunctionDef) and n.name == fname)
        for node in _ast.walk(fn):
            if not isinstance(node, _ast.FormattedValue):
                continue
            inner = node.value
            if isinstance(inner, _ast.Name) and inner.id == var:
                # conversion: -1 none, 114 == ord('r')
                if node.conversion != 114:
                    offenders.append(f"{rel}::{fname} line {node.lineno}")
    assert not offenders, (
        f"these messages interpolate the supplied path without !r, so an empty "
        f"value renders as nothing at all: {offenders}")


def test_every_carrier_is_measured_on_both_unusable_shapes():
    """The shape axis needs its own quantifier, for the reason above.

    ⛔ Compare with `test_every_measured_carrier_has_a_case`: that one asserts
    each carrier appears SOMEWHERE. This one asserts each carrier is exercised
    on BOTH shapes — a non-existent path and an empty string. Deleting either
    parametrize list, or letting a new carrier land in only one of them,
    reproduces exactly how `""` stayed invisible.
    """
    missing_axis = {(script, bad[0]) for _, script, _, bad in _CASES}
    empty_axis = {(script, flag) for _, script, _, flag in _EMPTY_CARRIERS}
    only_one = missing_axis ^ empty_axis
    assert not only_one, (
        f"these (tool, flag) carriers are exercised on one unusable shape but "
        f"not the other: {sorted(only_one)}. Both lists must cover the same "
        f"carriers, or a shape can silently stop being tested.")
    assert len(empty_axis) >= 6, (
        f"the empty-string axis covers {len(empty_axis)} carriers; emptying "
        f"this list must not pass silently.")
