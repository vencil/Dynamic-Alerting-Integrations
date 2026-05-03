"""Unit tests for detect_sed_damage.py (PR-portal-6).

Covers two layers:
  1. Existing detection logic (NUL bytes appearing, >50% shrink)
  2. NEW exemption marker via .sed-damage-allowlist (PR-portal-6) —
     allows legitimate inline shrink refactors to land without
     disabling the guard repo-wide.

Implementation isolates check_file() from git: pass head_content via
monkeypatched get_head_content() so tests are hermetic.
"""
from __future__ import annotations

import os
import sys

import pytest

_TOOLS_DIR = os.path.join(
    os.path.dirname(__file__), "..", "..", "scripts", "tools", "lint"
)
sys.path.insert(0, _TOOLS_DIR)

import detect_sed_damage  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_head(monkeypatch):
    """Stub `get_head_content` to return a chosen byte string."""
    holder: dict[str, bytes | None] = {"value": b""}

    def _stub(_path):
        return holder["value"]

    monkeypatch.setattr(detect_sed_damage, "get_head_content", _stub)
    return holder


def _write(tmp_path, name, content_bytes):
    f = tmp_path / name
    f.write_bytes(content_bytes)
    return str(f)


# ---------------------------------------------------------------------------
# Existing behaviour — NUL bytes + truncation detection
# ---------------------------------------------------------------------------


class TestNulByteDetection:
    def test_new_nul_byte_flagged(self, tmp_path, fake_head):
        fake_head["value"] = b"clean content " * 20  # 280 bytes, no NULs
        path = _write(tmp_path, "f.txt", b"corrupted\x00content " * 20)
        issues = detect_sed_damage.check_file(path, allowlist=set())
        assert any("NUL bytes detected" in i for i in issues)

    def test_existing_nul_byte_not_flagged(self, tmp_path, fake_head):
        fake_head["value"] = b"binary\x00data " * 20
        path = _write(tmp_path, "f.bin", b"binary\x00more " * 20)
        issues = detect_sed_damage.check_file(path, allowlist=set())
        assert not any("NUL bytes detected" in i for i in issues)


class TestTruncationDetection:
    def test_50pct_shrink_flagged(self, tmp_path, fake_head):
        fake_head["value"] = b"a" * 1000
        path = _write(tmp_path, "f.txt", b"a" * 200)  # 20% of HEAD
        issues = detect_sed_damage.check_file(path, allowlist=set())
        assert any("truncated" in i for i in issues)

    def test_30pct_shrink_not_flagged(self, tmp_path, fake_head):
        fake_head["value"] = b"a" * 1000
        path = _write(tmp_path, "f.txt", b"a" * 700)  # 70% of HEAD
        issues = detect_sed_damage.check_file(path, allowlist=set())
        assert not any("truncated" in i for i in issues)

    def test_small_files_skipped(self, tmp_path, fake_head):
        """Files <100 bytes in HEAD don't trigger the shrink check."""
        fake_head["value"] = b"tiny"
        path = _write(tmp_path, "f.txt", b"x")  # 25% but <100B HEAD
        issues = detect_sed_damage.check_file(path, allowlist=set())
        assert not any("truncated" in i for i in issues)

    def test_new_file_no_head_no_check(self, tmp_path, fake_head):
        """File missing from HEAD → no truncation check (treated as new)."""
        fake_head["value"] = None
        path = _write(tmp_path, "f.txt", b"new content")
        issues = detect_sed_damage.check_file(path, allowlist=set())
        assert not any("truncated" in i for i in issues)


# ---------------------------------------------------------------------------
# NEW: allowlist-based exemption (PR-portal-6)
# ---------------------------------------------------------------------------


class TestAllowlistExemption:
    def test_allowlisted_path_skips_truncation_check(self, tmp_path, fake_head):
        """The whole point of the allowlist: legitimate large refactor
        shrinks a file >50% without firing the heuristic.
        """
        fake_head["value"] = b"a" * 1000
        path_str = _write(tmp_path, "shim.jsx", b"a" * 100)  # 10% of HEAD
        # Allowlist uses path-string match (the way pre-commit passes
        # paths to the script — relative to repo root). Use the literal
        # path the test passed in.
        normalized = path_str.replace("\\", "/")
        issues = detect_sed_damage.check_file(
            path_str, allowlist={normalized}
        )
        assert not any("truncated" in i for i in issues)

    def test_allowlist_does_not_suppress_nul_bytes(self, tmp_path, fake_head):
        """NUL bytes are ALWAYS damage — allowlist must not silence
        them even if path is exempted from the truncation check.
        """
        fake_head["value"] = b"clean content " * 20  # no NULs
        path_str = _write(tmp_path, "shim.jsx", b"data\x00more " * 20)
        normalized = path_str.replace("\\", "/")
        issues = detect_sed_damage.check_file(
            path_str, allowlist={normalized}
        )
        assert any("NUL bytes" in i for i in issues)

    def test_non_allowlisted_path_still_flagged(self, tmp_path, fake_head):
        """Allowlist is per-path — adding `a.jsx` does not exempt `b.jsx`."""
        fake_head["value"] = b"a" * 1000
        path_str = _write(tmp_path, "other.jsx", b"a" * 100)  # 10%
        # Allowlist contains a different path.
        issues = detect_sed_damage.check_file(
            path_str, allowlist={"some/other/file.jsx"}
        )
        assert any("truncated" in i for i in issues)

    def test_empty_allowlist_default_behaviour(self, tmp_path, fake_head):
        fake_head["value"] = b"a" * 1000
        path_str = _write(tmp_path, "f.jsx", b"a" * 100)
        issues = detect_sed_damage.check_file(path_str, allowlist=set())
        assert any("truncated" in i for i in issues)


# ---------------------------------------------------------------------------
# Allowlist file parsing
# ---------------------------------------------------------------------------


class TestLoadAllowlist:
    def test_missing_file_returns_empty(self, tmp_path):
        absent = tmp_path / "nonexistent"
        assert detect_sed_damage.load_allowlist(absent) == set()

    def test_simple_paths_parsed(self, tmp_path):
        f = tmp_path / "allow"
        f.write_text("a/b.jsx\nc/d.js\n", encoding="utf-8")
        assert detect_sed_damage.load_allowlist(f) == {"a/b.jsx", "c/d.js"}

    def test_comments_and_blanks_ignored(self, tmp_path):
        f = tmp_path / "allow"
        f.write_text(
            "# comment line\n"
            "\n"
            "real/path.jsx\n"
            "  # indented comment\n"
            "another/path.js\n",
            encoding="utf-8",
        )
        assert detect_sed_damage.load_allowlist(f) == {
            "real/path.jsx",
            "another/path.js",
        }

    def test_inline_trailing_comment_stripped(self, tmp_path):
        f = tmp_path / "allow"
        f.write_text("real/path.jsx  # PR-7 shim\n", encoding="utf-8")
        assert detect_sed_damage.load_allowlist(f) == {"real/path.jsx"}

    def test_windows_separator_normalized_to_posix(self, tmp_path):
        f = tmp_path / "allow"
        f.write_text("docs\\foo\\bar.jsx\n", encoding="utf-8")
        # Loader normalizes to forward slash so it matches both
        # POSIX and Windows-style staged paths.
        assert detect_sed_damage.load_allowlist(f) == {"docs/foo/bar.jsx"}

    def test_real_repo_allowlist_parses_cleanly(self):
        """The shipped `.sed-damage-allowlist` at repo root must parse
        without error. Currently it has only commented examples; the
        set should be empty.
        """
        result = detect_sed_damage.load_allowlist()
        # Don't assert the EXACT set (it'll grow over time); just that
        # parsing succeeds and returns a set of strings.
        assert isinstance(result, set)
        for entry in result:
            assert isinstance(entry, str)
            assert "#" not in entry  # comments stripped
            assert entry.strip() == entry  # leading/trailing ws stripped
