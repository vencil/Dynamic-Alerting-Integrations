"""Tests for scripts/tools/lint/check_doc_datools_cmds.py.

Pins the L4 doc-staleness defense added after #141 Track A / F3: the try-local
README showed `da-tools ... guard /conf.d`, but the shipped CLI takes
`guard defaults-impact`. Scoped to the binary-wrapper subcommands (guard /
parser / batch-pr) — a broad command-tree check was rejected as too FP-heavy
(scenario docs use illustrative pseudo-commands even in code blocks).
"""
from __future__ import annotations

import ast
import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = REPO_ROOT / "scripts" / "tools" / "lint" / "check_doc_datools_cmds.py"
_spec = importlib.util.spec_from_file_location("check_doc_datools_cmds", _SCRIPT)
mod = importlib.util.module_from_spec(_spec)
sys.modules["check_doc_datools_cmds"] = mod
_spec.loader.exec_module(mod)


def _doc(tmp_path: Path, body: str) -> Path:
    d = tmp_path / "docs"
    d.mkdir(parents=True, exist_ok=True)
    f = d / "g.md"
    f.write_text(body, encoding="utf-8")
    return f


def _scan(tmp_path, body):
    return mod.check_datools_subcommands(
        [_doc(tmp_path, body)], mod.WRAPPER_SUBCOMMANDS, tmp_path)


_FENCE = "```bash\n{}\n```\n"


class TestDatoolsSubcommands:
    def test_flags_f3_guard_conf_d(self, tmp_path):
        issues = _scan(tmp_path, _FENCE.format(
            "docker run ghcr.io/vencil/da-tools:v2.8.0 guard /conf.d"))
        assert len(issues) == 1
        assert issues[0].check == "datools-bad-subcommand"
        assert "defaults-impact" in issues[0].message

    def test_passes_valid_subcommand(self, tmp_path):
        assert _scan(tmp_path, _FENCE.format(
            "da-tools guard defaults-impact --config-dir /conf.d")) == []

    def test_passes_help_flag(self, tmp_path):
        assert _scan(tmp_path, _FENCE.format("da-tools guard --help")) == []

    def test_flags_bogus_parser_subcommand(self, tmp_path):
        assert len(_scan(tmp_path, _FENCE.format("da-tools parser frobnicate"))) == 1

    def test_passes_batchpr_refresh_source(self, tmp_path):
        assert _scan(tmp_path, _FENCE.format("da-tools batch-pr refresh-source")) == []

    def test_ignores_prose_outside_code_block(self, tmp_path):
        # bare prose mention (no fence) must not be scanned
        assert _scan(tmp_path, "Use `da-tools guard /conf.d` in your pipeline.\n") == []

    def test_skips_placeholder_line(self, tmp_path):
        assert _scan(tmp_path, _FENCE.format("da-tools guard <subcommand>")) == []

    def test_respects_inline_ignore(self, tmp_path):
        assert _scan(tmp_path, _FENCE.format(
            "da-tools guard legacy  # datools-cmd-ignore: old example")) == []


class TestSubcommandMapDrift:
    """WRAPPER_SUBCOMMANDS must stay in sync with the dispatchers (the SOT)."""

    _FILES = {"guard": "guard_dispatch.py", "parser": "parser_dispatch.py",
              "batch-pr": "batchpr_dispatch.py"}

    @staticmethod
    def _declared_subcommands(path: Path) -> set:
        """Read the dispatcher's `subcommands={...}` literal via AST.

        ⛔ Not `sub in text`. That was the previous predicate and it is very
        nearly a tautology: measured on these three files, the tokens `sys`,
        `os`, `import`, `json`, `path`, `help`, `install` and `validate` all
        satisfy it for every wrapper, because they occur in import statements,
        flag help strings and prose. Worse, one entry of the real map — the
        `parser` wrapper's `import` — was passing purely on the 14 Python
        `import` statements in parser_dispatch.py, so the dispatcher could
        have dropped that subcommand outright and this "drift guard" would
        have stayed green.
        """
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        found = []
        for node in ast.walk(tree):
            if isinstance(node, ast.keyword) and node.arg == "subcommands":
                found.append({elt.value for elt in node.value.elts})
        assert len(found) == 1, f"{path.name}: expected one subcommands= literal"
        return found[0]

    def test_subcommand_map_matches_dispatchers(self):
        """Set EQUALITY, both directions.

        The old check only asserted map ⊆ dispatcher. A dispatcher that GAINS
        a subcommand while the map lags is the other failure — and it is the
        user-visible one, because a legal invocation in the docs then gets
        reported as a bad subcommand with no legal way to go green.
        """
        for wrapper, subs in mod.WRAPPER_SUBCOMMANDS.items():
            declared = self._declared_subcommands(mod.OPS_DIR / self._FILES[wrapper])
            assert set(subs) == declared, (
                f"{wrapper}: WRAPPER_SUBCOMMANDS {sorted(subs)} != dispatcher "
                f"{sorted(declared)} ({self._FILES[wrapper]})")

    def test_the_extractor_rejects_a_token_that_merely_appears_in_the_file(self):
        """器材自證：證明新的擷取法真的不是子字串比對。"""
        declared = self._declared_subcommands(mod.OPS_DIR / self._FILES["guard"])
        text = (mod.OPS_DIR / self._FILES["guard"]).read_text(encoding="utf-8")
        assert "sys" in text and "sys" not in declared


class TestWritableMountNeedsUser:
    """#1495 — 可寫掛載必須帶 `--user`，而且要帶在 image 之前。

    ⛔ 這一組存在的理由是**上一版沒有**：規則加進來時零單元測試、也沒有
    repo-level 斷言，於是「它在 CI 上到底有沒有跑」無法回答（實測：沒有）。
    """

    # ⛔ Built with chr(92), never a "\\" literal. The first version of these
    # fixtures wrote the continuation marker as an escape sequence and every
    # one of them came out as the two characters backslash+n INSIDE a single
    # line — so all 12 tests exercised one-line commands, the continuation
    # buffer was never entered, and reverting the continuation handling left
    # the suite green. The bug was invisible because the fixtures still
    # "looked" multi-line in the source.
    _BS = chr(92)

    @classmethod
    def _fenced(cls, *arg_lines, quote=False, fence="```"):
        """A fenced block whose command really does span physical lines."""
        cont = " " + cls._BS
        body = [fence + "bash"]
        body += [ln + cont for ln in arg_lines[:-1]]
        body.append(arg_lines[-1])
        body.append(fence)
        if quote:
            body = ["> " + ln for ln in body]
        return "\n".join(body) + "\n"

    @staticmethod
    def _doc(tmp_path, body):
        p = tmp_path / "docs" / "x.md"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body, encoding="utf-8")
        return p

    def test_the_fixture_helper_really_produces_continuation_lines(self):
        """⛔ 先證明器材本身是對的，否則下面每一格都在測單行指令。"""
        out = self._fenced("docker run --rm", "  -v a:/b", "  img cmd")
        lines = out.splitlines()
        assert len(lines) == 5, lines
        assert lines[1].endswith(" " + self._BS), repr(lines[1])
        assert lines[3] == "  img cmd", repr(lines[3])
        assert (self._BS + "n") not in out, "又寫成字面反斜線 n 了"

    def test_writable_bind_mount_without_user_is_flagged(self, tmp_path):
        p = self._doc(tmp_path, self._fenced(
            "docker run --rm",
            "  -v $(pwd)/out:/data/output",
            "  ghcr.io/vencil/da-tools:v2.9.0",
            "  generate-routes -o /data/output/x.yaml"))
        issues = mod.check_writable_mount_has_user([p], tmp_path)
        assert len(issues) == 1
        assert issues[0].check == "datools-writable-mount-without-user"

    def test_read_only_mount_is_not_flagged(self, tmp_path):
        p = self._doc(tmp_path, self._fenced(
            "docker run --rm",
            "  -v $(pwd)/conf.d:/etc/config:ro",
            "  ghcr.io/vencil/da-tools:v2.9.0 validate-config"))
        assert mod.check_writable_mount_has_user([p], tmp_path) == []

    @pytest.mark.parametrize("flag", [
        "--user $(id -u):$(id -g)",
        "--user=$(id -u):$(id -g)",
        "-u $(id -u):$(id -g)",          # 官方短式；子字串比對認不得
    ])
    def test_every_spelling_of_the_flag_satisfies_the_rule(self, tmp_path, flag):
        p = self._doc(tmp_path, self._fenced(
            f"docker run --rm {flag}",
            "  -v $(pwd)/out:/data/output",
            "  ghcr.io/vencil/da-tools:v2.9.0 init"))
        assert mod.check_writable_mount_has_user([p], tmp_path) == []

    def test_userns_is_not_mistaken_for_user(self, tmp_path):
        """`--userns=keep-id` 設不了 uid —— 子字串比對會誤放。"""
        p = self._doc(tmp_path, self._fenced(
            "docker run --rm --userns=keep-id",
            "  -v $(pwd)/out:/data/output",
            "  ghcr.io/vencil/da-tools:v2.9.0 init"))
        assert len(mod.check_writable_mount_has_user([p], tmp_path)) == 1

    def test_user_after_the_image_is_its_own_finding(self, tmp_path):
        """docker 只套用 image 之前的旗標；之後的會變成容器的參數。"""
        p = self._doc(tmp_path, self._fenced(
            "docker run --rm -v $(pwd)/o:/data/output"
            " ghcr.io/vencil/da-tools:v2.9.0",
            "  --user $(id -u):$(id -g)",
            "  init"))
        issues = mod.check_writable_mount_has_user([p], tmp_path)
        assert len(issues) == 1
        assert issues[0].check == "datools-user-flag-after-image"

    def test_a_mount_path_containing_da_tools_is_not_mistaken_for_the_image(
        self, tmp_path
    ):
        """⛔ image token 不能用「第一個含 da-tools 的 token」找。

        掛載路徑叫 `da-tools-out` 時，那個 token 會被當成 image，於是一個
        **排在 image 之前的正確 `--user`** 被判成排在之後——而訊息叫人
        「移到 image 之前」，它已經在前面，沒有任何合法改寫能轉綠。
        """
        p = self._doc(tmp_path, self._fenced(
            "docker run --rm",
            "  -v $(pwd)/da-tools-out:/data/output",
            "  --user $(id -u):$(id -g)",
            "  ghcr.io/vencil/da-tools:v2.9.0 init"))
        assert mod.check_writable_mount_has_user([p], tmp_path) == []

    @pytest.mark.parametrize("extra", [
        "--platform linux/amd64",
        "--group-add 999",
        "--userns host",          # ⚠️ 空白形式；`--userns=` 那格在別處
        "--security-opt label=disable",
    ])
    def test_an_unlisted_value_flag_does_not_donate_its_value_as_the_image(
        self, tmp_path, extra
    ):
        """⛔ `_VALUE_FLAGS` 是列舉，而 docker 的旗標集合永遠列不完。

        清單裡沒有的「吃下一個 token」旗標，會把**它的值**當成 image，於是
        一個正確排在 image 之前的 `--user` 被判成排在之後——而訊息叫人做
        它已經做了的事，沒有任何合法轉綠路徑。所以判定不能只靠列舉：
        候選 operand 還必須真的長得像 da-tools image。
        """
        p = self._doc(tmp_path, self._fenced(
            f"docker run --rm {extra}",
            "  --user $(id -u):$(id -g)",
            "  -v $(pwd)/out:/data/output",
            "  ghcr.io/vencil/da-tools:v2.9.0 init"))
        assert mod.check_writable_mount_has_user([p], tmp_path) == []

    def test_an_image_not_named_da_tools_still_anchors_the_position_check(
        self, tmp_path
    ):
        """⛔ 上一格的修法（operand 必須長得像 da-tools image）有反方向代價。

        它嚴格擴大了「找不到 image」的集合，而找不到時位置檢查是 fail-OPEN
        （呼叫端寫的是 `img_i is not None and ...`）⇒ 私有鏡像、改名的 build
        裡一個真的排錯位置的 `--user` 會**靜默通過**。所以第二層要退回「第一個
        裸 operand」。

        這一格與上一格互為對照：兩者都不能單獨拿掉。
        """
        p = self._doc(tmp_path, self._fenced(
            "docker run --rm",
            "  -v $(pwd)/da-tools-out:/data/output",
            "  ghcr.io/acme/platform-cli:v1 --user 1000 init"))
        issues = mod.check_writable_mount_has_user([p], tmp_path)
        assert len(issues) == 1, issues
        assert issues[0].check == "datools-user-flag-after-image"

    def test_a_mount_wrapped_right_after_dash_v_is_still_seen(self, tmp_path):
        """⛔ `-v` 落在行尾時，續行的 `\\` 曾被當成它的值。

        `_mounts()` 自己再 split 一次，所以只在呼叫端丟掉 `\\` 不夠——那條
        路徑會得到空字串掛載、靜默判乾淨。
        """
        p = self._doc(tmp_path, self._fenced(
            "docker run --rm -v",
            "  $(pwd)/out:/data/output",
            "  ghcr.io/vencil/da-tools:v2.9.0 init"))
        assert len(mod.check_writable_mount_has_user([p], tmp_path)) == 1

    def test_the_equals_form_of_volume_is_recognised(self, tmp_path):
        p = self._doc(tmp_path, self._fenced(
            "docker run --rm",
            "  --volume=$(pwd)/out:/data/output",
            "  ghcr.io/vencil/da-tools:v2.9.0 init"))
        assert len(mod.check_writable_mount_has_user([p], tmp_path)) == 1

    @pytest.mark.parametrize("spec,expect_issue", [
        # 判得到的：`_normalise` 接得回去，或接不回去但殘骸仍以 `$` 開頭
        ("${{ github.workspace }}/out:/output", True),        # 支援的樣板
        ("${{ format('{0}/out', github.workspace) }}:/output", True),  # 巢狀括號
        ("${PWD}/out:/data/output", True),                    # 普通 shell 展開
        # ⛔ 判不到的：已在 check_writable_mount_has_user docstring §4 揭露
        ("{{ mkdocs_var }}/out:/output", False),   # 非 GitHub 樣板，接回去的頭是 `}}`
        ("${{ github.workspace /out:/output", False),  # 缺結尾 `}}`，`-v` 的值變成碎片
    ])
    def test_template_mounts_judged_or_disclosed_never_guessed(
        self, tmp_path, spec, expect_issue
    ):
        """⛔ 這一格的兩個方向都要釘，因為只釘一邊各燒過一次。

        **漏判方向**：靜默跳過 `${{ }}` 掛載，曾經蓋住 operator-gitops-deployment
        的一個真缺陷。**誤判方向**：接著補上的「有括號就說判不了」，反過來對
        上面三個 `True` 的正當寫法全部誤紅，還把那個真缺陷從「指名 + 正確處方」
        降級成「判不了」。

        所以 `expect_issue` 這一欄必須兩種值都有——它只有 True 的時候，斷言退化
        成 `assert issues`，第二個方向等於沒測。兩個 False 是**已揭露的盲區**
        （docstring §4），不是「還沒修的 bug」：把它們改成 True 之前，先讀那一節
        為什麼第三個「看外觀」的謂詞不該再寫一次。
        """
        p = self._doc(tmp_path, self._fenced(
            "docker run --rm",
            f"  -v {spec}",
            "  ghcr.io/vencil/da-tools:v2.9.0 operator-generate"))
        issues = mod.check_writable_mount_has_user([p], tmp_path)
        assert bool(issues) is expect_issue, issues
        if issues:
            # ⛔ 斷言 check 名，不只斷言「有一條」。少了這一行，任何新的
            # finding kind 都能滿足這一格，而守衛本體停用照樣綠。
            assert issues[0].check == "datools-writable-mount-without-user"

    @pytest.mark.parametrize("spec,is_bind", [
        ("C:" + chr(92) + "Users" + chr(92) + "me:/data/output", True),
        ("C:" + chr(92) + "cache", False),          # 磁碟機代號但無容器路徑
        (chr(92) * 2 + "srv" + chr(92) + "share:/data/output", True),  # UNC
        ("/cache", False),                            # 匿名 volume
        ("cache-vol:/data/output", False),            # 具名 volume
    ])
    def test_bind_mount_domain_boundaries(self, spec, is_bind):
        assert mod._is_bind_mount(spec) is is_bind, spec

    def test_a_flagged_position_is_found_by_token_not_by_substring(
        self, tmp_path
    ):
        """⛔ 位置要比 token 序，不能比字串偏移。

        `build-utils` 裡就有 `-u`，`norm.index("-u")` 會落在 image 之前，
        於是真正排錯位的短旗標整條消失。
        """
        p = self._doc(tmp_path, self._fenced(
            "docker run --rm -v $(pwd)/build-utils:/data/output"
            " ghcr.io/vencil/da-tools:v2.9.0",
            "  -u $(id -u):$(id -g)",
            "  init"))
        issues = mod.check_writable_mount_has_user([p], tmp_path)
        assert len(issues) == 1
        assert issues[0].check == "datools-user-flag-after-image"

    @pytest.mark.parametrize("spec", [
        "da-tools-cache:/home/nonroot/.cache",   # 具名 volume
        "/cache",                                 # 匿名 volume
    ])
    def test_volumes_are_out_of_domain(self, tmp_path, spec):
        """⛔ 具名／匿名 volume 沒有 host uid，加 `--user` 反而寫不進去。"""
        p = self._doc(tmp_path, self._fenced(
            "docker run --rm",
            f"  -v {spec}",
            "  ghcr.io/vencil/da-tools:v2.9.0 guard defaults-impact"))
        assert mod.check_writable_mount_has_user([p], tmp_path) == []

    def test_a_windows_host_path_is_a_bind_mount(self, tmp_path):
        """`C:\\Users\\me\\out:/data/output` 是 bind mount，不是具名 volume。"""
        p = self._doc(tmp_path, self._fenced(
            "docker run --rm",
            "  -v C:" + chr(92) + "Users" + chr(92) + "me:/data/output",
            "  ghcr.io/vencil/da-tools:v2.9.0 init"))
        assert len(mod.check_writable_mount_has_user([p], tmp_path)) == 1

    def test_a_readonly_alias_mount_is_not_flagged(self, tmp_path):
        p = self._doc(tmp_path, self._fenced(
            "docker run --rm",
            "  -v $(pwd)/conf.d:/etc/config:readonly",
            "  ghcr.io/vencil/da-tools:v2.9.0 validate-config"))
        assert mod.check_writable_mount_has_user([p], tmp_path) == []

    def test_a_tilde_fence_is_scanned_too(self, tmp_path):
        p = self._doc(tmp_path, self._fenced(
            "docker run --rm",
            "  -v $(pwd)/out:/data/output",
            "  ghcr.io/vencil/da-tools:v2.9.0 init", fence="~~~"))
        assert len(mod.check_writable_mount_has_user([p], tmp_path)) == 1

    def test_a_fence_inside_a_blockquote_is_still_scanned(self, tmp_path):
        """共用範本 `docs/includes/docker-usage-pattern.md` 就是這個形狀
        ——**三行 blockquote 續行**，所以續行的去引號也被這一格走到。"""
        p = self._doc(tmp_path, self._fenced(
            "docker run --rm",
            "  -v $(pwd)/out:/data/output",
            "  ghcr.io/vencil/da-tools:v2.9.0 init", quote=True))
        assert len(mod.check_writable_mount_has_user([p], tmp_path)) == 1

    def test_a_ci_template_mount_is_judged_not_skipped(self, tmp_path):
        """`${{ github.workspace }}` 內含空白；跳過它曾蓋住一個真缺陷。"""
        p = self._doc(tmp_path, self._fenced(
            "docker run --rm",
            "  -v ${{ github.workspace }}/out:/output",
            "  ghcr.io/vencil/da-tools:latest operator-generate"))
        issues = mod.check_writable_mount_has_user([p], tmp_path)
        assert len(issues) == 1, issues
        # ⛔ 這一行不是裝飾。原本只斷言 `len(issues) == 1` 時，一個新增的
        # finding kind 把這格從「指名可寫掛載缺 --user」換成「我判不了」，
        # 斷言照樣成立、測試照樣綠——整條 writable 規則停用也綠。守衛的名字
        # 就是它的斷言，不驗名字等於只驗「有東西被回報」。
        assert issues[0].check == "datools-writable-mount-without-user"

    def test_actual_repo_is_clean(self):
        """⛔ repo-level 迴歸：沒有這一格，上面全部只證明函式能動，
        不證明**這棵樹**符合不變式——而 CI 跑的正是這棵樹。

        ⚠️ 斷言 `run()` 完全為空，不是「除了 bad-subcommand 以外為空」。
        先前的寫法對姊妹規則恆真——樹上任何 `datools-bad-subcommand` 都會被
        靜默放過，而那條規則在 pytest 側沒有別的 repo-level 斷言。
        """
        issues = mod.run()          # 呼叫一次；它掃 258 個檔（實測，非估計）
        assert issues == [], [f"{i.file}:{i.line} {i.check}" for i in issues]

    def test_a_blockquoted_fence_is_scanned_by_the_subcommand_rule_too(
        self, tmp_path
    ):
        """⛔ blockquote 這個類別要對**兩條**規則都成立。

        只修 mount 那一支的話，共用範本對子命令規則仍然隱形——而那份檔案
        正是這個修法的起因。
        """
        p = self._doc(tmp_path, self._fenced(
            "docker run --rm ghcr.io/vencil/da-tools:v2.9.0"
            " guard totally-bogus-subcommand", quote=True))
        issues = mod.check_datools_subcommands(
            [p], mod.WRAPPER_SUBCOMMANDS, tmp_path)
        assert len(issues) == 1, issues
        assert issues[0].check == "datools-bad-subcommand"
class TestPlaceholderIsFilteredPerSpec:
    """一個不可判的掛載，不可以讓同一個範例裡可判的鄰居也免受檢查。"""

    _BS = TestWritableMountNeedsUser._BS
    _fenced = TestWritableMountNeedsUser._fenced
    # ⚠️ staticmethod() is required: reading it off the other class yields the
    # plain function, which this class body would turn back into an instance
    # method and hand `self` as the first argument.
    _doc = staticmethod(TestWritableMountNeedsUser._doc)

    def test_placeholder_inside_a_concrete_path_no_longer_silences_its_sibling(
        self, tmp_path
    ):
        """⛔ counterfactual：這一格在修改前回報 0 個 issue。

        `-v /srv/<tenant>/conf.d:/data/conf.d` 通得過 `_is_bind_mount`（以 `/`
        開頭），所以它**進得了** `specs`；舊版看到 specs 裡任何一個帶 `<`
        就 `continue`，整塊跳過——連旁邊那個完全具體、確實可寫、確實缺
        `--user` 的 `$(pwd)/out` 都不判。下一格就是同一個掛載單獨出現時的
        對照組，證明差別只來自那個佔位符鄰居。
        """
        p = self._doc(tmp_path, self._fenced(
            "docker run --rm",
            "  -v /srv/<tenant>/conf.d:/data/conf.d",
            "  -v $(pwd)/out:/data/output",
            "  ghcr.io/vencil/da-tools:v2.9.0 init"))
        issues = mod.check_writable_mount_has_user([p], tmp_path)
        assert len(issues) == 1, issues
        assert issues[0].check == "datools-writable-mount-without-user"
        # 只點名判得動的那一個，不把佔位符寫進訊息
        assert "$(pwd)/out:/data/output" in issues[0].message
        assert "<tenant>" not in issues[0].message

    def test_control_the_same_concrete_mount_alone_is_flagged(self, tmp_path):
        p = self._doc(tmp_path, self._fenced(
            "docker run --rm",
            "  -v $(pwd)/out:/data/output",
            "  ghcr.io/vencil/da-tools:v2.9.0 init"))
        assert len(mod.check_writable_mount_has_user([p], tmp_path)) == 1

    def test_a_placeholder_only_command_stays_unjudged(self, tmp_path):
        """誤紅方向的對照：全部不可判時仍然不可以報。"""
        p = self._doc(tmp_path, self._fenced(
            "docker run --rm",
            "  -v /srv/<tenant>/out:/data/output",
            "  ghcr.io/vencil/da-tools:v2.9.0 init"))
        assert mod.check_writable_mount_has_user([p], tmp_path) == []

    def test_a_placeholder_neighbour_does_not_manufacture_a_finding(
        self, tmp_path
    ):
        """誤紅方向：帶了 `--user` 就不該因為多一個佔位符而轉紅。"""
        p = self._doc(tmp_path, self._fenced(
            "docker run --rm",
            "  --user $(id -u):$(id -g)",
            "  -v /srv/<tenant>/conf.d:/data/conf.d",
            "  -v $(pwd)/out:/data/output",
            "  ghcr.io/vencil/da-tools:v2.9.0 init"))
        assert mod.check_writable_mount_has_user([p], tmp_path) == []


class TestExtraDocListIsNotSilentlyShrunk:
    """`_EXTRA_DOC_FILES` 少一個檔，必須是訊號，不是靜靜少掃一個檔。"""

    def test_a_missing_entry_reports_instead_of_being_dropped(
        self, tmp_path, monkeypatch
    ):
        """⛔ 舊版是 `[f for f in extra if f.is_file()]`。

        把其中一個 landing page 改名或搬走，掃描面就少一個檔而**輸出完全
        不變**——閘門看起來仍然全綠，實際上已經不再看守那一頁。這正是這
        支 checker 存在要擋的形狀，所以它處理自己的清單時不能是這個形狀。
        """
        (tmp_path / "docs").mkdir()
        monkeypatch.setattr(mod, "_EXTRA_DOC_FILES", ("no/such/README.md",))
        issues = mod.run(tmp_path)
        missing = [i for i in issues if i.check == "datools-doc-file-missing"]
        assert len(missing) == 1, issues
        assert missing[0].file == "no/such/README.md"

    def test_a_present_entry_produces_no_such_issue(self, tmp_path, monkeypatch):
        (tmp_path / "docs").mkdir()
        real = tmp_path / "READY.md"
        real.write_text("no docker commands here\n", encoding="utf-8")
        monkeypatch.setattr(mod, "_EXTRA_DOC_FILES", ("READY.md",))
        assert [i for i in mod.run(tmp_path)
                if i.check == "datools-doc-file-missing"] == []
class TestOnlyHelpMayOmitTheSubcommand:
    """`da-tools <wrapper> --anything` 曾經一律放行，而註解只承諾 `--help`。"""

    @staticmethod
    def _scan(tmp_path, line):
        p = tmp_path / "docs" / "x.md"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("```bash\n" + line + "\n```\n", encoding="utf-8")
        return mod.check_datools_subcommands(
            [p], mod.WRAPPER_SUBCOMMANDS, tmp_path)

    @pytest.mark.parametrize("line", [
        "da-tools guard --conf-d conf.d/ --report",
        "da-tools guard --conf-d conf.d/ --layer schema --verbose",
    ])
    def test_a_stale_flag_in_the_subcommand_slot_is_reported(self, tmp_path, line):
        """⛔ counterfactual：這兩行在收窄前回報 0。

        它們是這棵樹上**真的出貨過**的寫法（troubleshooting-checklist 兩份
        語系共 12 行）。真的 dispatcher 對它們的回答是
        `Error: unknown guard subcommand '--conf-d'.`，也就是這支 checker
        存在的唯一理由——而舊的 `nxt.startswith("-")` 把它們全部放行。
        """
        issues = self._scan(tmp_path, line)
        assert len(issues) == 1, issues
        assert issues[0].check == "datools-bad-subcommand"

    @pytest.mark.parametrize("line", [
        "da-tools guard --help",
        "da-tools guard -h",
        "da-tools parser --help",
    ])
    def test_help_still_needs_no_subcommand(self, tmp_path, line):
        """誤紅方向：收窄不可以波及註解真正承諾放行的那兩個。"""
        assert self._scan(tmp_path, line) == []


class TestImageReferenceFormsAreAllRecognised:
    """tag 樣式曾經只吃 `:vN.N`，於是 `:latest` 那批完全在判定面之外。"""

    _scan = staticmethod(TestOnlyHelpMayOmitTheSubcommand._scan)

    @pytest.mark.parametrize("image", [
        "ghcr.io/vencil/da-tools",
        "ghcr.io/vencil/da-tools:v2.9.0",
        "ghcr.io/vencil/da-tools:latest",
        "ghcr.io/vencil/da-tools:2.9.0",
        "ghcr.io/vencil/da-tools:v2.9.0-rc1",
        "ghcr.io/vencil/da-tools@sha256:abc123",
    ])
    def test_every_reference_form_reaches_the_judgement(self, tmp_path, image):
        """⛔ `:latest` 不是假想形式：掃描面內有 36 處，其中兩處帶著上面那個
        stale invocation ⇒ 同一個缺陷有兩條互相獨立的漏法。"""
        issues = self._scan(tmp_path, f"{image} guard --conf-d conf.d/")
        assert len(issues) == 1, (image, issues)

    @pytest.mark.parametrize("image", [
        "ghcr.io/vencil/da-tools:latest",
        "ghcr.io/vencil/da-tools@sha256:abc123",
    ])
    def test_a_valid_subcommand_stays_clean_under_those_forms(self, tmp_path, image):
        assert self._scan(
            tmp_path, f"{image} guard defaults-impact --config-dir conf.d/") == []


class TestFailureMessagesDoNotPrescribeTheCheaperWorseFix:
    """守衛的失敗訊息會被照做，所以它不能指名一個會打開洞的修法。"""

    def test_the_mount_message_does_not_offer_ro(self):
        """⛔ `:ro` 是七個字元 vs 一個判斷，對只想轉綠的人永遠更便宜。

        實測：對一個確實會寫（`--output /data/output/...`）的範例，刪掉
        `--user` 回報 1，補上 `:ro` 回報 0——而客戶的失敗只是從
        PermissionError 換成 Read-only file system，該區塊還就此永久離開
        判定面（`writable` 變空也一併停用位置檢查）。
        """
        src = mod.__file__ and open(mod.__file__, encoding="utf-8").read()
        msg_region = src[src.index("datools-writable-mount-without-user\""):]
        msg = msg_region[:msg_region.index("))")]
        assert "`:ro`" not in msg, msg
        assert "--user" in msg

    def test_the_missing_file_message_does_not_offer_dropping_the_entry(self):
        """同一族，而且這一條是本輪自己寫進去的：訊息原本把
        「從 tuple 拿掉」列為兩個選項之一，實測照做之後整支 lint 回報 0、
        該頁的真缺陷原樣出貨。"""
        src = open(mod.__file__, encoding="utf-8").read()
        region = src[src.index("\"datools-doc-file-missing\""):]
        msg = region[:region.index("for rel in missing")]
        assert "drop it from the tuple" not in msg, msg
        assert "current path" in msg
class TestRunActuallyScansWhatItClaims:
    """⛔ `assert run() == []` 是被任何「少掃一點」的變異自動滿足的。

    這一組是它缺的 **positive control**：先種下已知缺陷，再要求 `run()`
    找出來。實測（對本檔當時的 66 格）三個變異全部存活、66 綠：

      * `run()` 回傳裡把 `check_datools_subcommands(...)` 換成 `[]`
      * `_doc_files` 的 `rglob("*.md")` 改成 `glob("*.md")`（掃描面 256 → 45 檔）
      * `_EXTRA_DOC_FILES` 清空

    三者的後果都是「守衛還在、但看的東西變少了」，而唯一的 repo 層斷言
    問的是「有沒有 finding」——沒有 finding 正是它們製造出來的畫面。
    """

    _BS = chr(92)

    @classmethod
    def _seed(cls, root, rel):
        """寫一個同時帶兩種缺陷的文件。"""
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(
            "```bash\n"
            "da-tools guard totally-bogus-subcommand\n"
            "```\n\n"
            "```bash\n"
            "docker run --rm " + cls._BS + "\n"
            "  -v $(pwd)/out:/data/output " + cls._BS + "\n"
            "  ghcr.io/vencil/da-tools:v2.9.0 init\n"
            "```\n", encoding="utf-8")
        return p

    def test_run_reports_both_rules_from_a_nested_doc(self, tmp_path):
        """⛔ 刻意放在 `docs/a/b/` 底下：非遞迴的掃描會漏掉它。"""
        self._seed(tmp_path, Path("docs/a/b/deep.md"))
        checks = {i.check for i in mod.run(tmp_path)}
        assert "datools-bad-subcommand" in checks, checks
        assert "datools-writable-mount-without-user" in checks, checks

    def test_run_reaches_the_extra_landing_pages_too(self, tmp_path, monkeypatch):
        """`_EXTRA_DOC_FILES` 被清空時，這一格必須紅。"""
        (tmp_path / "docs").mkdir()
        self._seed(tmp_path, Path("try-local/README.md"))
        monkeypatch.setattr(mod, "_EXTRA_DOC_FILES", ("try-local/README.md",))
        files = {i.file for i in mod.run(tmp_path)}
        assert "try-local/README.md" in files, files

    def test_the_extra_list_still_names_the_three_landing_pages(self):
        """⚠️ 這是對**真實 repo** 的斷言，不是對 tmp fixture。

        清單縮短不需要動檔案系統，所以 `datools-doc-file-missing` 那一組
        （檔案不存在時要報）攔不到它——後果卻一模一樣。
        """
        assert set(mod._EXTRA_DOC_FILES) == {
            "components/da-tools/README.md",
            "components/da-tools/app/QUICKSTART.md",
            "try-local/README.md",
        }, mod._EXTRA_DOC_FILES
        for rel in mod._EXTRA_DOC_FILES:
            assert (REPO_ROOT / rel).is_file(), rel

    def test_the_reported_line_number_points_at_the_offending_block(self, tmp_path):
        """行號沒有任何斷言時，回報可以指到別的地方去而測試全綠。"""
        p = self._doc_with_leading_prose(tmp_path)
        issues = [i for i in mod.check_writable_mount_has_user([p], tmp_path)]
        assert len(issues) == 1, issues
        text = p.read_text(encoding="utf-8").splitlines()
        assert text[issues[0].line - 1].startswith("docker run"), (
            issues[0].line, text[issues[0].line - 1])

    @classmethod
    def _doc_with_leading_prose(cls, tmp_path):
        p = tmp_path / "docs" / "prose.md"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(
            "# Title\n\nsome prose\n\nmore prose\n\n"
            "```bash\n"
            "docker run --rm " + cls._BS + "\n"
            "  -v $(pwd)/out:/data/output " + cls._BS + "\n"
            "  ghcr.io/vencil/da-tools:v2.9.0 init\n"
            "```\n", encoding="utf-8")
        return p


class TestBindMountHeadsAreAllCovered:
    """`_is_bind_mount` 的 head 判準是 fail-open 方向，且原本只有 `$` 有覆蓋。"""

    _BS = chr(92)

    @staticmethod
    def _scan_one(tmp_path, mount):
        p = tmp_path / "docs" / "m.md"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(
            "```bash\n"
            f"docker run --rm -v {mount} ghcr.io/vencil/da-tools:v2.9.0 init\n"
            "```\n", encoding="utf-8")
        return mod.check_writable_mount_has_user([p], tmp_path)

    @pytest.mark.parametrize("mount", [
        "/srv/acme/out:/data/output",     # 絕對路徑：拿掉 "/" head 就整類靜默
        "./out:/data/output",             # 相對路徑
        "~/out:/data/output",             # home 展開
        "$(pwd)/out:/data/output",        # 原本唯一有覆蓋的形式
    ])
    def test_each_host_path_shape_is_judged(self, tmp_path, mount):
        issues = self._scan_one(tmp_path, mount)
        assert len(issues) == 1, (mount, issues)
        assert issues[0].check == "datools-writable-mount-without-user"

    def test_a_named_volume_is_still_not_a_bind_mount(self, tmp_path):
        """誤紅方向的對照：放寬 head 不可以把名稱卷拉進來。"""
        assert self._scan_one(tmp_path, "da-tools-cache:/cache") == []
class TestScopingInvariantsOfTheMountRule:
    """模組 docstring 宣稱的兩個範圍前提，mount 規則這一側原本零斷言。

    子命令規則兩條都有測（`test_ignores_prose_outside_code_block`、
    `test_respects_inline_ignore`），mount 規則兩條都沒有——實測兩個變異
    都存活。兩者都是**誤紅方向**：放寬之後受害的是本來就正確的文件。
    """

    _BS = chr(92)
    _fenced = TestWritableMountNeedsUser._fenced
    _doc = staticmethod(TestWritableMountNeedsUser._doc)

    def test_prose_outside_a_fence_is_not_judged(self, tmp_path):
        """散文裡的 inline-code `docker run` 不是可執行範例。"""
        p = self._doc(tmp_path, (
            "Mount your checkout with "
            "`docker run --rm -v $(pwd)/out:/data/output "
            "ghcr.io/vencil/da-tools:v2.9.0 init` and you are done.\n"))
        assert mod.check_writable_mount_has_user([p], tmp_path) == []

    def test_an_inline_ignore_exempts_the_whole_block(self, tmp_path):
        """逃生門必須真的開著——否則標了記號的歷史範例會轉紅。"""
        p = self._doc(tmp_path, self._fenced(
            "docker run --rm  # datools-cmd-ignore: historical example",
            "  -v $(pwd)/out:/data/output",
            "  ghcr.io/vencil/da-tools:v2.9.0 init"))
        assert mod.check_writable_mount_has_user([p], tmp_path) == []

    def test_a_continuation_does_not_swallow_the_closing_fence(self, tmp_path):
        r"""收尾 fence 前一行以反斜線結尾時，緩衝區不可以吞掉 fence 之後的散文。

        ⛔ 素材刻意設計成**變異前後不同**：fence 內的指令不完整（沒有
        image），所以現況判不動、回 `[]`；而 fence 之後的散文帶著 image
        卻沒有 `--user`。緩衝區若越過 fence，兩段會被併成一條「有可寫掛載、
        有 image、沒有 --user」的假指令——對一段散文誤紅。
        （第一版素材在變異前後都是 `[]`，等於沒有對照組。）
        """
        body = (
            "```bash" + "\n"
            "docker run --rm -v $(pwd)/out:/data/output " + self._BS + "\n"
            "```" + "\n"
            "\n"
            "接著改用 ghcr.io/vencil/da-tools:v2.9.0 init 重跑一次。\n")
        p = self._doc(tmp_path, body)
        assert mod.check_writable_mount_has_user([p], tmp_path) == []

    def test_archived_notes_are_out_of_scope(self, tmp_path, monkeypatch):
        """`docs/internal/archive/` 的例子是刻意過期的歷史紀錄。"""
        p = tmp_path / "docs" / "internal" / "archive" / "old.md"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(self._fenced(
            "docker run --rm",
            "  -v $(pwd)/out:/data/output",
            "  ghcr.io/vencil/da-tools:v2.9.0 init"), encoding="utf-8")
        # tmp 樹沒有那三個 landing page，否則 run() 會回 datools-doc-file-missing
        monkeypatch.setattr(mod, "_EXTRA_DOC_FILES", ())
        assert mod.run(tmp_path) == []


class TestPositionCheckDoesNotFireOutsideItsDomain:
    """`datools-user-flag-after-image` 的兩個前置條件都是誤紅方向。

    ⛔ 這條 issue 的訊息是「Move it ahead of the image」。當它誤紅時，
    使用者**已經做了訊息要求的事**——沒有任何合法的轉綠路徑，只剩下
    `:ro` 或 `datools-cmd-ignore` 這類會順帶關掉真檢查的手段。
    """

    _BS = chr(92)
    _fenced = TestWritableMountNeedsUser._fenced
    _doc = staticmethod(TestWritableMountNeedsUser._doc)

    def test_a_read_only_example_is_out_of_domain_even_with_a_late_user(
        self, tmp_path
    ):
        """實測：拿掉 `writable and` 前置後，這一格從 `[]` 變成一條誤紅。"""
        p = self._doc(tmp_path, self._fenced(
            "docker run --rm -v $(pwd)/conf.d:/etc/config:ro"
            " ghcr.io/vencil/da-tools:v2.9.0",
            "  --user $(id -u):$(id -g)",
            "  validate-config"))
        assert mod.check_writable_mount_has_user([p], tmp_path) == []

    def test_continuation_backslashes_do_not_become_the_image(self, tmp_path):
        r"""⛔ 多行指令的每個 `\` 都是獨立 token。

        不濾掉它們時，第一個 `\` 會被當成 image，於是每一個**正確**排在
        image 之前的 `--user` 都被判成排在之後。實測：這一格在該過濾器被
        拿掉後從 `[]` 變成 `datools-user-flag-after-image`。
        """
        p = self._doc(tmp_path, self._fenced(
            "docker run --rm",
            "  --user $(id -u):$(id -g)",
            "  -v $(pwd)/da-tools-out:/data/output",
            "  ghcr.io/acme/platform-cli:v1 init"))
        assert mod.check_writable_mount_has_user([p], tmp_path) == []

    def test_an_env_value_is_not_mistaken_for_the_image(self, tmp_path):
        """`-e` 吃下一個 token；它的值不可以變成 image 錨點。"""
        p = self._doc(tmp_path, self._fenced(
            "docker run --rm -e DA_TOOLS_MODE=strict",
            "  --user $(id -u):$(id -g)",
            "  -v $(pwd)/out:/data/output",
            "  ghcr.io/vencil/da-tools:v2.9.0 init"))
        assert mod.check_writable_mount_has_user([p], tmp_path) == []

    def test_a_quoted_writable_spec_is_still_judged(self, tmp_path):
        """⛔ 這是去引號真正守著的方向，而且是**假陰性**方向。

        不剝掉前引號時，host 端變成 `"$(pwd)/out`，開頭是引號而不是
        `/ . ~ $` 之一 ⇒ `_is_bind_mount` 判否 ⇒ 整個掛載從判定面消失、
        回報乾淨。⚠️ 前一版只測唯讀方向，分不出差別：`rstrip` 仍會去掉
        尾引號，所以 option 欄位照樣讀成 `ro`，變異前後都是 `[]`。
        而引號正是本 repo 主推的寫法（共用範本與 CI/CD 精靈都產出引號版）。
        """
        p = self._doc(tmp_path, self._fenced(
            "docker run --rm",
            '  -v "$(pwd)/out:/data/output"',
            "  ghcr.io/vencil/da-tools:v2.9.0 init"))
        issues = mod.check_writable_mount_has_user([p], tmp_path)
        assert len(issues) == 1, issues
        assert issues[0].check == "datools-writable-mount-without-user"

    def test_a_quoted_read_only_spec_is_not_read_as_writable(self, tmp_path):
        """引號是本 repo 主推的寫法（共用範本與精靈都產出引號版）。

        不先剝引號就讀 option 欄位時，`"…:/conf.d:ro"` 的收尾引號會落進
        option group，於是唯讀掛載被讀成可寫——對一個已經正確的範例誤紅。
        """
        p = self._doc(tmp_path, self._fenced(
            "docker run --rm",
            '  -v "$(pwd)/conf.d:/etc/config:ro"',
            "  ghcr.io/vencil/da-tools:v2.9.0 validate-config"))
        assert mod.check_writable_mount_has_user([p], tmp_path) == []
