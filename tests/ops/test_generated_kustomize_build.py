#!/usr/bin/env python3
"""把 `da-tools init` 產出的 kustomize 樹餵給**真的** `kustomize build`（#1349）。

WHY THIS FILE EXISTS
--------------------
`--deploy kustomize` 是 init 的預設 deploy。它產出的樹曾經 `kustomize build`
直接失敗（`configMapGenerator.files` 指向 `kustomize/base/` 裡不存在的檔），
而全 repo 對這棵樹的斷言只有 `yaml.safe_load` 與 `'kustomize build' in
yaml_str` 這種子字串——檔案是合法 YAML、字串也在，每一條都成立，產物卻不能用。
修好之後（#1454 A 的 A2：README 教 symlink、產出的 workflow 帶
`--load-restrictor`），守著它的仍然只是 issue 留言裡的三條手動讀數。

這個檔把那三條讀數變成閘門。判準是 **rc 與 ConfigMap 的鍵集合**，不是字串：

* 客戶路徑逐字照產物執行——README 的 `ln -s` 行、README 的 `cp` fallback 行、
  兩份 workflow 裡的 `kustomize build` 那一行都是**從產物裡讀出來**再跑的，
  不是在這裡另寫一份。README 或 workflow 改了而另一邊沒跟上，這裡會紅。
* 產出的 ConfigMap 鍵必須等於 `conf.d/` 的檔名集合，也等於
  `kustomization.yaml` 的 `files:` 清單——三者任一漂移都紅。
* 產物在 `-o alerting/` 子目錄、在真的 git repo 裡產生：non-repo 的 tmp 目錄會讓
  路徑 offset 為空，量到的是另一種產物。

⛔ SKIP ≠ PASS
--------------
本機沒有 `kustomize` 時整個模組 skip，而 skip 什麼都沒驗。CI 的
`lint-rule-packs` job 以釘住版本（見 ``KUSTOMIZE_PIN``）與 SHA-256 安裝它，並以
``KUSTOMIZE_REQUIRE=1`` 跑本模組——那裡缺 binary 或版本不符是**失敗**，不是
skip。``test_ci_runs_this_module_with_the_pinned_kustomize`` 從 ci.yml 解析出那個
安裝步驟與執行步驟，免得「閘門存在」與「閘門有在跑」被混為一談。

誠實邊界
--------
* 只驗到 `kustomize build`，不驗 `kubectl apply`（沒有 cluster）。
* symlink 路徑只在 POSIX 跑：README 的 `ln -s` 本來就是 POSIX 指令。
* gitops overlay（``--config-source git``）已撤下（issue #1349）：它的 git-sync
  patch 以 ``Deployment/threshold-exporter`` 為目標，而這棵樹只有 ConfigMap，
  kustomize 把它當成 no-op。拒絕行為由 ``tests/ops/test_init_project.py`` 釘。
"""
from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
INIT = REPO_ROOT / "scripts" / "tools" / "ops" / "init_project.py"
CI_YML = REPO_ROOT / ".github" / "workflows" / "ci.yml"
THIS_MODULE = "tests/ops/test_generated_kustomize_build.py"

# ⛔ 與 ci.yml `Install kustomize` 步驟的版本必須相同；
# test_ci_runs_this_module_with_the_pinned_kustomize 比對兩者。
KUSTOMIZE_PIN = "5.8.1"

TENANTS = ("tenant-one", "tenant-two")
TIMEOUT_S = 120

pytestmark = pytest.mark.skipif(
    os.name == "nt", reason="README 的 setup 是 POSIX `ln -s`；見模組 docstring")


# ═══════════════════════════════════════════════════════════════════════════
# kustomize binary
# ═══════════════════════════════════════════════════════════════════════════
def _require() -> bool:
    return os.environ.get("KUSTOMIZE_REQUIRE") == "1"


@pytest.fixture(scope="module")
def kustomize() -> str:
    exe = os.environ.get("KUSTOMIZE_BIN") or shutil.which("kustomize")
    if not exe:
        msg = ("kustomize not found (PATH / $KUSTOMIZE_BIN) — this module "
               "verified NOTHING; a skip here is not a pass")
        if _require():
            pytest.fail(msg + " (KUSTOMIZE_REQUIRE=1)")
        pytest.skip(msg)
    out = subprocess.run([exe, "version"], capture_output=True, text=True,
                         timeout=TIMEOUT_S).stdout.strip()
    if out.lstrip("v") != KUSTOMIZE_PIN:
        msg = f"kustomize reports {out!r}, pin is v{KUSTOMIZE_PIN}"
        if _require():
            pytest.fail(msg + " (KUSTOMIZE_REQUIRE=1)")
        pytest.skip(msg + " — results would not describe the pinned version")
    return exe


# ═══════════════════════════════════════════════════════════════════════════
# Generated project
# ═══════════════════════════════════════════════════════════════════════════
def _generate(tmp: Path, *extra: str) -> tuple[Path, Path]:
    """`da-tools init` into `<tmp>/repo/alerting` inside a real git repo."""
    root = tmp / "repo"
    root.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(root)], check=True,
                   timeout=TIMEOUT_S)
    out = root / "alerting"
    proc = subprocess.run(
        [sys.executable, str(INIT), "--ci", "both", "--deploy", "kustomize",
         "--tenants", ",".join(TENANTS), "--rule-packs", "mariadb",
         "--non-interactive", "-o", str(out), *extra],
        capture_output=True, text=True, encoding="utf-8", timeout=TIMEOUT_S,
    )
    assert proc.returncode == 0, f"init failed: {proc.stderr[-800:]}"
    return root, out


def _base(out: Path) -> Path:
    return out / "kustomize" / "base"


def _readme(out: Path) -> str:
    return (_base(out) / "README.md").read_text(encoding="utf-8")


def _readme_symlink_commands(out: Path) -> list[list[str]]:
    cmds = [shlex.split(line) for line in _readme(out).splitlines()
            if line.startswith("ln -s ")]
    assert cmds, "kustomize/base/README.md no longer carries any `ln -s` line"
    return cmds


def _readme_cp_fallback(out: Path) -> str:
    lines = [line for line in _readme(out).splitlines()
             if line.startswith("find -L ")]
    assert len(lines) == 1, (
        f"expected exactly one `find -L …` cp-fallback line in the README, "
        f"got {len(lines)}")
    return lines[0]


def _run(argv: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(argv, cwd=str(cwd), capture_output=True, text=True,
                          encoding="utf-8", timeout=TIMEOUT_S)


def _setup_symlinks(out: Path) -> None:
    for argv in _readme_symlink_commands(out):
        proc = _run(argv, _base(out))
        assert proc.returncode == 0, f"README step {argv} failed: {proc.stderr}"


_BUILD_RE = re.compile(r"\bkustomize build\b[^\n>|]*")


def _workflow_build_commands(out: Path) -> dict[str, list[str]]:
    """The `kustomize build …` invocation each generated pipeline runs.

    Parsed from the YAML (step `run:` bodies / job `script:` lists), then cut
    at the first `>`/`|` so the shell redirection is not part of argv.
    """
    found: dict[str, list[list[str]]] = {"github": [], "gitlab": []}
    gh = yaml.safe_load((out / ".github" / "workflows" /
                         "dynamic-alerting.yaml").read_text(encoding="utf-8"))
    for job in (gh.get("jobs") or {}).values():
        for step in job.get("steps") or []:
            for m in _BUILD_RE.finditer(str(step.get("run") or "")):
                found["github"].append(shlex.split(m.group(0)))
    gl = yaml.safe_load((out / ".gitlab-ci.d" /
                         "dynamic-alerting.yml").read_text(encoding="utf-8"))
    for job in gl.values():
        if not isinstance(job, dict):
            continue
        for line in job.get("script") or []:
            for m in _BUILD_RE.finditer(str(line)):
                found["gitlab"].append(shlex.split(m.group(0)))
    for leg, cmds in found.items():
        assert len(cmds) == 1, (
            f"expected exactly one `kustomize build` in the {leg} pipeline, "
            f"found {cmds}")
    return {leg: cmds[0] for leg, cmds in found.items()}


def _configmap(stdout: str) -> dict:
    docs = [d for d in yaml.safe_load_all(stdout) if d]
    cms = [d for d in docs if d.get("kind") == "ConfigMap"
           and d.get("metadata", {}).get("name") == "threshold-config"]
    assert len(cms) == 1, f"expected one threshold-config ConfigMap, got {docs}"
    return cms[0]


def _expected_keys(out: Path) -> set[str]:
    conf = {p.name for p in (out / "conf.d").iterdir()
            if p.suffix in (".yaml", ".yml") and not p.name.startswith(".")}
    listed = set(yaml.safe_load((_base(out) / "kustomization.yaml").read_text(
        encoding="utf-8"))["configMapGenerator"][0]["files"])
    assert conf == listed, (
        f"kustomization.yaml `files:` {sorted(listed)} != conf.d/ "
        f"{sorted(conf)} — a key would be missing from (or dangling in) "
        "the ConfigMap")
    assert {f"{t}.yaml" for t in TENANTS} <= conf, conf
    return conf


def _assert_built(proc: subprocess.CompletedProcess, out: Path) -> None:
    assert proc.returncode == 0, f"kustomize build rc={proc.returncode}: {proc.stderr[-800:]}"
    cm = _configmap(proc.stdout)
    assert set(cm.get("data", {})) == _expected_keys(out), (
        f"ConfigMap keys {sorted(cm.get('data', {}))} != conf.d/ files")
    assert cm["metadata"].get("namespace") == "monitoring", cm["metadata"]


# ═══════════════════════════════════════════════════════════════════════════
# The gate
# ═══════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("leg", ["github", "gitlab"])
def test_pipeline_build_command_after_readme_setup(leg, kustomize, tmp_path):
    """README 的 `ln -s` → 產出 pipeline 的那一行 `kustomize build`（從 repo 根）。"""
    root, out = _generate(tmp_path)
    _setup_symlinks(out)
    argv = _workflow_build_commands(out)[leg]
    assert argv[0] == "kustomize", argv
    _assert_built(_run([kustomize, *argv[1:]], root), out)


def test_readme_cp_fallback_then_bare_build(kustomize, tmp_path):
    """README 的 cp fallback → 不帶旗標的 `kustomize build`（README 說這樣就能過）。"""
    _root, out = _generate(tmp_path)
    proc = _run(["sh", "-c", _readme_cp_fallback(out)], _base(out))
    assert proc.returncode == 0, f"README cp fallback failed: {proc.stderr}"
    assert not any(p.is_symlink() for p in _base(out).iterdir())
    _assert_built(_run([kustomize, "build", "kustomize/overlays/prod"], out), out)


def test_symlink_without_the_flag_fails_the_way_the_readme_says(kustomize,
                                                                tmp_path):
    """README 逐字寫出不帶 `--load-restrictor` 時的錯誤；那句話必須仍然是真的。"""
    _root, out = _generate(tmp_path)
    _setup_symlinks(out)
    proc = _run([kustomize, "build", "kustomize/overlays/prod"], out)
    assert proc.returncode != 0, "bare build over symlinks now succeeds — the README's warning is stale"
    # README 把那句錯誤折成多行；比對前先把空白正規化。
    readme = " ".join(_readme(out).split())
    for phrase in ("security; file", "is not in or below"):
        assert phrase in proc.stderr, (phrase, proc.stderr[-800:])
        assert phrase in readme, f"README no longer quotes {phrase!r}"


def test_the_readme_setup_is_load_bearing(kustomize, tmp_path):
    """對照組：沒做 README 的 setup，同一行 pipeline 指令必須失敗。

    否則上面那幾條就說明不了 setup 步驟有沒有作用。
    """
    root, out = _generate(tmp_path)
    argv = _workflow_build_commands(out)["github"]
    proc = _run([kustomize, *argv[1:]], root)
    assert proc.returncode != 0, "raw generated tree builds without the README setup"


OVERLAYS = ("dev", "prod")


def test_the_overlays_under_test_are_all_the_overlays_init_writes(tmp_path):
    """下面那條只 build ``OVERLAYS``；init 若多產一個 overlay 而沒人補上 build
    驗收，它就會像 gitops overlay 那樣帶著 no-op patch 出貨（issue #1349）。"""
    _root, out = _generate(tmp_path)
    written = {d.name for d in (out / "kustomize" / "overlays").iterdir() if d.is_dir()}
    assert written == set(OVERLAYS), written


@pytest.mark.parametrize("overlay", OVERLAYS)
def test_every_generated_overlay_builds(overlay, kustomize, tmp_path):
    root, out = _generate(tmp_path)
    _setup_symlinks(out)
    overlay_dir = out / "kustomize" / "overlays" / overlay
    assert overlay_dir.is_dir(), f"init did not generate overlays/{overlay}"
    _assert_built(_run([kustomize, "build", "--load-restrictor",
                        "LoadRestrictionsNone", str(overlay_dir)], root), out)



# ═══════════════════════════════════════════════════════════════════════════
# The gate runs (not just exists)
# ═══════════════════════════════════════════════════════════════════════════
def test_ci_runs_this_module_with_the_pinned_kustomize():
    """ci.yml 必須以釘住版本安裝 kustomize，並以 KUSTOMIZE_REQUIRE=1 跑本模組。

    不需要 kustomize binary——這條在任何地方都跑，保證上面那些 skip 在 CI
    上不會發生。
    """
    ci = yaml.safe_load(CI_YML.read_text(encoding="utf-8"))
    hits = []
    for name, job in (ci.get("jobs") or {}).items():
        steps = job.get("steps") or []
        installs = [s for s in steps
                    if "kustomize" in str(s.get("run") or "")
                    and f"KUSTOMIZE_VERSION={KUSTOMIZE_PIN}" in str(s.get("run") or "")
                    and "_verify_download.sh" in str(s.get("run") or "")]
        runs = [s for s in steps
                if THIS_MODULE in str(s.get("run") or "")
                and str((s.get("env") or {}).get("KUSTOMIZE_REQUIRE")) == "1"]
        if installs and runs:
            assert steps.index(installs[0]) < steps.index(runs[0]), (
                f"job {name}: the module runs before kustomize is installed")
            assert not job.get("if"), (
                f"job {name} is conditional (`if:`) — the gate must run on every PR")
            hits.append(name)
    assert hits, (
        f"no ci.yml job installs kustomize v{KUSTOMIZE_PIN} (sha-verified) and "
        f"then runs {THIS_MODULE} with KUSTOMIZE_REQUIRE=1 — without that step "
        "every test above is a skip in CI")
