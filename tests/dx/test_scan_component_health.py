"""Tests for scan_component_health.py — Tier scoring & A-5b archived handling.

Covers:
  - _is_archive_candidate: pure-function threshold semantics.
  - scan(): archived tools produce tier="Archived", status="ARCHIVED" and are
    excluded from tier_distribution / token_group_distribution / playwright
    coverage / hex|px offenders / i18n aggregates.
  - scan(): non-archived tools behave identically to pre-A-5b (smoke test).
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml

_TOOLS_DIR = os.path.join(
    os.path.dirname(__file__), "..", "..", "scripts", "tools", "dx"
)
sys.path.insert(0, _TOOLS_DIR)

import scan_component_health as sch  # noqa: E402


# ---------------------------------------------------------------------------
# _is_archive_candidate — pure function
# ---------------------------------------------------------------------------
def _base_candidate_entry() -> dict:
    """An entry that meets every archive-candidate criterion."""
    return {
        "tier": "Tier 3 (deprecation_candidate)",
        "loc": 30,
        "tier_breakdown": {"recency": -1, "writer": 0},
        "playwright_spec": False,
        "first_commit": "2024-01-01 10:00:00 +0000",
    }


class TestIsArchiveCandidate:
    TODAY = datetime(2026, 4, 19, tzinfo=timezone.utc)

    def test_meets_all_criteria(self):
        ok, reason = sch._is_archive_candidate(_base_candidate_entry(), self.TODAY)
        assert ok is True
        assert "LOC=30" in reason
        assert "writer=0" in reason
        assert "no-spec" in reason

    def test_wrong_tier_is_rejected(self):
        entry = _base_candidate_entry()
        entry["tier"] = "Tier 3"  # plain Tier 3, not deprecation_candidate
        ok, _ = sch._is_archive_candidate(entry, self.TODAY)
        assert ok is False

    def test_loc_too_large_is_rejected(self):
        entry = _base_candidate_entry()
        entry["loc"] = 100
        ok, _ = sch._is_archive_candidate(entry, self.TODAY)
        assert ok is False

    def test_not_stale_is_rejected(self):
        entry = _base_candidate_entry()
        entry["tier_breakdown"] = {"recency": 0, "writer": 0}
        ok, _ = sch._is_archive_candidate(entry, self.TODAY)
        assert ok is False

    def test_has_write_surface_is_rejected(self):
        entry = _base_candidate_entry()
        entry["tier_breakdown"] = {"recency": -1, "writer": 2}
        ok, _ = sch._is_archive_candidate(entry, self.TODAY)
        assert ok is False

    def test_has_spec_is_rejected(self):
        entry = _base_candidate_entry()
        entry["playwright_spec"] = True
        ok, _ = sch._is_archive_candidate(entry, self.TODAY)
        assert ok is False

    def test_young_file_is_rejected(self):
        entry = _base_candidate_entry()
        # first_commit <= 365 days → still a WIP / recently introduced
        recent = self.TODAY - timedelta(days=200)
        entry["first_commit"] = recent.strftime("%Y-%m-%d %H:%M:%S %z")
        ok, _ = sch._is_archive_candidate(entry, self.TODAY)
        assert ok is False

    def test_missing_first_commit_is_rejected(self):
        entry = _base_candidate_entry()
        entry["first_commit"] = ""
        ok, _ = sch._is_archive_candidate(entry, self.TODAY)
        assert ok is False


# ---------------------------------------------------------------------------
# scan() integration — with tmp fixtures
# ---------------------------------------------------------------------------
@pytest.fixture
def scan_env(tmp_path, monkeypatch):
    """Construct a minimal repo-like environment for scan()."""
    docs = tmp_path / "docs"
    assets = docs / "assets"
    assets.mkdir(parents=True)
    jsx_root = docs  # scan_component_health resolves paths relative to JSX_ROOT
    e2e = tmp_path / "tests" / "e2e"
    e2e.mkdir(parents=True)

    # Two JSX files under docs/interactive/tools/
    jsx_dir = docs / "interactive" / "tools"
    jsx_dir.mkdir(parents=True)
    active_jsx = jsx_dir / "ActiveTool.jsx"
    active_jsx.write_text(
        "const t = window.__t;\n"
        "export default function ActiveTool() {\n"
        "  return <div>hello {t('greet')}</div>;\n"
        "}\n",
        encoding="utf-8",
    )
    archived_jsx = jsx_dir / "LegacyTool.jsx"
    archived_jsx.write_text(
        "export default function LegacyTool() {\n"
        "  return <div style={{color:'#ff0000'}}>legacy</div>;\n"
        "}\n",
        encoding="utf-8",
    )

    registry = {
        "tools": [
            {
                "key": "active-tool",
                "file": "interactive/tools/ActiveTool.jsx",
                "title": {"en": "Active Tool"},
                "audience": ["maintainer", "tenant"],
                "journey_phase": "configure",
                "hub_section": "ops",
                "appears_in": ["portal/index.md"],
            },
            {
                "key": "legacy-tool",
                "file": "interactive/tools/LegacyTool.jsx",
                "title": {"en": "Legacy Tool"},
                "audience": ["tenant"],
                "journey_phase": "monitor",
                "hub_section": "ops",
                "appears_in": ["portal/index.md"],
                "status": "archived",
                "archived_reason": "superseded by active-tool",
            },
        ]
    }
    registry_path = assets / "tool-registry.yaml"
    registry_path.write_text(
        yaml.safe_dump(registry, sort_keys=False), encoding="utf-8"
    )

    # Point module globals at tmp_path fixtures.
    monkeypatch.setattr(sch, "REPO", tmp_path)
    monkeypatch.setattr(sch, "REGISTRY", registry_path)
    monkeypatch.setattr(sch, "JSX_ROOT", jsx_root)
    monkeypatch.setattr(sch, "E2E_DIR", e2e)

    # Bypass git: deterministic mtime map.
    monkeypatch.setattr(
        sch, "build_git_mtime_cache",
        lambda paths: (
            {p.relative_to(tmp_path).as_posix(): "2026-03-01 12:00:00 +0000"
             for p in paths},
            {p.relative_to(tmp_path).as_posix(): "2025-01-01 12:00:00 +0000"
             for p in paths},
        ),
    )

    return tmp_path


class TestScanArchivedHandling:
    TODAY = datetime(2026, 4, 19, tzinfo=timezone.utc)

    def test_archived_tool_reports_archived_tier(self, scan_env):
        data = sch.scan(today=self.TODAY)
        archived = [r for r in data["tools"] if r["key"] == "legacy-tool"]
        assert len(archived) == 1
        entry = archived[0]
        assert entry["status"] == "ARCHIVED"
        assert entry["tier"] == "Archived"
        # Archived entries should retain visibility metrics (LOC / i18n / tokens).
        assert entry["loc"] > 0
        assert "archived_reason" in entry
        assert entry["archived_reason"] == "superseded by active-tool"
        # Archived entries must NOT contribute tier_score / tier_breakdown /
        # token_group (policy-level exclusion).
        assert "tier_score" not in entry
        assert "tier_breakdown" not in entry
        assert "token_group" not in entry

    def test_archived_tool_excluded_from_tier_distribution(self, scan_env):
        data = sch.scan(today=self.TODAY)
        summary = data["summary"]
        # Tier distribution only counts active tools.
        assert sum(summary["tier_distribution"].values()) == 1
        assert "Archived" not in summary["tier_distribution"]
        # New summary fields populated.
        assert summary["archived_count"] == 1
        assert summary["archived_tools"] == ["legacy-tool"]
        assert summary["total_active_tools"] == 1
        assert summary["total_registered_tools"] == 2
        # Playwright coverage denominator uses active count, not registered.
        assert summary["playwright_coverage"].endswith("/1")
        # hex_hardcoded came from the archived file — must NOT pollute the offender count.
        assert summary["tools_with_hardcoded_hex"] == 0
        # token_group distribution also excludes archived.
        assert sum(summary["token_group_distribution"].values()) == 1

    def test_archive_candidates_detected_when_stricter_threshold_met(
        self, scan_env, monkeypatch
    ):
        # Rewrite the registry so `active-tool` degrades into a Tier-3
        # deprecation candidate (narrow audience, tiny LOC, stale recency)
        # and verify scan() auto-suggests it.
        jsx = scan_env / "docs" / "interactive" / "tools" / "ActiveTool.jsx"
        jsx.write_text("// tiny\n", encoding="utf-8")  # 2 LOC, no write surface

        reg_path = scan_env / "docs" / "assets" / "tool-registry.yaml"
        registry = yaml.safe_load(reg_path.read_text(encoding="utf-8"))
        # Strip the audience/phase signals that would keep it out of Tier 3.
        registry["tools"][0]["audience"] = []
        registry["tools"][0]["journey_phase"] = ""
        reg_path.write_text(
            yaml.safe_dump(registry, sort_keys=False), encoding="utf-8"
        )

        # Force last_modified well in the past so recency == -1.
        monkeypatch.setattr(
            sch, "build_git_mtime_cache",
            lambda paths: (
                {p.relative_to(scan_env).as_posix(): "2024-01-01 12:00:00 +0000"
                 for p in paths},
                {p.relative_to(scan_env).as_posix(): "2023-06-01 12:00:00 +0000"
                 for p in paths},
            ),
        )

        data = sch.scan(today=self.TODAY)
        suggestions = data["summary"]["archive_candidates"]
        keys = [c["key"] for c in suggestions]
        assert "active-tool" in keys
        # Reason string includes the key diagnostic markers.
        reason = next(c["reason"] for c in suggestions if c["key"] == "active-tool")
        assert "LOC=" in reason
        assert "first_commit>365d" in reason

    def test_non_archived_behavior_unchanged(self, scan_env):
        """Active tool pathway retains pre-A-5b field set."""
        data = sch.scan(today=self.TODAY)
        active = [r for r in data["tools"] if r["key"] == "active-tool"][0]
        assert active["status"] == "OK"
        # Every pre-A-5b field must still be present.
        for key in (
            "tier", "tier_score", "tier_breakdown",
            "i18n_enabled", "i18n_calls",
            "cjk_strings_total", "cjk_hardcoded_strings", "i18n_coverage_ratio",
            "hex_colors_total", "hex_colors_hardcoded", "px_hardcoded",
            "design_tokens", "tailwind_palette", "token_density_per_100_loc",
            "token_group", "playwright_spec",
            "last_modified", "first_commit",
        ):
            assert key in active, f"missing field on active entry: {key}"
