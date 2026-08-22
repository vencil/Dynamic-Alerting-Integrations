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

    @staticmethod
    def _doc(tmp_path, body):
        p = tmp_path / "docs" / "x.md"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body, encoding="utf-8")
        return p

    def test_writable_bind_mount_without_user_is_flagged(self, tmp_path):
        p = self._doc(tmp_path, "```bash\ndocker run --rm \\n"
                                "  -v $(pwd)/out:/data/output \\n"
                                "  ghcr.io/vencil/da-tools:v2.9.0 \\n"
                                "  generate-routes -o /data/output/x.yaml\n```\n")
        issues = mod.check_writable_mount_has_user([p], tmp_path)
        assert len(issues) == 1
        assert issues[0].check == "datools-writable-mount-without-user"

    def test_read_only_mount_is_not_flagged(self, tmp_path):
        p = self._doc(tmp_path, "```bash\ndocker run --rm \\n"
                                "  -v $(pwd)/conf.d:/etc/config:ro \\n"
                                "  ghcr.io/vencil/da-tools:v2.9.0 validate-config\n```\n")
        assert mod.check_writable_mount_has_user([p], tmp_path) == []

    @pytest.mark.parametrize("flag", [
        "--user $(id -u):$(id -g)",
        "--user=$(id -u):$(id -g)",
        "-u $(id -u):$(id -g)",          # 官方短式；子字串比對認不得
    ])
    def test_every_spelling_of_the_flag_satisfies_the_rule(self, tmp_path, flag):
        p = self._doc(tmp_path, f"```bash\ndocker run --rm {flag} \\n"
                                "  -v $(pwd)/out:/data/output \\n"
                                "  ghcr.io/vencil/da-tools:v2.9.0 init\n```\n")
        assert mod.check_writable_mount_has_user([p], tmp_path) == []

    def test_userns_is_not_mistaken_for_user(self, tmp_path):
        """`--userns=keep-id` 設不了 uid —— 子字串比對會誤放。"""
        p = self._doc(tmp_path, "```bash\ndocker run --rm --userns=keep-id \\n"
                                "  -v $(pwd)/out:/data/output \\n"
                                "  ghcr.io/vencil/da-tools:v2.9.0 init\n```\n")
        assert len(mod.check_writable_mount_has_user([p], tmp_path)) == 1

    def test_user_after_the_image_is_its_own_finding(self, tmp_path):
        """docker 只套用 image 之前的旗標；之後的會變成容器的參數。"""
        p = self._doc(tmp_path, "```bash\ndocker run --rm -v $(pwd)/o:/data/output"
                                " ghcr.io/vencil/da-tools:v2.9.0 \\n"
                                "  --user $(id -u):$(id -g) \\n  init\n```\n")
        issues = mod.check_writable_mount_has_user([p], tmp_path)
        assert len(issues) == 1
        assert issues[0].check == "datools-user-flag-after-image"

    @pytest.mark.parametrize("spec", [
        "da-tools-cache:/home/nonroot/.cache",   # 具名 volume
        "/cache",                                 # 匿名 volume
    ])
    def test_volumes_are_out_of_domain(self, tmp_path, spec):
        """⛔ 具名／匿名 volume 沒有 host uid，加 `--user` 反而寫不進去。"""
        p = self._doc(tmp_path, f"```bash\ndocker run --rm \\n  -v {spec} \\n"
                                "  ghcr.io/vencil/da-tools:v2.9.0 guard defaults-impact\n```\n")
        assert mod.check_writable_mount_has_user([p], tmp_path) == []

    def test_a_fence_inside_a_blockquote_is_still_scanned(self, tmp_path):
        """共用範本 `docs/includes/docker-usage-pattern.md` 就是這個形狀。"""
        p = self._doc(tmp_path, "> ```bash\n> docker run --rm \\n"
                                ">   -v $(pwd)/out:/data/output \\n"
                                ">   ghcr.io/vencil/da-tools:v2.9.0 init\n> ```\n")
        assert len(mod.check_writable_mount_has_user([p], tmp_path)) == 1

    def test_a_ci_template_mount_is_judged_not_skipped(self, tmp_path):
        """`${{ github.workspace }}` 內含空白；跳過它曾蓋住一個真缺陷。"""
        p = self._doc(tmp_path, "```bash\ndocker run --rm \\n"
                                "  -v ${{ github.workspace }}/out:/output \\n"
                                "  ghcr.io/vencil/da-tools:latest operator-generate\n```\n")
        assert len(mod.check_writable_mount_has_user([p], tmp_path)) == 1

    def test_actual_repo_is_clean(self):
        """⛔ repo-level 迴歸：沒有這一格，上面全部只證明函式能動，
        不證明**這棵樹**符合不變式——而 CI 跑的正是這棵樹。"""
        assert mod.run() == [] or all(
            i.check == "datools-bad-subcommand" for i in mod.run()), [
                i.message for i in mod.run()]
