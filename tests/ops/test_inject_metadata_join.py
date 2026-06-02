"""Tests for inject_metadata_join.py — Rule Pack metadata injector.

Audit flagged this as a 0% covered MUTATING tool (modifies Rule Pack
YAML in-place). Tests cover:
  - collect_alert_block: pure block-extraction logic
  - inject_metadata: pure block-transformation
  - process_file: end-to-end on tmp YAML files (idempotency, no-op)
  - main: directory walk + filtering of operational rule pack
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

_TOOLS_DIR = os.path.join(os.path.dirname(__file__), '..', '..', 'scripts', 'tools', 'ops')
sys.path.insert(0, _TOOLS_DIR)

import inject_metadata_join as imj  # noqa: E402
from _lib_exitcodes import EXIT_CALLER_ERROR  # noqa: E402


# ---------------------------------------------------------------------------
# collect_alert_block — pure block extraction
# ---------------------------------------------------------------------------
class TestCollectAlertBlock:
    def test_single_alert_to_eof(self):
        lines = [
            "  - alert: HighCPU",
            "    expr: cpu > 0.9",
            "    for: 5m",
        ]
        block, end = imj.collect_alert_block(lines, 0)
        assert end == 3
        assert block == lines

    def test_stops_at_next_alert(self):
        lines = [
            "  - alert: First",
            "    expr: foo",
            "  - alert: Second",  # boundary
            "    expr: bar",
        ]
        block, end = imj.collect_alert_block(lines, 0)
        assert end == 2
        assert block == lines[0:2]

    def test_stops_at_record_at_same_indent(self):
        lines = [
            "  - alert: A",
            "    expr: foo",
            "  - record: r",  # boundary
        ]
        block, end = imj.collect_alert_block(lines, 0)
        assert end == 2
        assert block == lines[0:2]

    def test_continues_through_blank_lines(self):
        # Blank lines inside the block are preserved, not treated as boundary.
        lines = [
            "  - alert: A",
            "    expr: foo",
            "",
            "    for: 5m",
        ]
        block, end = imj.collect_alert_block(lines, 0)
        assert end == 4
        assert "" in block

    def test_deeper_indent_continues(self):
        # Lines indented deeper than the alert marker stay in the block.
        lines = [
            "  - alert: A",
            "    annotations:",
            "      summary: 'high'",
            "  - alert: B",
        ]
        block, end = imj.collect_alert_block(lines, 0)
        assert end == 3
        assert "      summary: 'high'" in block


# ---------------------------------------------------------------------------
# process_file — end-to-end YAML mutation
# ---------------------------------------------------------------------------
class TestProcessFile:
    def test_idempotent_skip_when_already_has_metadata_info(self, tmp_path, capsys):
        f = tmp_path / "pack.yaml"
        f.write_text("groups:\n  - rules:\n      - alert: A\n        expr: foo on(tenant) tenant_metadata_info\n", encoding="utf-8")
        assert imj.process_file(str(f)) is False
        out = capsys.readouterr().out
        assert "SKIP" in out
        assert "already has tenant_metadata_info" in out

    def test_skip_when_no_alert_threshold_pattern(self, tmp_path, capsys):
        # Alert exists but doesn't match the on(tenant)+alert_threshold pattern.
        f = tmp_path / "pack.yaml"
        f.write_text(
            "groups:\n"
            "  - rules:\n"
            "      - alert: A\n"
            "        expr: cpu_usage > 0.9\n",
            encoding="utf-8",
        )
        assert imj.process_file(str(f)) is False
        out = capsys.readouterr().out
        assert "SKIP" in out
        assert "no alerts need metadata join" in out

    def test_modifies_when_alert_threshold_pattern_present(self, tmp_path, capsys):
        # Matches the on(tenant) + alert_threshold pattern → injects metadata.
        f = tmp_path / "pack.yaml"
        original = (
            "groups:\n"
            "  - rules:\n"
            "      - alert: ConnectionsHigh\n"
            "        expr: |\n"
            "          mysql_connections * on(tenant) alert_threshold{key=\"connections\"}\n"
            "        for: 5m\n"
            "        annotations:\n"
            "          summary: \"too many connections\"\n"
        )
        f.write_text(original, encoding="utf-8")
        assert imj.process_file(str(f)) is True
        out = capsys.readouterr().out
        assert "MODIFIED" in out
        # Side effect: file now contains the metadata join.
        new_content = f.read_text(encoding="utf-8")
        assert "tenant_metadata_info" in new_content
        assert "group_left(runbook_url, owner, tier)" in new_content


# ---------------------------------------------------------------------------
# main — directory walk + filtering
# ---------------------------------------------------------------------------
class TestMain:
    def test_missing_rule_packs_dir_exits_caller_error(self, tmp_path, monkeypatch, capsys, cli_argv):
        ghost = tmp_path / "no-such-dir"
        monkeypatch.setattr(imj, "RULE_PACKS_DIR", str(ghost))
        cli_argv("inject_metadata_join.py")
        with pytest.raises(SystemExit) as exc:
            imj.main()
        assert exc.value.code == EXIT_CALLER_ERROR
        err = capsys.readouterr().err
        assert "Rule packs directory not found" in err

    def test_skips_operational_pack_and_non_yaml(self, tmp_path, monkeypatch, capsys, cli_argv):
        # 3 files: operational (skipped), README.md (not yaml), real pack.
        # Real pack has no alerts needing inject → process_file returns False
        # → count stays 0 in the summary.
        rp_dir = tmp_path / "rule-packs"
        rp_dir.mkdir()
        (rp_dir / "rule-pack-operational.yaml").write_text(
            "groups: []\n", encoding="utf-8"
        )
        (rp_dir / "README.md").write_text("# docs", encoding="utf-8")
        (rp_dir / "rule-pack-database.yaml").write_text(
            "groups:\n  - rules:\n      - alert: A\n        expr: cpu > 0.9\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(imj, "RULE_PACKS_DIR", str(rp_dir))
        cli_argv("inject_metadata_join.py")
        imj.main()
        out = capsys.readouterr().out
        assert "Modified 0 Rule Pack files" in out
        # operational was skipped (its name should not appear in any
        # process_file output line)
        assert "rule-pack-operational" not in out
