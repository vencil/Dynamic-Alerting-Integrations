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

* Only the CASE axis. Four readers (`operator_generate`,
  `generate_tenant_metadata`, `check_path_metadata_consistency`,
  `custom_alerts/loader`) accept `*.yaml` and not `*.yml`, so they do not
  see `db-b.yml` at all while the exporter does. That is a real
  divergence on the extension-SPELLING axis; it is deliberately NOT fixed
  here (widening them is a behaviour change that must not ride along
  inside a case fix) and is filed separately.
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

# The three fixture names, and the mapping used to normalise them away.
_PAIRS = (("_DEFAULTS.YAML", "_defaults.yaml"),
          ("alpha.YAML", "alpha.yaml"),
          ("beta.YML", "beta.yml"))


# ── fixtures ──────────────────────────────────────────────────────────
def _write_tree(root: pathlib.Path, *, upper: bool, tenants: bool) -> None:
    """One conf.d. `upper` flips only the NAMES; bodies never change."""
    root.mkdir(parents=True, exist_ok=True)
    names = {"defaults": "_DEFAULTS.YAML" if upper else "_defaults.yaml",
             "alpha": "alpha.YAML" if upper else "alpha.yaml",
             "beta": "beta.YML" if upper else "beta.yml"}
    (root / names["defaults"]).write_text(
        "defaults:\n  pg_connections: 80\n", encoding="utf-8")
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
    for upper, lower in _PAIRS:
        out = out.replace(upper, lower)
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
    __slots__ = ("skip_reason", "insensitive", "lower", "upper")

    def __init__(self, skip_reason=None, insensitive=False,
                 lower="", upper=""):
        self.skip_reason = skip_reason
        self.insensitive = insensitive
        self.lower = lower
        self.upper = upper


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
    return _Outcome(lower=norm["lower"], upper=norm["upper"])


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
    "policy_engine.py",
    "tenant_verify.py",
    "threshold_govern.py",
}


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
