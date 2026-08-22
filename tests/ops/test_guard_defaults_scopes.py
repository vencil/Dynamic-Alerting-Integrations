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

    def test_a_leading_blank_does_not_move_the_file_to_another_tree(self):
        """⛔ 這是 TAB 缺陷的安靜版本，而且結局一樣是綠燈報錯的樹。

        ` conf.d/db/_defaults.yaml`（一個前導空格）在 Linux 上是一棵**不同**的
        樹。先前 `resolve()` 開頭寫 `raw.strip()`，於是它被改寫成
        `conf.d/db/_defaults.yaml`——**一棵確實存在的別的樹**——da-guard 去驗
        那一棵、sticky 留言貼綠。#1219 的結局，換一個入口。
        """
        targets, unmanaged = mod.resolve([" conf.d/db/_defaults.yaml"])
        assert targets == [], (
            f"a path was rewritten onto another tree: {targets}")
        assert unmanaged == [" conf.d/db/_defaults.yaml"], (
            "the leading blank must survive into the report, so the operator "
            f"sees the name that was actually changed: {unmanaged}")

    def test_a_trailing_blank_is_not_trimmed_away_either(self):
        """同一條的另一半：尾隨空白也是檔名的一部分。"""
        targets, _ = mod.resolve(["x/conf.d/db /_defaults.yaml"])
        assert targets == [("x/conf.d", "x/conf.d/db ")], targets

    def test_a_backslash_in_a_name_is_not_a_directory_separator(self):
        """⛔ 同一類別的第三個成員，先前活在同一個函式裡。

        反斜線在 POSIX 檔名裡合法（`mkdir 'we\\ird'` 在 Linux 上直接成功），而
        `conf_d_root` / `resolve` 都對路徑做過 `replace("\\\\", "/")`。於是
        `conf.d/we\\ird/_defaults.yaml` 被改寫成 `conf.d/we/ird`——scope 指向一個
        **不存在的目錄** ⇒ da-guard exit 2 ⇒ job 紅，而訊息指著一個從沒出現在
        PR diff 裡的路徑。輸入永遠來自 Linux 的 `git diff -z`，`/` 是唯一的分隔符，
        所以那個正規化沒有任何合法輸入。
        """
        path = "conf.d/we\\ird/_defaults.yaml"
        assert mod.conf_d_root(path) == "conf.d", mod.conf_d_root(path)
        targets, unmanaged = mod.resolve([path])
        assert targets == [("conf.d", "conf.d/we\\ird")], targets
        assert unmanaged == []

    def test_a_backslash_does_not_invent_a_conf_d_ancestor(self):
        """另一個方向：正規化也會**憑空生出**一層 conf.d 祖先。

        真實目錄名 `we\\conf.d` 是一個元件、不是兩層，所以這個檔案不在任何
        conf.d 樹底下（正確答案是 unmanaged、由呼叫端出聲）。改寫過後它變成
        `we/conf.d/...`，於是被當成 target 送去驗一棵不存在的樹。
        """
        path = "we\\conf.d/x/_defaults.yaml"
        assert mod.conf_d_root(path) is None
        targets, unmanaged = mod.resolve([path])
        assert targets == []
        assert unmanaged == [path]

    @pytest.mark.parametrize("entries, expect_unmanaged", [
        # 分隔符是**終止符**，所以最後一個切片一定是 ""。只有它該被跳過。
        (["x/conf.d/_defaults.yaml", ""], []),
        # ⛔ 反向：全空白的項目不是空字串，不可以被靜默丟掉——它沒有 conf.d
        # 祖先，所以該落到 unmanaged 讓呼叫端出聲說「沒有為它驗任何東西」。
        ([" "], [" "]),
    ], ids=["trailing-empty-is-skipped", "whitespace-only-is-reported"])
    def test_only_the_genuinely_empty_entry_is_skipped(self, entries,
                                                       expect_unmanaged):
        _, unmanaged = mod.resolve(entries)
        assert unmanaged == expect_unmanaged


class TestLiveRepo:
    def test_discovery_is_not_vacuous(self):
        found = _discover_defaults()
        roots = {mod.conf_d_root(f) for f in found}
        missing = _ANCHOR_ROOTS - roots
        assert not missing, (
            f"conf.d 樹 {sorted(missing)} 不見了——若是刻意移除請更新錨點，"
            "否則下面每一條斷言都會空虛通過"
        )

    def test_no_repo_defaults_file_is_silently_dropped(self):
        """⛔ 逐項對帳：每一個檔案要嘛落在某個 target 的根底下，要嘛被點名為
        unmanaged。**沒有第三種下場**——靜默消失正是 #1219 的形狀。

        ⚠️ 這一條**刻意不再斷言 `unmanaged == []`**。那個版本斷言的是 repo 的
        擺放政策（「所有 `_defaults.yaml` 都必須住在某棵 conf.d 樹裡」），不是
        guard 的正確性：`resolve()` 設計上就把樹外的檔案報成 unmanaged，那是它
        該做的事。於是任何一個新增的樹外 `_defaults.yaml`——文件範例、客戶樣板、
        測試 fixture——都會讓這條紅，而它**沒有豁免機制**，所以最省事的轉綠方式
        是在 `_discover_defaults()` 裡自己發明一份 skip list。那份清單會擴散、
        不需要理由、而且會連帶蓋掉真正該被看見的檔案。

        現在守的是可推導的那一半：`conf_d_root()` 與 `resolve()` 對每一個檔案的
        判斷必須一致。任一個檔案兩邊都沒有認領它，這裡就點名它。
        """
        found = _discover_defaults()
        targets, unmanaged = mod.resolve(found)
        roots = {root for root, _scope in targets}
        missing = []
        for path in found:
            root = mod.conf_d_root(path)
            claimed = path in unmanaged if root is None else root in roots
            if not claimed:
                missing.append((path, root))
        assert not missing, (
            f"這些 _defaults.yaml 既沒有被解析成 target、也沒有被回報為 "
            f"unmanaged，等於從 guard 的視野裡消失：{missing}"
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


class TestNonAsciiPathsAreNotSilentlyMisplaced:
    """git 預設會編碼非 ASCII 路徑，而本腳本無法安全解碼它。

    ⛔ 這一類**兩個方向都壞、其中一個是靜默的**（皆為實測，#1419 第七輪）：
    conf.d 根在上層時解析成一個不存在的目錄（da-guard exit 2 → 紅，訊息指著
    da-guard）；根在下層時被歸成 `unmanaged` → WORST 維持 0 → **job 綠**，
    而留言斷言「此檔不在任何 conf.d 樹底下」——它就在底下。

    因此正解是**拒絕評分**而不是加一個反跳脫器：判準是「git 只有在編碼過時
    才會產生開頭的 `"`」，對兩個方向都封閉，不需要列舉跳脫序列的形狀。
    """

    _QUOTED_ABOVE_ROOT = '"\\347\\222\\260\\345\\242\\203-prod/conf.d/_defaults.yaml"'
    _QUOTED_BELOW_ROOT = '"conf.d/\\347\\247\\237\\346\\210\\266-a/_defaults.yaml"'

    @pytest.mark.parametrize("line", [_QUOTED_ABOVE_ROOT, _QUOTED_BELOW_ROOT])
    def test_c_quoted_input_is_refused_not_scored(self, line, capsys,
                                                  monkeypatch):
        """⛔ Newline 模式才拒絕——這一半的封閉性是實測過的。

        git 對「本來就以引號開頭的檔名」會再包一層引號，所以 newline 模式下
        每一個 `"` 開頭的行都是編碼過的輸出，拒絕是唯一安全的答案。
        （NUL 模式相反，見下一支測試。）
        """
        monkeypatch.setattr("sys.stdin", _Stdin(line + "\n"))
        rc = mod.main(["-"])
        captured = capsys.readouterr()
        # ⛔ 字面值，不是 `mod.EXIT_QUOTED_INPUT`。從產物抄期望值只證明它等於
        # 自己：把那個常數改成 0 之後「拒絕評分」變成印一段 stderr 然後回 0，
        # 呼叫端在 `bash -e` 下看不到失敗、TSV 是空檔、workflow 落到 whole-tree
        # 分支——綠燈，而那個檔從頭到尾沒被評分。整份測試仍全綠。
        assert rc == 2, (
            f"C-quoted input was scored instead of refused (rc={rc}); "
            f"stdout={captured.out!r}")
        assert mod.EXIT_QUOTED_INPUT == 2, (
            "the caller-visible contract is a non-zero exit; changing the "
            "constant is a behaviour change, not a rename")
        assert not captured.out, (
            f"a refused run still emitted rows: {captured.out!r} — the caller "
            f"writes stdout to guard-targets.tsv, so a partial answer here is "
            f"worse than none")
        assert "-z" in captured.err and "--null" in captured.err, (
            f"the refusal must name the fix on the producer side: {captured.err!r}")

    def test_the_refusal_message_does_not_advise_dropping_the_paths(self, capsys,
                                                                    monkeypatch):
        """守衛的錯誤訊息也是一條繳械路徑。

        「照做之後系統會變好還是變壞」——教人剝掉引號或略過這些列，就是把這個
        缺陷原樣留下並且從此不再出聲。
        """
        monkeypatch.setattr("sys.stdin", _Stdin(self._QUOTED_ABOVE_ROOT + "\n"))
        mod.main(["-"])
        err = capsys.readouterr().err
        assert "Do NOT drop" in err and "do NOT strip" in err, err

    def test_null_separated_input_is_read_verbatim(self, capsys, monkeypatch):
        """`-z` 之後路徑是原始位元組，非 ASCII 必須原封不動落到 target。"""
        payload = "conf.d/租戶-a/_defaults.yaml\0conf.d/租戶-b/_defaults.yaml\0"
        monkeypatch.setattr("sys.stdin", _Stdin(payload))
        rc = mod.main(["--null", "-"])
        out = capsys.readouterr().out
        assert rc == 0, out
        # 兩個目錄同根 → scope 收窄到根（既有規則），且完全沒有 unmanaged。
        assert "unmanaged" not in out, (
            f"a real conf.d tree was reported as outside every tree: {out!r}")
        assert out.splitlines() == ["target\tconf.d\tconf.d"], out

    def test_ascii_input_still_works_without_the_flag(self, capsys, monkeypatch):
        """反向樣本：拒絕規則不可以朝『見人就咬』漂移。"""
        monkeypatch.setattr("sys.stdin", _Stdin("conf.d/a/_defaults.yaml\n"))
        rc = mod.main(["-"])
        out = capsys.readouterr().out
        assert rc == 0, out
        assert out.splitlines() == ["target\tconf.d\tconf.d/a"], out

    def test_a_leading_quote_is_a_legal_name_under_null_mode(self, capsys,
                                                             monkeypatch):
        """⛔ 反向樣本：`-z` 之下開頭的 `"` 是檔名本身，不是編碼。

        實測（`git mktree -z` 建出名為 `"quoted-dir` 的目錄）：
          git diff --name-only     -> "\\"quoted-dir/conf.d/_defaults.yaml"
          git diff -z --name-only  -> "quoted-dir/conf.d/_defaults.yaml\\0
        先前把拒絕擴到 NUL 模式，就是對這個合法名字硬失敗（`set -euo pipefail`
        之下整個 step 死掉），而訊息叫操作者「改用 -z + --null」——那正是他
        已經在做的事。一個走不通的補救配一個誤紅。
        """
        monkeypatch.setattr(
            "sys.stdin", _Stdin('"quoted-dir/conf.d/_defaults.yaml\0'))
        rc = mod.main(["--null", "-"])
        out = capsys.readouterr()
        assert rc == 0, f"a legal directory name was refused: {out.err!r}"
        assert out.out.splitlines() == [
            'target\t"quoted-dir/conf.d\t"quoted-dir/conf.d'], out.out

    @pytest.mark.parametrize("bad", ["conf.d/we\tird/_defaults.yaml",
                                     "conf.d/we\nird/_defaults.yaml"])
    def test_a_path_carrying_the_output_delimiters_is_refused(self, bad, capsys,
                                                              monkeypatch):
        """⛔ 控制字元不可能出現在欄位裡，而理由有兩層。

        實測：路徑內含 TAB 時輸出 `unmanaged\\twe\\tird/…`，呼叫端的
        `IFS=$'\\t' read -r kind a b` 把欄位整個位移，列形狀檢查照過（兩欄都
        非空），最後 sticky 留言寫 `### \\`we\\` — NOT CHECKED`——**綠燈，而且
        指著一個不存在的檔案**。這一層封閉在**輸出格式**上。

        ⚠️ 但 `\\r` 過得了那一層而仍然是缺陷：`read` 把它留在欄位裡、da-guard
        拿到一個不存在的目錄 ⇒ 紅燈，**而那則 `::error::` 被終端機重畫**，操作
        者讀到的是另一個路徑。所以判準取兩個來源的聯集（格式的分隔符＋ASCII
        C0），不是「TAB 與 newline」這兩個字。
        """
        monkeypatch.setattr("sys.stdin", _Stdin(bad + "\0"))
        rc = mod.main(["--null", "-"])
        out = capsys.readouterr()
        assert rc == 3, f"an unrepresentable path was emitted: {out.out!r}"
        assert not out.out, out.out
        assert "control character" in out.err, out.err
        # ⛔ 訊息要指名是哪一個字元。第一版只說「TAB or newline」，而現在被拒的
        # 集合更大——一則說不出兇手的拒絕，操作者無從照做（訊息教人 rename）。
        assert repr(next(c for c in bad if c in mod.UNREPRESENTABLE)) in out.err, \
            out.err

    @pytest.mark.parametrize("sep", ['\r', '\x0b', '\x0c', '\x1c', '\x1d', '\x1e'],
                             ids=["cr", "vtab", "ff", "fs", "gs", "rs"])
    def test_a_c0_separator_is_refused_rather_than_splitting_the_path(
            self, sep, capsys, monkeypatch):
        """⛔ 切分器只准斷在真正的換行上 —— 而這條先前零覆蓋。

        `splitlines()` 在**十一**個字元上斷行。實測：把它放回去，
        `conf.d/we<SEP>ird/_defaults.yaml` 會被腰斬成兩列——一列 target 把驗證
        範圍擴到整棵根，一列 unmanaged 指著一個**不存在的檔案**——而 rc=0 全綠。
        下面那道拒絕看的是切完之後的碎片，所以它完全不會醒：切分器在它上游。
        """
        monkeypatch.setattr(
            "sys.stdin", _Stdin("conf.d/we" + sep + "ird/_defaults.yaml\n"))
        rc = mod.main(["-"])
        out = capsys.readouterr()
        assert rc == 3, (
            f"{sep!r} split the path instead of being refused: {out.out!r}")
        assert not out.out, out.out

    @pytest.mark.parametrize("sep", ['\x85', '\u2028', '\u2029'],
                             ids=["nel", "ls", "ps"])
    def test_a_non_c0_line_separator_survives_whole(self, sep, capsys,
                                                    monkeypatch):
        """⛔ 同一條規則的另一半，而且這一半才抓得住剩下的三種切法。

        `splitlines()` 的十一個字元裡有三個**不在** C0（NEL 是 C1，LS/PS 是
        Unicode），所以拒絕集合對它們沉默——換回 `splitlines()` 之後它們照樣把
        路徑腰斬，而上面那六格全部照過。這裡要的不是「被拒絕」而是「**原封不動
        穿過**」：它們在 TSV 裡可表示（消費端的 `read` 只斷換行），所以正確行為
        是逐字保留。
        """
        path = "conf.d/we" + sep + "ird/_defaults.yaml"
        monkeypatch.setattr("sys.stdin", _Stdin(path + "\n"))
        rc = mod.main(["-"])
        out = capsys.readouterr()
        assert rc == 0, out.err
        expected = "target\tconf.d\tconf.d/we" + sep + "ird\n"
        assert out.out == expected, (
            f"{sep!r} split the path: {out.out!r}")
    def test_the_newline_splitter_still_accepts_ordinary_lines(self, capsys,
                                                               monkeypatch):
        """反向樣本：真正的換行仍然是分隔符，兩筆就是兩筆。"""
        monkeypatch.setattr("sys.stdin", _Stdin(
            "a/conf.d/x/_defaults.yaml\nb/conf.d/y/_defaults.yaml\n"))
        rc = mod.main(["-"])
        out = capsys.readouterr()
        assert rc == 0, out.err
        assert out.out.splitlines() == ["target\ta/conf.d\ta/conf.d/x",
                                        "target\tb/conf.d\tb/conf.d/y"], out.out

    @pytest.mark.parametrize("suffix", ["\r", "\v", "\x85"],
                             ids=["cr", "vtab", "nel"])
    def test_both_modes_give_the_same_answer_for_the_same_bytes(
            self, suffix, capsys, monkeypatch):
        """⛔ 同一串位元組，兩個模式必須同一個答案。

        newline 模式先前在切分後做 `p[:-1] if p.endswith("\\r")`，也就是把一個
        已經被 UNREPRESENTABLE 判死的字元**在拒絕之前**剝掉。實測 `conf.d/db` 尾隨
        一個 CR：`--null` rc=3 拒絕，newline 模式 rc=0 且輸出 `target\\tconf.d\\tconf.d`
        ——同一個輸入、相反的結局，而剝除那半正是同檔 `resolve()` 判死的 `strip()`。

        ⚠️ 誠實揭露鑑別力：這三格裡真正動過的只有 `[cr]`（CR 剝除那個回歸）與
        `[vtab]`（切分器換回 `splitlines()`）；`[nel]` 在本輪四個相關變異裡**一次
        都沒開火**。它是冗餘而不是覆蓋——絕對值那一面由上面兩支（C0 必拒／非 C0
        必須原封穿過）釘住，這一支釘的只是「兩個模式必須給同一個答案」這個差分
        性質。留著是因為那個性質本身值得釘，但不要把它讀成三種輸入都被證明過。
        """
        payload = f"conf.d/db{suffix}"
        monkeypatch.setattr("sys.stdin", _Stdin(payload + "\n"))
        newline_rc = mod.main(["-"])
        capsys.readouterr()
        monkeypatch.setattr("sys.stdin", _Stdin(payload + "\0"))
        null_rc = mod.main(["--null", "-"])
        capsys.readouterr()
        assert newline_rc == null_rc, (
            f"{payload!r} was refused in one mode and scored in the other: "
            f"newline={newline_rc} null={null_rc}")

    def test_the_two_refusals_do_not_share_one_exit_code(self, capsys,
                                                          monkeypatch):
        """⛔ 兩個拒絕的補救動作相反，退出碼不可以同一個。

        `EXIT_QUOTED_INPUT` 說的是「生產端沒給 `-z`」（去改 producer），
        unrepresentable 說的是「這個檔名不能被表示」（去 rename）。先前兩者共用
        一個常數，於是一個名叫 QUOTED_INPUT 的碼被回給一個含 TAB 的路徑——
        斷言了一個從未成立的成因。
        ⚠️ 呼叫端目前**不**分辨兩者（`set -euo pipefail` 之下都是死），這條釘的
        是日誌與這支測試分得出來，不是行為差異。
        """
        assert mod.EXIT_QUOTED_INPUT != mod.EXIT_UNREPRESENTABLE_PATH
        monkeypatch.setattr("sys.stdin", _Stdin(self._QUOTED_ABOVE_ROOT + "\n"))
        quoted_rc = mod.main(["-"])
        capsys.readouterr()
        monkeypatch.setattr(
            "sys.stdin", _Stdin("conf.d/we\tird/_defaults.yaml\0"))
        tab_rc = mod.main(["--null", "-"])
        capsys.readouterr()
        assert quoted_rc != tab_rc, (
            "the C-quoted refusal and the unrepresentable-path refusal are "
            "indistinguishable from the outside")

    @pytest.mark.parametrize("path, refused", [
        # 必須被拒：格式層（兩個分隔符）
        ("conf.d/we\tird/_defaults.yaml", True),
        ("conf.d/we\nird/_defaults.yaml", True),
        # 必須被拒：訊息層（C0，先前整批放行）
        ("conf.d/we\rird/_defaults.yaml", True),
        ("conf.d/we\vird/_defaults.yaml", True),
        ("conf.d/we\x1eird/_defaults.yaml", True),
        ("conf.d/we\x7fird/_defaults.yaml", True),
        # ⛔ 反向樣本：規則不可朝「見人就咬」漂移。這些都是 Linux 上合法且
        # 完全可表示的名字，被擋掉的話最省事的轉綠方式是刪掉這道拒絕。
        ("conf.d/we ird/_defaults.yaml", False),
        ("conf.d/ leading/_defaults.yaml", False),
        ("conf.d/trailing /_defaults.yaml", False),
        ("conf.d/租戶-a/_defaults.yaml", False),
        ('conf.d/"quoted/_defaults.yaml', False),
    ], ids=["tab", "newline", "cr", "vtab", "rs", "del",
            "interior-space", "leading-space", "trailing-space",
            "non-ascii", "quote-under-null"])
    def test_the_refusal_set_is_two_sided(self, path, refused, capsys,
                                          monkeypatch):
        """正反兩組樣本。只釘「該拒的被拒」的話，全部拒絕也能通過那一半。"""
        monkeypatch.setattr("sys.stdin", _Stdin(path + "\0"))
        rc = mod.main(["--null", "-"])
        out = capsys.readouterr()
        if refused:
            assert rc == 3, f"{path!r} was emitted anyway: {out.out!r}"
            assert not out.out, out.out
        else:
            assert rc == 0, f"a legal path was refused: {out.err!r}"
            # 輸出的是 scope（父目錄），所以逐位元組比對的對象是父目錄——這正
            # 是「別把期望值從產物抄下來」的相反面：期望值由**輸入**推導。
            scope = path.rsplit("/", 1)[0]
            assert out.out == f"target\tconf.d\t{scope}\n", (
                f"{scope!r} did not reach the output verbatim: {out.out!r}")

    def test_non_utf8_path_bytes_survive_verbatim(self, capsysbinary,
                                                  monkeypatch):
        """⛔ `-z` 只解掉 C-quoting，不解掉「位元組不是 UTF-8」。

        盲審實測：Big5 位元組在預設 reader 下被讀成 `??`，於是又落到一個不存在
        的目錄 ⇒ da-guard exit 2 ⇒ 紅燈怪罪 da-guard——正是 `-z` 宣稱已消除的那
        個結果。stdin 讀 bytes + surrogateescape、輸出同樣編回去，才是真的解掉。
        """
        raw = b"conf.d/\xb4\xfa-prod/_defaults.yaml\0"
        stdin = _Stdin("")
        stdin.buffer = _StdinBuffer(raw)
        monkeypatch.setattr("sys.stdin", stdin)
        rc = mod.main(["--null", "-"])
        out = capsysbinary.readouterr().out
        assert rc == 0, out
        # 逐位元組原封不動——不是 `??`，也不是任何 replacement 字元。
        assert out == b"target\tconf.d\tconf.d/\xb4\xfa-prod\n", out
        assert b"?" not in out


class _Stdin:
    """⛔ Exposes `.buffer`, because the script reads BYTES.

    A text-only stub would have made the surrogateescape path — added so that
    non-UTF-8 path bytes survive instead of decoding to `??` and pointing at a
    directory that does not exist — untestable, and the stub raising
    AttributeError is the only reason that was noticed.
    """

    def __init__(self, text: str) -> None:
        self._text = text
        self.buffer = _StdinBuffer(text.encode("utf-8", errors="surrogateescape"))

    def read(self) -> str:
        return self._text


class _StdinBuffer:
    def __init__(self, data: bytes) -> None:
        self._data = data

    def read(self) -> bytes:
        return self._data
