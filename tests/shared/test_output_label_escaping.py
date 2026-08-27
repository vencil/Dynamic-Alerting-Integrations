#!/usr/bin/env python3
"""#1538 — a tenant config FILENAME is untrusted input, and the plain-text
report branch printed it with zero escaping.

Two things ride in on an unescaped name. A **newline** ends the current report
line and starts a new one, so a file called ``evil\\n[PASS] all good\\nx.yaml``
prints ``[PASS] all good`` at column 0 — indistinguishable, to a human or to a
``grep '^\\[PASS\\]'``, from a verdict the tool actually emitted. An **ESC**
starts an ANSI sequence, so the same channel recolours the terminal or moves the
cursor. ``--json`` was never vulnerable (``json.dumps`` escapes control chars
unconditionally) and its bytes must not change; the fix is
:func:`_lib_io.safe_label` applied at the print sites only.

⛔ THE POPULATION IS DERIVED, NOT LISTED. It is every tool under
``scripts/tools/{ops,dx,lint}`` whose source contains ``"--config-dir"`` — the
same predicate as the shell sweep the issue was scoped with. A hardcoded list
would silently stop covering the repo the day tool #27 lands, which is the exact
failure mode #1339's family of defects is made of (one rule, many hand-copies).

⛔ "COULD NOT MEASURE" AND "MEASURED CLEAN" ARE DIFFERENT ANSWERS and this file
keeps them apart. A tool that never produced an observation is ``pytest.skip``ed
with the reason printed in the skip message; it is never counted as a pass.
:func:`test_the_unmeasurable_set_is_named` then asserts the skip set is exactly
the set we know about, so a tool going dark is a FAILURE rather than a silent
downgrade from "clean" to "skipped".

⚠️ SCOPE, so nobody over-reads a green run:

* This measures what the fixture REACHES, and a pass on a tool that never
  printed an untrusted name is now reported as a SKIP rather than a pass (see
  the vacuity check in the parametrized test).

* ⛔ **Seven of the nineteen tools patched for #1538 are NOT covered by this
  file at all.** Neither fixture drives them to a site that renders an
  untrusted name, which was established by counterfactual: short-circuiting
  ``safe_label`` to the identity function reddens twelve tools and leaves these
  seven green —

      analyze_rule_pack_gaps.py   backtest_threshold.py
      maintenance_scheduler.py    notification_tester.py
      policy_opa_bridge.py        threshold_govern.py
      check_routing_profiles.py

  Their ``safe_label`` calls are retained as deliberate defence in depth, and
  the owner ruled they stay. But a green run of this file — "N passed" — says
  **nothing whatsoever** about those seven. Do not cite it for them.

* ``operator_check.py`` is a separate case: it is not patched, and grepping it
  turns up no site that renders a config-derived name at all.
* The **uncaught-traceback** channel is out of #1538's scope and is NOT fixed:
  when ``yaml.safe_load`` raises, the ``YAMLError`` message embeds the full
  file path, and Python prints it to stderr as a crash dump that no print-site
  escaping can reach. Assertions here run against the pre-traceback region and
  a separate test pins the crashing set so it cannot grow unnoticed.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
TOOLS_DIR = REPO_ROOT / "scripts" / "tools"

# The payloads. Distinct markers for the two untrusted channels so a failure
# message says WHICH one leaked.
FN_MARK = "[PASS] FORGED-BY-FILENAME"
TN_MARK = "[PASS] FORGED-BY-TENANT"
ESC = "\x1b"


# ── population ────────────────────────────────────────────────────────
def collect_config_dir_tools() -> list[Path]:
    """Every ops/dx/lint tool that accepts ``--config-dir``.

    Derived from the source text, matching the sweep #1538 was scoped with::

        grep -q '"--config-dir"' "$x"

    so a tool added tomorrow is covered without editing this file.
    """
    out: list[Path] = []
    for sub in ("ops", "dx", "lint"):
        d = TOOLS_DIR / sub
        if not d.is_dir():
            continue
        for p in sorted(d.glob("*.py")):
            if p.name.startswith("_") or p.name == "__init__.py":
                continue
            if '"--config-dir"' in p.read_text(encoding="utf-8"):
                out.append(p)
    return out


ALL_TOOLS = collect_config_dir_tools()

# Positional/flag arguments a tool needs before it will read --config-dir at
# all. Anything NOT listed here that still refuses to run gets skipped with the
# reason, not silently passed.
EXTRA_ARGS: dict[str, list[str]] = {
    "deprecate_rule.py": ["pg_connections"],
    "offboard_tenant.py": ["evilt"],
    # #1538 review round: diagnose was previously parked in KNOWN_UNMEASURABLE
    # on the grounds that inventing a tenant name "would test a different code
    # path". That reasoning was wrong twice over — this map already fabricates
    # arguments for the two tools above, and the tenant here never has to exist:
    # diagnose reads and ANNOUNCES every conf.d file before it looks the tenant
    # up. Measured: with these two words it reaches its `WARN: skip <file>` line
    # and forged a report line at column 0. "Unmeasurable" was hiding a real hole.
    "diagnose.py": ["victim", "--show-inheritance"],
}

# `migrate_to_operator` names the same input `--source-dir`.
SOURCE_DIR_TOOLS = {"migrate_to_operator.py"}

# Tools whose DEFAULT output path is anchored to the repo via `__file__` rather
# than to the cwd, so running them from a tmp cwd is not enough to contain the
# write. Each needs an explicit redirect. ⛔ Load-bearing: without this,
# `compile_custom_alerts` rewrites the shipped rule-pack on every CI run.
# `test_the_suite_does_not_write_to_the_repo` is the backstop that catches any
# tool this map misses.
REPO_ANCHORED_OUTPUT: dict[str, str] = {
    "compile_custom_alerts.py": "--out",
}


# ── fixtures ──────────────────────────────────────────────────────────
def _write_hostile_tree(root: Path, *, malformed: bool) -> None:
    """Build a conf.d whose FILENAMES and TENANT NAMES are hostile.

    Two variants, because they reach different code:

    * ``malformed=True`` — bodies fail to parse, driving the error-reporting
      paths that print ``{filename}: {exception}``. This is the shape #1538 was
      filed against.
    * ``malformed=False`` — bodies are valid, so tools reach their normal
      report renderers. Necessary because a malformed body makes several tools
      raise before they print anything, which HIDES their happy-path sites.
    """
    root.mkdir(parents=True, exist_ok=True)
    body = "tenants:\n  {name}:\n    a: [1, 2\n" if malformed else \
           "tenants:\n  {name}:\n    pg_connections: 90\n"

    (root / "_defaults.yaml").write_text(
        "defaults:\n  pg_connections: 80\n", encoding="utf-8")
    (root / "benign.yaml").write_text(body.format(name="benign"), encoding="utf-8")

    # Channel 1: hostile FILENAME (newline, and ANSI).
    os.write(os.open(os.path.join(os.fsencode(root),
                                  f"evil\n{FN_MARK}\nx.yaml".encode()),
                     os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600),
             body.format(name="evilt").encode())
    os.write(os.open(os.path.join(os.fsencode(root), b"\x1b[31mred\x1b[0m.yaml"),
                     os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600),
             body.format(name="redt").encode())

    # Channel 1b: U+0085 NEL as the line break instead of "\n". #1538's review
    # round found C1 passing through ten already-fixed tools, and terminals that
    # decode C1 treat NEL as a line break — so this is the SAME forged-line
    # attack, not a separate concern. No special assertion is needed: Python's
    # str.splitlines() splits on \x85, so the column-0 check below sees it the
    # way a C1-decoding terminal would.
    os.write(os.open(os.path.join(os.fsencode(root),
                                  ("nel\x85" + FN_MARK + "\x85y.yaml").encode("utf-8")),
                     os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600),
             body.format(name="nelt").encode())

    # Channel 2: hostile TENANT NAME inside an innocuous filename. Only
    # reachable when the body parses, so it is skipped for the malformed tree.
    if not malformed:
        (root / "names.yaml").write_text(
            f'tenants:\n  "tn\\n{TN_MARK}\\nz":\n    pg_connections: 93\n'
            '  "\\x1b[31mredtenant\\x1b[0m":\n    pg_connections: 94\n',
            encoding="utf-8")


@pytest.fixture(scope="session")
def hostile_dirs(tmp_path_factory) -> dict[str, str]:
    base = tmp_path_factory.mktemp("confd_hostile")
    out = {}
    for name, malformed in (("malformed", True), ("valid", False)):
        d = base / name
        _write_hostile_tree(d, malformed=malformed)
        out[name] = str(d)
    return out


# ── running one tool ──────────────────────────────────────────────────
class Unmeasurable(Exception):
    """The tool produced no observation; why is in ``args[0]``."""


def _run(tool: Path, config_dir: str, sandbox: Path) -> tuple[bytes, bytes, int]:
    flag = "--source-dir" if tool.name in SOURCE_DIR_TOOLS else "--config-dir"
    args = [sys.executable, str(tool), flag, config_dir]
    args += EXTRA_ARGS.get(tool.name, [])
    if tool.name in REPO_ANCHORED_OUTPUT:
        args += [REPO_ANCHORED_OUTPUT[tool.name], str(sandbox / "out.yaml")]
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    sandbox.mkdir(parents=True, exist_ok=True)
    try:
        # cwd=sandbox contains every tool whose default output path is
        # cwd-relative (operator_generate, migrate_to_operator, ...).
        r = subprocess.run(args, capture_output=True, timeout=120,
                           cwd=str(sandbox), env=env)
    except subprocess.TimeoutExpired:
        raise Unmeasurable("timed out after 120s")
    return r.stdout, r.stderr, r.returncode


def _classify_unmeasurable(stdout: bytes, stderr: bytes) -> str | None:
    blob = stdout + stderr
    m = re.search(rb"error: (the following arguments are required[^\n]*)", blob)
    if m:
        return f"argparse: {m.group(1).decode(errors='replace')}"
    if re.search(rb"error: (unrecognized arguments[^\n]*)", blob):
        return "argparse: unrecognized arguments"
    m = re.search(rb"(ModuleNotFoundError|ImportError): ([^\n]*)", blob)
    if m:
        return f"missing dependency: {m.group(2).decode(errors='replace')[:90]}"
    if re.search(rb"(?im)^[^\n]*python client is required", blob):
        return "missing dependency: kubernetes Python client"
    return None


def _print_region(stdout: bytes, stderr: bytes) -> bytes:
    """The PLAIN-TEXT output only. Two things are deliberately excluded.

    **The uncaught traceback.** It is Python's crash dump, not a print site: the
    ``YAMLError`` message it renders embeds the file path, and no output-layer
    escaping can intercept it. :func:`test_uncaught_traceback_channel_is_a_known_gap`
    covers what is excluded so it cannot quietly expand.

    **A JSON document on stdout.** Some tools in this population emit JSON by
    DEFAULT, not only under ``--json`` (``diagnose``, ``generate_tenant_metadata``),
    so for them the plain-text branch is stderr alone. Including their stdout
    would point these assertions at the machine-readable branch — which #1538
    must not change, and which is not what ``safe_label`` guards.

    ⛔ This is load-bearing, and NOT merely a tidiness rule, because the premise
    "the JSON branch was never vulnerable" is only true for C0. ``json.dumps``
    escapes control characters below ``\x20``; with ``ensure_ascii=False`` — the
    repo default via ``format_json_report`` — it passes **C1 through verbatim**
    (measured: ``json.dumps("a\x85b", ensure_ascii=False)`` -> ``"a\x85b"``). So a
    NEL in a tenant name does reach JSON output unescaped. That is a real,
    PRE-EXISTING property of the machine-readable branch, unchanged by #1538 and
    out of its scope; recorded here so the next reader does not rediscover it as
    a regression of this work.
    """
    i = stderr.find(b"Traceback (most recent call last):")
    text_stderr = stderr if i < 0 else stderr[:i]
    try:
        json.loads(stdout.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return stdout + text_stderr      # stdout is prose
    return text_stderr                   # stdout is a JSON document


# Any of these appearing in the output proves an untrusted name actually
# reached it. Written to match both the raw and the escaped rendering (the
# newline becomes "?", the ESC becomes "?", the surrounding text survives), so
# the predicate answers "did a hostile name get here", never "was it escaped" —
# that second question is what the assertions are for.
_UNTRUSTED_NAME_REACHED_OUTPUT = re.compile(
    rb"FORGED-BY-FILENAME|FORGED-BY-TENANT|\[31mred")


def _observe(tool: Path, config_dir: str, sandbox: Path) -> bytes:
    stdout, stderr, _rc = _run(tool, config_dir, sandbox)
    why = _classify_unmeasurable(stdout, stderr)
    if why:
        raise Unmeasurable(why)
    return _print_region(stdout, stderr)


def _first_line(stdout: bytes, stderr: bytes) -> str:
    for ln in (stdout + stderr).splitlines():
        if ln.strip():
            return ln.decode("utf-8", errors="replace")[:100]
    return "<no output>"


# ── the assertions ────────────────────────────────────────────────────
@pytest.mark.parametrize("tool", ALL_TOOLS, ids=[t.name for t in ALL_TOOLS])
@pytest.mark.parametrize("variant", ["malformed", "valid"])
def test_untrusted_names_cannot_forge_a_report_line(
    tool: Path, variant: str, hostile_dirs, tmp_path,
):
    """No control character from a filename or tenant name survives to stdout."""
    try:
        stdout, stderr, _rc = _run(tool, hostile_dirs[variant], tmp_path)
    except Unmeasurable as exc:
        pytest.skip(f"NOT MEASURED ({variant}) — {exc.args[0]}")
    why = _classify_unmeasurable(stdout, stderr)
    if why:
        pytest.skip(f"NOT MEASURED ({variant}) — {why}")

    region = _print_region(stdout, stderr)
    text = region.decode("utf-8", errors="replace")

    for mark, channel in ((FN_MARK, "filename"), (TN_MARK, "tenant name")):
        bad = [ln for ln in text.splitlines() if ln.startswith(mark)]
        assert not bad, (
            f"{tool.name} [{variant}]: a {channel} forged {len(bad)} report "
            f"line(s) starting at column 0 with {mark!r}. The newline in the "
            f"name must be escaped at the print site (_lib_io.safe_label)."
        )

    assert ESC not in text, (
        f"{tool.name} [{variant}]: raw ESC (0x1b) reached the plain-text "
        f"report from an untrusted name — the terminal will interpret it as an "
        f"ANSI sequence. Escape the field with _lib_io.safe_label. "
        f"Context: {text[max(0, text.find(ESC) - 70):text.find(ESC) + 40]!r}"
    )

    # ⛔ #1538 review round. Everything above passes trivially when no untrusted
    # name ever reached the output — and several tools in this population exit
    # on an unmet EXTERNAL precondition (Prometheus unreachable, --opa-url
    # missing) long before they read the config, while others simply never print
    # a name. Reporting those as PASSED claims an escaping guarantee that was
    # never exercised, which is the precise confusion this file's docstring says
    # it exists to prevent. They are skipped, with the tool's own first line as
    # the evidence of why.
    if not _UNTRUSTED_NAME_REACHED_OUTPUT.search(region):
        pytest.skip(
            f"NOT MEASURED ({variant}) — ran, but no untrusted name reached the "
            f"output, so the escaping assertions were vacuous. "
            f"First line: {_first_line(stdout, stderr)!r}"
        )


# Tools that could not be measured, each with the reason a human can check.
# ⛔ This is an INVENTORY, not an exemption list: the test below asserts the
# measured-unmeasurable set equals this one, so a tool that starts failing to
# run shows up as a FAILURE instead of quietly becoming a skip.
KNOWN_UNMEASURABLE: dict[str, str] = {
    # Exits before reading any config: `kubernetes` is not installed here and
    # is not a test dependency.
    "da_assembler.py": "missing dependency: kubernetes Python client",
    # Requires a live `--target-url` (and `--output-dir`) — a soak runner, not
    # a reporter; there is nothing to point it at in a unit test.
    "run_chaos_soak.py": "argparse: required --target-url / --output-dir",
    # ⛔ `diagnose.py` used to sit here. It does not belong: see EXTRA_ARGS.
    # Removing it turned up a live line-start forgery. Treat this list as a
    # standing invitation to check whether an entry is really unmeasurable or
    # merely un-invoked.
}


@pytest.mark.parametrize("variant", ["malformed", "valid"])
def test_the_unmeasurable_set_is_named(variant: str, hostile_dirs, tmp_path):
    """"Could not measure" must stay a short, named list — never a silent gap.

    If a tool that used to report starts refusing to run, the parametrized test
    above turns into a skip and stops asserting anything. Without this, that
    downgrade is invisible in a green run.
    """
    measured_unmeasurable: dict[str, str] = {}
    for tool in ALL_TOOLS:
        try:
            _observe(tool, hostile_dirs[variant], tmp_path / tool.stem)
        except Unmeasurable as exc:
            measured_unmeasurable[tool.name] = exc.args[0]

    assert set(measured_unmeasurable) == set(KNOWN_UNMEASURABLE), (
        f"[{variant}] the set of tools that cannot be measured changed.\n"
        f"  now unmeasurable : {sorted(measured_unmeasurable)}\n"
        f"  expected         : {sorted(KNOWN_UNMEASURABLE)}\n"
        f"  reasons observed : {measured_unmeasurable}\n"
        f"Newly unmeasurable tools are NOT covered by the escaping assertion — "
        f"fix the invocation or record it in KNOWN_UNMEASURABLE with a reason."
    )


# The (variant, tool) pairs where an untrusted name DEMONSTRABLY reached the
# output — i.e. where the escaping assertions actually bit. Pinned because the
# vacuity skip above is a one-way ratchet otherwise: a tool that stops printing
# names (a refactor, a new early-exit, an unmet precondition) would quietly
# downgrade from "proven clean" to "skipped" and the suite would stay green.
# ⛔ Membership is decided by COUNTERFACTUAL, not by the text predicate:
# every pair below was confirmed to turn RED when `safe_label` is short-circuited
# to the identity function. That is a stronger claim than
# `_UNTRUSTED_NAME_REACHED_OUTPUT` matching, and the difference is not academic —
# ("valid", "generate_tenant_metadata.py") satisfies the predicate but is
# deliberately ABSENT here, because that tool's default stdout is a JSON
# document: the hostile tenant name arrives inside a JSON string literal with
# its newline already escaped to backslash-n by `json.dumps`, so the plain-text
# assertions cannot bite and the identity break leaves it green. Counting it
# would overstate coverage by exactly one pair.
#
# Measured on 2026-08-27 (19 pairs red under the identity counterfactual).
# Shrinking this set is a FAILURE; growing it is fine and just needs the pair
# added — after checking the counterfactual actually reddens it.
MEASURED: set[tuple[str, str]] = {
    ("malformed", "check_confd_schema.py"),
    ("malformed", "compile_custom_alerts.py"),
    ("malformed", "deprecate_rule.py"),
    ("malformed", "diagnose.py"),
    ("malformed", "explain_route.py"),
    ("malformed", "generate_alertmanager_routes.py"),
    ("malformed", "generate_tenant_metadata.py"),
    ("malformed", "migrate_to_operator.py"),
    ("malformed", "offboard_tenant.py"),
    ("malformed", "operator_generate.py"),
    ("malformed", "validate_config.py"),
    ("valid", "blind_spot_discovery.py"),
    ("valid", "check_confd_schema.py"),
    ("valid", "deprecate_rule.py"),
    ("valid", "explain_route.py"),
    ("valid", "generate_alertmanager_routes.py"),
    ("valid", "offboard_tenant.py"),
    ("valid", "operator_generate.py"),
    ("valid", "threshold_recommend.py"),
}


def test_the_measured_set_does_not_shrink(hostile_dirs, tmp_path):
    """⛔ Guards the vacuity skip against becoming a silent green.

    Without this, "no untrusted name reached the output" is indistinguishable in
    a passing run from "this tool is fine", and a regression that stops a tool
    printing names would look like an improvement.
    """
    measured: set[tuple[str, str]] = set()
    for variant in ("malformed", "valid"):
        for tool in ALL_TOOLS:
            try:
                stdout, stderr, _ = _run(tool, hostile_dirs[variant],
                                         tmp_path / f"{variant}_{tool.stem}")
            except Unmeasurable:
                continue
            if _classify_unmeasurable(stdout, stderr):
                continue
            if _UNTRUSTED_NAME_REACHED_OUTPUT.search(_print_region(stdout, stderr)):
                measured.add((variant, tool.name))

    lost = MEASURED - measured
    assert not lost, (
        f"tool/variant pairs that used to exercise the escaping no longer do: "
        f"{sorted(lost)}. They are now skipped as vacuous, so nothing is "
        f"checking their output any more. Restore the code path, or fix the "
        f"invocation, before editing MEASURED."
    )


# Tools whose *uncaught traceback* still renders the hostile path with a raw
# ESC, because `yaml.YAMLError` embeds the file path in its message and Python
# prints it as a crash dump. ⛔ NOT fixed by #1538 and deliberately out of its
# scope: the fix is for these tools to catch the parse error rather than for the
# output layer to escape a stack trace. Recorded here so the channel is visible
# instead of being quietly excluded by `_print_region`.
#
# Latent, related, and also NOT fixed. First measured 2026-08-27 (this fixture
# never reaches these lines, so nothing here is proven safe), then independently
# REPRODUCED by an adversarial blind review that built a dedicated fixture for
# each and confirmed the hole is real. Left unfixed only because the owner's
# ruling scoped #1538 to nineteen tools and these three are outside it:
#   policy_engine.py:545                     lines.append(f"[{tenant}]")
#   check_retire_drift.py:185,197            f"tenant '{tenant}' declares ..."
#   check_path_metadata_consistency.py:204   f"WARN path/metadata mismatch: {display}"
KNOWN_CRASH_DUMP_LEAKS = {
    "analyze_rule_pack_gaps.py", "backtest_threshold.py", "blind_spot_discovery.py",
    "maintenance_scheduler.py", "notification_tester.py", "policy_opa_bridge.py",
    "threshold_govern.py", "threshold_recommend.py", "check_routing_profiles.py",
}


def test_uncaught_traceback_channel_is_a_known_gap(hostile_dirs, tmp_path):
    """The crash-dump leak may shrink, but it must not grow unnoticed."""
    leaking = set()
    for tool in ALL_TOOLS:
        try:
            stdout, stderr, _ = _run(tool, hostile_dirs["malformed"], tmp_path / tool.stem)
        except Unmeasurable:
            continue
        i = stderr.find(b"Traceback (most recent call last):")
        if i >= 0 and b"\x1b" in stderr[i:]:
            leaking.add(tool.name)

    new = leaking - KNOWN_CRASH_DUMP_LEAKS
    assert not new, (
        f"new tool(s) leak an untrusted name through an uncaught traceback: "
        f"{sorted(new)}. Either catch the parse error at the tool's entry point, "
        f"or add it to KNOWN_CRASH_DUMP_LEAKS with a reason."
    )


def _sweep_every_tool(hostile_dirs, tmp_path) -> None:
    """Run every tool against both fixtures. Shared by the two containment tests."""
    for variant in ("malformed", "valid"):
        for tool in ALL_TOOLS:
            try:
                _run(tool, hostile_dirs[variant], tmp_path / f"{variant}_{tool.stem}")
            except Unmeasurable:
                pass


def _repo_anchored_artifact_digests() -> dict[str, str]:
    """Digest the artifacts a tool can reach WITHOUT cwd containment.

    `compile_custom_alerts` resolves its default output from ``__file__``
    (``repo / "rule-packs/rule-pack-custom-alerts.yaml"``), so running it from a
    tmp cwd does NOT stop it rewriting the shipped pack — measured: it did, on
    every run, until REPO_ANCHORED_OUTPUT redirected it.

    Named paths, not a tree scan, so this is safe under ``pytest -n auto``:
    nothing else in the suite writes these files, and a concurrent worker's
    unrelated scratch file cannot make it fire.
    """
    out = {}
    for rel in ("rule-packs/rule-pack-custom-alerts.yaml",):
        f = REPO_ROOT / rel
        out[rel] = hashlib.sha256(f.read_bytes()).hexdigest() if f.is_file() else "<absent>"
    return out


def test_repo_anchored_artifacts_are_untouched(hostile_dirs, tmp_path):
    """The narrow containment guarantee — and the only one that holds under xdist.

    Catches the regression this suite actually caused (dogfooded: dropping the
    ``--out`` redirect turns this red with the shipped rule-pack modified).
    """
    before = _repo_anchored_artifact_digests()
    _sweep_every_tool(hostile_dirs, tmp_path)
    after = _repo_anchored_artifact_digests()

    changed = [k for k in before if before[k] != after[k]]
    assert not changed, (
        f"running the --config-dir tools rewrote shipped repo artifact(s): {changed}. "
        f"A tool's default output path is anchored to the repo via __file__, so "
        f"cwd isolation does not contain it — add it to REPO_ANCHORED_OUTPUT with "
        f"an explicit redirect."
    )


@pytest.mark.skipif(
    os.environ.get("PYTEST_XDIST_WORKER") is not None,
    reason=(
        "NOT MEASURED under pytest-xdist: this assertion reads the WHOLE working "
        "tree, which is shared mutable state across workers. CI runs "
        "`pytest tests/ ... -n auto -x` (ci.yml), so a concurrent worker's "
        "transient file would surface here as a false failure and -x would redden "
        "the run. tests/lint/test_check_orphan_lint.py records the same hazard in "
        "the other direction. The narrow, path-scoped guarantee still runs in "
        "parallel: see test_repo_anchored_artifacts_are_untouched."
    ),
)
def test_the_suite_does_not_write_to_the_repo(hostile_dirs, tmp_path):
    """⛔ Serial-only backstop: these tools must not touch ANY repo file.

    Broader than the digest test — it catches a tool whose default output path
    nobody has thought of yet, including a future one, rather than only the
    artifacts named today. That breadth is exactly why it cannot run in
    parallel: "did the tree change" is a question about shared state, and under
    `-n auto` another worker is free to change it for unrelated reasons.

    Known default outputs today: `operator_generate` -> ``operator-manifests/``
    and `migrate_to_operator` -> ``migration-output/`` (both cwd-relative, so
    ``cwd=sandbox`` contains them); `compile_custom_alerts` ->
    ``rule-packs/rule-pack-custom-alerts.yaml`` resolved from ``__file__``,
    which cwd does NOT contain — hence ``REPO_ANCHORED_OUTPUT``.
    """
    def status() -> list[str]:
        r = subprocess.run(["git", "status", "--porcelain"], cwd=str(REPO_ROOT),
                           capture_output=True, text=True)
        assert r.returncode == 0, r.stderr
        return sorted(l for l in r.stdout.splitlines() if l.strip())

    before = status()
    _sweep_every_tool(hostile_dirs, tmp_path)
    after = status()

    assert after == before, (
        "running the --config-dir tools changed the repo working tree.\n"
        f"  appeared: {sorted(set(after) - set(before))}\n"
        f"  vanished: {sorted(set(before) - set(after))}\n"
        "A tool wrote to a default output path that is neither cwd-relative "
        "nor redirected — add it to REPO_ANCHORED_OUTPUT."
    )


def test_safe_label_is_defined_exactly_once():
    """⛔ One definition, not one copy per tool.

    #1538's whole point: `compile_custom_alerts._safe_log` already implemented
    this rule privately, and 18 other tools each lacked it. A second inlined
    `re.sub(r"[\\x00-\\x1f...]", ...)` anywhere under scripts/tools means the
    copy-per-tool shape has come back.
    """
    # The C0 character class as it appears in source. Matches whether it is
    # spelled inside re.compile(), re.sub(), or str.translate table building.
    pattern = re.compile(r"\[\\x00-\\x1f")
    offenders = []
    for p in sorted(TOOLS_DIR.rglob("*.py")):
        if "__pycache__" in p.parts:
            continue
        if pattern.search(p.read_text(encoding="utf-8")):
            offenders.append(str(p.relative_to(REPO_ROOT)))

    assert offenders == ["scripts/tools/_lib_io.py"], (
        f"the control-char escaping rule must exist once, in _lib_io.safe_label. "
        f"Found inline copies in: {offenders}"
    )
