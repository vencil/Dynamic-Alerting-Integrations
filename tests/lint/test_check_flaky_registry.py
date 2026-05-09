"""Tests for scripts/tools/lint/check_flaky_registry.py.

Covers:
  - Version parsing (plain `v*` + `prefix/v*`)
  - Cross-release-line comparison errors
  - CHANGELOG version extraction
  - Schema validation (required fields, max_retries shape, regex validity)
  - expire_at lifecycle (current >= expire_at → EXPIRED)
  - Empty / missing registry handling (healthy state)
  - CLI exit codes
  - --current-version override
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = REPO_ROOT / "scripts" / "tools" / "lint" / "check_flaky_registry.py"

_spec = importlib.util.spec_from_file_location("check_flaky_registry", _SCRIPT)
mod = importlib.util.module_from_spec(_spec)
sys.modules["check_flaky_registry"] = mod
_spec.loader.exec_module(mod)


# ============================================================
# parse_version
# ============================================================

class TestParseVersion:

    def test_plain_version(self):
        v = mod.parse_version("v2.7.0")
        assert v.prefix == ""
        assert (v.major, v.minor, v.patch) == (2, 7, 0)

    def test_prefixed_version(self):
        v = mod.parse_version("exporter/v2.9.1")
        assert v.prefix == "exporter"
        assert (v.major, v.minor, v.patch) == (2, 9, 1)

    def test_invalid_format_raises(self):
        with pytest.raises(ValueError, match="does not match"):
            mod.parse_version("2.7.0")  # missing 'v'
        with pytest.raises(ValueError, match="does not match"):
            mod.parse_version("v2.7")  # missing patch
        with pytest.raises(ValueError, match="does not match"):
            mod.parse_version("v2.7.0-rc1")  # pre-release suffix unsupported

    def test_str_round_trip(self):
        for s in ("v2.7.0", "exporter/v0.1.5", "tools/v3.0.0"):
            assert str(mod.parse_version(s)) == s


class TestVersionComparison:

    def test_lt_within_line(self):
        assert mod.parse_version("v2.7.0") < mod.parse_version("v2.8.0")
        assert mod.parse_version("v2.7.0") < mod.parse_version("v2.7.1")
        assert mod.parse_version("v1.0.0") < mod.parse_version("v2.0.0")

    def test_ge_within_line(self):
        # not a < b == True → a >= b == False; a >= a should be True
        v = mod.parse_version("v2.9.0")
        assert v >= v
        assert mod.parse_version("v2.9.0") >= mod.parse_version("v2.8.5")

    def test_cross_line_raises(self):
        a = mod.parse_version("v2.9.0")
        b = mod.parse_version("exporter/v2.9.0")
        with pytest.raises(ValueError, match="cannot compare"):
            _ = a < b


# ============================================================
# latest_version_from_changelog
# ============================================================

class TestChangelogVersion:

    def test_finds_top_heading(self, tmp_path):
        c = tmp_path / "CHANGELOG.md"
        c.write_text(
            "# Changelog\n\n"
            "## [v2.7.0] — title (date)\n"
            "## [v2.6.0] — older\n",
            encoding="utf-8",
        )
        v = mod.latest_version_from_changelog(c)
        assert v is not None
        assert str(v) == "v2.7.0"

    def test_missing_file_returns_none(self, tmp_path):
        assert mod.latest_version_from_changelog(tmp_path / "nope.md") is None

    def test_no_heading_returns_none(self, tmp_path):
        c = tmp_path / "CHANGELOG.md"
        c.write_text("# Changelog\n\nNo version headings here.\n", encoding="utf-8")
        assert mod.latest_version_from_changelog(c) is None


# ============================================================
# validate_entry — schema
# ============================================================

@pytest.fixture
def valid_entry():
    return {
        "test": "TestFlaky",
        "pattern": "^TestFlaky$",
        "max_retries": 2,
        "owner": "@team",
        "tracked_by": "HA-N description",
        "expire_at": "v2.9.0",
    }


@pytest.fixture
def current_v270():
    return mod.parse_version("v2.7.0")


class TestValidateEntrySchema:

    def test_valid_entry_no_issues(self, valid_entry, current_v270):
        assert mod.validate_entry(0, valid_entry, current_v270) == []

    def test_missing_required_field(self, valid_entry, current_v270):
        del valid_entry["owner"]
        issues = mod.validate_entry(0, valid_entry, current_v270)
        assert any(i.field == "owner" and "missing" in i.message for i in issues)

    def test_empty_string_field(self, valid_entry, current_v270):
        valid_entry["owner"] = ""
        issues = mod.validate_entry(0, valid_entry, current_v270)
        assert any(i.field == "owner" for i in issues)

    def test_too_short_tracked_by(self, valid_entry, current_v270):
        valid_entry["tracked_by"] = "TBD"  # 3 chars, below 5 threshold
        issues = mod.validate_entry(0, valid_entry, current_v270)
        assert any(i.field == "tracked_by" for i in issues)

    def test_max_retries_zero_rejected(self, valid_entry, current_v270):
        valid_entry["max_retries"] = 0
        issues = mod.validate_entry(0, valid_entry, current_v270)
        assert any(i.field == "max_retries" for i in issues)

    def test_max_retries_excessive_rejected(self, valid_entry, current_v270):
        valid_entry["max_retries"] = 10  # over cap
        issues = mod.validate_entry(0, valid_entry, current_v270)
        assert any(i.field == "max_retries" for i in issues)

    def test_max_retries_non_integer_rejected(self, valid_entry, current_v270):
        valid_entry["max_retries"] = "two"
        issues = mod.validate_entry(0, valid_entry, current_v270)
        assert any(i.field == "max_retries" for i in issues)

    def test_invalid_regex_pattern(self, valid_entry, current_v270):
        valid_entry["pattern"] = "TestFoo("  # unbalanced paren
        issues = mod.validate_entry(0, valid_entry, current_v270)
        assert any(i.field == "pattern" and "regex" in i.message for i in issues)

    def test_invalid_expire_at_format(self, valid_entry, current_v270):
        valid_entry["expire_at"] = "2.9"  # missing v + patch
        issues = mod.validate_entry(0, valid_entry, current_v270)
        assert any(i.field == "expire_at" for i in issues)


class TestValidateEntryLifecycle:

    def test_not_expired(self, valid_entry):
        # expire_at v2.9.0, current v2.7.0 → not expired
        current = mod.parse_version("v2.7.0")
        assert mod.validate_entry(0, valid_entry, current) == []

    def test_expired_at_exact_match(self, valid_entry):
        # current == expire_at → EXPIRED (must be removed before this version ships)
        current = mod.parse_version("v2.9.0")
        issues = mod.validate_entry(0, valid_entry, current)
        assert any("EXPIRED" in i.message for i in issues)

    def test_expired_past(self, valid_entry):
        current = mod.parse_version("v3.0.0")
        issues = mod.validate_entry(0, valid_entry, current)
        assert any("EXPIRED" in i.message for i in issues)

    def test_no_current_skips_lifecycle(self, valid_entry):
        # If we can't determine current version, don't fail-CI; just skip.
        assert mod.validate_entry(0, valid_entry, None) == []

    def test_cross_line_expire_at_surfaces_error(self, valid_entry):
        # current is platform v2.7.0; expire_at is exporter/v2.9.0.
        # Cross-line comparison → schema error (caller chose the wrong line).
        valid_entry["expire_at"] = "exporter/v2.9.0"
        current = mod.parse_version("v2.7.0")
        issues = mod.validate_entry(0, valid_entry, current)
        assert any("cannot compare" in i.message for i in issues)


# ============================================================
# validate_registry — top-level shape
# ============================================================

class TestValidateRegistry:

    def test_empty_known_flakes_healthy(self, current_v270):
        assert mod.validate_registry({"known_flakes": []}, current_v270) == []

    def test_missing_known_flakes_healthy(self, current_v270):
        assert mod.validate_registry({}, current_v270) == []

    def test_non_dict_top_level_error(self, current_v270):
        issues = mod.validate_registry(["not", "a", "dict"], current_v270)
        assert any(i.field == "<root>" for i in issues)

    def test_non_list_known_flakes_error(self, current_v270):
        issues = mod.validate_registry({"known_flakes": "string"}, current_v270)
        assert any(i.field == "known_flakes" for i in issues)

    def test_non_dict_entry_error(self, current_v270):
        issues = mod.validate_registry(
            {"known_flakes": ["string-not-dict"]}, current_v270,
        )
        assert any(i.field == "<entry>" for i in issues)


# ============================================================
# main — CLI / exit codes
# ============================================================

def _write_yaml(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")


class TestMainCLI:

    def test_missing_registry_exits_zero(self, tmp_path):
        # Healthy "no flakes" state when file doesn't exist.
        rc = mod.main([
            "--registry", str(tmp_path / "nope.yaml"),
            "--current-version", "v2.7.0",
        ])
        assert rc == 0

    def test_empty_known_flakes_exits_zero(self, tmp_path, capsys):
        f = tmp_path / "flakes.yaml"
        _write_yaml(f, "known_flakes: []\n")
        rc = mod.main([
            "--registry", str(f), "--current-version", "v2.7.0",
        ])
        assert rc == 0

    def test_valid_entry_exits_zero(self, tmp_path):
        f = tmp_path / "flakes.yaml"
        _write_yaml(f,
            "known_flakes:\n"
            "  - test: TestFlaky\n"
            "    pattern: '^TestFlaky$'\n"
            "    max_retries: 2\n"
            "    owner: '@team'\n"
            "    tracked_by: 'HA-N description'\n"
            "    expire_at: v2.9.0\n"
        )
        rc = mod.main([
            "--registry", str(f), "--current-version", "v2.7.0", "--ci",
        ])
        assert rc == 0

    def test_expired_entry_exits_one(self, tmp_path, capsys):
        f = tmp_path / "flakes.yaml"
        _write_yaml(f,
            "known_flakes:\n"
            "  - test: TestStale\n"
            "    pattern: '^TestStale$'\n"
            "    max_retries: 1\n"
            "    owner: '@team'\n"
            "    tracked_by: 'HA-N description'\n"
            "    expire_at: v2.5.0\n"  # past current v2.7.0
        )
        rc = mod.main([
            "--registry", str(f), "--current-version", "v2.7.0", "--ci",
        ])
        assert rc == 1
        captured = capsys.readouterr()
        assert "EXPIRED" in captured.err

    def test_schema_error_exits_one(self, tmp_path, capsys):
        f = tmp_path / "flakes.yaml"
        _write_yaml(f,
            "known_flakes:\n"
            "  - test: TestX\n"
            "    pattern: '^TestX$'\n"
            "    max_retries: 1\n"
            "    owner: '@team'\n"
            "    # tracked_by missing\n"
            "    expire_at: v2.9.0\n"
        )
        rc = mod.main([
            "--registry", str(f), "--current-version", "v2.7.0", "--ci",
        ])
        assert rc == 1
        assert "tracked_by" in capsys.readouterr().err

    def test_invalid_yaml_exits_two(self, tmp_path, capsys):
        f = tmp_path / "flakes.yaml"
        _write_yaml(f, "known_flakes:\n  - test: [unclosed\n")
        rc = mod.main([
            "--registry", str(f), "--current-version", "v2.7.0",
        ])
        assert rc == 2
        assert "cannot parse" in capsys.readouterr().err

    def test_bad_current_version_exits_two(self, tmp_path):
        rc = mod.main([
            "--registry", str(tmp_path / "anything.yaml"),
            "--current-version", "not-a-version",
        ])
        assert rc == 2

    def test_changelog_fallback(self, tmp_path):
        """When no --current-version, read CHANGELOG.md."""
        c = tmp_path / "CHANGELOG.md"
        c.write_text("## [v2.7.0] — title\n", encoding="utf-8")
        f = tmp_path / "flakes.yaml"
        _write_yaml(f,
            "known_flakes:\n"
            "  - test: TestStale\n"
            "    pattern: '^TestStale$'\n"
            "    max_retries: 1\n"
            "    owner: '@team'\n"
            "    tracked_by: 'HA-N description'\n"
            "    expire_at: v2.5.0\n"  # past v2.7.0
        )
        rc = mod.main([
            "--registry", str(f), "--changelog", str(c), "--ci",
        ])
        assert rc == 1


# ============================================================
# Repo registry passes its own validator (regression-style smoke test)
# ============================================================

class TestRepoRegistry:

    def test_repo_registry_passes(self):
        """The shipped flaky-tests.yaml + CHANGELOG.md must validate.

        This is a regression guard: any future edit to flaky-tests.yaml
        that violates schema or sets expire_at past current shipped
        version trips this test (in addition to the pre-commit hook).
        """
        # Run against the repo's actual files
        rc = mod.main(["--ci"])
        assert rc == 0, "repo's flaky-tests.yaml fails its own validator"
