"""#3276: a caller-given path the run cannot use is named, never a traceback.

Every path a CLI takes from its caller is judged by one check,
`_lib_paths.classify_path` (``os.stat`` and its errno): nothing there is
``*_not_found``, anything else — a path under a parent the caller cannot
enter — is ``*_not_accessible``. Before it each tool asked ``Path.is_dir()``
/ ``exists()`` / ``is_file()`` (a ``PermissionError`` traceback on 3.13, a
silent "not found" on 3.14) or ``os.path.*`` (already "not found").

The table below runs each migrated site, as shipped, on a path whose parent
the running uid cannot enter (``unprivileged_sandbox``: ``nobody`` under
root). Revert any one site to its old check and its row goes red.
"""
from __future__ import annotations

import argparse
import errno
import importlib
import json
import os
import pathlib
import shutil
import stat
import subprocess
import sys

import pytest

from _platform_fs import symlink_or_skip, unprivileged_sandbox  # noqa: E402

REPO = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scripts" / "tools"))
from _lib_paths import classify_path  # noqa: E402  (#3276)

DENIED = os.strerror(errno.EACCES)
#: The same refusal in a tool that speaks Chinese: the wording is
#: `_lib_paths._DETAIL["zh"]`, so the line it lands in is one language.
DENIED_ZH = "無法存取（EACCES）"

#: The tools whose refusal lines are Chinese (fixed, not by `DA_LANG`); every
#: other row's tool speaks English unless `DA_LANG=zh` (BILINGUAL below).
SPEAKS_ZH = {"ops/deprecate_rule.py", "ops/validate_migration.py", "dx/inject_waveform.py",
             "dx/list_subprocess_only_modules.py", "dx/verify_diff.py"}

#: OPS_SITES rows whose tool follows `DA_LANG` for its refusal line: run
#: again under `DA_LANG=zh`, the detail is Chinese too.
BILINGUAL = {"gitops-check", "policy-engine --config-dir", "policy-engine --policy",
             "threshold-govern --config-dir", "notification-tester --config-dir",
             "threshold-recommend --config-dir", "runtime-audit --rule-packs-dir",
             "runtime-audit --runtime-json", "alert-correlate --input",
             "scaffold --from-onboard", "rule-pack-split --rule-packs-dir"}


def _denied(script: str, lang: str = "en") -> str:
    return DENIED_ZH if script in SPEAKS_ZH or lang == "zh" else DENIED


def _one_language(out: str, script: str, lang: str = "en") -> None:
    """A Chinese refusal line carries no English strerror (#3276 review)."""
    if _denied(script, lang) == DENIED_ZH:
        assert DENIED not in out, out[-3000:]

#: (id, script under scripts/tools/ops, argv, the flag the message names,
#: the ``--json`` reason when the tool answers with an envelope, else None —
#: a tool whose document keeps its own shape names the path in a row).
#: {D} is a directory and {F} a file, both under the locked parent; {CONF}
#: a usable conf.d, {MAP} one with `_instance_mapping.yaml`, {RP} a usable
#: rule-packs directory, {BOX} the box.
OPS_SITES = [
    ("gitops-check", "gitops_check.py", ["local", "--dir", "{D}", "--json"], "--dir",
     "dir_not_accessible"),
    ("validate-config --config-dir", "validate_config.py", ["--config-dir", "{D}", "--json"],
     "--config-dir", None),
    ("validate-config --rule-packs", "validate_config.py",
     ["--config-dir", "{CONF}", "--rule-packs", "{D}", "--json"], "--rule-packs", None),
    ("validate-config --policy-dsl", "validate_config.py",
     ["--config-dir", "{CONF}", "--policy-dsl", "{F}", "--json"], "--policy-dsl", None),
    ("validate-config --policy", "validate_config.py",
     ["--config-dir", "{CONF}", "--policy", "{F}", "--json"], "--policy", None),
    ("explain-route --config-dir", "explain_route.py", ["--config-dir", "{D}", "--json"],
     "--config-dir", "dir_not_accessible"),
    ("diagnose --config-dir", "diagnose.py",
     ["db-a", "--config-dir", "{D}", "--show-inheritance", "--json"],
     "--config-dir", "dir_not_accessible"),
    ("explain-route --policy", "explain_route.py",
     ["--config-dir", "{CONF}", "--policy", "{F}", "--json"], "--policy", "policy_unusable"),
    ("explain-route --base-config", "explain_route.py",
     ["--config-dir", "{CONF}", "--trace", "--tenant", "db-a", "--base-config", "{F}", "--json"],
     "--base-config", None),
    ("generate-routes --config-dir", "generate_alertmanager_routes.py",
     ["--config-dir", "{D}", "--dry-run"], "--config-dir", None),
    ("generate-routes --policy", "generate_alertmanager_routes.py",
     ["--config-dir", "{CONF}", "--dry-run", "--policy", "{F}"], "--policy", None),
    ("generate-routes --base-config", "generate_alertmanager_routes.py",
     ["--config-dir", "{CONF}", "--output-configmap", "--base-config", "{F}",
      "-o", "{BOX}/routes.yaml"], "--base-config", None),
    ("analyze-gaps --config-dir", "analyze_rule_pack_gaps.py", ["--config-dir", "{D}"],
     "--config-dir", None),
    ("analyze-gaps --tenant-config", "analyze_rule_pack_gaps.py", ["--tenant-config", "{F}"],
     "--tenant-config", None),
    ("analyze-gaps --metric-dictionary", "analyze_rule_pack_gaps.py",
     ["--config-dir", "{CONF}", "--metric-dictionary", "{F}"], "--metric-dictionary", None),
    ("generator-service --repo", "generator_service.py",
     ["--socket", "{BOX}/gs.sock", "--repo", "{D}", "--config-subdir", "conf.d",
      "--work-dir", "{BOX}/gs-work"], "--repo", None),
    ("lint-custom-rules (dir)", "lint_custom_rules.py", ["{D}"], None, None),
    ("lint-custom-rules (file)", "lint_custom_rules.py", ["{F}"], None, None),
    ("drift-detect operator", "drift_detect.py", ["--mode", "operator", "--dirs", "{D}", "--json"],
     "--dirs", "dir_not_accessible"),
    ("drift-detect configmap", "drift_detect.py", ["--dirs", "{CONF},{D}", "--json"],
     "--dirs", "dir_not_accessible"),
    ("config-diff --old-dir", "config_diff.py",
     ["--old-dir", "{D}", "--new-dir", "{CONF}", "--format", "json"], "--old-dir",
     "dir_not_accessible"),
    ("config-diff --new-dir", "config_diff.py",
     ["--old-dir", "{CONF}", "--new-dir", "{D}", "--format", "json"], "--new-dir",
     "dir_not_accessible"),
    ("generate-tenant-mapping --config-dir", "generate_tenant_mapping_rules.py",
     ["--config-dir", "{D}", "--dry-run"], "--config-dir", None),
    ("generate-tenant-mapping --metric-dictionary", "generate_tenant_mapping_rules.py",
     ["--config-dir", "{MAP}", "--metric-dictionary", "{F}", "--dry-run"],
     "--metric-dictionary", None),
    ("silencer-drift-check --rule-source", "silencer_drift_check.py",
     ["--silences-file", "{BOX}/silences.json", "--rule-source", "{D}", "--json"],
     "--rule-source", "path_not_accessible"),
    ("policy-engine --config-dir", "policy_engine.py", ["--config-dir", "{D}", "--json"],
     "--config-dir", "dir_not_accessible"),
    ("policy-engine --policy", "policy_engine.py",
     ["--config-dir", "{CONF}", "--policy", "{F}", "--json"], "--policy", "file_not_accessible"),
    ("threshold-govern --config-dir", "threshold_govern.py", ["--config-dir", "{D}", "--json"],
     "--config-dir", "dir_not_accessible"),
    ("deprecate-rule --config-dir", "deprecate_rule.py", ["--config-dir", "{D}", "some_metric"],
     "--config-dir", None),
    ("notification-tester --config-dir", "notification_tester.py",
     ["--config-dir", "{D}", "--dry-run", "--json"], "--config-dir", "dir_not_accessible"),
    ("notification-tester --policy", "notification_tester.py",
     ["--config-dir", "{CONF}", "--policy", "{F}", "--dry-run", "--json"], "--policy",
     "policy_unusable"),
    ("grafana-import --dashboard-dir", "grafana_import.py",
     ["--dashboard-dir", "{D}", "--dry-run", "--json"], "--dashboard-dir", "dir_not_accessible"),
    ("grafana-import --dashboard", "grafana_import.py",
     ["--dashboard", "{F}", "--dry-run", "--json"], "--dashboard", None),
    ("assemble-config-dir --sources", "assemble_config_dir.py",
     ["--sources", "{D}", "--check", "--json"], "--sources", "dir_not_accessible"),
    ("maintenance-scheduler --config-dir", "maintenance_scheduler.py",
     ["--config-dir", "{D}", "--dry-run", "--json-output"], "--config-dir", "dir_not_accessible"),
    ("threshold-recommend --config-dir", "threshold_recommend.py",
     ["--config-dir", "{D}", "--dry-run", "--json"], "--config-dir", "dir_not_accessible"),
    ("explain-keys --config-dir", "explain_keys.py", ["--config-dir", "{D}", "--json"],
     "--config-dir", None),
    ("runtime-audit --rule-packs-dir", "runtime_audit.py",
     ["--rule-packs-dir", "{D}", "--runtime-json", "{BOX}/silences.json", "--json"],
     "--rule-packs-dir", None),
    # #3415
    ("alert-correlate --input", "alert_correlate.py", ["--input", "{F}", "--json"], "--input",
     "file_not_accessible"),
    ("validate --mapping", "validate_migration.py", ["--mapping", "{F}"], "--mapping", None),
    ("cutover --readiness-json", "cutover_tenant.py",
     ["--readiness-json", "{F}", "--tenant", "t", "--dry-run", "--json"], "--readiness-json",
     None),
    ("scaffold --from-onboard", "scaffold_tenant.py", ["--from-onboard", "{F}"],
     "--from-onboard", None),
    ("rule-pack-split --rule-packs-dir", "generate_rule_pack_split.py",
     ["--rule-packs-dir", "{D}", "--output-dir", "{BOX}/rps-out", "--json"], "--rule-packs-dir",
     "dir_not_accessible"),
    ("runtime-audit --runtime-json", "runtime_audit.py",
     ["--rule-packs-dir", "{RP}", "--runtime-json", "{F}", "--json"], "--runtime-json", None),
    # config-history is not a row: every subcommand creates `.da-history`
    # beside --config-dir first, so a locked parent never reaches its check;
    # test_config_history.py pins that check's message on a missing dir.
]

#: (id, script under scripts/tools, argv, the text the message names before
#: ``: Permission denied``, the exit code). Placeholders as in OPS_SITES,
#: plus {WF}: the waveform fixtures. No dx tool here answers with a `--json`
#: envelope. tenant-verify's caller errors are rc 1 (its sanctioned inverted
#: contract, #3393); the rest rc 2.
DX_SITES = [
    ("migrate-conf-d --conf-d", "dx/migrate_conf_d.py", ["--conf-d", "{D}", "--dry-run"],
     "--conf-d {D}", 2),
    ("run-chaos-soak --config-dir", "dx/run_chaos_soak.py",
     ["--target-url", "http://127.0.0.1:9", "--config-dir", "{D}", "--output-dir", "{BOX}/soak"],
     "--config-dir {D}", 2),
    ("reword-chain mapping", "dx/reword_chain.py", ["{F}", "--dry-run"], "mapping file {F}", 2),
    ("describe-tenant --conf-d", "dx/describe_tenant.py", ["t", "--conf-d", "{D}"],
     "--conf-d {D}", 2),
    ("describe-tenant --what-if", "dx/describe_tenant.py",
     ["t", "--conf-d", "{CONF}", "--what-if", "{F}", "--replaces", "{CONF}/tenants.yaml"],
     "--what-if {F}", 2),
    ("converge-status --scope", "dx/converge_status.py", ["--scope", "{D}"], "--scope {D}", 2),
    ("add-frontmatter --base-dir", "dx/add_frontmatter.py", ["--base-dir", "{D}", "--check"],
     "--base-dir {D}", 2),
    ("analyze-probe --from-log", "dx/analyze_probe.py", ["--from-log", "{F}"],
     "--from-log {F}", 2),
    ("analyze-probe --archive", "dx/analyze_probe.py", ["--archive", "{D}"], "--archive {D}", 2),
    # #3276: stdlib-only tools, reached once the judgement left `_lib_io`.
    ("coverage-delta before", "dx/coverage_delta.py", ["{F}", "{F}"],
     "coverage report not usable: {F}", 2),
    ("pr-preflight --check-commit-msg", "dx/pr_preflight.py", ["--check-commit-msg", "{F}"],
     "--check-commit-msg {F}", 2),
    ("pair-bench-ratio --reference", "dx/pair_bench_ratio.py",
     ["--reference", "{F}", "--main", "{BOX}/silences.json", "--reference-tag", "x",
      "--out", "{BOX}/pair.json"], "--reference {F}", 2),
    ("tenant-verify --conf-d", "dx/tenant_verify.py", ["t", "--conf-d", "{D}"],
     "--conf-d {D}", 1),
    ("generate-changelog --fragments", "dx/generate_changelog.py", ["--fragments", "{F}"],
     "--fragments {F}", 2),
    ("generate-changelog --lint", "dx/generate_changelog.py", ["--lint", "{F}"], "--lint {F}", 2),
    ("inject-waveform --rules", "dx/inject_waveform.py",
     ["{WF}/selftest_disk_used_percent.yaml", "--rules", "{D}", "--allow-selftest"],
     "--rules {D}", 2),
    ("inject-waveform --vmalert", "dx/inject_waveform.py",
     ["{WF}/selftest_disk_used_percent.yaml", "--rules", "{WF}/rules/selftest_disk.rules.yaml",
      "--allow-selftest", "--vmalert", "{F}"], "--vmalert {F}", 2),
    ("generate-tenant-metadata --config-dir", "dx/generate_tenant_metadata.py",
     ["--config-dir", "{D}"], "--config-dir {D}", 2),
    ("generate-tenant-metadata --state", "dx/generate_tenant_metadata.py",
     ["--config-dir", "{CONF}", "--state", "{F}"], "--state {F}", 2),
    ("offboard-tenant --state", "ops/offboard_tenant.py",
     ["tenant-a", "--config-dir", "{CONF}", "--state", "{F}"], "--state {F}", 2),
    ("agent-output-metrics changelog --path", "dx/agent_output_metrics.py",
     ["changelog", "--path", "{F}"], "--path {F}", 2),
    ("write-baseline-marker --baseline", "dx/write_baseline_marker.py",
     ["--baseline", "{F}", "--out", "{BOX}/marker"], "--baseline {F}", 2),
    ("generate-alert-reference --output-dir", "dx/generate_alert_reference.py",
     ["--output-dir", "{D}"], "--output-dir {D}", 2),
    ("generate-rule-pack-readme --rule-packs-dir", "dx/generate_rule_pack_readme.py",
     ["--rule-packs-dir", "{D}"], "--rule-packs-dir {D}", 2),
    ("sync-glossary-abbr --glossary", "dx/sync_glossary_abbr.py",
     ["--glossary", "{F}", "--check"], "--glossary {F}", 2),
    ("doc-coverage --repo-root", "dx/doc_coverage.py", ["--repo-root", "{D}"],
     "--repo-root {D}", 2),
    # The docs directory is derived from --repo-root: named without the flag.
    ("generate-nav --repo-root", "dx/generate_nav.py", ["--repo-root", "{D}"], "{D}/docs", 2),
    ("list-subprocess-only-modules --repo", "dx/list_subprocess_only_modules.py",
     ["--repo", "{D}"], "--repo {D}", 2),
    ("verify-diff --rules", "dx/verify_diff.py", ["--rules", "{F}", "--check"], "--rules {F}", 2),
    ("compile-custom-alerts --config-dir", "dx/compile_custom_alerts.py",
     ["--config-dir", "{D}", "--check"], "--config-dir {D}", 2),
    ("render-soak-diff --input-dir", "dx/render_soak_diff.py", ["--input-dir", "{D}"],
     "--input-dir {D}", 2),
    # {NX}: a directory that stats but cannot be entered — what is under it
    # is judged by its own check, past the one on the flag.
    ("list-subprocess-only-modules .git", "dx/list_subprocess_only_modules.py",
     ["--repo", "{NX}"], "{NX}/.git", 2),
    ("analyze-probe --archive run file", "dx/analyze_probe.py", ["--archive", "{NX}"],
     "--archive {NX}/probe-run1.txt", 2),
    # Not rows — the locked case does not reach their check; each is pinned
    # below by a path whose stat fails with ENAMETOOLONG (not "nothing
    # there"), which the old checks raised on or called missing:
    # * measure-judgement-budget --tree: amtool and da-guard are asked first;
    # * describe-tenant --replaces: the file is inside the scanned conf.d;
    # * generate-rule-pack-readme's k8s tree: derived, after the rule packs;
    # * list-subprocess-only-modules --coverage-data: after the population,
    #   which needs the repo's pyproject.toml and git.
    # describe-tenant's --what-if hint (no --replaces / --at) names no path:
    # test_a_what_if_hint_under_a_locked_parent_is_no_traceback.
]


@pytest.fixture(scope="module")
def box():
    """One sandbox for the table: a locked parent holding a directory and a
    file, a usable conf.d, one with an instance mapping, a silences dump."""
    with unprivileged_sandbox(str(REPO)) as (root, run):
        root = pathlib.Path(root)
        locked = root / "locked"
        (locked / "x").mkdir(parents=True)
        (locked / "f.yaml").write_text("a: 1\n", encoding="utf-8")
        nx = root / "nx"
        nx.mkdir()
        for src, dst in ((REPO / "tests" / "golden" / "fixtures" / "flat" / "conf.d", "conf.d"),
                         (REPO / "components" / "threshold-exporter" / "config" / "conf.d"
                          / "examples", "mapped")):
            shutil.copytree(src, root / dst)
        (root / "silences.json").write_text("[]\n", encoding="utf-8")
        (root / "rp").mkdir()
        shutil.copy2(REPO / "rule-packs" / "rule-pack-mariadb.yaml", root / "rp")
        # inject-waveform: its fixtures, and the VM harness it loads before
        # it asks for --vmalert (the box holds scripts/ only).
        shutil.copytree(REPO / "tests" / "dx" / "fixtures" / "waveform", root / "wf")
        (root / "tests" / "rulepacks").mkdir(parents=True)
        shutil.copy2(REPO / "tests" / "rulepacks" / "vm_harness.py",
                     root / "tests" / "rulepacks" / "vm_harness.py")
        for top in ("locked", "conf.d", "mapped", "silences.json", "wf", "tests", "rp"):
            for p in [root / top, *(root / top).rglob("*")]:
                p.chmod(0o755 if p.is_dir() else 0o644)
        # 000, not 0700: the owner is root, but a non-root CI runs the table
        # as itself — and an owner still cannot enter a 000 directory.
        locked.chmod(0)
        nx.chmod(0o644)   # r, no x: the directory stats, nothing under it does
        probe = run([sys.executable, "-c", "import os, sys; os.stat(sys.argv[1])",
                     str(locked / "x")])
        assert probe.returncode != 0, ("the uid can enter the locked parent: every row "
                                       "below would be vacuous")
        yield root, run, {"{D}": str(locked / "x"), "{F}": str(locked / "f.yaml"),
                          "{CONF}": str(root / "conf.d"), "{MAP}": str(root / "mapped"),
                          "{WF}": str(root / "wf"), "{BOX}": str(root), "{NX}": str(nx),
                          "{RP}": str(root / "rp")}


def _expand(arg: str, subst: dict) -> str:
    for k, v in subst.items():
        arg = arg.replace(k, v)
    return arg


@pytest.mark.parametrize("script, argv, flag, reason",
                         [c[1:] for c in OPS_SITES], ids=[c[0] for c in OPS_SITES])
def test_a_path_under_a_parent_it_cannot_enter_is_named(box, script, argv, flag, reason):
    _ops_row(box, script, argv, flag, reason, "en")


@pytest.mark.parametrize("script, argv, flag, reason",
                         [c[1:] for c in OPS_SITES if c[0] in BILINGUAL],
                         ids=[c[0] for c in OPS_SITES if c[0] in BILINGUAL])
def test_a_bilingual_tool_names_it_in_chinese_under_da_lang_zh(box, script, argv, flag, reason):
    _ops_row(box, script, argv, flag, reason, "zh")


def _ops_row(box, script, argv, flag, reason, lang):
    root, run, subst = box
    args = [_expand(a, subst) for a in argv]
    path = subst["{F}"] if "{F}" in " ".join(argv) else subst["{D}"]
    proc = run(["env", f"DA_LANG={lang}", sys.executable,
                str(root / "scripts" / "tools" / "ops" / script), *args])
    out = proc.stdout + proc.stderr
    assert proc.returncode == 2, out[-3000:]
    assert "Traceback" not in out, out[-3000:]
    named = f"{flag} {path}" if flag else path
    assert f"{named}: {_denied('ops/' + script, lang)}" in out, out[-3000:]
    _one_language(out, "ops/" + script, lang)
    if reason is not None:
        assert json.loads(proc.stdout)["reason"] == reason, proc.stdout


@pytest.mark.parametrize("script, argv, named, rc",
                         [c[1:] for c in DX_SITES], ids=[c[0] for c in DX_SITES])
def test_a_dx_path_under_a_parent_it_cannot_enter_is_named(box, script, argv, named, rc):
    root, run, subst = box
    args = [_expand(a, subst) for a in argv]
    proc = run([sys.executable, str(root / "scripts" / "tools" / script), *args],
               stdin=subprocess.DEVNULL)
    out = proc.stdout + proc.stderr
    assert proc.returncode == rc, out[-3000:]
    assert "Traceback" not in out, out[-3000:]
    assert f"{_expand(named, subst)}: {_denied(script)}" in out, out[-3000:]
    _one_language(out, script)


def test_a_what_if_hint_under_a_locked_parent_is_no_traceback(box):
    """describe-tenant --what-if without --replaces / --at only hints at the
    tree file a draft could stand in for; `is_file()` on a draft under a
    locked parent raised there (3.13) instead of reaching the usage error."""
    root, run, subst = box
    proc = run([sys.executable, str(root / "scripts" / "tools" / "dx" / "describe_tenant.py"),
                "t", "--conf-d", subst["{CONF}"], "--what-if", subst["{F}"]])
    out = proc.stdout + proc.stderr
    assert proc.returncode == 2, out[-3000:]
    assert "Traceback" not in out and "--what-if needs --replaces" in out, out[-3000:]


# ── the sites the locked case cannot reach ──────────────────────────────
# A path whose last component is longer than NAME_MAX: its stat fails with
# ENAMETOOLONG, which is not "nothing there" — the old `is_dir()` /
# `exists()` raised on it (3.13), `os.path.isdir` called it "not a directory".

@pytest.fixture
def too_long(tmp_path):
    return tmp_path / ("n" * 300)


TOO_LONG = os.strerror(errno.ENAMETOOLONG)


def _dx(name):
    sys.path.insert(0, str(REPO / "scripts" / "tools" / "dx"))
    return importlib.import_module(name)


def test_measure_judgement_budget_tree(too_long, monkeypatch):
    mjb = _dx("measure_judgement_budget")
    monkeypatch.setattr(mjb.shutil, "which", lambda name: sys.executable)
    a = argparse.Namespace(tree=str(too_long), da_guard_binary=sys.executable, work_dir=None)
    with pytest.raises(mjb.NotMeasured) as exc:
        mjb._preflight(a)
    assert str(exc.value) == f"--tree {too_long}: {TOO_LONG}"


def test_rule_pack_readme_k8s_tree(tmp_path):
    readme = _dx("generate_rule_pack_readme")
    with pytest.raises(FileNotFoundError) as exc:
        readme.count_preloaded_packs(tmp_path / ("n" * 300) / "rule-packs")
    assert f"{tmp_path / ('n' * 300) / 'k8s' / '03-monitoring'}: {TOO_LONG}" in str(exc.value)


def test_list_subprocess_only_modules_coverage_data(too_long):
    pytest.importorskip("coverage")
    lsom = _dx("list_subprocess_only_modules")
    with pytest.raises(RuntimeError) as exc:
        lsom.read_coverage_data(too_long)
    assert f"--coverage-data {too_long}: 無法存取（ENAMETOOLONG）" in str(exc.value)


def test_describe_tenant_replaces(tmp_path):
    conf = tmp_path / "conf.d"
    shutil.copytree(REPO / "tests" / "golden" / "fixtures" / "flat" / "conf.d", conf)
    draft = tmp_path / "draft.yaml"
    draft.write_text("tenants: {}\n", encoding="utf-8")
    target = conf / ("n" * 300 + ".yaml")
    proc = subprocess.run([sys.executable,
                           str(REPO / "scripts" / "tools" / "dx" / "describe_tenant.py"),
                           "tenant-a", "--conf-d", str(conf), "--what-if", str(draft),
                           "--replaces", str(target)],
                          capture_output=True, text=True, encoding="utf-8", timeout=120)
    out = proc.stdout + proc.stderr
    assert proc.returncode == 2, out[-3000:]
    assert "Traceback" not in out and f"--replaces {target}: {TOO_LONG}" in out, out[-3000:]


# ── the exit codes this migration changes (owner-approved, #3276) ─────────

def _cli(script, *argv, **kw):
    return subprocess.run([sys.executable, str(REPO / "scripts" / "tools" / "dx" / script), *argv],
                          capture_output=True, text=True, encoding="utf-8", timeout=120, **kw)


@pytest.mark.parametrize("name", ["nope", "d"])
def test_reword_chain_mapping_not_a_file_is_a_caller_error(tree, name):
    """E1: a mapping that is missing or not a file was rc 1 (`SystemExit(<text>)`)."""
    proc = _cli("reword_chain.py", str(tree / name), "--dry-run")
    assert proc.returncode == 2, proc.stderr
    assert f"ERROR: mapping file {tree / name}: " in proc.stderr


@pytest.mark.parametrize("name", ["nope", "d"])
def test_write_baseline_marker_baseline_not_a_file_is_a_caller_error(tree, name):
    """E2: a --baseline that is missing or not a file was rc 1."""
    proc = _cli("write_baseline_marker.py", "--baseline", str(tree / name),
                "--out", str(tree / "marker"))
    assert proc.returncode == 2, proc.stderr
    assert f"::error::--baseline {tree / name}: " in proc.stderr
    assert not (tree / "marker").exists()


def test_analyze_probe_from_log_directory_is_a_caller_error(tree):
    """E3: a --from-log that is a directory was a traceback at rc 1 (the
    locked cases are DX_SITES rows)."""
    proc = _cli("analyze_probe.py", "--from-log", str(tree / "d"))
    out = proc.stdout + proc.stderr
    assert proc.returncode == 2, out
    assert "Traceback" not in out
    assert f"::error::--from-log {tree / 'd'}: {os.strerror(errno.EISDIR)}" in out


def test_doc_coverage_repo_root_file_is_a_caller_error(tree):
    """E4: a --repo-root that is a file passed `exists()`: rc 0 over no docs."""
    proc = _cli("doc_coverage.py", "--repo-root", str(tree / "f"))
    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert f"--repo-root {tree / 'f'}: not a directory" in proc.stderr


# ── the judgement itself ─────────────────────────────────────────────────

@pytest.fixture
def tree(tmp_path):
    (tmp_path / "d").mkdir()
    (tmp_path / "f").write_text("x", encoding="utf-8")
    return tmp_path


@pytest.mark.parametrize("kind, name, reason, missing", [
    ("dir", "d", None, None),
    ("dir", "nope", "dir_not_found", True),
    ("dir", "f", "dir_not_found", False),
    ("dir", "f/below", "dir_not_found", True),       # ENOTDIR: nothing is there
    ("file", "f", None, None),
    ("file", "nope", "file_not_found", True),
    ("file", "d", "file_not_found", False),
    ("any", "d", None, None),
    ("any", "f", None, None),
    ("any", "nope", "path_not_found", True),
])
def test_classify_path(tree, kind, name, reason, missing):
    problem = classify_path(tree / name, kind, lang="en", flag="--x")
    if reason is None:
        assert problem is None
        return
    assert (problem.reason, problem.missing) == (reason, missing)
    assert problem.message.startswith(f"--x {tree / name}: ")


def test_an_empty_path_is_shown_as_supplied(tmp_path):
    """'' is what an unset shell variable expands to: named, never blank."""
    assert classify_path("", "file", lang="en", flag="--policy").message.startswith("--policy '': ")


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="POSIX FIFOs")
def test_a_file_is_a_regular_file(tmp_path):
    """A FIFO or a device is not a file (as `is_file()` said): a reader that
    asks `is_file()` would take it for "no file" and pass on nothing — and it
    is never opened, so a writer waiting on the FIFO is not woken."""
    fifo = tmp_path / "p"
    os.mkfifo(fifo)
    assert stat.S_ISFIFO(os.stat(fifo).st_mode)
    # Held open read-write by the test (never blocks on Linux), so a check
    # that wrongly opens the FIFO returns at once and fails here — not hangs.
    held = os.open(fifo, os.O_RDWR)
    try:
        for p in (fifo, os.devnull):
            problem = classify_path(p, "file", lang="en", flag="--policy")
            assert problem is not None, p
            assert (problem.reason, problem.missing) == ("file_not_found", False), p
            assert problem.message == f"--policy {p}: not a regular file"
    finally:
        os.close(held)


@pytest.mark.parametrize("lang, words", [("zh", "--dir 無法使用："), ("en", "ERROR: --dir ")])
def test_a_tool_keeps_its_own_words(tmp_path, lang, words):
    """The judgement is shared; each tool's language for it stays its own —
    the judgement's detail included."""
    import subprocess
    proc = subprocess.run([sys.executable, str(REPO / "scripts" / "tools" / "ops" / "gitops_check.py"),
                           "local", "--dir", str(tmp_path / "nope")],
                          capture_output=True, text=True, encoding="utf-8", timeout=120,
                          env={**os.environ, "DA_LANG": lang})
    assert proc.returncode == 2, proc.stderr
    detail = "不存在" if lang == "zh" else os.strerror(errno.ENOENT)
    assert f"{words}" in proc.stderr and f"{tmp_path / 'nope'}: {detail}" in proc.stderr, proc.stderr


def test_policy_on_dev_null_is_refused(tmp_path):
    """The fail-open the review measured: `--policy /dev/null` evaluated no
    policy at rc 0. A device is not the policy file the flag asks for."""
    import subprocess
    (tmp_path / "t.yaml").write_text("tenants:\n  t: {}\n", encoding="utf-8")
    proc = subprocess.run([sys.executable, str(REPO / "scripts" / "tools" / "ops" / "policy_engine.py"),
                           "--config-dir", str(tmp_path), "--policy", os.devnull, "--ci"],
                          capture_output=True, text=True, encoding="utf-8", timeout=120)
    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert f"--policy {os.devnull}: not a regular file" in proc.stderr


def test_the_judgement_runs_without_pyyaml_or_the_crash_hook():
    """`_lib_paths` is for every tool, including those that run without
    PyYAML (pr-preflight, coverage-delta's CI job) and without the crash hook
    (analyze-probe): importing it pulls in neither, and a refusal still raises
    the named CallerError."""
    import subprocess
    probe = (
        "import builtins, sys\n"
        "real = builtins.__import__\n"
        "def guard(name, *a, **k):\n"
        "    if name.split('.')[0] == 'yaml':\n"
        "        raise ImportError('yaml is not installed here')\n"
        "    return real(name, *a, **k)\n"
        "builtins.__import__ = guard\n"
        f"sys.path.insert(0, {str(REPO / 'scripts' / 'tools')!r})\n"
        "hook = sys.excepthook\n"
        "import _lib_paths\n"
        "assert '_lib_exitcodes' not in sys.modules, 'imported _lib_exitcodes'\n"
        "assert sys.excepthook is hook, 'installed a crash hook'\n"
        "assert _lib_paths.classify_path('/nonexistent/x', 'dir', lang='en').missing\n"
        "try:\n"
        "    _lib_paths.require_dir('/nonexistent/x', lang='en', flag='--x')\n"
        "except Exception as exc:\n"
        "    print(type(exc).__name__, exc.reason)\n"
    )
    proc = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True,
                          encoding="utf-8", timeout=60)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "CallerError dir_not_found", proc.stdout



@pytest.mark.parametrize("kind, name, detail", [
    ("dir", "nope", "不存在"),
    ("dir", "f/below", "不存在（ENOTDIR）"),
    ("any", "loop", "不存在（ELOOP）"),
    ("dir", "f", "不是目錄"),
    ("file", "d", "不是一般檔案"),
    ("any", "nope", "不存在"),
])
def test_the_chinese_wording(tree, kind, name, detail):
    """`lang="zh"`: the same verdicts, said in Chinese — no English strerror."""
    if name == "loop":
        symlink_or_skip(tree / "loop", tree / "loop")
    problem = classify_path(tree / name, kind, lang="zh", flag="--x")
    assert problem.message == f"--x {tree / name}: {detail}"


@pytest.mark.parametrize("lang, line", [
    ("zh", "錯誤：目錄無法使用 --config-dir {p}: 不存在"),
    ("en", "Error: --config-dir {p}: " + os.strerror(errno.ENOENT)),
])
def test_config_history_says_it_in_its_own_language(tmp_path, lang, line):
    """config-history is no OPS_SITES row (a locked parent never reaches its
    check); a missing --config-dir does, in both of its languages."""
    p = tmp_path / "nope"
    proc = subprocess.run([sys.executable,
                           str(REPO / "scripts" / "tools" / "ops" / "config_history.py"),
                           "--config-dir", str(p), "snapshot"],
                          capture_output=True, text=True, encoding="utf-8", timeout=120,
                          env={**os.environ, "DA_LANG": lang})
    assert proc.returncode == 2, proc.stderr
    assert line.format(p=p) in proc.stderr, proc.stderr


def test_every_language_words_every_verdict():
    """A wording added to one language and not the other is a KeyError the
    first time that verdict is reached in the other language."""
    import _lib_paths
    keys = {lang: set(table) for lang, table in _lib_paths._DETAIL.items()}
    assert len(set(map(frozenset, keys.values()))) == 1, keys


def test_the_language_is_never_implied(tree):
    """No default: a caller that forgot `lang` would put English inside a
    Chinese line without anyone noticing — it is a TypeError instead."""
    import _lib_paths
    for call in (lambda: classify_path(tree / "nope", "dir"),
                 lambda: _lib_paths.require_dir(tree / "nope"),
                 lambda: _lib_paths.require_file(tree / "nope"),
                 lambda: _lib_paths.require_path(tree / "nope", "any")):
        with pytest.raises(TypeError, match="lang"):
            call()


# ── #3415: inputs that bypassed the judgement ─────────────────────────────
# Missing or of the wrong type, each was a traceback (alert-correlate,
# validate, cutover, scaffold), "no rule pack files found" after the output
# tree was made (rule-pack-split), or a message cut at 60 characters
# (runtime-audit). The locked-parent rows above cover EACCES.

NOT_FOUND = os.strerror(errno.ENOENT)

#: (id, script under ops/, argv with {P} for the path under test, the flag,
#: "file" or "dir", the tool's language for the detail).
INPUT_SITES_3415 = [
    ("alert-correlate --input", "alert_correlate.py", ["--input", "{P}"], "--input", "file", "en"),
    ("validate --mapping", "validate_migration.py", ["--mapping", "{P}"], "--mapping", "file", "zh"),
    ("cutover --readiness-json", "cutover_tenant.py",
     ["--readiness-json", "{P}", "--tenant", "t", "--dry-run"], "--readiness-json", "file", "en"),
    ("scaffold --from-onboard", "scaffold_tenant.py", ["--from-onboard", "{P}"],
     "--from-onboard", "file", "en"),
    ("rule-pack-split --rule-packs-dir", "generate_rule_pack_split.py",
     ["--rule-packs-dir", "{P}", "--output-dir", "{OUT}"], "--rule-packs-dir", "dir", "en"),
    ("runtime-audit --runtime-json", "runtime_audit.py",
     ["--rule-packs-dir", str(REPO / "rule-packs"), "--runtime-json", "{P}"], "--runtime-json",
     "file", "en"),
]

_DETAIL_3415 = {
    ("missing", "en"): NOT_FOUND, ("missing", "zh"): "不存在",
    ("file", "en"): "not a regular file", ("file", "zh"): "不是一般檔案",
    ("dir", "en"): "not a directory", ("dir", "zh"): "不是目錄",
}


def _run_ops(script, args):
    env = {k: v for k, v in os.environ.items() if k != "DA_LANG"}
    env["DA_LANG"] = "en"
    return subprocess.run([sys.executable, str(REPO / "scripts" / "tools" / "ops" / script), *args],
                          capture_output=True, text=True, encoding="utf-8", timeout=120,
                          env=env, stdin=subprocess.DEVNULL)


@pytest.mark.usefixtures("da_read_env")
@pytest.mark.parametrize("case", ["missing", "wrong_type"])
@pytest.mark.parametrize("script, argv, flag, kind, lang",
                         [c[1:] for c in INPUT_SITES_3415], ids=[c[0] for c in INPUT_SITES_3415])
def test_an_unusable_input_is_named_not_a_traceback(tmp_path, script, argv, flag, kind, lang,
                                                     case):
    if case == "missing":
        # Longer than the 60 characters runtime-audit used to cut at.
        path = tmp_path / ("missing-" + "x" * 80)
        detail = _DETAIL_3415[("missing", lang)]
    else:
        # A file where a directory is wanted, a directory where a file is.
        path = tmp_path / "given"
        if kind == "dir":
            path.write_text("a: 1\n", encoding="utf-8")
        else:
            path.mkdir()
        detail = _DETAIL_3415[(kind, lang)]
    out_dir = tmp_path / "out"
    args = [a.replace("{P}", str(path)).replace("{OUT}", str(out_dir)) for a in argv]
    proc = _run_ops(script, args)
    out = proc.stdout + proc.stderr
    assert proc.returncode == 2, out[-3000:]
    assert "Traceback" not in out, out[-3000:]
    assert f"{flag} {path}: {detail}" in out, out[-3000:]
    assert not out_dir.exists(), "an output tree was made before the input was judged"


def test_scaffold_names_onboard_hints_that_are_not_json(tmp_path):
    hints = tmp_path / "hints.json"
    hints.write_text("{bad", encoding="utf-8")
    proc = _run_ops("scaffold_tenant.py", ["--from-onboard", str(hints)])
    out = proc.stdout + proc.stderr
    assert proc.returncode == 2, out[-3000:]
    assert "Traceback" not in out, out[-3000:]
    assert f"ERROR: --from-onboard {hints}: " in proc.stderr, out[-3000:]


@pytest.mark.parametrize("case", ["missing", "not_executable", "empty"])
def test_generator_service_names_a_git_it_cannot_run(tmp_path, case):
    """--git is an executable: the exec decides, and its failure names the
    flag — `''` shown as `''`, as `_lib_paths` shows a supplied empty value."""
    git = tmp_path / "git"
    if case == "not_executable":
        git.write_text("#!/bin/sh\n", encoding="utf-8")
        git.chmod(0o644)
    (tmp_path / "repo").mkdir()
    proc = _run_ops("generator_service.py",
                    ["--socket", str(tmp_path / "gs.sock"), "--repo", str(tmp_path / "repo"),
                     "--config-subdir", "conf.d", "--work-dir", str(tmp_path / "work"),
                     "--git", "" if case == "empty" else str(git)])
    out = proc.stdout + proc.stderr
    assert proc.returncode == 2, out[-3000:]
    if case == "empty":
        assert "ERROR: --git '': " in proc.stderr, out[-3000:]
        return
    detail = NOT_FOUND if case == "missing" else os.strerror(errno.EACCES)
    assert f"ERROR: --git {git}: {detail}" in proc.stderr, out[-3000:]


def test_rule_pack_split_names_a_dir_it_cannot_list(box):
    """A --rule-packs-dir that stats but cannot be listed (x, no r): the glob
    swallowed the EACCES and said "No rule pack files found" after making the
    output tree (#3415 review)."""
    root, run, _subst = box
    noread = root / "noread-3415"
    noread.mkdir(exist_ok=True)
    noread.chmod(0o311)
    out_dir = root / "rps-noread-out"
    proc = run(["env", "DA_LANG=en", sys.executable,
                str(root / "scripts" / "tools" / "ops" / "generate_rule_pack_split.py"),
                "--rule-packs-dir", str(noread), "--output-dir", str(out_dir), "--json"])
    out = proc.stdout + proc.stderr
    assert proc.returncode == 2, out[-3000:]
    assert f"--rule-packs-dir {noread}: {DENIED}" in out, out[-3000:]
    assert json.loads(proc.stdout)["reason"] == "dir_not_accessible", proc.stdout
    assert not out_dir.exists(), "the output tree was made before the input was judged"
