"""Tests for check_lint_toolchain_fit.py — the anti-wheel-reinvention meta-lint.

Covers the detector's two failure-prevention duties:
  - POSITIVE: a NEW lint that globs *.jsx/*.css content and is not allowlisted
    is flagged (this is the whole point — stop reinventing ESLint/stylelint).
  - NEGATIVE (no false positives, the meta-lint's own trap): a python/md/yaml
    lint that merely *mentions* a filename, or globs *.md / *.py, is NOT flagged.
  - Allowlisted files pass; a stale allowlist entry (file removed) is reported.

Detector contract is verified against a synthetic tmp lint dir, not the live
tree (which TestLiveTreeClean pins separately).
"""
from __future__ import annotations

import sys
from pathlib import Path

_LINT_DIR = str(Path(__file__).resolve().parent.parent.parent / "scripts" / "tools" / "lint")
sys.path.insert(0, _LINT_DIR)

import check_lint_toolchain_fit as fit  # noqa: E402


def _write(d: Path, name: str, body: str) -> Path:
    p = d / name
    p.write_text(body, encoding="utf-8")
    return p


class TestDetector:
    def test_jsx_glob_is_detected(self, tmp_path):
        p = _write(tmp_path, "check_new_jsx.py",
                   "for f in root.rglob('**/*.jsx'):\n    scan(f)\n")
        assert fit.targets_js_toolchain(p) is True

    def test_css_glob_is_detected(self, tmp_path):
        p = _write(tmp_path, "check_new_css.py",
                   "list(d.glob('*.css'))\n")
        assert fit.targets_js_toolchain(p) is True

    def test_md_glob_not_detected(self, tmp_path):
        p = _write(tmp_path, "check_docs.py", "for f in d.glob('**/*.md'):\n    pass\n")
        assert fit.targets_js_toolchain(p) is False

    def test_python_glob_not_detected(self, tmp_path):
        p = _write(tmp_path, "check_py.py", "list(d.glob('*.py'))\n")
        assert fit.targets_js_toolchain(p) is False

    def test_bare_filename_mention_not_detected(self, tmp_path):
        # The classic false-positive trap: a tool that merely references a
        # .jsx path string (e.g. version bumper) must NOT be flagged.
        p = _write(tmp_path, "validate_versions.py",
                   "TARGET = 'docs/interactive/tools/cli-playground.jsx'\n"
                   "bump(TARGET)\n")
        assert fit.targets_js_toolchain(p) is False

    def test_dot_ts_not_detected(self, tmp_path):
        # .ts/.js are too ubiquitous → deliberately excluded from the signal.
        p = _write(tmp_path, "check_specs.py", "list(d.glob('*.spec.ts'))\n")
        assert fit.targets_js_toolchain(p) is False


class TestMainGate:
    def _patch(self, monkeypatch, tmp_path, allowlist):
        monkeypatch.setattr(fit, "LINT_DIR", tmp_path)
        monkeypatch.setattr(fit, "ALLOWLIST", allowlist)

    def test_new_unjustified_jsx_lint_fails(self, tmp_path, monkeypatch, capsys):
        _write(tmp_path, "check_reinvented.py", "root.rglob('*.jsx')\n")
        self._patch(monkeypatch, tmp_path, {})
        assert fit.main() == 1
        out = capsys.readouterr().out
        assert "check_reinvented.py" in out
        assert "lint-policy.md" in out

    def test_allowlisted_jsx_lint_passes(self, tmp_path, monkeypatch):
        _write(tmp_path, "check_reinvented.py", "root.rglob('*.jsx')\n")
        self._patch(monkeypatch, tmp_path,
                    {"check_reinvented.py": "justified: cross-file registry parity"})
        assert fit.main() == 0

    def test_md_lint_never_gates(self, tmp_path, monkeypatch):
        _write(tmp_path, "check_docs.py", "d.glob('*.md')\n")
        self._patch(monkeypatch, tmp_path, {})
        assert fit.main() == 0

    def test_stale_allowlist_entry_fails(self, tmp_path, monkeypatch):
        # Allowlist names a file that no longer exists → keep the list honest.
        self._patch(monkeypatch, tmp_path, {"check_gone.py": "removed long ago"})
        assert fit.main() == 1


class TestLiveTreeClean:
    """The real scripts/tools/lint/ tree must pass: every JS-globbing lint is
    allowlisted with a reason (grandfathered), no new un-justified ones."""

    def test_live_tree_passes(self):
        assert fit.main() == 0
