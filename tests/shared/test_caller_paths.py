"""#3276: a caller-given path the run cannot use is named, never a traceback.

Every path a CLI takes from its caller is judged by one check,
`_lib_io.classify_path` (``os.stat`` and its errno): nothing there is
``*_not_found``, anything else — a path under a parent the caller cannot
enter — is ``*_not_accessible``. Before it each tool asked ``Path.is_dir()``
/ ``exists()`` / ``is_file()`` (a ``PermissionError`` traceback on 3.13, a
silent "not found" on 3.14) or ``os.path.*`` (already "not found").

The table below runs each migrated site, as shipped, on a path whose parent
the running uid cannot enter (``unprivileged_sandbox``: ``nobody`` under
root). Revert any one site to its old check and its row goes red.
"""
from __future__ import annotations

import errno
import json
import os
import pathlib
import shutil
import stat
import sys

import pytest

from _platform_fs import unprivileged_sandbox  # noqa: E402

REPO = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scripts" / "tools"))
from _lib_io import classify_path  # noqa: E402

DENIED = os.strerror(errno.EACCES)

#: (id, script under scripts/tools/ops, argv, the flag the message names,
#: the ``--json`` reason when the tool answers with an envelope, else None —
#: a tool whose document keeps its own shape names the path in a row).
#: {D} is a directory and {F} a file, both under the locked parent; {CONF}
#: a usable conf.d, {MAP} one with `_instance_mapping.yaml`, {BOX} the box.
OPS_SITES = [
    ("gitops-check", "gitops_check.py", ["local", "--dir", "{D}", "--json"], "--dir",
     "dir_not_accessible"),
    ("validate-config --config-dir", "validate_config.py", ["--config-dir", "{D}", "--json"],
     "--config-dir", None),
    ("validate-config --rule-packs", "validate_config.py",
     ["--config-dir", "{CONF}", "--rule-packs", "{D}", "--json"], "--rule-packs", None),
    ("validate-config --policy-dsl", "validate_config.py",
     ["--config-dir", "{CONF}", "--policy-dsl", "{F}", "--json"], "--policy-dsl", None),
    ("explain-route --config-dir", "explain_route.py", ["--config-dir", "{D}", "--json"],
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
    ("generator-service --repo", "generator_service.py",
     ["--socket", "{BOX}/gs.sock", "--repo", "{D}", "--config-subdir", "conf.d",
      "--work-dir", "{BOX}/gs-work"], "--repo", None),
    ("lint-custom-rules (dir)", "lint_custom_rules.py", ["{D}"], None, None),
    ("lint-custom-rules (file)", "lint_custom_rules.py", ["{F}"], None, None),
    ("migration-audit --config-dir", "migration_audit.py", ["--config-dir", "{D}", "--json"],
     "--config-dir", None),
    ("drift-detect operator", "drift_detect.py", ["--mode", "operator", "--dirs", "{D}", "--json"],
     "--dirs", "dir_not_accessible"),
    ("drift-detect configmap", "drift_detect.py", ["--dirs", "{CONF},{D}", "--json"],
     "--dirs", "dir_not_accessible"),
    ("config-diff --old-dir", "config_diff.py",
     ["--old-dir", "{D}", "--new-dir", "{CONF}", "--format", "json"], "--old-dir",
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
    # config-history is not a row: every subcommand creates `.da-history`
    # beside --config-dir first, so a locked parent never reaches its check;
    # test_config_history.py pins that check's message on a missing dir.
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
        for src, dst in ((REPO / "tests" / "golden" / "fixtures" / "flat" / "conf.d", "conf.d"),
                         (REPO / "components" / "threshold-exporter" / "config" / "conf.d"
                          / "examples", "mapped")):
            shutil.copytree(src, root / dst)
        (root / "silences.json").write_text("[]\n", encoding="utf-8")
        for top in ("locked", "conf.d", "mapped", "silences.json"):
            for p in [root / top, *(root / top).rglob("*")]:
                p.chmod(0o755 if p.is_dir() else 0o644)
        # 000, not 0700: the owner is root, but a non-root CI runs the table
        # as itself — and an owner still cannot enter a 000 directory.
        locked.chmod(0)
        probe = run([sys.executable, "-c", "import os, sys; os.stat(sys.argv[1])",
                     str(locked / "x")])
        assert probe.returncode != 0, ("the uid can enter the locked parent: every row "
                                       "below would be vacuous")
        yield root, run, {"{D}": str(locked / "x"), "{F}": str(locked / "f.yaml"),
                          "{CONF}": str(root / "conf.d"), "{MAP}": str(root / "mapped"),
                          "{BOX}": str(root)}


def _expand(arg: str, subst: dict) -> str:
    for k, v in subst.items():
        arg = arg.replace(k, v)
    return arg


@pytest.mark.parametrize("script, argv, flag, reason",
                         [c[1:] for c in OPS_SITES], ids=[c[0] for c in OPS_SITES])
def test_a_path_under_a_parent_it_cannot_enter_is_named(box, script, argv, flag, reason):
    root, run, subst = box
    args = [_expand(a, subst) for a in argv]
    path = subst["{F}"] if "{F}" in " ".join(argv) else subst["{D}"]
    proc = run([sys.executable, str(root / "scripts" / "tools" / "ops" / script), *args])
    out = proc.stdout + proc.stderr
    assert proc.returncode == 2, out[-3000:]
    assert "Traceback" not in out, out[-3000:]
    named = f"{flag} {path}" if flag else path
    assert f"{named}: {DENIED}" in out, out[-3000:]
    if reason is not None:
        assert json.loads(proc.stdout)["reason"] == reason, proc.stdout


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
    problem = classify_path(tree / name, kind, flag="--x")
    if reason is None:
        assert problem is None
        return
    assert (problem.reason, problem.missing) == (reason, missing)
    assert problem.message.startswith(f"--x {tree / name}: ")


def test_an_empty_path_is_shown_as_supplied(tmp_path):
    """'' is what an unset shell variable expands to: named, never blank."""
    assert classify_path("", "file", flag="--policy").message.startswith("--policy '': ")


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="POSIX FIFOs")
def test_a_file_is_a_regular_file(tmp_path):
    """A FIFO or a device is not a file (as `is_file()` said): a reader that
    asks `is_file()` would take it for "no file" and pass on nothing — and it
    is never opened, so a writer waiting on the FIFO is not woken."""
    fifo = tmp_path / "p"
    os.mkfifo(fifo)
    assert stat.S_ISFIFO(os.stat(fifo).st_mode)
    for p in (fifo, os.devnull):
        problem = classify_path(p, "file", flag="--policy")
        assert (problem.reason, problem.missing) == ("file_not_found", False), p
        assert problem.message == f"--policy {p}: not a regular file"


@pytest.mark.parametrize("lang, words", [("zh", "--dir 無法使用："), ("en", "ERROR: --dir ")])
def test_a_tool_keeps_its_own_words(tmp_path, lang, words):
    """The judgement is shared; each tool's language for it stays its own."""
    import subprocess
    proc = subprocess.run([sys.executable, str(REPO / "scripts" / "tools" / "ops" / "gitops_check.py"),
                           "local", "--dir", str(tmp_path / "nope")],
                          capture_output=True, text=True, encoding="utf-8", timeout=120,
                          env={**os.environ, "DA_LANG": lang})
    assert proc.returncode == 2, proc.stderr
    assert f"{words}" in proc.stderr and f"{tmp_path / 'nope'}: " in proc.stderr, proc.stderr


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
