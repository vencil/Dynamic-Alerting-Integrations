"""Regression tests for scripts/tools/dx/generate_rule_pack_readme.py.

Pins the two failure modes that left rule-packs/README.md badly stale on main
behind a dead drift gate (the generator never ran successfully, so nobody
noticed):

1. **Path default** — the argparse ``--rule-packs-dir`` default must resolve to
   the real repo-root ``rule-packs/`` dir. The original bug used one too few
   ``.parent`` hops (``parents[2]`` → ``scripts/rule-packs/``, which does not
   exist), so every invocation exited 2 (FileNotFoundError) and the
   ``validate_all`` ``--check`` gate silently no-op'd.

2. **Prose pack-count** — the header / "Dynamic Runbook Injection" "N 個 Rule
   Pack" figure must be DERIVED and equal the AUTHORITATIVE count
   (``validate_docs_versions.count_rule_packs``), not a hardcoded literal that
   silently goes stale (it was frozen at "11" while the platform shipped 15).

conftest.py already puts scripts/tools{,/dx,/lint} on sys.path, so the tool
modules import directly.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

import generate_rule_pack_readme as grpr  # noqa: E402  (path via conftest)
import validate_docs_versions as vdv  # noqa: E402  (path via conftest)

REPO_ROOT = Path(__file__).resolve().parents[2]
GENERATOR = REPO_ROOT / "scripts" / "tools" / "dx" / "generate_rule_pack_readme.py"

DRIFT_MARKER = "DRIFT-MARKER-PLANTED-BY-TEST"


def test_default_rule_packs_dir_resolves_to_repo_root():
    """Regression: parents[3] (repo root), not parents[2] (scripts/)."""
    default = grpr.DEFAULT_RULE_PACKS_DIR
    assert default == REPO_ROOT / "rule-packs"
    assert default.is_dir(), f"{default} is not a directory (path-default bug?)"
    # ...and it actually holds rule-pack YAMLs, so generate_table_rows won't bail.
    assert list(default.glob("rule-pack-*.yaml")), "no rule-pack-*.yaml under default dir"


def test_prose_pack_count_is_derived_and_matches_authoritative():
    """Prose count derives from the authoritative pack SET, not a literal.

    count_preloaded_packs (configmap set − custom-alerts) must equal
    validate_docs_versions.count_rule_packs()['pack_count'] (rule-packs ∪
    configmaps − custom-alerts) — the same set, so they must agree now and as
    packs are added. (Guards against the old hardcoded "11" / "15" and against
    a derivation that only coincidentally lands on the right number.)
    """
    generated = grpr.count_preloaded_packs(grpr.DEFAULT_RULE_PACKS_DIR)
    authoritative = vdv.count_rule_packs()["pack_count"]
    assert generated == authoritative, (
        f"prose pack-count {generated} != authoritative {authoritative}"
    )


def test_check_mode_passes_with_default_dir():
    """End-to-end: ``--check`` with the default dir exits 0 (gate live + in sync).

    Before the path-default fix this exited 2 (FileNotFoundError) regardless of
    drift — the exact symptom that made the validate_all gate dead.
    """
    result = subprocess.run(
        [sys.executable, str(GENERATOR), "--check"],
        capture_output=True, text=True, cwd=str(REPO_ROOT), timeout=60,
    )
    assert result.returncode == 0, (
        f"--check exited {result.returncode}\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )


# ---------------------------------------------------------------------------
# 三個模式的契約。`make generate-rule-pack-readme` 的 help 字串會陳述它們，而
# 在 #1892 之後本 repo 的判準是：**寫下而不守，就是下一句會腐爛的散文**。
# 上面那格 `test_check_mode_passes_with_default_dir` 只釘了 `--check` 的 rc 0
# 方向；少了 rc 1 那半，一支永遠回 0 的壞 gate 照樣全綠。
# ---------------------------------------------------------------------------


@pytest.fixture
def packs(tmp_path):
    """一份用完即丟的 rule-packs/ 副本——這幾格會實際觸發寫入模式。

    ⛔ 不可以對 repo 內的 `rule-packs/` 跑 `--update`：那會在測試執行期間改動
    受版控的產物，而 drift gate（validate_all）讀的正是同一個檔。
    """
    dst = tmp_path / "rule-packs"
    shutil.copytree(REPO_ROOT / "rule-packs", dst)
    return dst


def _run(packs_dir: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(GENERATOR), "--rule-packs-dir", str(packs_dir), *args],
        capture_output=True, text=True, timeout=60,
    )


def _plant_drift(packs_dir: Path) -> None:
    readme = packs_dir / "README.md"
    readme.write_text(readme.read_text(encoding="utf-8") + f"\n{DRIFT_MARKER}\n",
                      encoding="utf-8")


def test_default_mode_is_a_dry_run_that_writes_nothing(packs):
    """不帶旗標 = dry-run 印到 stdout。⚠️ 這是 Makefile help 指給讀者的那個模式。

    同時斷言「有印東西」與「沒動檔案」：只驗其中一半的話，一支什麼都不做的實作
    （rc 0、stdout 空）也會過。
    """
    readme = packs / "README.md"
    before = readme.read_bytes()
    proc = _run(packs)
    assert proc.returncode == 0, f"rc={proc.returncode}\nstderr:\n{proc.stderr}"
    assert 'title: "Rule Packs' in proc.stdout, (
        f"dry-run 應把產生的 README 印到 stdout，實得 {len(proc.stdout)} 字元"
    )
    assert readme.read_bytes() == before, "dry-run 不得寫檔"


def test_update_rewrites_the_readme(packs):
    """`--update` = 真的寫回。這是 make 目標固定帶的那個旗標。

    先種一筆 drift 再跑，否則「寫入相同位元組」與「根本沒寫」分不出來。

    ⛔ 斷言的是「內容等於產生出來的那份」，**不是**「drift marker 不見了」。
    變異測試抓到過這個洞：`open(readme_path, "w")` 光是開檔就會截斷，所以把
    `f.write()` 整行拿掉之後 marker 一樣會消失——只驗 marker 的版本照樣全綠。
    """
    expected = _run(packs).stdout          # dry-run 印出的就是「應該被寫進去的內容」
    assert 'title: "Rule Packs' in expected, "前置條件：dry-run 要真的有產出"
    _plant_drift(packs)
    readme = packs / "README.md"
    assert DRIFT_MARKER in readme.read_text(encoding="utf-8")
    proc = _run(packs, "--update")
    assert proc.returncode == 0, f"rc={proc.returncode}\nstderr:\n{proc.stderr}"
    written = readme.read_text(encoding="utf-8")
    assert written.rstrip("\n") == expected.rstrip("\n"), (
        "--update 應把 README 重寫成產生出來的內容；實得 "
        f"{len(written)} 字元，期待 {len(expected.rstrip(chr(10)))} 字元"
    )


def test_check_reports_drift_as_rc_1_without_writing(packs):
    """`--check` 的違規方向：有 drift ⇒ rc 1，而且**不修**它。

    ⚠️ 這是既有那格 rc 0 測試的反空轉下限。`--check` 是 validate_all 的
    「Rule Pack README drift」gate 所呼叫的模式；少了這一半，gate 退化成
    永遠回 0 也沒有東西會喊。
    """
    _plant_drift(packs)
    readme = packs / "README.md"
    proc = _run(packs, "--check")
    assert proc.returncode == 1, (
        f"有 drift 時 --check 應回 rc 1，實得 rc={proc.returncode}\n"
        f"stderr:\n{proc.stderr}"
    )
    assert DRIFT_MARKER in readme.read_text(encoding="utf-8"), (
        "--check 是偵測不是修復，不得寫檔"
    )


def test_check_wins_over_update(packs):
    """`--update --check` ⇒ check 贏（rc 1、不寫檔），因為 main() 先判 `args.check`。

    ⛔ 這一格守的是一個**介面決定**，不只是實作細節：`make generate-rule-pack-readme`
    的 recipe 固定帶 `--update` 且**刻意不接 `$(ARGS)``，理由正是這個優先序——若把
    兩者串起來，`ARGS="--check"` 是否會意外寫檔就取決於 main() 裡兩個分支的先後。

    ⇒ 這一格紅掉不代表「測試過時」。它代表那個先後被調換了，屆時要一併重新檢視
    Makefile 的 recipe 與 help 字串，而不是改這格的期待值。
    """
    _plant_drift(packs)
    readme = packs / "README.md"
    proc = _run(packs, "--update", "--check")
    assert proc.returncode == 1, (
        f"--update --check 應由 --check 決定結果（rc 1），實得 rc={proc.returncode}\n"
        f"stderr:\n{proc.stderr}"
    )
    assert DRIFT_MARKER in readme.read_text(encoding="utf-8"), (
        "--check 在前 ⇒ 不得寫檔。若這行紅了，`ARGS=\"--check\"` 會變成靜默寫檔，"
        "Makefile 那個「不接 $(ARGS)」的決定就要重做。"
    )
