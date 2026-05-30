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
