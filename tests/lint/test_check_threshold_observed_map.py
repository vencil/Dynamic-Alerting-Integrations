"""check_threshold_observed_map — 缺 map 與 coverage gap 不得靜默通過（#3017）。

先前 `load_observed_map()` 缺檔回 `{}`，drift-guard 在 map 被刪掉時印
`[OK] ... 64 gap` 並 rc 0；coverage gap 只是 WARN。
"""
from __future__ import annotations

import os
import sys

_TOOLS_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "scripts", "tools", "lint")
sys.path.insert(0, _TOOLS_DIR)

import check_threshold_observed_map as guard  # noqa: E402
from _lib_exitcodes import EXIT_CALLER_ERROR, EXIT_OK, EXIT_VIOLATION  # noqa: E402


def test_missing_map_exits_2_naming_the_file(tmp_path, monkeypatch, capsys):
    missing = tmp_path / "metric_observed_map.yaml"
    monkeypatch.setattr(guard.observed_map_lib, "DEFAULT_MAP_PATH", str(missing))
    for argv in (["--ci"], []):
        assert guard.main(argv) == EXIT_CALLER_ERROR
        err = capsys.readouterr().err
        assert "ERROR:" in err and str(missing) in err


def _with_gaps(monkeypatch, gaps):
    monkeypatch.setattr(guard, "run_check", lambda: {
        "errors": [], "infos": [], "orphan_thresholds": [], "coverage_gaps": gaps})


def test_coverage_gap_fails_ci(monkeypatch, capsys):
    _with_gaps(monkeypatch, ["new_key"])
    assert guard.main(["--ci"]) == EXIT_VIOLATION
    assert "coverage gap" in capsys.readouterr().err


def test_coverage_gap_without_ci_is_reported_not_failed(monkeypatch, capsys):
    _with_gaps(monkeypatch, ["new_key"])
    assert guard.main([]) == EXIT_OK
    assert "coverage gap 'new_key'" in capsys.readouterr().out


def test_no_gap_passes_ci(monkeypatch):
    _with_gaps(monkeypatch, [])
    assert guard.main(["--ci"]) == EXIT_OK


def test_committed_map_passes_ci():
    """真的 map＋真的 rule pack：CI 現況必須綠（0 gap）。"""
    assert guard.main(["--ci"]) == EXIT_OK
