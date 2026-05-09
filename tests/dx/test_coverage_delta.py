"""Tests for scripts/tools/dx/coverage_delta.py.

Gap 5 (testing-quality memory roadmap) — first PR. The delta script
is the foundation for both per-PR delta gating and weekly trend
aggregation. Both consume its structured output, so a regression
here cascades into both planned features. Pin the contract before
the consumers exist.

Coverage:
  - parse_cobertura: missing file / malformed XML / bad root /
    pytest-cov shape (lines synthesized) / dedup-by-filename
  - compute_delta: clean (no changes), improved, regressed, added,
    removed, unchanged_count, total computation
  - format_text_report: total line, section headers, top-N truncation,
    sign formatting
  - evaluate_thresholds: no gates / total-only gate / file-only gate /
    both gates / no violation under threshold
  - main CLI: missing input → exit 2, malformed → exit 2, clean delta
    → exit 0, threshold violation → exit 1, --json shape
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(
    os.path.dirname(__file__), "..", "..", "scripts", "tools", "dx",
))
import coverage_delta as cd  # noqa: E402


# ============================================================
# XML fixture helpers
# ============================================================


def _make_cobertura(
    total_rate: float,
    total_covered: int,
    total_valid: int,
    files: dict,  # filename → (line_rate, lines_covered, lines_valid)
) -> str:
    """Build a minimal Cobertura XML report from per-file numbers."""
    classes = []
    for fn, (rate, cov, valid) in files.items():
        classes.append(
            f'      <class name="{fn}" filename="{fn}" '
            f'line-rate="{rate}" lines-covered="{cov}" '
            f'lines-valid="{valid}">\n'
            f'        <lines/>\n'
            f'      </class>'
        )
    body = "\n".join(classes)
    return (
        f'<?xml version="1.0" ?>\n'
        f'<coverage line-rate="{total_rate}" '
        f'lines-covered="{total_covered}" lines-valid="{total_valid}">\n'
        f'  <packages>\n'
        f'    <package name="default" line-rate="{total_rate}">\n'
        f'      <classes>\n{body}\n      </classes>\n'
        f'    </package>\n'
        f'  </packages>\n'
        f'</coverage>\n'
    )


def _write_xml(path: Path, content: str) -> Path:
    path.write_text(content, encoding="utf-8")
    return path


# ============================================================
# parse_cobertura
# ============================================================


class TestParseCobertura:

    def test_missing_file_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            cd.parse_cobertura(tmp_path / "nope.xml")

    def test_malformed_xml_raises_value_error(self, tmp_path):
        f = _write_xml(tmp_path / "bad.xml", "not even <xml>")
        with pytest.raises(ValueError, match="malformed"):
            cd.parse_cobertura(f)

    def test_wrong_root_raises_value_error(self, tmp_path):
        f = _write_xml(tmp_path / "bad.xml", "<wrong-root/>")
        with pytest.raises(ValueError, match="unexpected root"):
            cd.parse_cobertura(f)

    def test_basic_shape(self, tmp_path):
        f = _write_xml(
            tmp_path / "cov.xml",
            _make_cobertura(0.85, 850, 1000, {
                "a.py": (0.9, 90, 100),
                "b.py": (0.8, 80, 100),
            }),
        )
        report = cd.parse_cobertura(f)
        assert report.total_line_rate == pytest.approx(0.85)
        assert report.total_percent == pytest.approx(85.0)
        assert report.total_lines_covered == 850
        assert report.total_lines_valid == 1000
        assert set(report.files.keys()) == {"a.py", "b.py"}
        assert report.files["a.py"].line_rate == pytest.approx(0.9)
        assert report.files["a.py"].percent == pytest.approx(90.0)

    def test_synthesizes_lines_covered_when_missing(self, tmp_path):
        # Property: when pytest-cov omits `lines-covered`/`lines-valid`
        # on <class>, we recompute from the <line hits="..."> children.
        xml_body = (
            '<?xml version="1.0" ?>\n'
            '<coverage line-rate="0.5" lines-covered="2" lines-valid="4">\n'
            '  <packages>\n'
            '    <package name="x">\n'
            '      <classes>\n'
            '        <class name="x.py" filename="x.py" line-rate="0.5">\n'
            '          <lines>\n'
            '            <line number="1" hits="1"/>\n'
            '            <line number="2" hits="0"/>\n'
            '            <line number="3" hits="3"/>\n'
            '            <line number="4" hits="0"/>\n'
            '          </lines>\n'
            '        </class>\n'
            '      </classes>\n'
            '    </package>\n'
            '  </packages>\n'
            '</coverage>\n'
        )
        f = _write_xml(tmp_path / "x.xml", xml_body)
        report = cd.parse_cobertura(f)
        x = report.files["x.py"]
        # 2 of 4 lines have hits > 0.
        assert x.lines_covered == 2
        assert x.lines_valid == 4

    def test_class_without_filename_skipped(self, tmp_path):
        # Property: a <class> tag missing `filename=` is not parseable
        # → silently skipped (defensive against odd Cobertura producers).
        xml_body = (
            '<?xml version="1.0" ?>\n'
            '<coverage line-rate="1.0" lines-covered="1" lines-valid="1">\n'
            '  <packages>\n'
            '    <package name="x">\n'
            '      <classes>\n'
            '        <class line-rate="0.5"/>\n'
            '        <class filename="real.py" line-rate="1.0" '
            '               lines-covered="1" lines-valid="1"/>\n'
            '      </classes>\n'
            '    </package>\n'
            '  </packages>\n'
            '</coverage>\n'
        )
        f = _write_xml(tmp_path / "x.xml", xml_body)
        report = cd.parse_cobertura(f)
        assert set(report.files.keys()) == {"real.py"}


# ============================================================
# compute_delta
# ============================================================


class TestComputeDelta:

    def test_no_changes_yields_empty_buckets(self, tmp_path):
        # Property: identical reports → all buckets empty,
        # unchanged_count = N, total_delta = 0.
        before = cd.parse_cobertura(_write_xml(
            tmp_path / "b.xml",
            _make_cobertura(0.85, 85, 100, {"a.py": (0.9, 90, 100),
                                              "b.py": (0.8, 80, 100)}),
        ))
        after = cd.parse_cobertura(_write_xml(
            tmp_path / "a.xml",
            _make_cobertura(0.85, 85, 100, {"a.py": (0.9, 90, 100),
                                              "b.py": (0.8, 80, 100)}),
        ))
        report = cd.compute_delta(before, after)
        assert report.improved == []
        assert report.regressed == []
        assert report.added == []
        assert report.removed == []
        assert report.unchanged_count == 2
        assert report.total_delta == 0

    def test_improved_bucket(self, tmp_path):
        # Property: a file whose coverage went up appears in `improved`.
        before = cd.parse_cobertura(_write_xml(
            tmp_path / "b.xml",
            _make_cobertura(0.7, 70, 100, {"a.py": (0.7, 70, 100)}),
        ))
        after = cd.parse_cobertura(_write_xml(
            tmp_path / "a.xml",
            _make_cobertura(0.9, 90, 100, {"a.py": (0.9, 90, 100)}),
        ))
        report = cd.compute_delta(before, after)
        assert len(report.improved) == 1
        assert report.improved[0].filename == "a.py"
        assert report.improved[0].before == pytest.approx(70.0)
        assert report.improved[0].after == pytest.approx(90.0)
        assert report.improved[0].delta == pytest.approx(20.0)
        assert report.regressed == []
        assert report.total_delta == pytest.approx(20.0)

    def test_regressed_bucket(self, tmp_path):
        before = cd.parse_cobertura(_write_xml(
            tmp_path / "b.xml",
            _make_cobertura(0.95, 95, 100, {"a.py": (0.95, 95, 100)}),
        ))
        after = cd.parse_cobertura(_write_xml(
            tmp_path / "a.xml",
            _make_cobertura(0.82, 82, 100, {"a.py": (0.82, 82, 100)}),
        ))
        report = cd.compute_delta(before, after)
        assert report.regressed
        assert report.regressed[0].delta < 0
        assert report.regressed[0].delta == pytest.approx(-13.0)

    def test_added_bucket(self, tmp_path):
        # Property: a file present only in `after` appears in `added`
        # (with before=0, delta=after).
        before = cd.parse_cobertura(_write_xml(
            tmp_path / "b.xml",
            _make_cobertura(1.0, 0, 0, {}),
        ))
        after = cd.parse_cobertura(_write_xml(
            tmp_path / "a.xml",
            _make_cobertura(1.0, 50, 50, {"new.py": (1.0, 50, 50)}),
        ))
        report = cd.compute_delta(before, after)
        assert len(report.added) == 1
        assert report.added[0].filename == "new.py"
        assert report.added[0].before == 0.0
        assert report.added[0].after == pytest.approx(100.0)
        assert report.improved == []  # not "improved" — first appearance

    def test_removed_bucket(self, tmp_path):
        before = cd.parse_cobertura(_write_xml(
            tmp_path / "b.xml",
            _make_cobertura(1.0, 50, 50, {"gone.py": (1.0, 50, 50)}),
        ))
        after = cd.parse_cobertura(_write_xml(
            tmp_path / "a.xml",
            _make_cobertura(1.0, 0, 0, {}),
        ))
        report = cd.compute_delta(before, after)
        assert len(report.removed) == 1
        assert report.removed[0].filename == "gone.py"
        assert report.removed[0].before == pytest.approx(100.0)
        assert report.removed[0].after == 0.0
        assert report.regressed == []  # removed isn't a "regression"

    def test_mixed_buckets(self, tmp_path):
        before = cd.parse_cobertura(_write_xml(
            tmp_path / "b.xml",
            _make_cobertura(0.8, 80, 100, {
                "improved.py":  (0.5, 50, 100),
                "regressed.py": (0.9, 90, 100),
                "stable.py":    (0.95, 95, 100),
                "gone.py":      (0.7, 70, 100),
            }),
        ))
        after = cd.parse_cobertura(_write_xml(
            tmp_path / "a.xml",
            _make_cobertura(0.85, 85, 100, {
                "improved.py":  (0.85, 85, 100),
                "regressed.py": (0.7, 70, 100),
                "stable.py":    (0.95, 95, 100),
                "new.py":       (1.0, 100, 100),
            }),
        ))
        report = cd.compute_delta(before, after)
        assert {d.filename for d in report.improved} == {"improved.py"}
        assert {d.filename for d in report.regressed} == {"regressed.py"}
        assert {d.filename for d in report.added} == {"new.py"}
        assert {d.filename for d in report.removed} == {"gone.py"}
        assert report.unchanged_count == 1  # stable.py


# ============================================================
# format_text_report
# ============================================================


class TestFormatTextReport:

    def _make(self, **kwargs):
        defaults = dict(
            total_before=80.0, total_after=85.0, total_delta=5.0,
            improved=[], regressed=[], added=[], removed=[],
            unchanged_count=0,
        )
        defaults.update(kwargs)
        return cd.DeltaReport(**defaults)

    def test_total_line_includes_arrow_and_sign(self):
        r = self._make()
        out = cd.format_text_report(r)
        assert "80.0%" in out
        assert "85.0%" in out
        assert "→" in out
        assert "+5.00%" in out

    def test_negative_total_no_plus_sign(self):
        r = self._make(total_before=85.0, total_after=80.0, total_delta=-5.0)
        out = cd.format_text_report(r)
        # Negative: no '+' prefix on the value.
        assert "-5.00%" in out
        assert "+5" not in out

    def test_section_headers_appear_only_when_non_empty(self):
        # Property: empty buckets don't print their section headers.
        r = self._make(improved=[], regressed=[], added=[], removed=[])
        out = cd.format_text_report(r)
        assert "Improved" not in out
        assert "Regressed" not in out
        assert "Newly tracked" not in out
        assert "Removed" not in out

    def test_top_n_truncation(self):
        # Property: when more than `top_n` items are in a bucket, the
        # output is limited and indicates the truncation.
        items = [
            cd.FileDelta(f"file{i}.py", 50.0, 50.0 + i, float(i))
            for i in range(1, 16)
        ]
        r = self._make(improved=items, total_delta=0.0)
        out = cd.format_text_report(r, top_n=5)
        # All 15 files exist but only top 5 are listed.
        assert "showing top 5 of 15" in out
        # file15.py (highest delta) is shown.
        assert "file15.py" in out
        # file1.py (smallest delta) is NOT shown.
        assert "file1.py" not in out

    def test_unchanged_count_line(self):
        r = self._make(unchanged_count=42)
        out = cd.format_text_report(r)
        assert "Unchanged: 42 files" in out


# ============================================================
# evaluate_thresholds
# ============================================================


class TestEvaluateThresholds:

    def test_no_gates_returns_empty(self):
        r = cd.DeltaReport(
            80.0, 75.0, -5.0, [], [], [], [], 0,
        )
        # No thresholds → no violations even though total dropped.
        assert cd.evaluate_thresholds(r) == []

    def test_total_regression_violation(self):
        r = cd.DeltaReport(
            80.0, 75.0, -5.0, [], [], [], [], 0,
        )
        v = cd.evaluate_thresholds(r, max_total_regression=2.0)
        assert len(v) == 1
        assert "total coverage dropped" in v[0]
        assert "5.00%" in v[0]

    def test_total_regression_within_threshold(self):
        r = cd.DeltaReport(
            80.0, 79.5, -0.5, [], [], [], [], 0,
        )
        # -0.5% drop with a 1.0% threshold → no violation.
        assert cd.evaluate_thresholds(r, max_total_regression=1.0) == []

    def test_file_regression_violation(self):
        r = cd.DeltaReport(
            80.0, 80.0, 0.0,
            [], [cd.FileDelta("x.py", 95.0, 80.0, -15.0)],
            [], [], 0,
        )
        v = cd.evaluate_thresholds(r, max_file_regression=10.0)
        assert len(v) == 1
        assert "x.py" in v[0]
        assert "15.0%" in v[0]

    def test_file_regression_within_threshold_no_violation(self):
        r = cd.DeltaReport(
            80.0, 80.0, 0.0,
            [], [cd.FileDelta("x.py", 95.0, 90.0, -5.0)],
            [], [], 0,
        )
        # -5% drop with a 10% threshold → no violation.
        assert cd.evaluate_thresholds(r, max_file_regression=10.0) == []

    def test_both_gates_independent(self):
        # Property: total threshold and file threshold are evaluated
        # independently — both can fire at once.
        r = cd.DeltaReport(
            80.0, 75.0, -5.0,
            [], [cd.FileDelta("x.py", 95.0, 80.0, -15.0)],
            [], [], 0,
        )
        v = cd.evaluate_thresholds(
            r, max_total_regression=1.0, max_file_regression=5.0,
        )
        assert len(v) == 2


# ============================================================
# main — CLI / exit codes
# ============================================================


class TestMainCLI:

    def test_missing_before_exits_two(self, tmp_path, capsys):
        rc = cd.main([str(tmp_path / "nope.xml"),
                       str(tmp_path / "also-nope.xml")])
        assert rc == 2
        assert "ERROR" in capsys.readouterr().err

    def test_malformed_xml_exits_two(self, tmp_path, capsys):
        bad = _write_xml(tmp_path / "bad.xml", "not xml")
        ok = _write_xml(
            tmp_path / "ok.xml",
            _make_cobertura(0.8, 80, 100, {"a.py": (0.8, 80, 100)}),
        )
        rc = cd.main([str(bad), str(ok)])
        assert rc == 2

    def test_clean_delta_exits_zero(self, tmp_path, capsys):
        b = _write_xml(
            tmp_path / "b.xml",
            _make_cobertura(0.85, 85, 100, {"a.py": (0.85, 85, 100)}),
        )
        a = _write_xml(
            tmp_path / "a.xml",
            _make_cobertura(0.85, 85, 100, {"a.py": (0.85, 85, 100)}),
        )
        rc = cd.main([str(b), str(a)])
        assert rc == 0
        assert "Coverage delta" in capsys.readouterr().out

    def test_threshold_violation_exits_one(self, tmp_path, capsys):
        b = _write_xml(
            tmp_path / "b.xml",
            _make_cobertura(0.90, 90, 100, {"a.py": (0.90, 90, 100)}),
        )
        a = _write_xml(
            tmp_path / "a.xml",
            _make_cobertura(0.80, 80, 100, {"a.py": (0.80, 80, 100)}),
        )
        rc = cd.main([
            str(b), str(a),
            "--max-total-regression", "1.0",
        ])
        assert rc == 1
        err = capsys.readouterr().err
        assert "Threshold violations" in err

    def test_json_output_shape(self, tmp_path, capsys):
        b = _write_xml(
            tmp_path / "b.xml",
            _make_cobertura(0.80, 80, 100, {"a.py": (0.80, 80, 100)}),
        )
        a = _write_xml(
            tmp_path / "a.xml",
            _make_cobertura(0.85, 85, 100, {"a.py": (0.85, 85, 100)}),
        )
        rc = cd.main([str(b), str(a), "--json"])
        assert rc == 0
        payload = json.loads(capsys.readouterr().out)
        assert "total" in payload
        assert {"before", "after", "delta"} <= set(payload["total"])
        for bucket in ("improved", "regressed", "added", "removed"):
            assert bucket in payload
            assert isinstance(payload[bucket], list)
        assert "unchanged_count" in payload
