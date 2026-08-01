"""#1219：Defaults Impact Guard 的 scope 解析必須指到真的被改的那棵樹。

原本的失效是複合的：shallow clone 讓 `git diff` 取不到 base ref、`|| true`
把失敗吞掉、scope 於是退回一個 PR 從沒碰過的目錄，然後貼綠。**兩個缺陷互相
遮蔽**——scope 永遠算不出來，所以永遠踩不到「scope 落在 config-dir 之外」
那條路。本檔釘住修好後的那一步：給定改動清單，解析出來的 (config-dir, scope)
必須落在該檔自己的 conf.d 樹上。

非空虛守衛的期望值刻意寫成**字面值**：從被守護的對象推導出來的斷言不守護它
（#1283 的教訓——期望集寫成 `*_EXTRA_SCANNED` 會讓清空那個 tuple 同時清空期望）。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO / "scripts" / "ops"))

import guard_defaults_scopes as mod  # noqa: E402

_WORKFLOW = _REPO / ".github" / "workflows" / "guard-defaults-impact.yml"

# 字面錨點：這些 conf.d 樹在 repo 內確實存在，且**沒有一棵**位於 guard 從前
# 唯一會驗的那個目錄底下。若整批消失，下面的 live-repo 測試會變成空虛通過，
# 所以它們必須寫死在這裡而不是從掃描結果推導。
_ANCHOR_ROOTS = {
    "components/threshold-exporter/config/conf.d",
    "try-local/seed/conf.d",
    "rule-packs/recipes/examples/conf.d",
}
_LEGACY_ONLY_ROOT = "components/threshold-exporter/config/conf.d"


def _discover_defaults() -> list[str]:
    """探索式，非列舉：任何新增的 `_defaults.yaml` 自動納入。"""
    out = []
    for p in _REPO.rglob("_defaults.yaml"):
        rel = p.relative_to(_REPO).as_posix()
        if rel.startswith(".git/"):
            continue
        out.append(rel)
    return sorted(out)


class TestRootResolution:
    @pytest.mark.parametrize(
        "path,expected",
        [
            ("try-local/seed/conf.d/_defaults.yaml", "try-local/seed/conf.d"),
            ("a/conf.d/db/mariadb/_defaults.yaml", "a/conf.d"),
            # 巢狀 conf.d 取內層——那才是該份設定被解析時的根
            ("a/conf.d/b/conf.d/_defaults.yaml", "a/conf.d/b/conf.d"),
            ("no/such/tree/_defaults.yaml", None),
            # 目錄自己叫 conf.d 但檔案就在其下 → 根是它
            ("conf.d/_defaults.yaml", "conf.d"),
        ],
    )
    def test_conf_d_root(self, path, expected):
        assert mod.conf_d_root(path) == expected

    def test_single_dir_narrows_scope(self):
        targets, unmanaged = mod.resolve(["x/conf.d/db/_defaults.yaml"])
        assert targets == [("x/conf.d", "x/conf.d/db")]
        assert unmanaged == []

    def test_multiple_dirs_under_one_root_widen_to_root(self):
        """串接編輯必須整棵重驗，否則跨層的 redundant-override 驗不出來。"""
        targets, _ = mod.resolve(
            ["x/conf.d/_defaults.yaml", "x/conf.d/db/_defaults.yaml"]
        )
        assert targets == [("x/conf.d", "x/conf.d")]

    def test_independent_trees_produce_independent_targets(self):
        targets, _ = mod.resolve(
            ["a/conf.d/_defaults.yaml", "b/conf.d/_defaults.yaml"]
        )
        assert targets == [("a/conf.d", "a/conf.d"), ("b/conf.d", "b/conf.d")]

    def test_unmanaged_is_reported_not_dropped(self):
        """⛔ 靜默略過正是本票要消滅的形狀——必須回報出來讓呼叫端說明。"""
        targets, unmanaged = mod.resolve(["docs/example/_defaults.yaml"])
        assert targets == []
        assert unmanaged == ["docs/example/_defaults.yaml"]


class TestLiveRepo:
    def test_discovery_is_not_vacuous(self):
        found = _discover_defaults()
        roots = {mod.conf_d_root(f) for f in found}
        missing = _ANCHOR_ROOTS - roots
        assert not missing, (
            f"conf.d 樹 {sorted(missing)} 不見了——若是刻意移除請更新錨點，"
            "否則下面每一條斷言都會空虛通過"
        )

    def test_every_repo_defaults_file_resolves_to_a_tree(self):
        """逐項而非全域下限：任何一個解不出根的檔案都要點名。"""
        found = _discover_defaults()
        _, unmanaged = mod.resolve(found)
        assert unmanaged == [], (
            f"這些 _defaults.yaml 不在任何 conf.d 樹下：{unmanaged}。"
            "guard 會回報『未檢查』而非靜默貼綠，但若這是預期外的擺放位置，"
            "應該先確認它是否真的該被 guard 涵蓋"
        )

    def test_repo_spans_more_than_the_legacy_root(self):
        """#1219 的成因本身：guard 從前只驗一棵樹，而 repo 有很多棵。"""
        found = _discover_defaults()
        roots = {mod.conf_d_root(f) for f in found}
        outside = {r for r in roots if r != _LEGACY_ONLY_ROOT}
        assert len(outside) >= 2, (
            "repo 只剩一棵 conf.d 樹的話，本 guard 的多樹處理就沒有守護對象了"
        )

    def test_the_1219_regression_case(self):
        """PR #1216 的實際情境：只改 try-local 那一份。

        修好前 guard 驗的是 components/.../conf.d（該 PR 完全沒碰）並貼綠。
        """
        targets, unmanaged = mod.resolve(["try-local/seed/conf.d/_defaults.yaml"])
        assert unmanaged == []
        assert targets == [("try-local/seed/conf.d", "try-local/seed/conf.d")]
        assert targets[0][0] != _LEGACY_ONLY_ROOT


class TestWorkflowContract:
    """workflow 端的三個前提——任一被改回去，這裡就轉紅。"""

    @staticmethod
    def _wf() -> dict:
        return yaml.safe_load(_WORKFLOW.read_text(encoding="utf-8"))

    @staticmethod
    def _steps() -> list[dict]:
        return TestWorkflowContract._wf()["jobs"]["guard"]["steps"]

    def test_checkout_fetches_full_history(self):
        """shallow clone 沒有 origin/<base>，diff 會 fatal（#1219 第 1 步）。"""
        checkout = [s for s in self._steps() if str(s.get("uses", "")).startswith("actions/checkout")]
        assert checkout, "找不到 checkout step——本斷言會空虛通過"
        for step in checkout:
            assert (step.get("with") or {}).get("fetch-depth") == 0, (
                "checkout 必須 fetch-depth: 0，否則 scope 步驟取不到 base ref"
            )

    @staticmethod
    def _code_lines(run: str) -> list[str]:
        """只留可執行行，且把 `\\` 續行接成一條。

        兩個都是實測踩出來的，不是預防性設計：

        1. 整段字串比對會咬到解釋用的**註解**——本檔第一版說明「原本是
           `|| true`」的那行讓斷言誤報。比對法比目標寬鬆 → 假陽性。
        2. 逐行比對又會漏掉**續行**——`git diff ... \\` 換行後才接重導向，
           把 `|| true` 加在續行上就完全逃過斷言。變異測試實測證明了這個洞：
           把 `|| true` 加回去，測試照樣全綠。比對法比目標窄 → 假陰性。

        兩者是同一個病的兩個方向，所以這裡先接續行、再濾註解。
        """
        joined = run.replace("\\\n", " ")
        out = []
        for raw in joined.splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            out.append(line)
        return out

    def test_scope_step_does_not_swallow_diff_failure(self):
        """`|| true` 讓 base ref 取不到看起來像『沒有檔案改動』（#1219 第 2 步）。"""
        checked = 0
        for step in self._steps():
            run = step.get("run") or ""
            if "git diff" not in run or "_defaults.yaml" not in run:
                continue
            checked += 1
            for line in self._code_lines(run):
                if "git diff" in line:
                    assert "|| true" not in line, (
                        f"git diff 不可用 `|| true` 吞掉失敗——那是本票的根因：{line}"
                    )
        assert checked == 1, (
            f"預期恰有 1 個 step 對 _defaults.yaml 跑 git diff，實際 {checked} 個"
            "——0 表示本斷言空虛通過"
        )

    def test_zero_defaults_is_not_an_error(self):
        """觸發面有 6 條 path，其中 5 條與 `_defaults.yaml` 無關。

        本 PR 自己就是實例：它只改 workflow 與 guard 周邊，`_defaults.yaml`
        變更數為 0。若把「解析不出 `_defaults.yaml`」當成硬錯誤，任何改
        da-guard 原始碼或這支 workflow 的 PR 都會被自己擋下。第一版正是這樣
        寫的（且註解還宣稱「this workflow only runs on that path filter」，
        那句是錯的）——留下這條斷言避免再犯。
        """
        wf = self._wf()
        paths = wf[True]["pull_request"]["paths"]
        non_defaults = [p for p in paths if "_defaults.yaml" not in p]
        assert non_defaults, (
            "觸發面只剩 _defaults.yaml 的話本斷言就沒有守護對象了；"
            "若真的改成單一 path，請一併重新考慮零結果的處置"
        )
        for step in self._steps():
            run = step.get("run") or ""
            if "guard-targets.tsv" not in run and "TARGETS" not in run:
                continue
            for line in self._code_lines(run):
                if "::error::" in line and "_defaults.yaml" in line:
                    pytest.fail(
                        f"零個 _defaults.yaml 不可當硬錯誤——{len(non_defaults)} "
                        f"條觸發 path 與它無關：{line}"
                    )

    def test_scope_step_uses_the_resolver(self):
        """避免 inline bash 重新長出一份未受測的根推導邏輯。"""
        runs = "\n".join((s.get("run") or "") for s in self._steps())
        assert "guard_defaults_scopes.py" in runs, (
            "scope 解析必須走 scripts/ops/guard_defaults_scopes.py（有單元測試釘住）"
        )
