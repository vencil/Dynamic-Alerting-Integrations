"""Tests for scripts/tools/lint/check_doc_datools_cmds.py.

Pins the L4 doc-staleness defense added after #141 Track A / F3: the try-local
README showed `da-tools ... guard /conf.d`, but the shipped CLI takes
`guard defaults-impact`. Scoped to the binary-wrapper subcommands (guard /
parser / batch-pr) — a broad command-tree check was rejected as too FP-heavy
(scenario docs use illustrative pseudo-commands even in code blocks).
"""
from __future__ import annotations

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

    def test_subcommand_map_matches_dispatchers(self):
        files = {"guard": "guard_dispatch.py", "parser": "parser_dispatch.py",
                 "batch-pr": "batchpr_dispatch.py"}
        for wrapper, subs in mod.WRAPPER_SUBCOMMANDS.items():
            text = (mod.OPS_DIR / files[wrapper]).read_text(encoding="utf-8")
            for sub in subs:
                assert sub in text, (
                    f"{sub} not found in {files[wrapper]} — WRAPPER_SUBCOMMANDS "
                    f"drifted from the dispatcher SOT")


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
