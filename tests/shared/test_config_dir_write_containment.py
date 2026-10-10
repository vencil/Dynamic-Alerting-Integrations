"""#1582 — a tool pointed at a throwaway ``--config-dir`` must not write into the repo.

The defect: ``compile_custom_alerts`` resolved its default output from
``__file__`` and rewrote the shipped rule pack however ``--config-dir`` was
redirected, and two other tools wrote to a cwd-relative default. Those three
were fixed (#1648, #1739). This file is what keeps the WHOLE family fixed:

* ⛔ THE POPULATION IS DERIVED BY AST, any quoting: every ``add_argument`` call
  naming ``--config-dir`` and every ``add_config_dir_arg(...)`` call under
  ``scripts/tools``. The older sweep in ``test_output_label_escaping.py`` keys
  on the substring ``'"--config-dir"'`` in three subdirectories and so never saw
  ``config_history`` / ``generate_tenant_mapping_rules`` (single quotes).
* Every tool runs from a PRIVATE COPY of the tracked tree, with a throwaway
  config dir and a throwaway cwd. "Did the repo change" is then a question about
  a tree nobody else writes, so it holds under ``pytest -n auto`` — the
  whole-tree check in the older file is serial-only and skipped in CI.
* Writes are detected by (mtime_ns, size) plus file-set, not by ``git status``:
  a rewrite with identical bytes is still a write, and a byte-identical fixture
  is exactly how a "did not reproduce" once read as "clean" (#1582 thread).

Each write lands in one of five places. ``repo`` and ``cwd`` are never allowed.
``named`` (under a path passed on the command line) always is. ``config`` (in
the tree pointed at) and ``beside`` (anywhere else around it) must match
:data:`EXPECTED_WRITES` exactly, so a new writer is a failure and a stale entry
is too.

⚠️ SCOPE: only the shapes in :data:`SHAPES` are measured, and writes outside
the run's own scratch directory (``$HOME``, ``/tmp`` at large) are not
observed. A tool that needs a live service stops before its write path; those
are listed in :data:`STOPS_EARLY` with what they do reach.
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

pytestmark = pytest.mark.usefixtures("da_guard_env")

REPO_ROOT = Path(__file__).resolve().parents[2]
TOOLS_REL = Path("scripts") / "tools"
# The throwaway tree: the exporter's sample conf.d (threshold keys, tenants)
# with the custom-alert recipe examples as a subtree, so the compilers have
# declarations to compile and the --execute shapes have keys to act on.
FIXTURE_REL = Path("components") / "threshold-exporter" / "config" / "conf.d"
RECIPES_REL = Path("rule-packs") / "recipes" / "examples" / "conf.d"


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
                              ["--baseline", "{cfg}", "-o", "{named}/r.json"]],
    "config_history.py": [["snapshot"], ["log"]],
    "deprecate_rule.py": [["{metric}"], ["{metric}", "--execute"]],
    "offboard_tenant.py": [["{tenant}"], ["{tenant}", "--execute"]],
    "diagnose.py": [["{tenant}"]],
    "policy_opa_bridge.py": [["--policy-path", "{named}/p.rego"]],
    "run_chaos_soak.py": [["--target-url", "http://127.0.0.1:9",
                           "--output-dir", "{named}/soak"]],
    "operator_generate.py": [["--output-dir", "{named}/m"]],
    "migrate_to_operator.py": [["--output-dir", "{named}/m"]],
    "generate_alertmanager_routes.py": [["-o", "{named}/r.yaml"]],
    "generate_tenant_mapping_rules.py": [["-o", "{named}/r.yaml"]],
    "generate_tenant_metadata.py": [["--output", "{named}/m.json"]],
    "analyze_rule_pack_gaps.py": [["-o", "{named}/g.json"]],
}
# Tools whose bare shape fails argument validation (a required flag); their
# measured shapes are all in SHAPES.
NO_BARE_SHAPE = {"run_chaos_soak.py", "config_history.py", "deprecate_rule.py",
                 "offboard_tenant.py", "diagnose.py"}
# `migrate_to_operator` names its input `--source-dir`.
SOURCE_DIR_TOOLS = {"migrate_to_operator.py"}

# (tool, shape args with placeholders) -> {place: path prefix} every write in
# that place must start with. Absent = no write allowed there.
EXPECTED_WRITES: dict[tuple[str, tuple[str, ...]], dict[str, str]] = {
    # Output follows the input: <parent of --config-dir>/.da-history.
    ("config_history.py", ("snapshot",)): {"beside": ".da-history/"},
    # In-place edits of the tree pointed at, which is what --execute is for.
    ("deprecate_rule.py", ("{metric}", "--execute")): {"config": ""},
    ("offboard_tenant.py", ("{tenant}", "--execute")): {"config": ""},
}
# Must-fire controls: these shapes MUST produce a write in `named`, or the
# detector (or the shape) is broken and every "no write" above is vacuous.
MUST_WRITE_NAMED = {
    ("compile_custom_alerts.py", ("--out", "{named}/pack.yaml")),
    ("operator_generate.py", ("--output-dir", "{named}/m")),
    ("generate_alertmanager_routes.py", ("-o", "{named}/r.yaml")),
}
# Need something this suite does not stand up; what they still reach is
# measured, and the reason is printed.
STOPS_EARLY = {
    "run_chaos_soak.py": "needs a live /metrics endpoint; aborts before the soak "
                         "writes anything, so only the pre-abort path is measured",
    "da_assembler.py": "needs the kubernetes Python client and a cluster; only "
                       "argument handling is measured",
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
    shutil.copytree(repo_copy / RECIPES_REL, cfg / "recipes")
    cwd.mkdir()
    named.mkdir()
    (named / "p.rego").write_text("package x\n", encoding="utf-8")
    subst = dict(_fixture_names(cfg), cfg=str(cfg), named=str(named))
    flag = "--source-dir" if name in SOURCE_DIR_TOOLS else "--config-dir"
    args = [sys.executable, str(repo_copy / rel), flag, str(cfg)]
    args += [a.format(**subst) for a in shape]

    before_repo = _snapshot(repo_copy)
    before_base = _snapshot(base)
    r = subprocess.run(args, cwd=str(cwd), capture_output=True, timeout=180,
                       env=dict(os.environ, PYTHONIOENCODING="utf-8",
                                PYTHONDONTWRITEBYTECODE="1"))
    err = r.stderr.decode("utf-8", "replace")
    repo_writes = _changed(before_repo, _snapshot(repo_copy))
    base_writes = _changed(before_base, _snapshot(base))

    # A shape argparse rejects measures nothing; that is a broken shape here.
    for marker in ("the following arguments are required",
                   "unrecognized arguments", "invalid choice"):
        assert marker not in err, f"shape not accepted ({marker}): {err[-400:]}"
    if "No module named" in err or "ModuleNotFoundError" in err:
        assert name in STOPS_EARLY, f"missing dependency, unlisted: {err[-300:]}"

    assert not repo_writes, (
        f"{name} {list(shape)} wrote into the repo while pointed at a throwaway "
        f"--config-dir: {repo_writes[:10]}")

    places: dict[str, list[str]] = {"cwd": [], "config": [], "named": [],
                                    "beside": []}
    for p in base_writes:
        top, _, rest = p.partition(os.sep)
        if top == "cwd":
            places["cwd"].append(rest)
        elif top == "conf.d":
            places["config"].append(rest)
        elif top == "named":
            places["named"].append(rest)
        else:
            places["beside"].append(p.replace(os.sep, "/"))
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
            f"{r.returncode}): the detector or the shape is broken. {err[-300:]}")


def test_every_ledger_entry_names_a_tool_in_the_population():
    names = {Path(r).name for r in POPULATION}
    keys = (set(SHAPES) | NO_BARE_SHAPE | SOURCE_DIR_TOOLS | set(STOPS_EARLY)
            | {k[0] for k in EXPECTED_WRITES} | {k[0] for k in MUST_WRITE_NAMED})
    assert keys <= names, f"stale entries: {sorted(keys - names)}"
    for (tool, shape) in set(EXPECTED_WRITES) | MUST_WRITE_NAMED:
        assert list(shape) in SHAPES.get(tool, []), (tool, shape)


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


def test_the_population_includes_the_single_quoted_tools():
    """The two the older substring sweep never saw (#1582 thread)."""
    names = {Path(r).name for r in POPULATION}
    assert {"config_history.py", "generate_tenant_mapping_rules.py"} <= names


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
