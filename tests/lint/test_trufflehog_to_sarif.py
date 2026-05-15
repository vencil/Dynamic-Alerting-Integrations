"""Tests for trufflehog_to_sarif.py — NDJSON→SARIF converter + verified-finding
merge-block policy (#445 AC ii, L2 secret-scan).

The converter owns two responsibilities, both tested here:
  1. Faithful SARIF 2.1.0 emission from trufflehog --json (NDJSON) input.
  2. The policy exit code — exit 1 iff ≥1 VERIFIED finding (blocks the PR),
     exit 0 otherwise (unverified findings are warnings, non-blocking).
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

_TOOLS_DIR = os.path.join(os.path.dirname(__file__), '..', '..', 'scripts', 'tools', 'lint')
sys.path.insert(0, _TOOLS_DIR)

import trufflehog_to_sarif as tts  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures" / "trufflehog"


# ---------------------------------------------------------------------------
# parse_ndjson — line-delimited JSON, defensive skipping
# ---------------------------------------------------------------------------
class TestParseNdjson:
    def test_parses_one_object_per_line(self):
        raw = '{"DetectorName":"AWS"}\n{"DetectorName":"GitHubToken"}\n'
        findings = tts.parse_ndjson(raw)
        assert len(findings) == 2
        assert findings[0]["DetectorName"] == "AWS"

    def test_skips_blank_lines(self):
        raw = '{"DetectorName":"AWS"}\n\n   \n{"DetectorName":"X"}\n'
        assert len(tts.parse_ndjson(raw)) == 2

    def test_skips_non_json_lines(self, capsys):
        # trufflehog --json shouldn't emit log lines, but be defensive:
        # a stray non-JSON line must be skipped, not crash the run.
        raw = '{"DetectorName":"AWS"}\n2026/05/15 INFO: scanning...\n{"DetectorName":"X"}\n'
        findings = tts.parse_ndjson(raw)
        assert len(findings) == 2
        assert "not JSON" in capsys.readouterr().err

    def test_skips_json_non_objects(self):
        # A bare JSON array / string on a line is valid JSON but not a
        # finding object — drop it.
        raw = '{"DetectorName":"AWS"}\n["not", "a", "finding"]\n"bare string"\n'
        assert len(tts.parse_ndjson(raw)) == 1

    def test_empty_input_yields_empty_list(self):
        assert tts.parse_ndjson("") == []


# ---------------------------------------------------------------------------
# _extract_location — probe Git / Filesystem source metadata shapes
# ---------------------------------------------------------------------------
class TestExtractLocation:
    def test_git_source(self):
        finding = {"SourceMetadata": {"Data": {"Git": {"file": "a/b.sh", "line": 42}}}}
        assert tts._extract_location(finding) == ("a/b.sh", 42)

    def test_filesystem_source(self):
        finding = {"SourceMetadata": {"Data": {"Filesystem": {"file": "x.py", "line": 7}}}}
        assert tts._extract_location(finding) == ("x.py", 7)

    def test_missing_metadata_falls_back(self):
        assert tts._extract_location({}) == ("<unknown>", 1)

    def test_line_zero_clamped_to_one(self):
        # SARIF region.startLine must be >= 1.
        finding = {"SourceMetadata": {"Data": {"Git": {"file": "a.sh", "line": 0}}}}
        assert tts._extract_location(finding) == ("a.sh", 1)

    def test_non_integer_line_degrades_to_one(self):
        finding = {"SourceMetadata": {"Data": {"Git": {"file": "a.sh", "line": "??"}}}}
        assert tts._extract_location(finding) == ("a.sh", 1)


# ---------------------------------------------------------------------------
# _classify — verified vs unverified
# ---------------------------------------------------------------------------
class TestClassify:
    def test_verified_true(self):
        assert tts._classify({"Verified": True}) == "verified"

    def test_verified_false(self):
        assert tts._classify({"Verified": False}) == "unverified"

    def test_verified_missing_is_unverified(self):
        # A finding with no Verified key must NOT be treated as verified
        # (fail-safe: only an explicit True blocks the PR).
        assert tts._classify({}) == "unverified"

    def test_verified_truthy_non_bool_is_unverified(self):
        # Defensive: only the literal bool True counts. A string "true"
        # or 1 must not escalate to a blocking finding.
        assert tts._classify({"Verified": "true"}) == "unverified"
        assert tts._classify({"Verified": 1}) == "unverified"


# ---------------------------------------------------------------------------
# convert — SARIF document shape
# ---------------------------------------------------------------------------
class TestConvert:
    def test_verified_finding_is_error_level(self):
        findings = [{"DetectorName": "AWS", "Verified": True,
                     "SourceMetadata": {"Data": {"Git": {"file": "a.sh", "line": 1}}}}]
        sarif, verified = tts.convert(findings, "3.95.3")
        assert verified == 1
        assert sarif["runs"][0]["results"][0]["level"] == "error"

    def test_unverified_finding_is_warning_level(self):
        findings = [{"DetectorName": "AWS", "Verified": False,
                     "SourceMetadata": {"Data": {"Git": {"file": "a.sh", "line": 1}}}}]
        sarif, verified = tts.convert(findings, "3.95.3")
        assert verified == 0
        assert sarif["runs"][0]["results"][0]["level"] == "warning"

    def test_sarif_top_level_shape(self):
        sarif, _ = tts.convert([], "3.95.3")
        assert sarif["version"] == "2.1.0"
        assert sarif["runs"][0]["tool"]["driver"]["name"] == "TruffleHog"
        assert sarif["runs"][0]["tool"]["driver"]["version"] == "3.95.3"
        assert sarif["runs"][0]["results"] == []

    def test_rules_deduplicated(self):
        # Two findings, same detector → one rule entry.
        findings = [
            {"DetectorName": "AWS", "Verified": False, "SourceMetadata": {}},
            {"DetectorName": "AWS", "Verified": True, "SourceMetadata": {}},
        ]
        sarif, _ = tts.convert(findings, "x")
        rules = sarif["runs"][0]["tool"]["driver"]["rules"]
        assert len(rules) == 1
        assert rules[0]["id"] == "AWS"

    def test_result_location_carries_file_and_line(self):
        findings = [{"DetectorName": "X", "Verified": False,
                     "SourceMetadata": {"Data": {"Git": {"file": "deploy/x.sh", "line": 99}}}}]
        sarif, _ = tts.convert(findings, "x")
        loc = sarif["runs"][0]["results"][0]["locations"][0]["physicalLocation"]
        assert loc["artifactLocation"]["uri"] == "deploy/x.sh"
        assert loc["region"]["startLine"] == 99

    def test_output_is_json_serializable(self):
        sarif, _ = tts.convert(
            [{"DetectorName": "AWS", "Verified": True, "SourceMetadata": {}}], "x")
        # Must not raise.
        json.dumps(sarif)


# ---------------------------------------------------------------------------
# main — end-to-end via fixtures, exit-code policy
# ---------------------------------------------------------------------------
class TestMainExitPolicy:
    def _run(self, cli_argv, monkeypatch, *args):
        cli_argv("trufflehog_to_sarif.py", *args)
        return tts.main()

    def test_mixed_findings_block_on_verified(self, monkeypatch, tmp_path, capsys, cli_argv):
        """findings_mixed.json has 1 verified + 2 unverified → exit 1."""
        out = tmp_path / "r.sarif"
        rc = self._run(cli_argv, monkeypatch,
                       "--input", str(FIXTURES / "findings_mixed.json"),
                       "--output", str(out))
        assert rc == tts.EXIT_VERIFIED_FINDING
        err = capsys.readouterr().err
        assert "1 VERIFIED" in err
        assert "ROTATE FIRST" in err
        # SARIF still written even on the blocking path.
        sarif = json.loads(out.read_text(encoding="utf-8"))
        assert len(sarif["runs"][0]["results"]) == 3

    def test_unverified_only_does_not_block(self, monkeypatch, tmp_path, capsys, cli_argv):
        """findings_unverified_only.json has 0 verified → exit 0 (warnings)."""
        out = tmp_path / "r.sarif"
        rc = self._run(cli_argv, monkeypatch,
                       "--input", str(FIXTURES / "findings_unverified_only.json"),
                       "--output", str(out))
        assert rc == tts.EXIT_OK
        out_text = capsys.readouterr().out
        assert "2 unverified finding(s)" in out_text
        sarif = json.loads(out.read_text(encoding="utf-8"))
        assert all(r["level"] == "warning" for r in sarif["runs"][0]["results"])

    def test_empty_input_clean_exit(self, monkeypatch, tmp_path, capsys, cli_argv):
        empty = tmp_path / "empty.json"
        empty.write_text("", encoding="utf-8")
        out = tmp_path / "r.sarif"
        rc = self._run(cli_argv, monkeypatch,
                       "--input", str(empty), "--output", str(out))
        assert rc == tts.EXIT_OK
        assert "no secrets detected" in capsys.readouterr().out

    def test_missing_input_returns_usage_error(self, monkeypatch, tmp_path, capsys, cli_argv):
        rc = self._run(cli_argv, monkeypatch,
                       "--input", str(tmp_path / "nope.json"),
                       "--output", str(tmp_path / "r.sarif"))
        assert rc == tts.EXIT_USAGE
        assert "not found" in capsys.readouterr().err
