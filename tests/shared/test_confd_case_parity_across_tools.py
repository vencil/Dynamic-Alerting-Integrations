#!/usr/bin/env python3
"""#1588 — every conf.d reader must give the SAME answer on `upper.YAML`.

The exporter reads a mixed-case filename and serves the tenant inside it
(#1537 measured `ResolveEffective` returning `source_file="upper.YAML"`).
Nine tools did not, each having hand-written its own `endswith(".yaml")`,
and the divergence was silent in both directions:

* `offboard_tenant` reported ✅ Pre-check 通過 for a tenant whose entire
  config it could not see.
* `operator_generate` emitted 1 CRD instead of 2, dropping the tenant's
  AlertmanagerConfig with no warning.
* `deprecate_rule` reported 「未發現任何引用」 on a tree with three.
* `check_confd_schema` returned `rc=0` / "0 tenant conf.d file(s) valid"
  on a tree whose files violate the schema.

⛔ WHY THIS IS A DIFFERENTIAL TEST AND NOT AN AST GATE.

The obvious structural guard — "flag any case-sensitive YAML comparison
under `scripts/tools/`" — was prototyped and rejected on measurement, not
taste. It needs to tell a *tenant* directory (filenames chosen by a
customer) from a *repo-owned* one (`rule-packs/`, `helm/`, `.github/`),
because folding is a correctness fix in the first and pure churn in the
second. Measured on the tree at the time: 94 candidate call sites whose
receivers are `config_dir`, `self.conf_d`, `RULE_PACKS_DIR`, `helm_dir`,
`wf`, `p`, `key` … — so any such classifier is an enumerated name list
wearing derivation's clothes, which is the exact anti-pattern
`test_confd_enumeration_contract.py`'s own docstring warns about.
Anchoring instead on the argparse declaration was tried and misses
`gitops_check` and `custom_alerts/loader` outright (neither takes a CLI
flag; both receive the directory as a function parameter) — and the
second is v2.9.0's shipped tenant self-service path, i.e. the gate would
have been silent on the highest-value target.

So this file asks the behavioural question instead: run the tool twice
against two trees whose CONTENT is byte-identical and whose FILENAMES
differ only in case, and require the same answer. Nothing has to classify
a directory, because only trees this file builds are ever passed in.

⛔ "COULD NOT MEASURE" AND "MEASURED CLEAN" ARE DIFFERENT ANSWERS, and a
third state matters just as much here: a tool that ignores the config dir
entirely would "agree" trivially. Every tool therefore also runs against
a THIRD tree with the tenant files removed; if that does not change its
output, the comparison had no discriminating power and the tool is
skipped with that reason rather than counted as a pass. Both skip sets
are pinned below, so a tool going dark is a FAILURE rather than a silent
downgrade.

⚠️ SCOPE, so nobody over-reads a green run:

* Only the CASE axis. Readers that glob `*.yaml` accept it and not
  `*.yml`, so they do not see `db-b.yml` at all while the exporter does.
  That is a real divergence on the extension-SPELLING axis; it is
  deliberately NOT fixed here (widening them is a behaviour change that
  must not ride along inside a case fix) and is filed separately (#1603).
  `custom_alerts/loader` was on that list when this file was written and
  no longer is — #1603 widened it, and the parity it now has is pinned by
  `tests/dx/test_compile_custom_alerts.py`, not by this file. Ask
  `_lib_confd` for the current set rather than trusting a count here.
* Only tools reachable through a conf.d CLI flag, plus the two
  library-shaped readers covered by direct calls at the bottom of this
  file.
* The fixture is flat. Nested-directory behaviour is
  `test_confd_enumeration_contract.py`'s axis, not this one.
"""

from __future__ import annotations

import ast
import os
import pathlib
import re
import subprocess
import sys

import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]
TOOLS_DIR = REPO / "scripts" / "tools"

# Every spelling a Vibe tool uses for "the tenant conf.d directory".
CONFD_FLAGS = ("--config-dir", "--conf-d", "--confd", "--config-base",
               "--source-dir")


# ── population ────────────────────────────────────────────────────────
def _confd_flag(path: pathlib.Path) -> str | None:
    """Which flag this tool takes a conf.d through, from its argparse AST.

    ⛔ Derived from the `add_argument` calls rather than from a substring
    search, because the two are not the same question: `"--config-dir"`
    appears in help text, comments and docstrings of tools that never
    accept it, and a tool that accepts `--conf-d` (`describe_tenant`)
    contains neither spelling of the other.

    ⚠️ It reads LITERAL declarations only. A tool that passes the flag
    name as a variable, an f-string or `*flags` is invisible to this walk
    — blind review measured that with a real fake tool, and the earlier
    wording here ("what makes tool #32 covered on the day it lands")
    claimed a guarantee this does not provide. That hole is now closed by
    `test_no_tool_declares_flags_the_population_walk_cannot_read`, which
    is what actually makes the claim true; this function alone does not.
    """
    try:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
    except SyntaxError:
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


def collect_confd_tools() -> list[tuple[pathlib.Path, str]]:
    out: list[tuple[pathlib.Path, str]] = []
    for sub in ("ops", "dx", "lint"):
        d = TOOLS_DIR / sub
        if not d.is_dir():
            continue
        for p in sorted(d.glob("*.py")):
            if p.name.startswith("_") or p.name == "__init__.py":
                continue
            flag = _confd_flag(p)
            if flag:
                out.append((p, flag))
    return out


ALL_TOOLS = collect_confd_tools()

# Arguments a tool needs before it will look at the conf.d at all.
# Anything not listed that still refuses to run is SKIPPED with the
# reason, never silently passed.
EXTRA_ARGS: dict[str, list[str]] = {
    "deprecate_rule.py": ["pg_connections"],
    "offboard_tenant.py": ["alpha"],
    "diagnose.py": ["alpha", "--show-inheritance"],
    "describe_tenant.py": ["--all"],
}

# Tools whose DEFAULT output path is anchored to the repo via `__file__`
# rather than to the cwd, so a tmp cwd is not enough to contain the write.
REPO_ANCHORED_OUTPUT: dict[str, str] = {
    "compile_custom_alerts.py": "--out",
}

# The fixture names, mapped to NEUTRAL sentinels rather than to each
# other's lower-case spelling.
#
# ⛔ Folding upper -> lower was the first version and blind review measured
# what it cost: a fix whose whole point is "report the name you actually
# read" becomes invisible, because both spellings collapse to the same
# string before comparison. `diagnose`'s `defaults_source` change — the one
# carrying a ⛔ comment about sending an operator to edit a file that does
# not exist — was a SECOND silently-green dogfood cell for exactly this
# reason, and the file's own disclosure said there was only one.
#
# A sentinel keeps the two runs comparable (the names legitimately differ)
# while preserving the distinction between "printed the carrier" and
# "printed a hardcoded canonical name".
_SENTINELS = (("_DEFAULTS.YAML", "<DEFAULTS>"), ("_defaults.yaml", "<DEFAULTS>"),
              ("_DEFAULTS.YML", "<DEFAULTS2>"), ("_defaults.yml", "<DEFAULTS2>"),
              ("alpha.YAML", "<ALPHA>"), ("alpha.yaml", "<ALPHA>"),
              ("beta.YML", "<BETA>"), ("beta.yml", "<BETA>"))


# ── fixtures ──────────────────────────────────────────────────────────
def _write_tree(root: pathlib.Path, *, upper: bool, tenants: bool) -> None:
    """One conf.d. `upper` flips only the NAMES; bodies never change."""
    root.mkdir(parents=True, exist_ok=True)
    names = {"defaults": "_DEFAULTS.YAML" if upper else "_defaults.yaml",
             "alpha": "alpha.YAML" if upper else "alpha.yaml",
             "beta": "beta.YML" if upper else "beta.yml"}
    # ⛔ `_policies` is here because its absence made a real divergence
    # invisible. `validate_config` resolves the policy document by name,
    # and with a defaults file carrying no `_policies` block BOTH runs
    # printed "No _policies defined — skipped" and the tool was counted
    # as agreeing. Blind review measured the truth: lower `[FAIL]
    # policy_dsl`, upper `[PASS] ... skipped`. Same empty-vs-empty shape
    # as the loader fixture before it grew `_custom_alerts`.
    (root / names["defaults"]).write_text(
        "defaults:\n"
        "  pg_connections: 80\n"
        "_policies:\n"
        "  - name: p1\n"
        "    when: \"tenant.pg_connections > 100\"\n"
        "    then: deny\n",
        encoding="utf-8")
    if tenants:
        (root / names["alpha"]).write_text(
            "tenants:\n  alpha:\n    pg_connections: 90\n", encoding="utf-8")
        (root / names["beta"]).write_text(
            "tenants:\n  beta:\n    pg_connections: 91\n", encoding="utf-8")


@pytest.fixture(scope="session")
def trees(tmp_path_factory) -> dict[str, pathlib.Path]:
    """Three trees: lower, UPPER, and lower-with-no-tenants.

    The third is the sensitivity control. ⛔ It removes the TENANT
    carriers rather than one arbitrary file: an earlier version dropped
    `beta.yml` alone and wrongly cleared four tools as "no discriminating
    power", because a reader that globs `*.yaml` cannot see a `.yml` file
    in ANY tree and so was never going to react to its absence.
    """
    base = tmp_path_factory.mktemp("confd_case_parity")
    out = {}
    for name, upper, tenants in (("lower", False, True),
                                 ("upper", True, True),
                                 ("notenants", False, False)):
        d = base / name
        _write_tree(d, upper=upper, tenants=tenants)
        out[name] = d
    return out


# ── running one tool ──────────────────────────────────────────────────
def _run(tool: pathlib.Path, flag: str, config_dir: pathlib.Path,
         sandbox: pathlib.Path) -> tuple[str, str, int] | None:
    args = [sys.executable, "-X", "utf8", str(tool), flag, str(config_dir)]
    args += EXTRA_ARGS.get(tool.name, [])
    if tool.name in REPO_ANCHORED_OUTPUT:
        args += [REPO_ANCHORED_OUTPUT[tool.name], str(sandbox / "out.yaml")]
    sandbox.mkdir(parents=True, exist_ok=True)
    try:
        r = subprocess.run(args, capture_output=True, timeout=120,
                           cwd=str(sandbox),
                           env=dict(os.environ, PYTHONIOENCODING="utf-8"))
    except subprocess.TimeoutExpired:
        return None
    return (r.stdout.decode("utf-8", "replace"),
            r.stderr.decode("utf-8", "replace"), r.returncode)


_TIMESTAMP = re.compile(
    r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}[.,]?\d*(?:[+-]\d{2}:?\d{2}|Z)?")
_DURATION = re.compile(r"\d+\.\d+s\b")


def _normalise(text: str, config_dir: pathlib.Path,
               sandbox: pathlib.Path) -> str:
    """Erase what legitimately differs between the two runs.

    The two runs use different directories and different filenames, so a
    raw comparison would be red for every tool that prints a path. What
    must NOT be erased is any difference in what the tool FOUND — counts,
    verdicts, exit codes, which tenants appeared.

    ⛔ Timestamps and durations are erased too. Both produced a false red
    while this file was being written (`da_assembler` differed only in the
    millisecond field of a log line), and a gate that cries wolf gets
    muted, which costs more than the defect it was watching for.
    """
    out = text.replace(str(config_dir), "<CONFDIR>")
    out = out.replace(str(sandbox), "<SANDBOX>")
    for name, sentinel in _SENTINELS:
        out = out.replace(name, sentinel)
    out = _TIMESTAMP.sub("<T>", out)
    return _DURATION.sub("<D>", out)


def _why_unmeasurable(stdout: str, stderr: str) -> str | None:
    blob = stdout + stderr
    m = re.search(r"error: (the following arguments are required[^\n]*)", blob)
    if m:
        return f"argparse: {m.group(1)}"
    m = re.search(r"error: (unrecognized arguments[^\n]*)", blob)
    if m:
        return f"argparse: {m.group(1)}"
    m = re.search(r"usage: [^\n]*\n(?:.*\n)*?\S+: error: ([^\n]*)", blob)
    if m:
        return f"argparse: {m.group(1)}"
    m = re.search(r"(ModuleNotFoundError|ImportError): ([^\n]*)", blob)
    if m:
        return m.group(0)
    m = re.search(r"(ConnectionError|URLError|Max retries|Connection refused)",
                  blob)
    if m:
        return f"external precondition: {m.group(1)}"
    return None


class _Outcome:
    __slots__ = ("skip_reason", "insensitive", "lower", "upper", "upper_raw")

    def __init__(self, skip_reason=None, insensitive=False,
                 lower="", upper="", upper_raw=""):
        self.skip_reason = skip_reason
        self.insensitive = insensitive
        self.lower = lower
        self.upper = upper
        # ⛔ PRE-normalisation. `_normalise` must collapse the two spellings
        # to make the runs comparable at all, which structurally erases
        # "did the tool print the name it actually read?" — blind review
        # measured a second silently-green dogfood cell for exactly that
        # reason. That property therefore needs its own assertion against
        # the raw bytes, below; no amount of cleverness in `_normalise`
        # can recover it.
        self.upper_raw = upper_raw


def _observe(tool: pathlib.Path, flag: str,
             trees: dict[str, pathlib.Path],
             tmp: pathlib.Path) -> _Outcome:
    runs = {}
    for key in ("lower", "upper", "notenants"):
        sandbox = tmp / key
        r = _run(tool, flag, trees[key], sandbox)
        if r is None:
            return _Outcome(skip_reason="timed out after 120s")
        runs[key] = (r, sandbox)

    for key in ("lower", "upper"):
        (so, se, _), _ = runs[key]
        why = _why_unmeasurable(so, se)
        if why:
            return _Outcome(skip_reason=why)

    norm = {}
    for key, ((so, se, rc), sandbox) in runs.items():
        norm[key] = (_normalise(so + se, trees[key], sandbox), rc)

    if norm["lower"] == norm["notenants"]:
        return _Outcome(
            insensitive=True,
            skip_reason=(
                f"ran (rc={norm['lower'][1]}) but its output is identical "
                f"with the tenant files REMOVED, so comparing the two "
                f"casings here proves nothing about this tool"),
        )
    (so_u, se_u, _), _ = runs["upper"]
    return _Outcome(lower=norm["lower"], upper=norm["upper"],
                    upper_raw=so_u + se_u)


# ── the parity assertion ──────────────────────────────────────────────
@pytest.mark.parametrize(
    "tool,flag", ALL_TOOLS, ids=[p.name for p, _ in ALL_TOOLS])
def test_tool_reads_upper_case_names_the_same(
        tool: pathlib.Path, flag: str, trees, tmp_path: pathlib.Path) -> None:
    outcome = _observe(tool, flag, trees, tmp_path)
    if outcome.skip_reason:
        pytest.skip(f"NOT MEASURED — {outcome.skip_reason}")
    assert outcome.lower == outcome.upper, (
        f"{tool.relative_to(REPO).as_posix()} gives a DIFFERENT answer when "
        f"the same conf.d is spelled `.YAML` instead of `.yaml`.\n"
        f"The exporter reads both and serves the tenants in them "
        f"(#1537), so whatever this tool just reported about the "
        f"upper-case tree is a claim about a config nobody runs.\n"
        f"  lower rc={outcome.lower[1]}  upper rc={outcome.upper[1]}\n"
        f"Fix by asking `_lib_confd`'s shared name predicates "
        f"(`has_yaml_extension` / `is_defaults_name` / `config_stem`) "
        f"instead of comparing the extension in this tool. ⛔ Keep the "
        f"tool's own recursion and its own suffix set — changing those "
        f"is a separate behaviour change. See issue #1588."
    )


# Tools measured printing a canonical name they did not read. ⛔ Pinned
# with reasons rather than fixed here, and the check above asserts each
# entry is still WRONG — a stale exemption fails instead of rotting.
#
# Both need a path plumbed into a function that does not have one today,
# and both hardcode their string on `18f18a0` as well, so neither is a
# regression from this change; `diagnose`'s only became reachable because
# this change let it read the tree at all. Filed separately rather than
# widened into a case-folding PR.
KNOWN_HARDCODED_NAMES: dict[str, str] = {
    # diagnose.py:408 builds the tenant layer's source as
    # f"{tenant}.yaml" from the tenant id. The carrier path is known one
    # function earlier (the `iter_config_files` loop) but is not carried
    # into `tenant_overrides`.
    "diagnose.py": "tenant layer source is built from the tenant id, not the carrier",
    # explain_route.py:63,103 hardcode "_defaults.yaml / _routing_defaults
    # key". `explain_tenant_routing(parsed, tenant)` receives no config dir
    # at all, so resolving the real carrier is a signature change.
    "explain_route.py": "routing layer sources are literal strings; the function takes no config dir",
}

_LOWER_ONLY_NAMES = ("_defaults.yaml", "_defaults.yml",
                     "alpha.yaml", "beta.yml")


@pytest.mark.parametrize(
    "tool,flag", ALL_TOOLS, ids=[p.name for p, _ in ALL_TOOLS])
def test_tool_does_not_name_a_file_that_is_not_there(
        tool: pathlib.Path, flag: str, trees, tmp_path: pathlib.Path) -> None:
    """On the upper-case tree, no lower-case fixture name may be printed.

    ⛔ This is the class the A/B comparison structurally cannot see. Both
    runs are normalised before comparison — they have to be, the filenames
    legitimately differ — so a tool that reads `_DEFAULTS.YAML` and then
    prints the hardcoded string `_defaults.yaml` compares EQUAL to the
    lower-case run and passes. Blind review found `diagnose`'s
    `defaults_source` fix sitting in exactly that blind spot, green,
    while the file claimed only one cell was.

    The property is easy to state against raw bytes: that tree contains no
    lower-case file, so naming one sends the operator to edit something
    that does not exist.
    """
    outcome = _observe(tool, flag, trees, tmp_path)
    if outcome.skip_reason:
        pytest.skip(f"NOT MEASURED — {outcome.skip_reason}")
    # ⛔ Whole path components, not substrings. The first version matched
    # `alpha.yaml` inside `da-tenant-alpha.yaml` — a manifest
    # `operator_generate` had just written, named after the tenant id and
    # nothing to do with a conf.d carrier. A gate that cries wolf gets
    # muted, so the token boundary is load-bearing.
    tokens = set(re.split(r"[^0-9A-Za-z._-]+", outcome.upper_raw))
    named = sorted(tokens & set(_LOWER_ONLY_NAMES))
    if tool.name in KNOWN_HARDCODED_NAMES:
        assert named, (
            f"{tool.name} is pinned in KNOWN_HARDCODED_NAMES but printed no "
            f"canonical name — the defect was fixed, so DELETE its entry "
            f"rather than leaving a stale exemption behind."
        )
        pytest.skip(f"KNOWN — {KNOWN_HARDCODED_NAMES[tool.name]}")
    assert not named, (
        f"{tool.relative_to(REPO).as_posix()} printed {named} while reading "
        f"a conf.d that contains no such file — the carriers there are "
        f"spelled `.YAML`/`.YML`.\n"
        f"An operator following that output edits a file that does not "
        f"exist, and the tool's own answer came from a different one.\n"
        f"Report the name actually read (`path.name`) instead of a "
        f"hardcoded canonical spelling. See issue #1588."
    )


def test_population_discovery_did_not_collapse() -> None:
    """Tripwire: a gate that discovers nothing passes vacuously.

    The argparse walk is the one thing every case above depends on, and
    it fails silently — a refactor that routes flags through a helper
    would leave `add_argument` unseen and turn this whole file green
    while measuring zero tools.
    """
    assert len(ALL_TOOLS) >= 25, (
        f"only {len(ALL_TOOLS)} conf.d tools discovered — check that "
        f"_confd_flag still recognises how tools declare their config-dir "
        f"argument"
    )


def test_no_tool_declares_flags_the_population_walk_cannot_read() -> None:
    """No `add_argument` may hide its flag name behind a non-literal.

    ⛔ This is the hole blind review found, closed at its root rather than
    patched per-tool. `_confd_flag` reads `add_argument("--config-dir")`;
    it cannot read `add_argument(FLAG)`, `add_argument(f"--{x}")` or
    `add_argument(*flags)`. A tool written that way does not land in a
    skip set where the pinned tables above would notice — it never enters
    `ALL_TOOLS` at all, so nothing here parametrizes over it and the
    floor below stays satisfied. Measured: a real conf.d reader declaring
    `p.add_argument(CONFD_FLAG)` produced a test run byte-identical to
    the baseline.

    ⛔ Deliberately NOT a text search for the flag names. That variant was
    measured first and reddens `check_doc_datools_cmds.py`, which merely
    discusses `--conf-d` in three comments — a gate that cries wolf gets
    muted. This asks the derivable question instead ("is any flag name
    unreadable by a static walk?"), whose answer today is: none, in any
    of the 3 tool directories.
    """
    offenders: list[str] = []
    for sub in ("ops", "dx", "lint"):
        d = TOOLS_DIR / sub
        if not d.is_dir():
            continue
        for path in sorted(d.glob("*.py")):
            if path.name.startswith("_") or path.name == "__init__.py":
                continue
            try:
                tree = ast.parse(path.read_text(encoding="utf-8",
                                                errors="replace"))
            except SyntaxError:
                continue
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                fn = node.func
                if not (isinstance(fn, ast.Attribute)
                        and fn.attr == "add_argument"):
                    continue
                for arg in node.args:
                    if not (isinstance(arg, ast.Constant)
                            and isinstance(arg.value, str)):
                        offenders.append(
                            f"{path.relative_to(REPO).as_posix()}:"
                            f"{node.lineno} ({type(arg).__name__})")
    assert not offenders, (
        f"these `add_argument` calls name their flag with something this "
        f"file's population walk cannot read statically:\n"
        f"  " + "\n  ".join(offenders) + "\n"
        f"⛔ A conf.d reader declared this way is invisible to "
        f"`collect_confd_tools`, and invisible is worse than skipped: the "
        f"pinned skip sets below only notice tools that ARE in the "
        f"population. Either spell the flag as a literal, or extend "
        f"`_confd_flag` to resolve this form and delete the entry here. "
        f"See issue #1588."
    )


# ── the two skip sets, pinned ─────────────────────────────────────────
#
# ⛔ These exist so "this tool stopped being measurable" is a FAILURE.
# Without them a tool can drift from "measured clean" to "skipped" and
# the suite stays green while coverage silently shrinks — the exact
# ratchet #1538 had to close by hand.

KNOWN_UNMEASURABLE: dict[str, str] = {
    # Needs a live Prometheus; refuses before reading the conf.d.
    "blind_spot_discovery.py": "external precondition",
    "threshold_recommend.py": "external precondition",
    # Requires arguments this file deliberately does not fabricate,
    # because inventing them would exercise a different code path than
    # the one an operator runs.
    "migrate_to_operator.py": "argparse: required argument",
    "run_chaos_soak.py": "argparse: required argument",
}

# Tools that RUN but whose output does not change when the tenant files
# are removed. ⚠️ This is not a clean bill of health — it says the A/B
# comparison cannot see them, so they may or may not read filenames
# correctly. Several are genuinely conf.d-insensitive (they read rule
# packs, or need a fixture this file does not build, e.g.
# `check_routing_profiles` needs `_routing_profiles.yaml`).
KNOWN_INSENSITIVE: set[str] = {
    "analyze_rule_pack_gaps.py",
    "backtest_threshold.py",
    "check_retire_drift.py",
    "check_routing_profiles.py",
    "compile_custom_alerts.py",
    "config_history.py",
    "da_assembler.py",
    "generate_tenant_mapping_rules.py",
    "maintenance_scheduler.py",
    "notification_tester.py",
    "operator_check.py",
    "tenant_verify.py",
    "threshold_govern.py",
}

# ⛔ `policy_engine.py` and `policy_opa_bridge.py` used to sit in the
# set above and do NOT belong. Both left the moment the fixture's
# defaults carrier grew a `_policies` block: they were always
# sensitive to the conf.d, the fixture just never gave them anything
# to be sensitive ABOUT, so `lower == notenants` and they were filed
# as having no discriminating power. `policy_engine` then turned out
# to be genuinely DIVERGENT and needed fixing; `policy_opa_bridge`
# measured clean. Blind review found the same emptiness hiding a real
# `validate_config` divergence, and this file had already been caught
# by it once with `custom_alerts/loader`.
#
# ⚠️ Read this list as a list of QUESTIONS TO RE-ASK, not answers. An
# entry means "this fixture cannot see the tool", never "the tool is
# fine" — three of them have already moved out by enriching the
# fixture rather than by changing any tool.


@pytest.fixture(scope="session")
def _all_outcomes(trees, tmp_path_factory) -> dict[str, _Outcome]:
    base = tmp_path_factory.mktemp("confd_case_parity_sweep")
    return {
        tool.name: _observe(tool, flag, trees, base / tool.name)
        for tool, flag in ALL_TOOLS
    }


def test_the_unmeasurable_set_is_named(_all_outcomes) -> None:
    actual = {
        name for name, o in _all_outcomes.items()
        if o.skip_reason and not o.insensitive
    }
    assert actual == set(KNOWN_UNMEASURABLE), (
        f"the set of tools that cannot be measured changed.\n"
        f"  newly unmeasurable: {sorted(actual - set(KNOWN_UNMEASURABLE))}\n"
        f"  now measurable    : {sorted(set(KNOWN_UNMEASURABLE) - actual)}\n"
        f"A tool moving INTO this set is coverage lost — fix the "
        f"invocation, or record it here with the reason. A tool moving "
        f"OUT is coverage gained: delete its entry."
    )


def test_the_insensitive_set_is_named(_all_outcomes) -> None:
    actual = {name for name, o in _all_outcomes.items() if o.insensitive}
    assert actual == KNOWN_INSENSITIVE, (
        f"the set of tools with no discriminating power changed.\n"
        f"  newly blind : {sorted(actual - KNOWN_INSENSITIVE)}\n"
        f"  now visible : {sorted(KNOWN_INSENSITIVE - actual)}\n"
        f"⛔ A tool arriving here has stopped reacting to the conf.d at "
        f"all, which is a bigger problem than the casing question."
    )


# ── the two library-shaped readers ────────────────────────────────────
#
# Neither takes a CLI flag, so neither is in ALL_TOOLS. They are called
# directly instead — and they are not an afterthought:
# `custom_alerts/loader` is v2.9.0's shipped tenant self-service path, so
# it is the one reader here a tenant can aim a filename at.

sys.path.insert(0, str(TOOLS_DIR))


def _pair_of_trees(tmp_path: pathlib.Path) -> tuple[pathlib.Path,
                                                    pathlib.Path]:
    lower, upper = tmp_path / "lower", tmp_path / "upper"
    _write_tree(lower, upper=False, tenants=True)
    _write_tree(upper, upper=True, tenants=True)
    return lower, upper


def _write_custom_alerts_tree(root: pathlib.Path, *, upper: bool) -> None:
    """A conf.d the LOADER can see something in.

    ⛔ The generic fixture above is not enough here and that was measured,
    not assumed: `collect_instances` only emits a triple for a tenant
    carrying `_custom_alerts`, so against a plain tenant tree it returns
    `[]` for BOTH casings and the comparison passes while proving
    nothing. Reverting the loader's fix left this test GREEN until this
    fixture existed — the same "assertion with no detection power" shape
    this whole file is about, committed by the file itself.
    """
    root.mkdir(parents=True, exist_ok=True)
    defaults = "_DEFAULTS.YAML" if upper else "_defaults.yaml"
    tenant = "alpha.YAML" if upper else "alpha.yaml"
    # ⛔ The defaults carrier declares a PLATFORM-level `_custom_alerts`
    # list on purpose. Without it, `_dir_defaults_alerts` contributes
    # nothing, and reverting the loader's defaults matching stayed GREEN —
    # measured. That silence was hiding a second unfixed site: the
    # inheritance walk matched `"_defaults.yaml" in files` literally, so a
    # `_DEFAULTS.YAML` declaring platform policy vanished for every tenant
    # beneath it.
    (root / defaults).write_text(
        "defaults:\n"
        "  pg_connections: 80\n"
        "_custom_alerts:\n"
        "  - recipe: platform_baseline\n"
        "    params:\n"
        "      threshold: 50\n",
        encoding="utf-8")
    # A SECOND carrier spelling in the same directory. ⛔ Without it,
    # `_dir_defaults_alerts` assigning instead of accumulating is invisible:
    # blind review measured a `critical` rule declared in `_defaults.yaml`
    # being discarded wholesale because `_defaults.yml` sat beside it, and
    # a one-carrier fixture called that green. Both spellings are chain
    # carriers to the exporter, so both must contribute.
    second = "_DEFAULTS.YML" if upper else "_defaults.yml"
    (root / second).write_text(
        "_custom_alerts:\n"
        "  - recipe: platform_second_carrier\n"
        "    params:\n"
        "      threshold: 60\n",
        encoding="utf-8")
    (root / tenant).write_text(
        "tenants:\n"
        "  alpha:\n"
        "    pg_connections: 90\n"
        "    _custom_alerts:\n"
        "      - recipe: pg_connections_high\n"
        "        params:\n"
        "          threshold: 95\n",
        encoding="utf-8")


def test_custom_alerts_loader_sees_both_casings(
        tmp_path: pathlib.Path) -> None:
    """v2.9.0's tenant self-service path (ADR-024 capability B)."""
    from dx.custom_alerts import loader  # noqa: PLC0415

    lower, upper = tmp_path / "lower", tmp_path / "upper"
    _write_custom_alerts_tree(lower, upper=False)
    _write_custom_alerts_tree(upper, upper=True)

    # `collect_instances` carries the enumeration; its third tuple field
    # is the origin filename, which is exactly what the casing changes.
    inst_lower, err_lower = loader.collect_instances(lower)
    inst_upper, err_upper = loader.collect_instances(upper)

    # ⛔ Vacuity guard FIRST. An empty-vs-empty comparison is not a pass.
    assert inst_lower, (
        "the lower-case fixture produced no instances at all, so the "
        "comparison below cannot detect anything — fix the fixture "
        "before trusting this test"
    )
    # ⛔ Vacuity guard, part two: BOTH platform carriers must have
    # contributed. A single-carrier result would make the accumulate-vs-
    # overwrite bug in `_dir_defaults_alerts` unobservable, which is
    # exactly how it survived its first review round.
    recipes = {i[1].get("recipe") for i in inst_lower if isinstance(i[1], dict)}
    assert {"platform_baseline", "platform_second_carrier"} <= recipes, (
        f"both `_defaults.yaml` and `_defaults.yml` are chain carriers and "
        f"must both contribute their `_custom_alerts`; got {sorted(recipes)}"
    )

    def _summary(instances):
        # tenant + instance body; the origin string legitimately carries
        # the filename, so only its casing is folded away.
        return sorted((t, str(i[2]).lower(), i[3], repr(i[1]))
                      for t, i in ((x[0], x) for x in instances))

    assert _summary(inst_lower) == _summary(inst_upper), (
        f"custom_alerts/loader collects a different set of instances when "
        f"the names are upper-cased. This is the shipped tenant "
        f"self-service path — the filename is chosen by the tenant.\n"
        f"  lower: {_summary(inst_lower)}\n"
        f"  upper: {_summary(inst_upper)}"
    )
    assert err_lower == err_upper, (
        f"loader reports different file errors across casings:\n"
        f"  lower: {err_lower}\n  upper: {err_upper}"
    )


def _upper_only_tree(root: pathlib.Path, *, defaults_body: str,
                     with_tenant: bool = True) -> None:
    """A conf.d whose carriers are ONLY spelled `.YAML` / `.YML`."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "_DEFAULTS.YAML").write_text(defaults_body, encoding="utf-8")
    if with_tenant:
        (root / "alpha.YAML").write_text(
            "tenants:\n  alpha:\n    pg_connections: 90\n", encoding="utf-8")


def test_error_messages_name_the_carrier_they_resolved(
        tmp_path: pathlib.Path) -> None:
    """Branches the A/B sweep cannot reach, asserted directly.

    ⛔ Three separate blind spots put these messages beyond
    `test_tool_does_not_name_a_file_that_is_not_there`, and each is worth
    stating because the same shapes will recur:

    1. `gitops_check` takes no CLI flag, so it is not in `ALL_TOOLS` at
       all — only the direct-call test below covers it, and that one
       compares lower against upper. Both runs printed the SAME wrong name,
       so they agreed and it passed.
    2. `policy_engine` IS in the sweep, but the shared fixture's defaults
       carrier grew a `_policies` block (added so `validate_config`'s real
       divergence would stop being invisible) — which makes its
       "no policy rules found" branch unreachable. ⚠️ Enriching a fixture
       to expose one defect hid another.
    3. The invalid-YAML branch needs a malformed body the sweep never
       writes.

    External review found all three; this test is what would have.
    """
    from ops import gitops_check  # noqa: PLC0415

    # (1) invalid defaults: the message used to say `_defaults.yaml is
    #     invalid` while the parser error it embeds quoted the path of
    #     `_DEFAULTS.YAML` — one line naming two different files.
    bad = tmp_path / "bad"
    _upper_only_tree(bad, defaults_body="defaults: [unclosed\n")
    res = gitops_check.check_local(str(bad))
    assert res.status == "fail" and "_DEFAULTS.YAML" in res.message, (
        f"gitops_check must name the carrier it parsed; got {res.message!r}"
    )
    assert "_defaults.yaml is invalid" not in res.message, (
        "the message still names the canonical spelling for a file that is "
        "not in this tree"
    )

    # (2) missing defaults: with NOTHING to resolve, falling back to the
    #     canonical name is correct — the operator needs a name they can
    #     create. This pins that the fix did not overshoot.
    empty = tmp_path / "empty"
    empty.mkdir()
    res2 = gitops_check.check_local(str(empty))
    assert "_defaults.yaml" in res2.message, (
        f"on an empty tree the canonical name is the useful one; "
        f"got {res2.message!r}"
    )


def test_policy_engine_no_policy_hint_names_the_carrier(
        tmp_path: pathlib.Path) -> None:
    """The `_policies`-less branch the shared fixture can no longer reach."""
    tree = tmp_path / "nopolicies"
    _upper_only_tree(tree, defaults_body="defaults:\n  pg_connections: 80\n")
    tool = TOOLS_DIR / "ops" / "policy_engine.py"
    r = subprocess.run(
        [sys.executable, "-X", "utf8", str(tool), "--config-dir", str(tree)],
        capture_output=True, timeout=120, cwd=str(tmp_path),
        env=dict(os.environ, PYTHONIOENCODING="utf-8"))
    out = r.stdout.decode("utf-8", "replace") + r.stderr.decode("utf-8",
                                                                "replace")
    assert "No policy rules found" in out, (
        f"fixture did not reach the no-policy branch — the assertion below "
        f"would be vacuous. Output:\n{out[:400]}"
    )
    assert "_DEFAULTS.YAML" in out, (
        f"the hint must name the carrier this run resolved; got:\n{out[:400]}"
    )


def test_gitops_check_local_sees_both_casings(tmp_path: pathlib.Path) -> None:
    from ops import gitops_check  # noqa: PLC0415

    lower, upper = _pair_of_trees(tmp_path)
    a = gitops_check.check_local(str(lower))
    b = gitops_check.check_local(str(upper))

    def _comparable(result, root: pathlib.Path):
        """Everything the check FOUND, with the tree's own path removed.

        ⛔ `details["directory"]` echoes the input path, which differs
        between the two trees by construction — comparing it would make
        this test red for a reason that has nothing to do with casing.
        """
        details = {k: v for k, v in (result.details or {}).items()
                   if k != "directory"}
        return result.status, result.message, details

    assert _comparable(a, lower) == _comparable(b, upper), (
        f"gitops_check.check_local disagrees across casings:\n"
        f"  lower: {_comparable(a, lower)}\n"
        f"  upper: {_comparable(b, upper)}\n"
        f"It reported a PASS on a tree whose tenant files it could not see."
    )
    assert a.details and a.details.get("tenant_files"), (
        "fixture produced no tenant files for gitops_check; the comparison "
        "would be vacuous"
    )


# ---------------------------------------------------------------------------
# #1607 — the THIRD axis: what `is_file()` drops must be NAMED
# ---------------------------------------------------------------------------
#
# ⛔ This axis is neither the extension axis #1588 is about nor the recursion
# axis it deliberately leaves alone. It appeared because `glob("*.yaml")` is
# case-sensitive on Linux and had to become `iterdir()` + a shared predicate —
# and `iterdir()` yields directories, so a filter was needed. The filter is
# the CORRECTNESS half; these tests are the half that keeps the signal.
#
# Measured before the filter existed: `operator_generate` emitted 20 CRDs on
# the tree below, two of them AlertmanagerConfigs for tenants `notes` and
# `broken` invented from a directory and a dangling symlink. So CodeRabbit's
# prescribed fix — "remove the filter and let the existing error handling
# record them" — is wrong at that site: `discover_tenant_configs` never opens
# the file, so there IS no error handling for the entry to fall into. The
# right answer is asymmetric, and these tests pin the half that is shared:
# every reader must NAME both entries.

_UNUSABLE_NAMES = ("broken.yaml", "notes.yaml")


def _unusable_tree(root: pathlib.Path) -> pathlib.Path:
    """A conf.d with two entries the operator called configuration and that
    no reader can read: a DIRECTORY named `notes.yaml/` (an interrupted
    mkdir, a ConfigMap projected as a dir) and a broken symlink."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "_defaults.yaml").write_text(
        "defaults:\n  pg_connections: 80\n", encoding="utf-8")
    (root / "alpha.yaml").write_text(
        "tenants:\n  alpha:\n    pg_connections: 90\n", encoding="utf-8")
    (root / "notes.yaml").mkdir()
    (root / "broken.yaml").symlink_to(root / "no-such-target.yaml")
    return root


def _argv_for(tool_rel: str, tree: pathlib.Path,
              out: pathlib.Path) -> list[str]:
    """Each tool's own way of being pointed at a conf.d."""
    return {
        "ops/operator_generate.py": ["--config-dir", str(tree),
                                     "--output-dir", str(out)],
        "ops/deprecate_rule.py": ["pg_connections", "--config-dir", str(tree)],
        "ops/offboard_tenant.py": ["alpha", "--config-dir", str(tree)],
        "dx/generate_tenant_metadata.py": ["--config-dir", str(tree),
                                           "--dry-run"],
        "dx/describe_tenant.py": ["--all", "--conf-d", str(tree)],
        "lint/check_path_metadata_consistency.py": ["--config-dir",
                                                    str(tree)],
    }[tool_rel]


# tool -> (stream the warning belongs on, a landmark proving the tool still
# did its real work after warning). ⛔ The channel is part of the contract,
# not an implementation detail: five of these have a machine-readable stdout
# (`--json`, or the lint's `path:0: warning:` annotations) that a warning on
# stdout makes unparseable. `offboard_tenant`'s stdout IS its human report —
# same channel as its own "無法讀取" warning — so stdout is correct there.
_WARNING_CONTRACT = {
    "ops/operator_generate.py": ("stderr", "CRDs"),
    "ops/deprecate_rule.py": ("stderr", "Processing:"),
    "ops/offboard_tenant.py": ("stdout", "Pre-check"),
    "dx/generate_tenant_metadata.py": ("stderr", "alpha"),
    "dx/describe_tenant.py": ("stderr", "alpha"),
    "lint/check_path_metadata_consistency.py": ("stderr", "tenant file(s)"),
}


@pytest.mark.parametrize("tool_rel", sorted(_WARNING_CONTRACT))
def test_reader_names_the_entries_it_could_not_read(
        tool_rel: str, tmp_path: pathlib.Path) -> None:
    """Every reader that filters on `is_file()` says what it filtered — on
    the right stream, with rc unchanged, and having still done its job.

    ⚠️ Asserts the NAME appears, not the exact sentence: the wording comes
    from the shared `unusable_reason`, and pinning it here would make this
    test a second copy of that function rather than a check on the reader.

    ⛔ The rc and landmark assertions exist because blind review broke
    `deprecate_rule` into warn-and-ABORT (rc=1, not one metric processed)
    and 9148 tests stayed green: the first version asserted only that the
    name was printed, which promoted a diagnostic into the whole contract
    and left "warn, then carry on serving" unguarded. The stream assertion
    exists because the same review moved a warning to stdout and made the
    `--json` output unparseable with every test still green.
    """
    stream, landmark = _WARNING_CONTRACT[tool_rel]
    tree = _unusable_tree(tmp_path / "conf.d")
    out = tmp_path / "out"
    tool = TOOLS_DIR / tool_rel
    r = subprocess.run(
        [sys.executable, "-X", "utf8", str(tool), *_argv_for(tool_rel, tree,
                                                             out)],
        capture_output=True, timeout=180, cwd=str(tmp_path),
        env=dict(os.environ, PYTHONIOENCODING="utf-8"))
    stdout = r.stdout.decode("utf-8", "replace")
    stderr = r.stderr.decode("utf-8", "replace")
    both = stdout + stderr
    named, forbidden = (stderr, stdout) if stream == "stderr" else (stdout,
                                                                   stderr)

    missing = [n for n in _UNUSABLE_NAMES if n not in both]
    assert not missing, (
        f"{tool_rel} silently dropped {missing} — an entry the operator "
        f"named like configuration disappeared with nothing said (#1607).\n"
        f"rc={r.returncode}\n{both[:800]}"
    )
    off_stream = [n for n in _UNUSABLE_NAMES if n not in named]
    assert not off_stream, (
        f"{tool_rel} must warn on {stream}; {off_stream} went to the other "
        f"stream.\nstdout={stdout[:400]}\nstderr={stderr[:400]}")
    leaked = [n for n in _UNUSABLE_NAMES if n in forbidden]
    assert not leaked, (
        f"{tool_rel} leaked {leaked} onto the stream that carries its "
        f"machine-readable output.\nstdout={stdout[:400]}")
    assert r.returncode == 0, (
        f"{tool_rel} changed its exit code to {r.returncode}. The contract "
        f"is warn-and-continue: an unusable entry is a diagnostic, not a "
        f"reason to refuse service.\n{both[:800]}")
    assert landmark in both, (
        f"{tool_rel} warned but did not finish its real work "
        f"({landmark!r} missing) — warn-and-abort, not warn-and-continue.\n"
        f"{both[:800]}")


def test_the_unusable_tree_is_not_vacuous(tmp_path: pathlib.Path) -> None:
    """⛔ The guard on the guard: the fixture must really carry both shapes.

    A `symlink_to` that silently no-ops, or a filesystem that refuses the
    directory, would make every assertion above pass by describing nothing.
    """
    tree = _unusable_tree(tmp_path / "conf.d")
    assert (tree / "notes.yaml").is_dir(), "fixture lost the directory shape"
    assert (tree / "broken.yaml").is_symlink(), "fixture lost the symlink"
    assert not (tree / "broken.yaml").exists(), "the symlink is not broken"
    assert not (tree / "notes.yaml").is_file()
    assert not (tree / "broken.yaml").is_file()


def test_custom_alerts_loader_quarantines_unusable_entries(
        tmp_path: pathlib.Path) -> None:
    """The loader's contract is `file_errors`, not stderr (#1008 Part B).

    ⭐ Measured regression this pins: `file_errors` went from four named
    records to EMPTY when `is_file()` began filtering the entries out before
    the `except` could see them — on the SHIPPED tenant self-service path.
    """
    from custom_alerts import loader  # noqa: PLC0415

    tree = _unusable_tree(tmp_path / "conf.d")
    _, file_errors = loader.collect_instances(tree)
    origins = [rec["origin"] for rec in file_errors]
    for name in _UNUSABLE_NAMES:
        assert name in origins, (
            f"{name} vanished from file_errors instead of being quarantined; "
            f"got {origins}")
    dupes = sorted({o for o in origins if origins.count(o) > 1})
    assert not dupes, (
        f"{dupes} recorded twice — the unusable-entry pass and the "
        f"malformed-YAML quarantine overlap, the double-count that "
        f"`unusable_config_paths` was narrowed twice to avoid")
    # ⛔ The SHAPE, not just the origin. `compile_custom_alerts` prints these
    # with `s['tenant']` / `s['name']` (its #1008 fail-soft quarantine line),
    # so a record missing a key crashes the shared compile gate on any tree
    # containing a bad file — the exact cross-tenant block fail-soft exists
    # to prevent. Blind review deleted two keys and 191 tests stayed green
    # because every existing consumer test reads only `origin`.
    required = {"tenant", "origin", "name", "reason"}
    for rec in file_errors:
        assert required <= set(rec), (
            f"file_errors record is missing {sorted(required - set(rec))}; "
            f"`compile_custom_alerts` indexes all four. got {rec}")


def test_defaults_carrier_is_reported_once_not_twice(
        tmp_path: pathlib.Path) -> None:
    """⛔ Disjointness, where it is easiest to get wrong.

    A BROKEN SYMLINK named `_defaults.yaml` is in `os.walk`'s `files`, so
    `_dir_defaults_alerts` already quarantines it. Without the
    `is_defaults_name` skip in `collect_instances`, one path would produce
    two `file_errors` records and the caveat line would say "2" for one file.
    """
    from custom_alerts import loader  # noqa: PLC0415

    tree = _unusable_tree(tmp_path / "conf.d")
    (tree / "_defaults.yaml").unlink()
    (tree / "_defaults.yaml").symlink_to(tree / "no-such-defaults.yaml")
    _, file_errors = loader.collect_instances(tree)
    hits = [r for r in file_errors if r["origin"] == "_defaults.yaml"]
    assert len(hits) == 1, (
        f"expected exactly one record for the broken defaults carrier, "
        f"got {len(hits)}: {hits}")


def test_operator_generate_does_not_invent_tenants_from_unusable_entries(
        tmp_path: pathlib.Path) -> None:
    """⛔ Why CodeRabbit's prescribed fix is wrong at THIS site.

    `discover_tenant_configs` takes the STEM as a tenant name and never
    opens the file, so dropping the `is_file()` filter would not "let the
    existing error handling record them" — there is none. It would emit an
    AlertmanagerConfig for a tenant named after a directory and one named
    after a dangling symlink. Measured: 20 CRDs before the filter, 18 after.
    """
    tree = _unusable_tree(tmp_path / "conf.d")
    out = tmp_path / "out"
    tool = TOOLS_DIR / "ops" / "operator_generate.py"
    r = subprocess.run(
        [sys.executable, "-X", "utf8", str(tool), "--config-dir", str(tree),
         "--output-dir", str(out)],
        capture_output=True, timeout=180, cwd=str(tmp_path),
        env=dict(os.environ, PYTHONIOENCODING="utf-8"))
    assert r.returncode == 0, r.stderr.decode("utf-8", "replace")[:600]
    produced = sorted(p.name for p in out.rglob("*.yaml"))
    phantom = [n for n in produced if "notes" in n or "broken" in n]
    assert not phantom, (
        f"generated CRDs for tenants invented from unusable entries: "
        f"{phantom}")
    assert any("alpha" in n for n in produced), (
        f"fixture produced no real tenant CRD, so the assertion above is "
        f"vacuous; got {produced}")


def test_each_reader_names_an_unusable_entry_exactly_once(
        tmp_path: pathlib.Path) -> None:
    """⛔ Said once per run, not once per scan.

    Every one of these tools walks its conf.d more than once — `deprecate_rule`
    scans per metric and again to clean tenants, `offboard_tenant` looks for
    the carrier and then loads everything, `check_path_metadata_consistency`
    consumes its own generator twice (once to scan, once to count). Measured
    before this was pinned: `deprecate_rule a b c` printed the same two
    warnings THREE times. A repeated warning trains the operator to skim past
    it, which costs the signal the report exists to give.
    """
    tree = _unusable_tree(tmp_path / "conf.d")
    out = tmp_path / "out"
    # three metrics, so a per-metric reporter would show up as three
    argv = {"ops/deprecate_rule.py": ["pg_connections", "mysql_connections",
                                      "redis_memory", "--config-dir",
                                      str(tree)]}
    for tool_rel in ("ops/operator_generate.py", "ops/deprecate_rule.py",
                     "ops/offboard_tenant.py",
                     "dx/generate_tenant_metadata.py",
                     "dx/describe_tenant.py",
                     "lint/check_path_metadata_consistency.py"):
        r = subprocess.run(
            [sys.executable, "-X", "utf8", str(TOOLS_DIR / tool_rel),
             *argv.get(tool_rel, _argv_for(tool_rel, tree, out))],
            capture_output=True, timeout=180, cwd=str(tmp_path),
            env=dict(os.environ, PYTHONIOENCODING="utf-8"))
        text = (r.stdout.decode("utf-8", "replace")
                + r.stderr.decode("utf-8", "replace"))
        for name in _UNUSABLE_NAMES:
            hits = text.count(name)
            assert hits == 1, (
                f"{tool_rel} named {name} {hits} time(s); expected exactly "
                f"once per invocation.\n{text[:800]}")


# ---------------------------------------------------------------------------
# #1607 round 2 — findings from adversarial blind review of the first fix
# ---------------------------------------------------------------------------

def _reserved_unusable_tree(root: pathlib.Path) -> pathlib.Path:
    """`_unusable_tree` plus a DIRECTORY-shaped `_defaults.yaml/`.

    The reserved prefix is a second axis on which a reader's report can be
    wrong, and it splits the tools two ways: those that never read `_*` at
    all, and those (the defaults chain) that do.
    """
    _unusable_tree(root)
    (root / "_defaults.yaml").unlink()
    (root / "_defaults.yaml").mkdir()
    return root


@pytest.mark.parametrize("tool_rel", [
    "ops/operator_generate.py",
    "dx/generate_tenant_metadata.py",
    "lint/check_path_metadata_consistency.py",
])
def test_reader_does_not_name_a_reserved_entry_it_never_reads(
        tool_rel: str, tmp_path: pathlib.Path) -> None:
    """⛔ The mirror image of the `suffixes` rule, on the reserved-name axis.

    These three drop every `_`-prefixed entry BEFORE looking at its shape, so
    a warning about a directory-shaped `_defaults.yaml/` tells the operator
    about a loss that did not happen in this tool. `check_path_metadata_
    consistency` is the sharpest case: it prints a machine-parseable
    `path:0: warning:` line, which a CI annotation consumer would surface as
    a finding about a file the lint never checks.
    """
    tree = _reserved_unusable_tree(tmp_path / "conf.d")
    out = tmp_path / "out"
    r = subprocess.run(
        [sys.executable, "-X", "utf8", str(TOOLS_DIR / tool_rel),
         *_argv_for(tool_rel, tree, out)],
        capture_output=True, timeout=180, cwd=str(tmp_path),
        env=dict(os.environ, PYTHONIOENCODING="utf-8"))
    text = (r.stdout.decode("utf-8", "replace")
            + r.stderr.decode("utf-8", "replace"))
    assert "_defaults.yaml" not in text, (
        f"{tool_rel} reported `_defaults.yaml`, which it never reads whatever "
        f"its shape — a finding the operator cannot act on from this tool.\n"
        f"{text[:800]}")
    # ⛔ Vacuity guard: the run must still name the NON-reserved unusable
    # entries, or this assertion would pass on a tool that reports nothing.
    for name in _UNUSABLE_NAMES:
        assert name in text, (
            f"{tool_rel} stopped naming {name}; the assertion above would be "
            f"vacuous.\n{text[:800]}")


def test_describe_tenant_does_name_an_unusable_defaults_carrier(
        tmp_path: pathlib.Path) -> None:
    """The other half: a reader that DOES read `_defaults*` must still say so.

    Pins that the reserved-name exclusion above was applied per-caller and
    not blanket — `describe_tenant` resolves the defaults chain, so a
    directory-shaped carrier is a real loss for it.
    """
    tree = _reserved_unusable_tree(tmp_path / "conf.d")
    r = subprocess.run(
        [sys.executable, "-X", "utf8",
         str(TOOLS_DIR / "dx" / "describe_tenant.py"), "--all",
         "--conf-d", str(tree)],
        capture_output=True, timeout=180, cwd=str(tmp_path),
        env=dict(os.environ, PYTHONIOENCODING="utf-8"))
    text = (r.stdout.decode("utf-8", "replace")
            + r.stderr.decode("utf-8", "replace"))
    assert "_defaults.yaml" in text, (
        f"describe_tenant reads the defaults chain, so an unusable carrier is "
        f"a real loss it must name.\n{text[:800]}")


def test_loader_names_a_directory_shaped_defaults_carrier(
        tmp_path: pathlib.Path) -> None:
    """⛔ The one path where "every reader names what it drops" was false.

    `_dir_defaults_alerts` walks with `os.walk`, which classifies by
    `is_dir()`: a broken symlink lands in `files` and is quarantined there, a
    DIRECTORY lands in `dirs` and is invisible to it. The skip in
    `collect_instances` must therefore follow the same line, not skip every
    defaults-named entry.
    """
    from custom_alerts import loader  # noqa: PLC0415

    tree = _reserved_unusable_tree(tmp_path / "conf.d")
    _, file_errors = loader.collect_instances(tree)
    origins = [r["origin"] for r in file_errors]
    assert "_defaults.yaml" in origins, (
        f"a directory-shaped defaults carrier vanished from file_errors; "
        f"got {origins}")
    dupes = sorted({o for o in origins if origins.count(o) > 1})
    assert not dupes, (
        f"{dupes} recorded twice — the disjointness with "
        f"`_dir_defaults_alerts` broke while closing the directory case")


def test_the_reserved_unusable_tree_is_not_vacuous(
        tmp_path: pathlib.Path) -> None:
    """⛔ Guard on the guard: the directory-shaped carrier must really exist."""
    tree = _reserved_unusable_tree(tmp_path / "conf.d")
    assert (tree / "_defaults.yaml").is_dir()
    assert not (tree / "_defaults.yaml").is_file()


@pytest.mark.parametrize("module_rel,call", [
    ("lint/check_path_metadata_consistency.py", "main"),
    ("dx/describe_tenant.py", "scanner"),
])
def test_reader_walks_the_tree_once(module_rel: str, call: str,
                                    tmp_path: pathlib.Path,
                                    monkeypatch) -> None:
    """⛔ Two walks can describe two DIFFERENT trees.

    Not only wasted I/O: if anything changes under `conf.d` between the
    passes, the warnings, the findings and the "N tenant file(s)" tail stop
    agreeing. `collect_instances` lists once and says so; blind review found
    these two doing otherwise.
    """
    tree = _unusable_tree(tmp_path / "conf.d")
    calls: list[str] = []
    real_rglob = pathlib.Path.rglob

    def counting_rglob(self, pattern, *a, **kw):
        calls.append(pattern)
        return real_rglob(self, pattern, *a, **kw)

    monkeypatch.setattr(pathlib.Path, "rglob", counting_rglob)
    monkeypatch.chdir(tmp_path)

    if call == "main":
        sys.path.insert(0, str(TOOLS_DIR / "lint"))
        import importlib  # noqa: PLC0415
        mod = importlib.import_module("check_path_metadata_consistency")
        monkeypatch.setattr(sys, "argv",
                            ["x", "--config-dir", str(tree)])
        mod.main()
    else:
        sys.path.insert(0, str(TOOLS_DIR / "dx"))
        import importlib  # noqa: PLC0415
        mod = importlib.import_module("describe_tenant")
        # ⛔ The compiler resolver is a DIFFERENT module with its own walk;
        # counting it here would make this test about `custom_alerts.loader`
        # rather than about this scanner. Disabled so the count is `_scan`'s.
        monkeypatch.setattr(mod, "_ca_loader", None)
        mod.ConfDScanner(tree)

    whole_tree = [p for p in calls if p == "*"]
    assert len(whole_tree) == 1, (
        f"{module_rel} walked the whole tree {len(whole_tree)} times "
        f"(patterns: {calls}); one listing must serve every pass")


def test_offboard_precheck_explains_a_tenant_whose_config_is_unusable(
        tmp_path: pathlib.Path) -> None:
    """⛔ The scenario `find_config_file` calls the worst place to be silent.

    Offboarding `notes` when `notes.yaml/` is a DIRECTORY: the carrier lookup
    returns None and the pre-check prints "❌ 找不到設定檔案", which reads as
    "there is nothing here to remove". The explanation must be in the same
    run — and it must be the shared `unusable_reason` wording, not merely the
    file name: "找不到設定檔案: notes.yaml" already contains the name, so a
    substring check on the name alone cannot tell the two apart. Blind review
    found the whole scenario uncovered and that assertion shape unable to
    cover it.
    """
    tree = _unusable_tree(tmp_path / "conf.d")
    r = subprocess.run(
        [sys.executable, "-X", "utf8",
         str(TOOLS_DIR / "ops" / "offboard_tenant.py"),
         "notes", "--config-dir", str(tree)],
        capture_output=True, timeout=180, cwd=str(tmp_path),
        env=dict(os.environ, PYTHONIOENCODING="utf-8"))
    text = (r.stdout.decode("utf-8", "replace")
            + r.stderr.decode("utf-8", "replace"))
    assert "is a directory, not a config file" in text, (
        f"the pre-check said the config was not found without saying WHY; an "
        f"operator reads that as 'nothing to remove'.\n{text[:800]}")
    assert "找不到設定檔案" in text, (
        f"fixture did not reach the not-found branch, so the assertion above "
        f"would be vacuous.\n{text[:800]}")


# ── the readers the population walk could never reach ─────────────────
#
# ⛔ #1588's body listed five tools as "same shape, NOT individually
# reproduced". Re-measured on `05d3136` — after the fix for the six that
# WERE reproduced had shipped — four of the five were still divergent, and
# each had escaped this file by a DIFFERENT route:
#
#   check_threshold_unit_sanity  not in ALL_TOOLS: exposes no conf.d flag,
#                                so `collect_confd_tools` cannot see it. Its
#                                root is a `run_check(root=)` argument.
#   batch_diagnose               not in ALL_TOOLS for the same reason, and
#                                its carrier is a ConfigMap KEY rather than
#                                a directory entry — the same
#                                filename->tenant question on another
#                                surface, which no filesystem fixture
#                                reaches.
#   backtest_threshold           IN the population but parked in
#                                KNOWN_INSENSITIVE: the CLI needs
#                                `--baseline` as well, and without it the
#                                A/B produced identical output in both arms
#                                and the tool was filed as having no
#                                discriminating power.
#   run_chaos_soak               IN the population but parked in
#                                KNOWN_UNMEASURABLE: argparse demands
#                                `--target-url` / `--output-dir`.
#
# ⚠️ Two of those four skip sets are still correct AS STATEMENTS ABOUT THE
# CLI — this section does not empty them, it stops them from being the
# only thing that was asked. `KNOWN_INSENSITIVE`'s own comment already
# said "this is not a clean bill of health"; the hole it declared went
# four commits without anybody re-asking, which is the part worth pinning.
#
# Each test below asserts BOTH arms and then asserts the lower arm was
# non-empty, because an A/B whose lower arm produces nothing reports
# parity for a fixture with no discriminating power — the exact way the
# first measurement of `check_threshold_unit_sanity` came back clean.

_CASE_ARMS = (("lower", "db-a.yaml"), ("UPPER", "DB-A.YAML"))
_TENANT_BODY = "tenants:\n  acme:\n    cpu_usage: 80\n"


def _import_tool(subdir: str, module: str):
    """Import a tool module by name, with its own sys.path expectations."""
    import importlib  # noqa: PLC0415
    for extra in (str(TOOLS_DIR / subdir), str(TOOLS_DIR)):
        if extra not in sys.path:
            sys.path.insert(0, extra)
    return importlib.import_module(module)


def test_unit_sanity_gate_reads_both_casings(tmp_path: pathlib.Path) -> None:
    """⛔ A lint gate that returns green because the carrier was upper-cased
    is worse than no gate: it reports a clean bill for a file it never
    opened. Measured before the fix: `_defaults.yaml` -> 1 OUT-OF-DOMAIN
    error, `_DEFAULTS.YAML` -> 0, same body.
    """
    import yaml as _yaml  # noqa: PLC0415
    gate = _import_tool("lint", "check_threshold_unit_sanity")
    registry = {"version": 1,
                "packs": {"t": {"k": {"value": 50, "unit": "%",
                                      "tier": "defaults"}}}}
    body = _yaml.safe_dump({"defaults": {"k": 300}}, allow_unicode=True)
    counts = {}
    for arm, fname in _CASE_ARMS:
        root = tmp_path / arm
        confd = root / "components/threshold-exporter/config/conf.d"
        confd.mkdir(parents=True)
        (confd / fname).write_text(body, encoding="utf-8")
        # ⛔ NEIGHBOURS, and they are what makes this test able to fail in
        # both directions. Blind review measured the first version: with
        # only the one carrier in the directory, deleting the extension
        # filter OUTRIGHT left this test green, because "accept everything"
        # and "accept the right thing" produce the same output on a
        # directory holding exactly one file.
        #   `neighbour.txt`  MUST NOT be read (it is not a config carrier)
        #   `db-c.yml`       MUST be read — this gate's declared set is
        #                    BOTH spellings, so dropping `.yml` is also a
        #                    regression and gets its own direction here.
        (confd / "neighbour.txt").write_text(body, encoding="utf-8")
        (confd / "db-c.yml").write_text(body, encoding="utf-8")
        errs = gate.run_check(registry=registry, root=str(root))["errors"]
        counts[arm] = errs
    assert counts["lower"], (
        "fixture is vacuous: the lower arm produced no violation, so the "
        "comparison below would pass for a gate that reads nothing")
    for arm in ("lower", "UPPER"):
        named = sorted(e.split(":")[1].strip().rsplit("/", 1)[-1]
                       for e in counts[arm])
        assert len(counts[arm]) == 2, (
            f"[{arm}] expected exactly the two YAML carriers to be read "
            f"(the tenant file and `db-c.yml`), got {len(counts[arm])}: "
            f"{named}. Three means the extension filter stopped filtering; "
            f"one means `.yml` stopped being read.\n{counts[arm]}")
        assert "neighbour.txt" not in named, (
            f"[{arm}] the gate read a non-config file: {named}")
    assert len(counts["UPPER"]) == len(counts["lower"]), (
        f"the gate saw {len(counts['lower'])} violation(s) under "
        f"`db-a.yaml` and {len(counts['UPPER'])} under `DB-A.YAML`; the "
        f"bodies are identical.\nlower: {counts['lower']}\n"
        f"UPPER: {counts['UPPER']}")


def test_batch_diagnose_discovers_tenants_from_either_casing(
        monkeypatch) -> None:
    """The carrier is a ConfigMap KEY, and the keys ARE the conf.d
    filenames — so the exporter's filename->tenant rule applies to them.
    Measured before the fix: `db-a.yaml` -> ['db-a'], `DB-A.YAML` -> [],
    i.e. every tenant vanished from the report and the run still exited 0.
    """
    import json as _json  # noqa: PLC0415
    import contextlib  # noqa: PLC0415
    import io  # noqa: PLC0415
    mod = _import_tool("ops", "batch_diagnose")
    found, noise = {}, {}
    for arm, fname in _CASE_ARMS:
        # ⛔ Three neighbours, one per filter this loop applies, so that
        # deleting any of them turns this test red rather than leaving it
        # green on a one-key ConfigMap (blind review measured that hole):
        #   `_defaults.yaml`  reserved — never a tenant
        #   `README.md`       not a config carrier at all
        #   `db-c.yml`        the SPELLING axis: `.yaml`-only is this
        #                     tool's declared set (widening it is #1603),
        #                     and `config_stem` internally accepts BOTH
        #                     spellings — so if the outer filter is ever
        #                     dropped, a `.yml` key silently becomes a
        #                     tenant. That is pinned here, not assumed.
        payload = _json.dumps(
            {"data": {fname: _TENANT_BODY,
                      "_defaults.yaml": "d: {}\n",
                      "README.md": "# notes\n",
                      "db-c.yml": _TENANT_BODY}})

        class _Result:
            returncode = 0
            stdout = payload
            stderr = ""

        monkeypatch.setattr(mod.subprocess, "run",
                            lambda *a, **k: _Result())
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            found[arm] = mod.discover_tenants()
        noise[arm] = err.getvalue()
    assert found["lower"] == ["db-a"], (
        f"fixture is vacuous — the lower arm found {found['lower']}")
    # ⛔ Compare the SHAPE, not the strings: `config_stem` preserves the
    # carrier's case on purpose (folding it would rename the tenant on the
    # write plane only), so the two arms legitimately carry different ids.
    assert len(found["UPPER"]) == len(found["lower"]), (
        f"upper-cased ConfigMap keys yielded {found['UPPER']} against "
        f"{found['lower']} for the identical body")
    assert found["UPPER"] == ["DB-A"], (
        f"the tenant id must keep the key's original case; got "
        f"{found['UPPER']}")
    # ⛔ The `_defaults.yaml` neighbour above does NOT pin the reserved
    # filter through the RETURN VALUE — `config_stem` answers "" for it
    # either way, so deleting `is_reserved_name` leaves the list identical.
    # Blind review measured what it does change: a spurious
    # "carries no tenant id" warning for the platform defaults key, on
    # every run. That is what this assertion pins.
    for arm in ("lower", "UPPER"):
        assert noise[arm] == "", (
            f"[{arm}] discover_tenants wrote to stderr on a ConfigMap whose "
            f"keys are all legitimate; a reserved key is expected, not a "
            f"finding: {noise[arm]!r}")


def test_backtest_sees_threshold_changes_under_either_casing(
        tmp_path: pathlib.Path) -> None:
    """⛔ This one fails in the direction that reads as good news: a
    backtest reporting "no threshold changes" for a change that IS there.
    Measured before the fix: 1 change vs 0.
    """
    mod = _import_tool("ops", "backtest_threshold")
    seen = {}
    for arm, fname in _CASE_ARMS:
        cur, old = tmp_path / arm / "cur", tmp_path / arm / "old"
        cur.mkdir(parents=True)
        old.mkdir(parents=True)
        # ⛔ Every neighbour carries the SAME edit, so anything that stops
        # filtering shows up as an extra change rather than as nothing —
        # the one-file fixture this replaced went green when the extension
        # filter was deleted outright (blind review measured it).
        for where, value in ((old, 80), (cur, 95)):
            body = f"tenants:\n  acme:\n    cpu_usage: {value}\n"
            (where / fname).write_text(body, encoding="utf-8")
            (where / "neighbour.txt").write_text(body, encoding="utf-8")
            (where / "db-c.yml").write_text(body, encoding="utf-8")
            # ⛔ A `.`-prefixed carrier: `config_stem` answers "" for it,
            # and the first version USED that answer without checking, so
            # this file produced a change whose tenant was the empty
            # string — worse than `05d3136`, which at least said `.foo`.
            # A surviving mutant until this neighbour existed.
            (where / ".hidden.yaml").write_text(body, encoding="utf-8")
            (where / "_defaults.yaml").write_text(
                f"defaults:\n  cpu_usage: {value}\n", encoding="utf-8")
        seen[arm] = mod.extract_changes_from_dirs(str(cur), str(old))
    assert seen["lower"], "fixture is vacuous — the lower arm found no change"
    for arm in ("lower", "UPPER"):
        tenants = sorted({c["tenant"] for c in seen[arm]})
        assert len(tenants) == 1, (
            f"[{arm}] exactly one carrier is a tenant here; the `.txt`, the "
            f"`.yml` (spelling axis is #1603) and the reserved "
            f"`_defaults.yaml` must all be skipped. Got {tenants}")
    assert len(seen["UPPER"]) == len(seen["lower"]), (
        f"upper-cased carrier yielded {len(seen['UPPER'])} change(s) against "
        f"{len(seen['lower'])} for the same edit")
    # The stem must be stripped in both arms: a report naming a tenant
    # called `DB-A.YAML` is the silent miss turned into a loud wrong answer.
    assert {c["tenant"] for c in seen["UPPER"]} == {"DB-A"}, (
        f"tenant id kept its extension: "
        f"{sorted(c['tenant'] for c in seen['UPPER'])}")


def test_chaos_soak_perturbs_a_carrier_under_either_casing(
        tmp_path: pathlib.Path) -> None:
    """A soak that never fires a reload still writes a full run report, so
    the exercise reads as "hot-reload survived N hours" having never
    reloaded once. Measured before the fix: True vs False.
    """
    mod = _import_tool("dx", "run_chaos_soak")
    fired = {}
    for arm, fname in _CASE_ARMS:
        confd = tmp_path / arm / "conf.d"
        confd.mkdir(parents=True)
        carrier = confd / fname
        carrier.write_text(_TENANT_BODY, encoding="utf-8")
        # ⛔ Neighbours that MUST NOT be the file this perturbs. Asserting
        # only "something fired" cannot tell the right carrier from the
        # wrong one, and `trigger_reload` writes to whichever entry it
        # matches FIRST — so a filter that stopped filtering would edit an
        # operator's `neighbour.txt` and still report success.
        others = {}
        for name, body in (("neighbour.txt", "not a config\n"),
                           ("db-c.yml", _TENANT_BODY),
                           ("_defaults.yaml", "defaults:\n  cpu_usage: 80\n")):
            (confd / name).write_text(body, encoding="utf-8")
            others[name] = body
        fired[arm] = (mod.trigger_reload(confd),
                      "# soak-toggle" in carrier.read_text(encoding="utf-8"))
        untouched = {n: (confd / n).read_text(encoding="utf-8") == b
                     for n, b in others.items()}
        assert all(untouched.values()), (
            f"[{arm}] the soak perturbed a file that is not its tenant "
            f"carrier: {sorted(n for n, ok in untouched.items() if not ok)}")
    assert fired["lower"] == (True, True), (
        f"fixture is vacuous — the lower arm did not perturb anything: "
        f"{fired['lower']}")
    assert fired["UPPER"] == fired["lower"], (
        f"upper-cased carrier: {fired['UPPER']} against {fired['lower']} "
        f"(fired, carrier_actually_written)")


def test_backtest_names_an_unreadable_config_dir_instead_of_raising(
        tmp_path: pathlib.Path, monkeypatch) -> None:
    """⛔ The case fix above swapped `glob("*.yaml")` for `iterdir()`, and
    the two disagree on a directory that cannot be READ — not one that is
    missing. `glob` swallows the scandir failure and yields nothing;
    `iterdir()` raises straight out of `main()`, before the Prometheus
    availability check, so `--skip-if-unavailable` cannot contain it and
    the `--json` contract loses its one document. Measured as a non-root
    uid on a `chmod 000` conf.d: `05d3136` exited 2 with "Prometheus not
    reachable", the first version of the fix exited 1 with a
    `PermissionError` traceback.

    ⚠️ `is_dir()` is NOT the guard for this: it answers True for an
    unreadable directory. The right answer is already settled in
    `_lib_confd.unusable_config_paths` — raising kills callers that
    iterate outside a `try`, `[]` is "a green light for a directory
    nothing ever read", so NAME it.

    ⚠️ Monkeypatched rather than `chmod`-driven, for the reason
    `test_lib_confd.py` gives for the same scenario: this suite runs as
    root in the dev container, where the permission bits do not apply.
    """
    mod = _import_tool("ops", "backtest_threshold")
    confd = tmp_path / "conf.d"
    confd.mkdir()
    (confd / "db-a.yaml").write_text(_TENANT_BODY, encoding="utf-8")
    baseline = tmp_path / "baseline"
    baseline.mkdir()

    real_iterdir = pathlib.Path.iterdir

    def deny(self):
        if os.fspath(self) == os.fspath(confd):
            raise PermissionError(13, "Permission denied", str(self))
        return real_iterdir(self)

    monkeypatch.setattr(pathlib.Path, "iterdir", deny)

    captured: list[str] = []
    monkeypatch.setattr(
        mod, "warn_nested", lambda *a, **k: False)  # its own walk, not ours
    import io  # noqa: PLC0415
    import contextlib  # noqa: PLC0415
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        changes = mod.extract_changes_from_dirs(str(confd), str(baseline))
    captured.append(err.getvalue())

    assert changes == [], (
        f"an unreadable config dir must not invent changes; got {changes}")
    assert str(confd) in captured[0], (
        f"the unreadable directory was skipped SILENTLY — that is the "
        f"green light this whole line of work exists to remove.\n"
        f"stderr was: {captured[0]!r}")
    assert "PermissionError" in captured[0], (
        f"the warning must name the errno that was actually caught, not a "
        f"re-probed guess; got {captured[0]!r}")
    assert "were NOT scanned" in captured[0], (
        f"the warning must say the report that follows is INCOMPLETE; "
        f"got {captured[0]!r}")


# ── the sites the first counterfactual did not reach ──────────────────
#
# ⛔ Blind review measured this: the fix touched SIX filename sites in
# `backtest_threshold.py`, the commit counted "of 4", and reverting three
# of them left the whole suite green. The one that was not counted was
# also the one still broken. The four tests above cover exactly one of
# those sites (`extract_changes_from_dirs`); these cover the rest.

def _git_conf_d_repo(root: pathlib.Path, carrier: str,
                     before: str, after: str) -> pathlib.Path:
    """A git repo whose `conf.d/<carrier>` changes between HEAD~1 and HEAD."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "conf.d").mkdir()
    run = lambda *a: subprocess.run(  # noqa: E731
        a, cwd=root, capture_output=True, timeout=60)
    run("git", "init", "-q")
    run("git", "config", "user.email", "t@example.invalid")
    run("git", "config", "user.name", "t")
    # ⛔ Two neighbours carrying the SAME edit, one per axis this path must
    # not confuse with the tenant carrier:
    #   `db-c.yml`       the spelling axis (#1603) — `.yaml`-only is what
    #                    every site here accepts, and blind review measured
    #                    that claim unguarded at sites 1, 3 and 5.
    #   `.hidden.yaml`   the hidden axis — the exporter's scanner skips
    #                    `.`-prefixed entries, and this chain SPLIT this
    #                    path against itself by aligning the diff parser
    #                    without aligning the listing beside it. With the
    #                    split present, `changed_conf_files` below returns
    #                    two entries and the assertions go red.
    for name, body in ((carrier, before), ("db-c.yml", before),
                       (".hidden.yaml", before)):
        (root / "conf.d" / name).write_text(body, encoding="utf-8")
    run("git", "add", "-A")
    run("git", "commit", "-qm", "before")
    for name, body in ((carrier, after), ("db-c.yml", after),
                       (".hidden.yaml", after)):
        (root / "conf.d" / name).write_text(body, encoding="utf-8")
    run("git", "add", "-A")
    run("git", "commit", "-qm", "after")
    return root


_BEFORE = "tenants:\n  acme:\n    cpu_usage: 80\n    mem_usage: 70\n"
_AFTER_REMOVED = "tenants:\n  acme:\n    mem_usage: 70\n"


def test_git_diff_path_reports_a_removal_under_either_casing(
        tmp_path: pathlib.Path, monkeypatch) -> None:
    """⛔ The REMOVAL direction — a threshold being taken away, i.e. an
    alert being disabled — through the `--git-diff` mode CI actually runs.

    Three sites at once: `changed_conf_files`, the diff parser's tenant
    extraction, and the HEAD~1 lookup that classifies a removal. The last
    one re-SPELLED the carrier from the tenant id (`{tenant}.yaml`), and
    because `config_stem` preserves case on purpose, an upper-cased
    carrier made `git show` fail and the removal was dropped. Measured:
    the operator saw "No threshold changes found." for a removal that was
    really there — on `05d3136` AND on the first version of the fix.
    """
    mod = _import_tool("ops", "backtest_threshold")
    kept = {}
    for arm, carrier in (("lower", "db-a.yaml"), ("UPPER", "DB-A.YAML")):
        repo = _git_conf_d_repo(tmp_path / arm, carrier,
                                _BEFORE, _AFTER_REMOVED)
        monkeypatch.chdir(repo)
        files = mod.changed_conf_files()
        parsed = mod.load_conf_files(files)
        raw = mod.extract_changes_from_git_diff()
        kept[arm] = (files, sorted(parsed),
                     [(c["tenant"], c["metric"]) for c in
                      mod.keep_flat_threshold_changes(raw, parsed)],
                     sorted({c["tenant"] for c in raw}))
    assert kept["lower"][2] == [("db-a", "cpu_usage")], (
        f"fixture is vacuous — the lower arm reported {kept['lower']}")
    # ⛔ Assert the three sites SEPARATELY. Asserting only the final
    # `kept` list left `changed_conf_files` uncovered: a removal is
    # classified against HEAD~1, not against `parsed`, so reverting that
    # site to a case-sensitive test made the file list empty and the end
    # result was still right. Measured — it was a surviving mutant.
    for arm, carrier, tid in (("lower", "db-a.yaml", "db-a"),
                              ("UPPER", "DB-A.YAML", "DB-A")):
        assert kept[arm][0] == [f"conf.d/{carrier}"], (
            f"[{arm}] `changed_conf_files` did not see the carrier git "
            f"itself reported as changed; got {kept[arm][0]}")
        assert kept[arm][1] == [tid], (
            f"[{arm}] the changed file did not parse into its tenant; "
            f"got {kept[arm][1]}")
        # ⛔ The PARSER's own output, separately. Widening site 1's
        # spelling set is invisible in `kept` — the `.yml` neighbour's
        # removal is dropped later anyway, by sites 3 and 5, which still
        # accept `.yaml` only. A surviving mutant until this assertion
        # existed; the axis has to be pinned where it is decided.
        assert kept[arm][3] == [tid], (
            f"[{arm}] the git-diff parser attributed changes to "
            f"{kept[arm][3]}; the `.yml` neighbour carries the same edit "
            f"and this site accepts `.yaml` only (#1603)")
    assert len(kept["UPPER"][2]) == len(kept["lower"][2]), (
        f"the removal survived under `db-a.yaml` and was dropped under "
        f"`DB-A.YAML`.\nlower: {kept['lower']}\nUPPER: {kept['UPPER']}")
    assert kept["UPPER"][2] == [("DB-A", "cpu_usage")], (
        f"tenant id must keep the carrier's case; got {kept['UPPER'][2]}")


def test_config_dir_recipe_scan_sees_either_casing(
        tmp_path: pathlib.Path) -> None:
    """The fourth site: `main()`'s own `--config-dir` scan, which feeds the
    custom-alert notice. It is a SEPARATE listing from
    `extract_changes_from_dirs`, so the tests above cannot reach it — and
    reverting it alone left the suite green.

    Driven through the CLI because that is the only way this site runs.
    """
    def _recipe_body(tenant: str) -> str:
        return (f"tenants:\n  {tenant}:\n    cpu_usage: 80\n"
                f"    _custom_alerts:\n      - recipe: threshold\n")

    bodies = _recipe_body("acme")
    seen = {}
    for arm, carrier in (("lower", "db-a.yaml"), ("UPPER", "DB-A.YAML")):
        cur, base = tmp_path / arm / "cur", tmp_path / arm / "base"
        cur.mkdir(parents=True)
        base.mkdir(parents=True)
        (cur / carrier).write_text(bodies, encoding="utf-8")
        (base / carrier).write_text(bodies, encoding="utf-8")
        # ⛔ Three neighbours whose tenants must NOT reach the notice, one
        # per filter this site applies. Blind review deleted this site's
        # extension filter outright and the suite stayed green while a
        # `.txt` file put a tenant called `ghost` in front of the operator;
        # and it measured this tool answering the HIDDEN axis one way here
        # and another way in `extract_changes_from_dirs`.
        # `_defaults.yaml` carries a `tenants:` block on purpose: that is
        # the only shape in which the reserved rule is OBSERVABLE here (a
        # defaults carrier without one contributes no tenant either way,
        # so dropping the rule would be an equivalent mutant and the
        # neighbour would pin nothing). Measured as a surviving mutant
        # until this shape was used.
        for name, tenant in (("neighbour.txt", "ghost_txt"),
                             (".hidden.yaml", "ghost_hidden"),
                             ("db-c.yml", "ghost_yml"),
                             ("_defaults.yaml", "ghost_reserved")):
            (cur / name).write_text(_recipe_body(tenant), encoding="utf-8")
            (base / name).write_text(_recipe_body(tenant), encoding="utf-8")
        r = subprocess.run(
            [sys.executable, "-X", "utf8",
             str(TOOLS_DIR / "ops" / "backtest_threshold.py"),
             "--config-dir", str(cur), "--baseline", str(base),
             "--skip-if-unavailable", "--json"],
            capture_output=True, timeout=180, cwd=str(tmp_path),
            env=dict(os.environ, PYTHONIOENCODING="utf-8"))
        seen[arm] = r.stderr.decode("utf-8", "replace")
    assert "_custom_alerts" in seen["lower"], (
        f"fixture is vacuous — the lower arm printed no recipe notice:\n"
        f"{seen['lower'][:400]}")
    for arm in ("lower", "UPPER"):
        assert "acme" in seen[arm], (
            f"[{arm}] the recipe-bearing tenant was not seen by the "
            f"--config-dir scan:\n{seen[arm][:400]}")
        assert "1 tenant(s)" in seen[arm], (
            f"[{arm}] exactly one carrier here is a tenant; a `.txt`, a "
            f"`.`-prefixed, a `.yml` and a reserved `_defaults.yaml` "
            f"neighbour must all be skipped by THIS site, the same way the "
            f"comparison scan skips them.\n"
            f"{seen[arm][:400]}")
        for ghost in ("ghost_txt", "ghost_hidden", "ghost_yml",
                      "ghost_reserved"):
            assert ghost not in seen[arm], (
                f"[{arm}] `{ghost}` reached the operator-facing notice from "
                f"a carrier this site must not read:\n{seen[arm][:400]}")


def test_config_dir_scan_still_lists_a_config_named_directory(
        tmp_path: pathlib.Path) -> None:
    """⛔ The `is_file()` axis (#1607) must stay OUT of this change.

    ⚠️ SCOPE, stated exactly, because the first version of this docstring
    overstated it: this pins the LISTING HELPER, not the two call sites.
    Blind review added `is_file()` at both call sites and the suite stayed
    green — and then measured that the mutant is behaviourally EQUIVALENT
    in this tool today, because `load_yaml_file` and `load_conf_files`
    each apply their own `is_file()` downstream. So there is nothing to
    observe at the call sites; the guard belongs where the axis is
    decided, and that is here. `glob("*.yaml")` returned a directory named
    `notes.yaml/`, so the replacement must too.
    """
    mod = _import_tool("ops", "backtest_threshold")
    tree = tmp_path / "conf.d"
    tree.mkdir()
    (tree / "notes.yaml").mkdir()
    (tree / "db-a.yaml").write_text(_BEFORE, encoding="utf-8")
    listed = [p.name for p in mod._confd_entries(tree)]
    assert "notes.yaml" in listed, (
        f"a config-named DIRECTORY dropped out of the listing; that is the "
        f"#1607 axis smuggled into a case fix. Listed: {listed}")
    assert "db-a.yaml" in listed, f"fixture is vacuous: {listed}"


def test_backtest_names_an_unreadable_config_dir_once_per_run(
        tmp_path: pathlib.Path, monkeypatch) -> None:
    """⛔ Said once per RUN, not once per scan.

    `--config-dir` lists the tree TWICE (the recipe scan in `main`, the
    comparison in `extract_changes_from_dirs`), and the first version of
    the unreadable-directory warning printed on both. Measured through the
    CLI as a non-root uid against a `chmod 000` conf.d: two identical
    lines. This file already states the rule for every other reader —
    "A repeated warning trains the operator to skim past it, which costs
    the signal the report exists to give."
    """
    mod = _import_tool("ops", "backtest_threshold")
    mod.reset_unlistable_warnings_for_test()
    confd = tmp_path / "conf.d"
    confd.mkdir()
    real_iterdir = pathlib.Path.iterdir

    def deny(self):
        if os.fspath(self) == os.fspath(confd):
            raise PermissionError(13, "Permission denied", str(self))
        return real_iterdir(self)

    monkeypatch.setattr(pathlib.Path, "iterdir", deny)
    monkeypatch.setattr(mod, "warn_nested", lambda *a, **k: False)

    import contextlib  # noqa: PLC0415
    import io  # noqa: PLC0415
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        mod._confd_entries(confd)
        mod._confd_entries(confd)
    hits = err.getvalue().count("could not be listed")
    assert hits == 1, (
        f"the unreadable root was named {hits} time(s) across two scans of "
        f"the same run; the contract is once.\n{err.getvalue()!r}")


def test_head1_carrier_lookup_keeps_the_yaml_only_spelling(
        tmp_path: pathlib.Path, monkeypatch) -> None:
    """⛔ The spelling axis (#1603) at the HEAD~1 carrier lookup.

    `_carrier_at_head1` resolves a tenant id back to the file git actually
    holds. Its docstring says `.yaml` ONLY — widening it would let a
    `.yml` carrier answer for a tenant this tool otherwise cannot see, i.e.
    it would make the removal path report a change from a file no other
    site in this tool reads. Blind review measured that claim to have no
    guard: widening it left the whole suite green.
    """
    mod = _import_tool("ops", "backtest_threshold")
    repo = _git_conf_d_repo(tmp_path / "yml-only", "db-a.yml",
                            _BEFORE, _AFTER_REMOVED)
    monkeypatch.chdir(repo)
    assert mod._carrier_at_head1("db-a") is None, (
        "a `.yml` carrier answered for the tenant; that is the spelling "
        "axis (#1603) widened inside a case fix")
    # ...and the control: the same lookup DOES find the spelling it accepts,
    # so the assertion above is not passing for want of any carrier at all.
    repo2 = _git_conf_d_repo(tmp_path / "yaml", "DB-A.YAML",
                             _BEFORE, _AFTER_REMOVED)
    monkeypatch.chdir(repo2)
    assert mod._carrier_at_head1("DB-A") == "./conf.d/DB-A.YAML", (
        "fixture is vacuous — the accepted spelling was not found either")
    # ⛔ And the comparison is EXACT, not case-folded. `config_stem` keeps
    # the carrier's case on purpose, so a folded compare here would hand
    # back a DIFFERENT tenant's file — the write-plane rename this whole
    # line of work exists to prevent, arriving through the back door.
    # Blind review measured the folded version passing the suite.
    assert mod._carrier_at_head1("db-a") is None, (
        "`db-a` resolved to a carrier whose stem is `DB-A`; the stem "
        "comparison must be exact, not case-folded")


def test_git_diff_path_gives_one_answer_about_a_hidden_carrier(
        tmp_path: pathlib.Path, monkeypatch) -> None:
    """⛔ One process, one file, one answer.

    This chain aligned `extract_changes_from_git_diff` on the hidden axis
    and left the two listings beside it alone, so the tool contradicted
    ITSELF inside a single run — `changed_conf_files` and
    `load_conf_files` both reported `.hidden`, while the parser had
    stopped producing changes for it. Measured against `05d3136`, which
    was consistent the other way (it kept the carrier everywhere):

        05d3136   KEPT = [('.hidden','cpu_usage'), ('real','cpu_usage')]
        the split KEPT = [('real','cpu_usage')]
                  changed_conf_files = ['conf.d/.hidden.yaml',
                                        'conf.d/real.yaml']

    Producing that split inside the change whose whole subject is "one
    tree, one answer" is the sharpest way to get this wrong, and blind
    review had already caught the identical split one code path over.
    """
    mod = _import_tool("ops", "backtest_threshold")
    repo = tmp_path / "hidden"
    (repo / "conf.d").mkdir(parents=True)
    run = lambda *a: subprocess.run(  # noqa: E731
        a, cwd=repo, capture_output=True, timeout=60)
    run("git", "init", "-q")
    run("git", "config", "user.email", "t@example.invalid")
    run("git", "config", "user.name", "t")
    for value in (80, 95):
        (repo / "conf.d" / ".hidden.yaml").write_text(
            f"tenants:\n  ghost:\n    cpu_usage: {value}\n", encoding="utf-8")
        (repo / "conf.d" / "real.yaml").write_text(
            f"tenants:\n  real:\n    cpu_usage: {value}\n", encoding="utf-8")
        run("git", "add", "-A")
        run("git", "commit", "-qm", str(value))
    monkeypatch.chdir(repo)

    files = mod.changed_conf_files()
    parsed = mod.load_conf_files(files)
    kept = mod.keep_flat_threshold_changes(
        mod.extract_changes_from_git_diff(), parsed)
    tenants = sorted({c["tenant"] for c in kept})

    assert tenants == ["real"], (
        f"fixture is vacuous or the visible tenant vanished; got {tenants}")
    assert files == ["conf.d/real.yaml"], (
        f"the listing still names a hidden carrier the parser will not "
        f"produce changes for: {files}")
    assert sorted(parsed) == ["real"], (
        f"the loader still keys a hidden carrier: {sorted(parsed)}")
    # ⛔ And `load_conf_files` INDEPENDENTLY. Asserting it only through the
    # list `changed_conf_files` hands it cannot see the loader's own rule:
    # once the listing filters hidden carriers out, reverting the loader
    # changes nothing and the mutant survives. Measured — it did.
    direct = mod.load_conf_files([
        str(repo / "conf.d" / ".hidden.yaml"),
        str(repo / "conf.d" / "real.yaml"),
    ])
    assert sorted(direct) == ["real"], (
        f"`load_conf_files` keyed a hidden carrier it was handed directly: "
        f"{sorted(direct)}")


def test_batch_diagnose_names_a_configmap_key_that_carries_no_tenant(
        monkeypatch) -> None:
    """⛔ The branch that DROPS a key must be walked by a test.

    `discover_tenants` skips a key whose `config_stem` is empty and says so
    on stderr. Nothing exercised that branch: the parity test above hands it
    only legitimate keys and asserts stderr stays CLEAN, so the `if not
    tenant:` arm was an `if` with no test walking it — the repo's own
    self-review checklist calls that hidden dead code, and the coverage bot
    measured it as a 100.0% -> 98.7% regression on this file.

    Why the branch has to stay: the symptom of the bug this whole change is
    about was a tenant quietly missing from the report, so a dropped key is
    exactly the thing that must not be silent.
    """
    import contextlib  # noqa: PLC0415
    import io  # noqa: PLC0415
    import json as _json  # noqa: PLC0415
    mod = _import_tool("ops", "batch_diagnose")
    payload = _json.dumps({"data": {"db-a.yaml": _TENANT_BODY,
                                    ".hidden.yaml": _TENANT_BODY}})

    class _Result:
        returncode = 0
        stdout = payload
        stderr = ""

    monkeypatch.setattr(mod.subprocess, "run", lambda *a, **k: _Result())
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        found = mod.discover_tenants()
    noise = err.getvalue()

    assert found == ["db-a"], (
        f"the hidden key became a tenant, or the visible one vanished: "
        f"{found}")
    assert ".hidden.yaml" in noise, (
        f"the dropped key was skipped SILENTLY; the whole point of this "
        f"change is that a vanished tenant must be audible.\n{noise!r}")
    assert "carries no tenant id" in noise, (
        f"the warning must say WHY the key was dropped; got {noise!r}")
