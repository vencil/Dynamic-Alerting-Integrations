"""#1582 — a tool pointed at a throwaway ``--config-dir`` must not write into the repo.

The defect: ``compile_custom_alerts`` resolved its default output from
``__file__`` and rewrote the shipped rule pack however ``--config-dir`` was
redirected, and two other tools wrote to a cwd-relative default. Those three
were fixed (#1648, #1739). This file is what keeps the WHOLE family fixed:

* ⛔ THE POPULATION IS DERIVED BY AST, any quoting: every ``add_argument`` call
  naming ``--config-dir`` and every ``add_config_dir_arg(...)`` call under
  ``scripts/tools``. The older sweep in ``test_output_label_escaping.py`` keys
  on the substring ``'"--config-dir"'`` in three subdirectories and so never saw
  ``generate_tenant_mapping_rules`` (single quotes only).
* Every tool runs from a PRIVATE COPY of the tracked tree, with a throwaway
  config dir and a throwaway cwd. "Did the repo change" is then a question about
  a tree nobody else writes, so it holds under ``pytest -n auto`` — the
  whole-tree check in the older file is serial-only and skipped in CI.
* Writes are detected by (mtime_ns, size) plus the set of files AND
  directories (an mkdir alone is a write), not by ``git status``:
  a rewrite with identical bytes is still a write, and a byte-identical fixture
  is exactly how a "did not reproduce" once read as "clean" (#1582 thread).

Each write lands in one of five places. ``repo`` and ``cwd`` are never allowed.
``named`` (under a path passed on the command line) always is. ``config`` (in
the tree pointed at) and ``beside`` (anywhere else around it) must match
:data:`EXPECTED_WRITES` exactly, so a new writer is a failure and a stale entry
is too.

⚠️ SCOPE: only the shapes in :data:`SHAPES` are measured, and writes outside
the test's own tmp dir (``$HOME``, ``/tmp`` at large) are not observed. A shape
that needs a live service (Prometheus, OPA, a cluster) stops before any write
path; each such shape is in :data:`STOPS_EARLY` with the line that proves it
stopped, and stops counting the moment that line disappears. A green case
outside that ledger has been checked to reach its main path only where a
must-fire control says so; the rest are read-only tools.
"""
from __future__ import annotations

import ast
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.usefixtures("da_guard_env", "da_crdecode_env")

REPO_ROOT = Path(__file__).resolve().parents[2]
TOOLS_REL = Path("scripts") / "tools"
# The throwaway tree: the exporter's sample conf.d (threshold keys, tenants)
# with the custom-alert recipe examples as a subtree, so the compilers have
# declarations to compile and the --execute shapes have keys to act on.
FIXTURE_REL = Path("components") / "threshold-exporter" / "config" / "conf.d"
RECIPES_REL = Path("rule-packs") / "recipes" / "examples" / "conf.d"
# Platform-level files the sample tree keeps under examples/, lifted to the
# top: tools read them only there (mapping rules, routing profiles, policy).
LIFTED = ("_instance_mapping.yaml", "_routing_profiles.yaml",
          "_domain_policy.yaml")
# ConfigMap-shaped input for migrate_to_operator's --source-dir.
SOURCE_CM_GLOB = "k8s/03-monitoring/configmap-rules-*.yaml"


def config_dir_tools(root: Path) -> list[str]:
    """Repo-relative paths of every CLI under scripts/tools taking --config-dir."""
    out = []
    for p in sorted((root / TOOLS_REL).rglob("*.py")):
        if "__pycache__" in p.parts or p.name.startswith("_"):
            continue
        tree = ast.parse(p.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(
                func, "id", None)
            if name == "add_config_dir_arg" or (
                    name == "add_argument" and any(
                        isinstance(a, ast.Constant) and a.value == "--config-dir"
                        for a in node.args)):
                out.append(p.relative_to(root).as_posix())
                break
    return out


POPULATION = config_dir_tools(REPO_ROOT)

# Invocation shapes per tool, beyond the bare `--config-dir <cfg>` every tool
# gets. Placeholders: {tenant} / {metric} come from the fixture (no tenant id
# is written here), {named} is a scratch path counted as an explicit output.
SHAPES: dict[str, list[list[str]]] = {
    "compile_custom_alerts.py": [["--check"], ["--out", "{named}/pack.yaml"]],
    "backtest_threshold.py": [["--baseline", "{cfg}"],
                              ["--baseline", "{cfg}", "--skip-if-unavailable",
                               "--markdown-output", "{named}/b.md"]],
    "config_history.py": [["snapshot"], ["log"]],
    "deprecate_rule.py": [["{metric}"], ["{metric}", "--execute"]],
    "offboard_tenant.py": [["{tenant}"], ["{tenant}", "--execute"]],
    "diagnose.py": [["{tenant}"]],
    "policy_opa_bridge.py": [["--policy-path", "{named}/p.rego"]],
    "run_chaos_soak.py": [["--target-url", "http://127.0.0.1:9",
                           "--output-dir", "{named}/soak"]],
    "operator_generate.py": [["--output-dir", "{named}/m"]],
    "migrate_to_operator.py": [["--config-dir", "{cfg}"],
                               ["--config-dir", "{cfg}",
                                "--output-dir", "{named}/m"]],
    "da_assembler.py": [["--render-cr", "{named}/tc.yaml"]],
    "generate_alertmanager_routes.py": [["-o", "{named}/r.yaml"]],
    "generate_tenant_mapping_rules.py": [["-o", "{named}/r.yaml"]],
    "generate_tenant_metadata.py": [["--output", "{named}/m.json"]],
    "analyze_rule_pack_gaps.py": [["-o", "{named}/g.json"]],
}
# Tools whose bare shape fails argument validation (a required flag); their
# measured shapes are all in SHAPES.
NO_BARE_SHAPE = {"run_chaos_soak.py", "config_history.py", "deprecate_rule.py",
                 "offboard_tenant.py", "diagnose.py"}
# `migrate_to_operator` reads ConfigMaps from a required `--source-dir`; its
# `--config-dir` (default `conf.d` under the cwd) is passed by its shapes.
SOURCE_DIR_TOOLS = {"migrate_to_operator.py"}

# (tool, shape args with placeholders) -> {place: path prefix} every write in
# that place must start with. Absent = no write allowed there.
EXPECTED_WRITES: dict[tuple[str, tuple[str, ...]], dict[str, str]] = {
    # Output follows the input: <parent of --config-dir>/.da-history.
    ("config_history.py", ("snapshot",)): {"beside": "run/.da-history/"},
    # Even the read-only `log` creates that directory (an mkdir, no file).
    ("config_history.py", ("log",)): {"beside": "run/.da-history/"},
    # In-place edits of the tree pointed at, which is what --execute is for.
    ("deprecate_rule.py", ("{metric}", "--execute")): {"config": ""},
    ("offboard_tenant.py", ("{tenant}", "--execute")): {"config": ""},
    # Offline render of a CR into the tree --config-dir names.
    ("da_assembler.py", ("--render-cr", "{named}/tc.yaml")): {"config": ""},
}
# Must-fire controls: these shapes MUST produce a write in `named`, or the
# detector (or the shape) is broken and every "no write" above is vacuous.
MUST_WRITE_NAMED = {
    ("compile_custom_alerts.py", ("--out", "{named}/pack.yaml")),
    ("operator_generate.py", ("--output-dir", "{named}/m")),
    ("generate_alertmanager_routes.py", ("-o", "{named}/r.yaml")),
    ("generate_tenant_mapping_rules.py", ("-o", "{named}/r.yaml")),
    ("backtest_threshold.py", ("--baseline", "{cfg}", "--skip-if-unavailable",
                               "--markdown-output", "{named}/b.md")),
    ("migrate_to_operator.py", ("--config-dir", "{cfg}",
                                "--output-dir", "{named}/m")),
}
# Shapes that stop before any write path because a service is absent, with
# the output line that proves they stopped. A shape listed here whose line
# no longer appears has started doing more, and must be re-measured: it fails.
STOPS_EARLY: dict[tuple[str, tuple[str, ...]], str] = {
    ("run_chaos_soak.py", ("--target-url", "http://127.0.0.1:9",
                           "--output-dir", "{named}/soak")):
        "aborting before soak start",
    ("backtest_threshold.py", ()): "--config-dir requires --baseline",
    ("backtest_threshold.py", ("--baseline", "{cfg}")):
        "Prometheus not reachable",
    ("policy_opa_bridge.py", ()): "Must specify --opa-url or --policy-path",
    ("policy_opa_bridge.py", ("--policy-path", "{named}/p.rego")):
        "OPA binary not found",
    ("diagnose.py", ("{tenant}",)): "Prometheus query failed",
    ("da_assembler.py", ()): "kubernetes Python client is required",
}


def _params():
    for rel in POPULATION:
        name = Path(rel).name
        shapes = ([] if name in NO_BARE_SHAPE else [[]]) + SHAPES.get(name, [])
        for shape in shapes:
            yield pytest.param(rel, tuple(shape),
                               id=f"{name}[{' '.join(shape) or 'bare'}]")


@pytest.fixture(scope="session")
def repo_copy(tmp_path_factory) -> Path:
    """The tracked tree, copied. Tools run from here, so a `__file__`-anchored
    default lands in the copy, where this worker alone can see it."""
    dst = tmp_path_factory.mktemp("repo_copy") / "repo"
    ls = subprocess.run(["git", "ls-files", "-z"], cwd=str(REPO_ROOT),
                        capture_output=True, timeout=120, check=True)
    for rel in filter(None, ls.stdout.decode("utf-8").split("\0")):
        src = REPO_ROOT / rel
        if not (src.is_file() or src.is_symlink()):
            continue  # deleted in the working tree
        d = dst / rel
        d.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, d, follow_symlinks=False)
    # A `.git` so a root-marker walk stops here instead of escaping upwards.
    subprocess.run(["git", "init", "-q", str(dst)], check=True, timeout=60)
    return dst


def _snapshot(root: Path) -> dict[str, tuple]:
    out = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in (".git", "__pycache__")]
        for d in dirnames:
            out[os.path.relpath(os.path.join(dirpath, d), root) + os.sep] = ()
        for fn in filenames:
            p = os.path.join(dirpath, fn)
            st = os.lstat(p)
            out[os.path.relpath(p, root)] = (st.st_mtime_ns, st.st_size)
    return out


def _changed(before: dict, after: dict) -> list[str]:
    return sorted({k for k in after if after[k] != before.get(k)}
                  | {k for k in before if k not in after})


def _fixture_names(cfg: Path) -> dict[str, str]:
    """A tenant and a metric key read from the fixture, never typed here."""
    tenant = metric = None
    for f in sorted(cfg.rglob("*.yaml")):
        doc = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
        if f.name == "_defaults.yaml":
            for key in (doc.get("defaults") or {}):
                if metric is None and not str(key).startswith("_"):
                    metric = str(key)
        elif tenant is None and doc.get("tenants"):
            tenant = str(next(iter(doc["tenants"])))
    assert tenant and metric, f"fixture {FIXTURE_REL} lost its tenant or defaults"
    return {"tenant": tenant, "metric": metric}


@pytest.mark.parametrize("rel,shape", list(_params()))
def test_a_tool_pointed_elsewhere_does_not_write_into_the_repo(
        repo_copy, tmp_path, rel, shape):
    name = Path(rel).name
    base = tmp_path / "run"
    cfg, cwd, named = base / "conf.d", base / "cwd", base / "named"
    shutil.copytree(repo_copy / FIXTURE_REL, cfg,
                    ignore=shutil.ignore_patterns("examples"))
    for f in LIFTED:
        shutil.copy2(repo_copy / FIXTURE_REL / "examples" / f, cfg / f)
    shutil.copytree(repo_copy / RECIPES_REL, cfg / "recipes")
    cwd.mkdir()
    named.mkdir()
    subst = dict(_fixture_names(cfg), cfg=str(cfg), named=str(named))
    (named / "p.rego").write_text("package x\n", encoding="utf-8")
    (named / "tc.yaml").write_text(
        "apiVersion: dynamicalerting.io/v1alpha1\nkind: ThresholdConfig\n"
        "metadata:\n  name: probe\n  namespace: monitoring\n"
        f"spec:\n  tenants:\n    {subst['tenant']}:\n"
        f"      {subst['metric']}: '1'\n", encoding="utf-8")
    src = named / "src"
    src.mkdir()
    shutil.copy2(sorted(repo_copy.glob(SOURCE_CM_GLOB))[0], src)
    if name in SOURCE_DIR_TOOLS:
        args = [sys.executable, str(repo_copy / rel), "--source-dir", str(src)]
    else:
        args = [sys.executable, str(repo_copy / rel), "--config-dir", str(cfg)]
    args += [a.format(**subst) for a in shape]

    # The whole tmp dir, so a write one level above the run dir is seen too.
    before_repo = _snapshot(repo_copy)
    before_tmp = _snapshot(tmp_path)
    r = subprocess.run(args, cwd=str(cwd), capture_output=True, timeout=180,
                       env=dict(os.environ, PYTHONIOENCODING="utf-8",
                                PYTHONDONTWRITEBYTECODE="1"))
    out = (r.stdout + r.stderr).decode("utf-8", "replace")
    repo_writes = _changed(before_repo, _snapshot(repo_copy))
    tmp_writes = _changed(before_tmp, _snapshot(tmp_path))

    # A shape argparse rejects measures nothing; that is a broken shape here.
    for marker in ("the following arguments are required",
                   "unrecognized arguments", "invalid choice"):
        assert marker not in out, f"shape not accepted ({marker}): {out[-400:]}"
    stop = STOPS_EARLY.get((name, shape))
    if stop is not None:
        assert stop in out, (
            f"{name} {list(shape)} is in STOPS_EARLY ({stop!r}) but no longer "
            f"stops there: it now reaches more code. Re-measure it and drop or "
            f"replace the entry. Output tail: {out[-300:]}")

    assert not repo_writes, (
        f"{name} {list(shape)} wrote into the repo while pointed at a throwaway "
        f"--config-dir: {repo_writes[:10]}")

    places: dict[str, list[str]] = {"cwd": [], "config": [], "named": [],
                                    "beside": []}
    roots = {"run/cwd/": "cwd", "run/conf.d/": "config", "run/named/": "named"}
    for p in (w.replace(os.sep, "/") for w in tmp_writes):
        for prefix, place in roots.items():
            if p.startswith(prefix):
                places[place].append(p[len(prefix):])
                break
        else:
            places["beside"].append(p)
    assert not places["cwd"], f"{name} wrote into its cwd: {places['cwd'][:10]}"

    expected = EXPECTED_WRITES.get((name, shape), {})
    for place in ("config", "beside"):
        got = places[place]
        if place not in expected:
            assert not got, (f"{name} {list(shape)}: new write in {place}: "
                             f"{got[:10]} — record it in EXPECTED_WRITES with "
                             "the reason, or stop it")
        else:
            assert got, (f"{name} {list(shape)}: EXPECTED_WRITES says it writes "
                         f"in {place}, and it no longer does — drop the entry")
            stray = [p for p in got if not p.startswith(expected[place])]
            assert not stray, f"{name}: {place} writes outside " \
                              f"{expected[place]!r}: {stray[:10]}"
    if (name, shape) in MUST_WRITE_NAMED:
        assert places["named"], (
            f"must-fire control {name} {list(shape)} wrote nothing (rc "
            f"{r.returncode}): the detector or the shape is broken. {out[-300:]}")


def test_every_ledger_entry_names_a_tool_in_the_population():
    names = {Path(r).name for r in POPULATION}
    keyed = set(EXPECTED_WRITES) | MUST_WRITE_NAMED | set(STOPS_EARLY)
    keys = (set(SHAPES) | NO_BARE_SHAPE | SOURCE_DIR_TOOLS | {k[0] for k in keyed})
    assert keys <= names, f"stale entries: {sorted(keys - names)}"
    for (tool, shape) in keyed:
        bare = [] if tool in NO_BARE_SHAPE else [[]]
        assert list(shape) in bare + SHAPES.get(tool, []), (tool, shape)


def test_the_population_finder_sees_every_spelling(tmp_path):
    """Both quotings and the shared helper; a near-miss flag is not counted."""
    tools = tmp_path / TOOLS_REL / "ops"
    tools.mkdir(parents=True)
    (tools / "dq.py").write_text('p.add_argument("--config-dir")\n',
                                 encoding="utf-8")
    (tools / "sq.py").write_text("p.add_argument('-c', '--config-dir')\n",
                                 encoding="utf-8")
    (tools / "helper.py").write_text("_lib_io.add_config_dir_arg(p)\n",
                                     encoding="utf-8")
    (tools / "other.py").write_text('p.add_argument("--config-dirs")\n',
                                    encoding="utf-8")
    (tools / "_lib.py").write_text('p.add_argument("--config-dir")\n',
                                   encoding="utf-8")
    assert [Path(p).name for p in config_dir_tools(tmp_path)] == [
        "dq.py", "helper.py", "sq.py"]


def test_the_population_includes_the_single_quoted_tool():
    """The one the older double-quote substring sweep never saw."""
    names = {Path(r).name for r in POPULATION}
    assert "generate_tenant_mapping_rules.py" in names


def test_the_detector_sees_a_byte_identical_rewrite(tmp_path):
    """A rewrite with the same bytes is still a write; content diffs miss it."""
    f = tmp_path / "pack.yaml"
    f.write_text("same\n", encoding="utf-8")
    os.utime(f, ns=(1_000_000_000, 1_000_000_000))
    before = _snapshot(tmp_path)
    f.write_text("same\n", encoding="utf-8")
    assert _changed(before, _snapshot(tmp_path)) == ["pack.yaml"]
    (tmp_path / "new").write_text("", encoding="utf-8")
    f.unlink()
    assert _changed(before, _snapshot(tmp_path)) == ["new", "pack.yaml"]
    (tmp_path / "made").mkdir()
    assert "made" + os.sep in _changed(before, _snapshot(tmp_path))
