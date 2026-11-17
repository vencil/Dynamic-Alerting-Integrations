"""寫 conf.d 的工具跑進一棵服務中的樹，那棵樹服務的東西不變（#4014）。

母體由 AST 推導：`scripts/tools/` 底下呼叫 `_lib_confedit.write_all` 或
`write_all_or_exit` 的每一支（ADR-036 §2：Python 寫 conf.d 只有這個入口）。每一支
都要歸類，新增的寫入工具沒歸類就轉紅：

* `DRAFT_WRITERS`：產物是草稿（要人審過才搬進 conf.d）。每支都真的跑進一棵服務中的
  樹——`-o` 就是那棵樹、`-o` 是樹裡還不存在的子目錄、在樹裡用相對預設值——斷言 Go
  服務的租戶集合（`da-guard served-values`）與 validate-config 的結論都不變，並確認
  草稿確實寫出來了（否則「什麼都沒寫」也會過）。
* `EDITS_THE_SERVED_TREE`：工具的職責就是改它指到的那棵 conf.d（或產生一棵要被服務的
  conf.d），寫完被服務是對的。每支寫明理由。

判準只問 Go，不在這裡複製路徑規則：草稿放在 `_lib_write_check.DRAFT_CONFD_DIR`，
讀取端略過它是 Go `confdname.IsHidden` 的事。

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

_WRITE_ENTRIES = frozenset({"write_all", "write_all_or_exit"})
_DRAFT = _lib_write_check.DRAFT_CONFD_DIR
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
    files = {"rules.yml": _RULES, "am.yml": _AM, "prom.yml": _SCRAPE}
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
    "ops/migrate_credentials.py": "rewrites receivers in place in the conf.d it is given",
    "ops/offboard_tenant.py": "removes a tenant from the conf.d it is given (--execute)",
    "ops/_registry_lib.py": "only asks `da-guard write --dry-run` (`_judge_confd`); "
                            "writes nothing",
}


def _derived_writers() -> frozenset:
    """Files under scripts/tools calling `write_all` / `write_all_or_exit`
    (by attribute or by name), `_lib_confedit` itself left out."""
    out = set()
    for p in TOOLS.rglob("*.py"):
        if "__pycache__" in p.parts or p.name == "_lib_confedit.py":
            continue
        for node in ast.walk(ast.parse(p.read_text(encoding="utf-8"))):
            if not isinstance(node, ast.Call):
                continue
            f = node.func
            name = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", None)
            if name in _WRITE_ENTRIES:
                out.add(p.relative_to(TOOLS).as_posix())
                break
    return frozenset(out)


def test_every_conf_d_writer_is_classified():
    derived = _derived_writers()
    classified = set(DRAFT_WRITERS) | set(EDITS_THE_SERVED_TREE)
    assert not derived - classified, (
        f"these tools write conf.d through `da-guard write` but are not classified: "
        f"{sorted(derived - classified)}. A tool whose output is a draft goes in "
        f"DRAFT_WRITERS (and writes it under `_lib_write_check.DRAFT_CONFD_DIR`); one "
        f"whose job is the served tree goes in EDITS_THE_SERVED_TREE with its reason.")
    assert not classified - derived, (
        f"classified but no longer a conf.d writer (or gone): {sorted(classified - derived)}")
    assert not set(DRAFT_WRITERS) & set(EDITS_THE_SERVED_TREE)


def test_the_derivation_did_not_collapse():
    derived = _derived_writers()
    assert len(derived) >= 8, sorted(derived)
    for must in ("ops/onboard_platform.py", "ops/da_assembler.py"):
        assert must in derived, (must, sorted(derived))


def _served_tenants(root: Path) -> list:
    sv = subprocess.run([os.environ["DA_GUARD_BINARY"], "served-values", "--config-dir",
                         str(root)], capture_output=True, text=True, encoding="utf-8",
                        timeout=120, check=False)
    assert sv.returncode == 0, sv.stdout + sv.stderr
    return sorted(json.loads(sv.stdout)["tenants"])


_CHECK_LINE = re.compile(r"^\[(PASS|FAIL|WARN|SKIP)\] \S+", re.M)


def _validate_conclusion(root: Path) -> tuple:
    """validate-config's rc and its per-check verdict lines."""
    r = subprocess.run([sys.executable, str(TOOLS / "ops" / "validate_config.py"),
                        "--config-dir", str(root)], capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=300, check=False)
    return r.returncode, tuple(m.group(0) for m in _CHECK_LINE.finditer(r.stdout))


@pytest.fixture(scope="module")
def served_baseline(tmp_path_factory):
    live = tmp_path_factory.mktemp("baseline") / "live"
    for rel, body in _SERVED_TREE.items():
        (live / rel).parent.mkdir(parents=True, exist_ok=True)
        (live / rel).write_text(body, encoding="utf-8")
    tenants, conclusion = _served_tenants(live), _validate_conclusion(live)
    assert tenants == ["tenant-a"], tenants
    assert conclusion[0] == 0, conclusion  # the tree is clean to begin with
    return tenants, conclusion


@pytest.mark.parametrize("where", ["o_is_the_tree", "o_is_a_new_subdirectory",
                                   "default_o_inside_the_tree"])
@pytest.mark.parametrize("tool", sorted(DRAFT_WRITERS))
def test_a_draft_writer_run_into_a_served_tree_changes_nothing_it_serves(
        tool, where, tmp_path, served_baseline):
    live = tmp_path / "live"
    for rel, body in _SERVED_TREE.items():
        (live / rel).parent.mkdir(parents=True, exist_ok=True)
        (live / rel).write_text(body, encoding="utf-8")
    argv = [sys.executable, str(TOOLS / tool), *DRAFT_WRITERS[tool](_inputs(tmp_path / "in"))]
    if where == "o_is_the_tree":
        out, cwd = live, None
        argv += ["-o", str(out)]
    elif where == "o_is_a_new_subdirectory":
        out, cwd = live / "newsub", None
        argv += ["-o", str(out)]
    else:  # the tool's relative default, run from inside the tree
        out, cwd = None, live
    r = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=300, cwd=cwd, check=False,
                       env={**os.environ, "PYTHONIOENCODING": "utf-8"})
    assert r.returncode == 0, r.stdout[-1500:] + r.stderr[-1500:]
    if out is None:
        made = [p for p in live.iterdir() if p.is_dir()]
        assert len(made) == 1, sorted(p.name for p in live.iterdir())
        out = made[0]
    draft = out / _DRAFT
    # The control: the draft was written, and judged as a root of its own it
    # serves the draft tenant — so "nothing changed" below is not "nothing ran".
    assert (draft / f"{_DRAFT_TENANT}.yaml").is_file(), sorted(
        p.relative_to(live).as_posix() for p in live.rglob("*"))
    assert _DRAFT_TENANT in _served_tenants(draft)
    tenants, conclusion = served_baseline
    assert _served_tenants(live) == tenants
    assert _validate_conclusion(live) == conclusion


#: tool → (argv for another draft tenant): the second run's own draft.
_OTHER_TENANT = "draft-b"
_SECOND_RUN = {
    "ops/migrate_rule.py": lambda i: [i["rules.yml"], "--tenant", _OTHER_TENANT],
    "ops/onboard_platform.py": lambda i: ["--alertmanager-config", i["am-b.yml"]],
    "ops/scaffold_tenant.py": lambda i: ["--tenant", _OTHER_TENANT, "--db", "mariadb",
                                         "--non-interactive"],
}


def _tree_bytes(root: Path) -> dict:
    return {p.relative_to(root).as_posix(): p.read_bytes()
            for p in sorted(root.rglob("*")) if p.is_file()}


def _run_tool(tool, args, out):
    return subprocess.run([sys.executable, str(TOOLS / tool), *args, "-o", str(out)],
                          capture_output=True, text=True, encoding="utf-8", errors="replace",
                          timeout=300, check=False,
                          env={**os.environ, "PYTHONIOENCODING": "utf-8"})


@pytest.mark.parametrize("tool", sorted(DRAFT_WRITERS))
def test_a_draft_holding_an_earlier_runs_tenant_is_refused(tool, tmp_path):
    # #3671 / #4014: a second run into the same -o for another tenant would
    # leave both tenants in the draft. Refused, exit 2, nothing written.
    assert set(_SECOND_RUN) == set(DRAFT_WRITERS)
    inputs = _inputs(tmp_path / "in")
    (tmp_path / "in" / "am-b.yml").write_text(_AM.replace(_DRAFT_TENANT, _OTHER_TENANT),
                                             encoding="utf-8")
    inputs["am-b.yml"] = str(tmp_path / "in" / "am-b.yml")
    out = tmp_path / "out"
    first = _run_tool(tool, DRAFT_WRITERS[tool](inputs), out)
    assert first.returncode == 0, first.stderr[-1500:]
    before = _tree_bytes(out)
    second = _run_tool(tool, _SECOND_RUN[tool](inputs), out)
    assert second.returncode == 2, second.stdout[-800:] + second.stderr[-800:]
    assert f"{_DRAFT_TENANT}.yaml, which this run does not write" in second.stderr, second.stderr
    assert "nothing was written" in second.stderr, second.stderr
    assert _tree_bytes(out) == before


def test_onboard_refuses_an_artifact_directory_that_is_a_file(tmp_path):
    # #4014 (as #3997 for migrate_rule): `-o/.onboard` a file ends the run
    # before the conf.d batch — no draft on disk.
    out = tmp_path / "out"
    out.mkdir()
    (out / ".onboard").write_text("", encoding="utf-8")
    r = _run_tool("ops/onboard_platform.py",
                  DRAFT_WRITERS["ops/onboard_platform.py"](_inputs(tmp_path / "in")), out)
    assert r.returncode == 2, r.stderr[-800:]
    assert ".onboard is not a directory" in r.stderr, r.stderr
    assert sorted(p.name for p in out.iterdir()) == [".onboard"]


def test_onboard_phase_2_alone_after_phase_1_keeps_the_draft_tenants(tmp_path):
    # #4014: only a run that writes tenants owns the draft's tenant set. The
    # phases may be run one at a time into one -o (the tool's own usage):
    # Phase 2 alone adds `_defaults.yaml` beside Phase 1's tenant files.
    inputs = _inputs(tmp_path / "in")
    out = tmp_path / "out"
    first = _run_tool("ops/onboard_platform.py", ["--alertmanager-config", inputs["am.yml"]], out)
    assert first.returncode == 0, first.stderr[-1500:]
    second = _run_tool("ops/onboard_platform.py", ["--rule-files", inputs["rules.yml"]], out)
    assert second.returncode == 0, second.stderr[-1500:]
    assert sorted(p.name for p in (out / _DRAFT).iterdir()) == [
        "_defaults.yaml", f"{_DRAFT_TENANT}.yaml"]
