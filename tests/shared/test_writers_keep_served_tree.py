"""寫 conf.d 的工具跑進一棵服務中的樹，那棵樹服務的東西不變（#4014）。

母體由 AST 的呼叫圖推導（ADR-036 §2：Python 寫 conf.d 只經 `_lib_confedit` 送給
`da-guard write`，送出的只有它的 `_run`）：從 `_lib_confedit._run` 出發，凡是呼叫或引用到
「寫入函式」的函式也是寫入函式（跨模組、經 alias／`from … import`、經包裝函式庫都算），
做到不動點；有寫入函式的模組就是母體。每一支都要歸類，新增的寫入工具沒歸類就轉紅：

* `DRAFT_WRITERS`：產物是草稿（要人審過才搬進 conf.d），寫在自己的點目錄
  （`_lib_write_check.DRAFT_ROOTS`，一支工具一個，兩邊一一對上）。每支都真的跑進一棵
  服務中的樹——`-o` 就是那棵樹、`-o` 是樹裡還不存在的子目錄、在樹裡用相對預設值——
  斷言 Go 服務的租戶集合（`da-guard served-values`）與 validate-config 的結論都不變，
  並確認草稿確實寫出來了（否則「什麼都沒寫」也會過）；全部依序跑進同一個 `-o` 時，
  每支的草稿與它單獨跑時逐位元組相同（工具會串在同一個 `-o` 上用）。
* `EDITS_THE_SERVED_TREE`：工具的職責就是改它指到的那棵 conf.d（或產生一棵要被服務的
  conf.d），寫完被服務是對的；或只送 dry run。每支寫明理由。

「服務的東西不變」比的是整份 `da-guard served-values`（同一個 `--at`），不只是租戶名：
換掉根層 `_defaults.yaml` 不會改租戶集合，卻會改每個租戶的值。

判準只問 Go，不在這裡複製路徑規則：讀取端略過點開頭目錄是 Go `confdname.IsHidden` 的事。

Tenant ids are fixture names (CLAUDE.md #9).
"""
from __future__ import annotations

import ast
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
TOOLS = REPO / "scripts" / "tools"
sys.path.insert(0, str(TOOLS))

import _lib_write_check  # noqa: E402

pytestmark = pytest.mark.usefixtures("da_guard_env", "da_read_env", "da_crdecode_env")

_CONFEDIT = "_lib_confedit"
#: The one function that sends a batch to `da-guard write`.
_SEED = (_CONFEDIT, "_run")
#: served-values is read at one fixed instant, so two readings compare.
_AT = "2026-11-13T12:00:00Z"
_DRAFT_TENANT = "draft-a"

#: The tree being served: it passes validate-config, so a draft that leaks
#: into it shows as a changed conclusion as well as a changed tenant set.
_SERVED_TREE = {
    "_defaults.yaml": "defaults:\n  mysql_connection_usage_percent: 80\n",
    "tenant-a.yaml": 'tenants:\n  tenant-a:\n    mysql_connection_usage_percent: "70"\n',
}

_RULES = ("groups:\n- name: g\n  rules:\n  - alert: Deep\n"
          "    expr: mysql_global_status_threads_connected > 80\n"
          "    labels: {severity: warning}\n")
_AM = ("route:\n  receiver: d\n  routes:\n    - receiver: r\n"
       f"      match: {{tenant: {_DRAFT_TENANT}}}\n"
       "receivers:\n  - name: d\n  - name: r\n"
       '    webhook_configs: [{url: "http://hook.example/x"}]\n')
_SCRAPE = 'scrape_configs:\n- job_name: mysql\n  static_configs: [{targets: ["a:9104"]}]\n'


def _inputs(tmp: Path) -> dict:
    files = {"rules.yml": _RULES, "am.yml": _AM, "prom.yml": _SCRAPE,
             "am-b.yml": _AM.replace(_DRAFT_TENANT, _OTHER_TENANT)}
    tmp.mkdir(parents=True, exist_ok=True)
    for name, body in files.items():
        (tmp / name).write_text(body, encoding="utf-8")
    return {name: str(tmp / name) for name in files}


#: tool → (argv without -o, given the input paths). Every phase each tool has
#: that writes YAML is exercised: a phase left out is a phase not guarded.
DRAFT_WRITERS = {
    "ops/migrate_rule.py": lambda i: [i["rules.yml"], "--tenant", _DRAFT_TENANT],
    "ops/onboard_platform.py": lambda i: ["--alertmanager-config", i["am.yml"],
                                          "--rule-files", i["rules.yml"],
                                          "--scrape-config", i["prom.yml"]],
    "ops/scaffold_tenant.py": lambda i: ["--tenant", _DRAFT_TENANT, "--db", "mariadb",
                                         "--non-interactive"],
}

EDITS_THE_SERVED_TREE = {
    "ops/da_assembler.py": "renders ThresholdConfig CRs into the conf.d it serves "
                           "(`--config-dir`): being served is its job",
    "dx/generate_tenant_fixture.py": "writes a whole conf.d test tree, meant to be served "
                                     "(it refuses a non-empty -o)",
    "ops/init_project.py": "-o is a new repo's root; its conf.d/ is that repo's conf.d",
    "ops/assemble_config_dir.py": "merges conf.d sources into the config-dir the exporter "
                                  "reads",
    "dx/migrate_conf_d.py": "moves files inside the conf.d it is given (--apply)",
    "ops/migrate_credentials.py": "asks a dry run of its edits of the conf.d it is given; "
                                  "the rewrite itself is an edit of that tree",
    "ops/offboard_tenant.py": "removes a tenant from the conf.d it is given (--execute)",
    "ops/deprecate_rule.py": "edits the conf.d it is given in place (--execute)",
    "ops/migrate_builtin_defaults.py": "edits the conf.d it is given in place (--apply); its "
                                      "proof edits a scratch copy of that tree",
    "ops/patch_config.py": "patches a tenant's keys in the conf.d it is given",
    "ops/_registry_lib.py": "writes back the conf.d files the threshold registry generates "
                            "in the repo (`write_planned`)",
    "lint/check_threshold_registry.py": "`--regen` rewrites the repo's generated conf.d "
                                        "surfaces (`_registry_lib.regen`)",
    "lint/check_threshold_reachability.py": "renders init's defaults through `da-guard write "
                                            "--dry-run` (`NewFile.preview`); writes nothing",
}


def _tool_of(rel: str) -> str:
    return Path(rel).stem


def _derived_writers(root: Path = TOOLS) -> frozenset:
    """Modules under *root* with a writer function, `_lib_confedit` left
    out: a fixpoint over the call graph from `_SEED`. A unit is a top-level
    function or class (its methods and nested functions included) or the
    module's other statements; it is a writer when it calls or references a
    writer — `mod.f` through an `import mod [as m]`, `f` through a `from mod
    import f [as g]`, or a top-level `f` of its own module."""
    mods = {}
    for p in sorted(root.rglob("*.py")):
        if "__pycache__" not in p.parts:
            mods.setdefault(p.stem, p)
    uses: dict = {}
    for stem, path in mods.items():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        alias, imported = {}, {}
        for n in ast.walk(tree):
            if isinstance(n, ast.Import):
                for a in n.names:
                    alias[a.asname or a.name] = a.name.split(".")[-1]
            elif isinstance(n, ast.ImportFrom) and n.module:
                for a in n.names:
                    imported[a.asname or a.name] = (n.module.split(".")[-1], a.name)
        defs = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
        own = {n.name for n in tree.body if isinstance(n, defs)}
        units = [(n.name, n) for n in tree.body if isinstance(n, defs)]
        units.append(("<module>", ast.Module(
            body=[n for n in tree.body if not isinstance(n, defs)], type_ignores=[])))
        for name, node in units:
            out = uses.setdefault((stem, name), set())
            for n in ast.walk(node):
                if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name) \
                        and n.value.id in alias:
                    out.add((alias[n.value.id], n.attr))
                elif isinstance(n, ast.Name):
                    if n.id in imported:
                        out.add(imported[n.id])
                    elif n.id in own and n.id != name:
                        out.add((stem, n.id))
    writers = {_SEED}
    grew = True
    while grew:
        grew = False
        for unit, used in uses.items():
            if unit not in writers and used & writers:
                writers.add(unit)
                grew = True
    return frozenset(mods[m].relative_to(root).as_posix()
                     for m, _ in writers if m != _CONFEDIT and m in mods)


def test_every_conf_d_writer_is_classified():
    derived = _derived_writers()
    classified = set(DRAFT_WRITERS) | set(EDITS_THE_SERVED_TREE)
    assert not derived - classified, (
        f"these tools import {_CONFEDIT} (they write conf.d through `da-guard write`) but "
        f"are not classified: {sorted(derived - classified)}. A tool whose output is a "
        f"draft goes in DRAFT_WRITERS and `_lib_write_check.DRAFT_ROOTS`; one whose job "
        f"is the served tree goes in EDITS_THE_SERVED_TREE with its reason.")
    assert not classified - derived, (
        f"classified but no longer a conf.d writer (or gone): {sorted(classified - derived)}")
    assert not set(DRAFT_WRITERS) & set(EDITS_THE_SERVED_TREE)


def test_the_draft_writers_are_the_draft_roots_one_each():
    # The one table: every draft writer has its own root, and no root is a
    # writer this test does not run.
    assert {_tool_of(t) for t in DRAFT_WRITERS} == set(_lib_write_check.DRAFT_ROOTS)
    roots = list(_lib_write_check.DRAFT_ROOTS.values())
    assert len(set(roots)) == len(roots), roots
    assert all(r.startswith(".") and "/" not in r for r in roots), roots


def test_the_derivation_sees_every_way_of_reaching_confedit(tmp_path):
    # #4014 reviews: an entry-name list missed `emit_all` / `edit_all` and an
    # aliased import; an import list missed a tool writing through a helper
    # library and took a reader of `_lib_confedit`'s value helpers for a
    # writer. Every way of reaching `_run` is a writer, and nothing else.
    forms = {
        "_lib_confedit.py": "def _run(*a):\n    pass\n"
                            "def write_all(*a):\n    return _run(*a)\n"
                            "def data_value(x):\n    return x\n",
        "a.py": "import _lib_confedit\n_lib_confedit.write_all('.', [])\n",
        "b.py": "from _lib_confedit import write_all as w\ndef go():\n    w('.', [])\n",
        "c.py": "def f():\n    import _lib_confedit as ce\n    ce.write_all('.', [])\n",
        "d.py": "import _lib_confedit\nWRITE = _lib_confedit.write_all\n",
        "_lib_draft.py": "import _lib_confedit\ndef put(x):\n    return helper(x)\n"
                         "def helper(x):\n    return _lib_confedit.write_all(x, [])\n",
        "e.py": "import _lib_draft as d\nd.put('.')\n",
        "reader.py": "import _lib_confedit\nprint(_lib_confedit.data_value(1))\n",
        "other.py": "import _lib_draft\nprint('no call')\n",
    }
    for name, body in forms.items():
        (tmp_path / "sub").mkdir(exist_ok=True)
        (tmp_path / "sub" / name).write_text(body, encoding="utf-8")
    assert _derived_writers(tmp_path) == {
        f"sub/{n}" for n in ("a.py", "b.py", "c.py", "d.py", "_lib_draft.py", "e.py")}


def test_the_derivation_did_not_collapse():
    derived = _derived_writers()
    assert len(derived) >= 10, sorted(derived)
    for must in ("ops/onboard_platform.py", "ops/da_assembler.py", "ops/deprecate_rule.py"):
        assert must in derived, (must, sorted(derived))


def _draft_confd(out: Path, tool: str) -> Path:
    return out / _lib_write_check.draft_confd_rel(_tool_of(tool))


def _draft_root(out: Path, tool: str) -> Path:
    return out / _lib_write_check.DRAFT_ROOTS[_tool_of(tool)]


def _served(root: Path) -> dict:
    """The whole `da-guard served-values` document of *root* at `_AT`, the
    binary's version left out: every tenant's every served value."""
    sv = subprocess.run([os.environ["DA_GUARD_BINARY"], "served-values", "--config-dir",
                         str(root), "--at", _AT], capture_output=True, text=True,
                        encoding="utf-8", timeout=120, check=False)
    assert sv.returncode == 0, sv.stdout + sv.stderr
    doc = json.loads(sv.stdout)
    doc.pop("da_guard_version", None)
    return doc


def _served_tenants(root: Path) -> list:
    return sorted(_served(root)["tenants"])


_CHECK_LINE = re.compile(r"^\[(PASS|FAIL|WARN|SKIP)\] \S+", re.M)


def _validate_conclusion(root: Path) -> tuple:
    """validate-config's rc and its per-check verdict lines."""
    r = subprocess.run([sys.executable, str(TOOLS / "ops" / "validate_config.py"),
                        "--config-dir", str(root)], capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=300, check=False)
    return r.returncode, tuple(m.group(0) for m in _CHECK_LINE.finditer(r.stdout))


def _write_tree(root: Path, files: dict) -> Path:
    for rel, body in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(body, encoding="utf-8")
    return root


@pytest.fixture(scope="module")
def served_baseline(tmp_path_factory):
    live = _write_tree(tmp_path_factory.mktemp("baseline") / "live", _SERVED_TREE)
    served, conclusion = _served(live), _validate_conclusion(live)
    assert sorted(served["tenants"]) == ["tenant-a"], served
    assert conclusion[0] == 0, conclusion  # the tree is clean to begin with
    return served, conclusion


def _run(tool, args, out=None, cwd=None):
    argv = [sys.executable, str(TOOLS / tool), *args]
    if out is not None:
        argv += ["-o", str(out)]
    return subprocess.run(argv, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=300, cwd=cwd, check=False,
                          env={**os.environ, "PYTHONIOENCODING": "utf-8"})


@pytest.mark.parametrize("where", ["o_is_the_tree", "o_is_a_new_subdirectory",
                                   "default_o_inside_the_tree"])
@pytest.mark.parametrize("tool", sorted(DRAFT_WRITERS))
def test_a_draft_writer_run_into_a_served_tree_changes_nothing_it_serves(
        tool, where, tmp_path, served_baseline):
    live = _write_tree(tmp_path / "live", _SERVED_TREE)
    args = DRAFT_WRITERS[tool](_inputs(tmp_path / "in"))
    if where == "o_is_the_tree":
        out = live
        r = _run(tool, args, out)
    elif where == "o_is_a_new_subdirectory":
        out = live / "newsub"
        r = _run(tool, args, out)
    else:  # the tool's relative default, run from inside the tree
        r = _run(tool, args, cwd=live)
        made = [p for p in live.iterdir() if p.is_dir()]
        assert len(made) == 1, sorted(p.name for p in live.iterdir())
        out = made[0]
    assert r.returncode == 0, r.stdout[-1500:] + r.stderr[-1500:]
    draft = _draft_confd(out, tool)
    # The control: the draft was written, and judged as a root of its own it
    # serves the draft tenant — so "nothing changed" below is not "nothing ran".
    assert (draft / f"{_DRAFT_TENANT}.yaml").is_file(), sorted(
        p.relative_to(live).as_posix() for p in live.rglob("*"))
    assert _DRAFT_TENANT in _served_tenants(draft)
    served, conclusion = served_baseline
    assert _served(live) == served
    assert _validate_conclusion(live) == conclusion


def _tree_bytes(root: Path) -> dict:
    """Every path under *root*: a file's bytes, a directory as None — so a
    directory left behind (an empty one too) is a difference."""
    return {p.relative_to(root).as_posix(): (p.read_bytes() if p.is_file() else None)
            for p in sorted(root.rglob("*"))}


def test_draft_writers_chained_through_one_o_keep_each_others_drafts(tmp_path):
    # #4014 review: the tools shared one draft directory, and a second tool
    # into the same -o replaced the first one's draft whole, rc 0. Each run
    # alone, then all of them in turn into one -o (both orders): every
    # tool's draft root is byte for byte what it was alone.
    inputs = _inputs(tmp_path / "in")
    alone = {}
    for tool in DRAFT_WRITERS:
        out = tmp_path / "alone" / _tool_of(tool)
        r = _run(tool, DRAFT_WRITERS[tool](inputs), out)
        assert r.returncode == 0, r.stderr[-1500:]
        alone[tool] = _tree_bytes(_draft_root(out, tool))
        assert alone[tool], tool
    for order in (sorted(DRAFT_WRITERS), sorted(DRAFT_WRITERS, reverse=True)):
        out = tmp_path / "-".join(_tool_of(t) for t in order)
        for tool in order:
            r = _run(tool, DRAFT_WRITERS[tool](inputs), out)
            assert r.returncode == 0, (tool, r.stderr[-1500:])
        for tool in order:
            assert _tree_bytes(_draft_root(out, tool)) == alone[tool], (order, tool)


def test_scaffold_from_onboard_into_the_same_o_keeps_onboards_draft(tmp_path):
    # The chain the tools are made for: onboard's hints feed scaffold. The
    # routing onboard recovered (a receiver URL) stays in onboard's draft.
    inputs = _inputs(tmp_path / "in")
    out = tmp_path / "out"
    first = _run("ops/onboard_platform.py", ["--alertmanager-config", inputs["am.yml"]], out)
    assert first.returncode == 0, first.stderr[-1500:]
    onboard = _tree_bytes(_draft_root(out, "ops/onboard_platform.py"))
    assert b"hook.example" in onboard[f"conf.d/{_DRAFT_TENANT}.yaml"]
    second = _run("ops/scaffold_tenant.py",
                  ["--from-onboard", str(out / "onboard-hints.json")], out)
    assert second.returncode == 0, second.stderr[-1500:]
    assert (_draft_confd(out, "ops/scaffold_tenant.py") / f"{_DRAFT_TENANT}.yaml").is_file()
    assert _tree_bytes(_draft_root(out, "ops/onboard_platform.py")) == onboard


#: tool → (argv for another draft tenant): the second run's own draft.
_OTHER_TENANT = "draft-b"
_SECOND_RUN = {
    "ops/migrate_rule.py": lambda i: [i["rules.yml"], "--tenant", _OTHER_TENANT],
    "ops/onboard_platform.py": lambda i: ["--alertmanager-config", i["am-b.yml"]],
    "ops/scaffold_tenant.py": lambda i: ["--tenant", _OTHER_TENANT, "--db", "mariadb",
                                         "--non-interactive"],
}


@pytest.mark.parametrize("tool", sorted(DRAFT_WRITERS))
def test_a_draft_holding_an_earlier_runs_tenant_is_refused(tool, tmp_path):
    # #3671 / #4014: a second run into the same -o for another tenant would
    # leave both tenants in the draft. Refused, exit 2, nothing written.
    assert set(_SECOND_RUN) == set(DRAFT_WRITERS)
    inputs = _inputs(tmp_path / "in")
    out = tmp_path / "out"
    first = _run(tool, DRAFT_WRITERS[tool](inputs), out)
    assert first.returncode == 0, first.stderr[-1500:]
    before = _tree_bytes(out)
    second = _run(tool, _SECOND_RUN[tool](inputs), out)
    assert second.returncode == 2, second.stdout[-800:] + second.stderr[-800:]
    assert f"{_DRAFT_TENANT}.yaml, which this run does not write" in second.stderr, second.stderr
    assert "nothing was written" in second.stderr, second.stderr
    assert _tree_bytes(out) == before


def _blockers(tool):
    """The directories *tool* makes after its conf.d batch, relative to -o."""
    root = _lib_write_check.DRAFT_ROOTS[_tool_of(tool)]
    extra = ["phase1-routing", "phase2-rules"] if tool == "ops/onboard_platform.py" else []
    return [root, f"{root}/{_lib_write_check.DRAFT_CONFD}", *extra]


@pytest.mark.parametrize("tool,blocker", [(t, b) for t in sorted(DRAFT_WRITERS)
                                          for b in _blockers(t)])
def test_a_directory_made_after_the_batch_that_is_a_file_writes_nothing(tool, blocker, tmp_path):
    # #3997 / #4014 review: the check is made before the batch — a file where
    # a directory goes ends the run with exit 2 and no draft on disk.
    out = tmp_path / "out"
    (out / blocker).parent.mkdir(parents=True)
    (out / blocker).write_text("", encoding="utf-8")
    before = _tree_bytes(out)
    r = _run(tool, DRAFT_WRITERS[tool](_inputs(tmp_path / "in")), out)
    assert r.returncode == 2, r.stdout[-800:] + r.stderr[-800:]
    assert f"{blocker} is not a directory" in r.stderr, r.stderr
    assert _tree_bytes(out) == before


def test_onboard_checks_only_the_csv_directories_of_the_phases_it_runs(tmp_path):
    # #4014 review: a run of Phase 3 alone makes no `phase1-routing/`, so a
    # file of that name in -o is none of its business.
    inputs = _inputs(tmp_path / "in")
    out = tmp_path / "out"
    out.mkdir()
    (out / "phase1-routing").write_text("notes\n", encoding="utf-8")
    r = _run("ops/onboard_platform.py", ["--scrape-config", inputs["prom.yml"]], out)
    assert r.returncode == 0, r.stderr[-1500:]
    assert (out / "phase1-routing").read_text(encoding="utf-8") == "notes\n"


def test_onboard_phase_2_alone_after_phase_1_keeps_the_draft_tenants(tmp_path):
    # #4014: only a run that writes tenants owns the draft's tenant set. The
    # phases may be run one at a time into one -o (the tool's own usage):
    # Phase 2 alone adds `_defaults.yaml` beside Phase 1's tenant files.
    inputs = _inputs(tmp_path / "in")
    out = tmp_path / "out"
    tool = "ops/onboard_platform.py"
    first = _run(tool, ["--alertmanager-config", inputs["am.yml"]], out)
    assert first.returncode == 0, first.stderr[-1500:]
    second = _run(tool, ["--rule-files", inputs["rules.yml"]], out)
    assert second.returncode == 0, second.stderr[-1500:]
    assert sorted(p.name for p in _draft_confd(out, tool).iterdir()) == [
        "_defaults.yaml", f"{_DRAFT_TENANT}.yaml"]
