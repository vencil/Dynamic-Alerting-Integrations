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

* This measures what the fixture REACHES. Several tools accept ``--config-dir``
  but never render a name from it on the path this fixture drives (see
  ``operator_check``, and the latent sites recorded in
  :func:`test_uncaught_traceback_channel_is_a_known_gap`'s docstring). Green
  here is not proof those tools escape correctly — only that nothing this
  fixture can see is broken.
* The **uncaught-traceback** channel is out of #1538's scope and is NOT fixed:
  when ``yaml.safe_load`` raises, the ``YAMLError`` message embeds the full
  file path, and Python prints it to stderr as a crash dump that no print-site
  escaping can reach. Assertions here run against the pre-traceback region and
  a separate test pins the crashing set so it cannot grow unnoticed.
"""
from __future__ import annotations

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
    """stdout plus the part of stderr BEFORE any uncaught traceback.

    The traceback is Python's crash dump, not a print site: the ``YAMLError``
    message it renders embeds the file path, and no output-layer escaping can
    intercept it. Excluding it keeps this assertion about the defect #1538
    actually fixes; :func:`test_uncaught_traceback_channel_is_a_known_gap`
    covers what is excluded so it cannot quietly expand.
    """
    i = stderr.find(b"Traceback (most recent call last):")
    return stdout + (stderr if i < 0 else stderr[:i])


def _observe(tool: Path, config_dir: str, sandbox: Path) -> bytes:
    stdout, stderr, _rc = _run(tool, config_dir, sandbox)
    why = _classify_unmeasurable(stdout, stderr)
    if why:
        raise Unmeasurable(why)
    return _print_region(stdout, stderr)


# ── the assertions ────────────────────────────────────────────────────
@pytest.mark.parametrize("tool", ALL_TOOLS, ids=[t.name for t in ALL_TOOLS])
@pytest.mark.parametrize("variant", ["malformed", "valid"])
def test_untrusted_names_cannot_forge_a_report_line(
    tool: Path, variant: str, hostile_dirs, tmp_path,
):
    """No control character from a filename or tenant name survives to stdout."""
    try:
        region = _observe(tool, hostile_dirs[variant], tmp_path)
    except Unmeasurable as exc:
        pytest.skip(f"NOT MEASURED ({variant}) — {exc.args[0]}")

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


# Tools that could not be measured, each with the reason a human can check.
# ⛔ This is an INVENTORY, not an exemption list: the test below asserts the
# measured-unmeasurable set equals this one, so a tool that starts failing to
# run shows up as a FAILURE instead of quietly becoming a skip.
KNOWN_UNMEASURABLE: dict[str, str] = {
    # Requires a positional `tenant`; this harness has no non-arbitrary value
    # to supply, and inventing one would test a different code path.
    "diagnose.py": "argparse: required positional 'tenant'",
    # Exits before reading any config: `kubernetes` is not installed here and
    # is not a test dependency.
    "da_assembler.py": "missing dependency: kubernetes Python client",
    # Requires a live `--target-url` (and `--output-dir`) — a soak runner, not
    # a reporter; there is nothing to point it at in a unit test.
    "run_chaos_soak.py": "argparse: required --target-url / --output-dir",
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


# Tools whose *uncaught traceback* still renders the hostile path with a raw
# ESC, because `yaml.YAMLError` embeds the file path in its message and Python
# prints it as a crash dump. ⛔ NOT fixed by #1538 and deliberately out of its
# scope: the fix is for these tools to catch the parse error rather than for the
# output layer to escape a stack trace. Recorded here so the channel is visible
# instead of being quietly excluded by `_print_region`.
#
# Latent, related, and also NOT fixed (measured 2026-08-27, filenames from this
# fixture never reach these lines, so nothing here is proven safe):
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


def test_the_suite_does_not_write_to_the_repo(hostile_dirs, tmp_path):
    """⛔ Running these tools must not touch tracked or untracked repo files.

    Several of them write generated artifacts to a DEFAULT path when none is
    given: `operator_generate` -> ``operator-manifests/``, `migrate_to_operator`
    -> ``migration-output/`` (both cwd-relative, contained by ``cwd=sandbox``),
    and `compile_custom_alerts` -> ``rule-packs/rule-pack-custom-alerts.yaml``
    resolved from ``__file__``, which cwd does NOT contain — hence
    ``REPO_ANCHORED_OUTPUT``. Measured: without that redirect this suite
    rewrote the shipped rule-pack on every run.

    This is the backstop for any tool the map misses, including future ones.
    """
    def status() -> list[str]:
        r = subprocess.run(["git", "status", "--porcelain"], cwd=str(REPO_ROOT),
                           capture_output=True, text=True)
        assert r.returncode == 0, r.stderr
        return sorted(l for l in r.stdout.splitlines() if l.strip())

    before = status()
    for variant in ("malformed", "valid"):
        for tool in ALL_TOOLS:
            try:
                _run(tool, hostile_dirs[variant], tmp_path / f"{variant}_{tool.stem}")
            except Unmeasurable:
                pass
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
