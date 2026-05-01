"""Tests for check_dev_rules_enforcement.py — doc-drift detector.

Pinned contracts
----------------
1. **Detection**: hook-name shaped inline-code identifier in a
   "trigger context" (line contains "pre-commit hook" / "scan" /
   "會掃描" etc) → flagged if name not in known hook id / script
   stem set. Both underscore and hyphen variants accepted.

2. **Triggers**: lint only fires when a same-line trigger word is
   present, not on every backtick mention. Keeps false-positive
   rate near zero.

3. **Identifier shape**: must look like a hook (``check_*`` /
   ``lint_*`` / ``*-check`` / ``fix_*`` / ``generate_*`` /
   ``*-guard`` / ``*-hygiene`` / ``*-drift``). Bare words /
   variable names / paths skip.

4. **Per-line ignore**: ``<!-- enforcement-claim: ignore -->``
   comment on the line suppresses the check for that line.

5. **Severity**: ``--ci`` makes drift fatal; default is report-only.

The headline regression is the v2.8.0 doc-drift discovered manually
in (closed) PR #168: ``lint_hardcode_tenant`` and
``check_marketing_language`` were claimed in dev-rules.md but absent
from .pre-commit-config.yaml. This lint catches those PLUS any
future drift of the same shape.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

_TOOLS_DIR = os.path.join(
    os.path.dirname(__file__), "..", "..", "scripts", "tools", "lint"
)
sys.path.insert(0, _TOOLS_DIR)

import check_dev_rules_enforcement as cdre  # noqa: E402


def _scan(source: str, known: set[str] | None = None):
    """Convenience: run scan_for_drift with explicit known names."""
    return cdre.scan_for_drift(source, known if known is not None else set())


# ---------------------------------------------------------------------------
# Detection — claim shape recognition
# ---------------------------------------------------------------------------
class TestClaimDetection:
    def test_hook_in_check_context_flagged_if_unknown(self):
        src = "**檢查方式**：pre-commit hook `lint_hardcode_tenant` 會掃描。\n"
        drifts = _scan(src)
        assert len(drifts) == 1
        assert drifts[0].claimed_hook == "lint_hardcode_tenant"
        assert drifts[0].line_no == 1

    def test_hook_in_check_context_passes_if_known(self):
        src = "**檢查方式**：pre-commit hook `lint_hardcode_tenant` 會掃描。\n"
        drifts = _scan(src, known={"lint_hardcode_tenant"})
        assert drifts == []

    def test_hyphen_variant_matches_underscore_known(self):
        """`lint-hardcode-tenant` claim should match `lint_hardcode_tenant`
        in the known set (both spellings are equivalent)."""
        src = "pre-commit hook `lint-hardcode-tenant` 會掃描。\n"
        # Known set has only the underscore form — but normalization
        # in _load_known_hook_names adds both variants. Simulate by
        # passing both:
        drifts = _scan(src, known={"lint_hardcode_tenant", "lint-hardcode-tenant"})
        assert drifts == []

    def test_marketing_language_claim_caught(self):
        src = "**檢查方式**：pre-commit hook `check_marketing_language` (manual stage)。\n"
        drifts = _scan(src)
        assert len(drifts) == 1
        assert drifts[0].claimed_hook == "check_marketing_language"

    def test_chinese_trigger_will_scan(self):
        src = "由 `check_foo` lint hook 自動掃描\n"
        drifts = _scan(src)
        # "lint hook" trigger fires; check_foo doesn't exist → flagged
        assert len(drifts) == 1


# ---------------------------------------------------------------------------
# Detection — trigger discrimination
# ---------------------------------------------------------------------------
class TestTriggerContext:
    def test_no_trigger_no_flag(self):
        """Even an unknown hook-shaped identifier without trigger
        context isn't flagged — reduces false positives in prose."""
        src = "The script `check_foo` runs nightly via cron.\n"
        drifts = _scan(src)
        assert drifts == []

    def test_trigger_must_be_same_line(self):
        """Trigger on line 1 doesn't apply to claim on line 3."""
        src = (
            "Some pre-commit hook context here.\n"
            "\n"
            "Unrelated mention of `check_foo` later.\n"
        )
        drifts = _scan(src)
        assert drifts == []

    def test_multiple_claims_one_line(self):
        """If a single line has multiple inline-code claims, all flagged."""
        src = "pre-commit hook `check_alpha` and `check_beta` both scan.\n"
        drifts = _scan(src)
        assert len(drifts) == 2
        names = {d.claimed_hook for d in drifts}
        assert names == {"check_alpha", "check_beta"}


# ---------------------------------------------------------------------------
# Detection — identifier shape filter
# ---------------------------------------------------------------------------
class TestIdentifierShape:
    def test_hook_shaped_name_flagged(self):
        # Various legitimate hook-name shapes:
        for name in [
            "check_foo",
            "check-foo",
            "lint_foo_bar",
            "fix_file_hygiene",
            "generate_doc_map",
            "validate_docs_versions",
            "foo-check",
            "bar_check",
            "sed-damage-guard",
            "tool-map-drift",
            "file-hygiene",
        ]:
            src = f"pre-commit hook `{name}` 會掃描。\n"
            drifts = _scan(src)
            assert len(drifts) == 1, f"Should flag `{name}` (no known set)"
            assert drifts[0].claimed_hook == name

    def test_non_hook_shaped_name_skipped(self):
        # Random words / variables that happen to be in `code` —
        # not hook-shaped.
        for name in [
            "True",
            "None",
            "MyClass",
            "x",
            "tenant_id",  # generic variable name, no hook prefix
            "MAX_RETRIES",  # all caps — env var
        ]:
            src = f"pre-commit hook `{name}` 會掃描。\n"
            drifts = _scan(src)
            assert drifts == [], f"Should NOT flag `{name}` (not hook-shaped)"


# ---------------------------------------------------------------------------
# Per-line ignore comment
# ---------------------------------------------------------------------------
class TestIgnoreComment:
    def test_ignore_on_same_line_suppresses(self):
        src = (
            "pre-commit hook `check_bandit_profile` 會掃描。 "
            "<!-- enforcement-claim: ignore -->\n"
        )
        drifts = _scan(src)
        assert drifts == []

    def test_ignore_does_not_carry_to_next_line(self):
        src = (
            "<!-- enforcement-claim: ignore -->\n"
            "pre-commit hook `check_bandit_profile` 會掃描。\n"
        )
        drifts = _scan(src)
        # Ignore is on line 1; claim is on line 2 — claim NOT suppressed.
        assert len(drifts) == 1


# ---------------------------------------------------------------------------
# main() integration — argparse + exit code
# ---------------------------------------------------------------------------
class TestMain:
    @pytest.mark.timeout(15)
    def test_main_clean_dev_rules_exits_0(self, tmp_path, capsys, monkeypatch):
        clean = tmp_path / "dev-rules.md"
        clean.write_text(
            "# Rules\n\n"
            "Some text without any hook claims.\n"
            "Or with: pre-commit hook `file-hygiene` 會掃描。\n",
            encoding="utf-8",
        )
        # `file-hygiene` is a real hook in the live config, so this passes.
        rc = cdre.main(["--ci", "--path", str(clean)])
        assert rc == 0

    @pytest.mark.timeout(15)
    def test_main_drifty_dev_rules_under_ci_exits_1(self, tmp_path, capsys, monkeypatch):
        drifty = tmp_path / "dev-rules.md"
        drifty.write_text(
            "pre-commit hook `lint_doesnotexist_foo` 會掃描。\n",
            encoding="utf-8",
        )
        rc = cdre.main(["--ci", "--path", str(drifty)])
        err = capsys.readouterr().err
        assert rc == 1
        assert "lint_doesnotexist_foo" in err

    @pytest.mark.timeout(15)
    def test_main_drifty_dev_rules_no_ci_exits_0(self, tmp_path, capsys):
        drifty = tmp_path / "dev-rules.md"
        drifty.write_text(
            "pre-commit hook `lint_nonexistent_bar` 會掃描。\n",
            encoding="utf-8",
        )
        rc = cdre.main(["--path", str(drifty)])
        # No --ci → report only.
        assert rc == 0

    @pytest.mark.timeout(15)
    def test_main_missing_path_silently_returns_0(self, tmp_path):
        bogus = tmp_path / "does-not-exist.md"
        rc = cdre.main(["--ci", "--path", str(bogus)])
        assert rc == 0


# ---------------------------------------------------------------------------
# Self-dogfood — the live dev-rules.md must pass under the live config
# ---------------------------------------------------------------------------
class TestLiveDevRules:
    """Ultimate dogfood: run the lint against the actual repo.
    If this PR's re-wording of Rules #2 / #5 / #6 didn't fully
    eliminate the drift, this test fails — preventing the PR from
    landing in a broken state.
    """

    @pytest.mark.timeout(30)
    def test_live_dev_rules_has_no_drift(self):
        rules_path = cdre.DEV_RULES_PATH
        if not rules_path.exists():
            pytest.skip(f"{rules_path} not present in checkout")
        source = rules_path.read_text(encoding="utf-8", errors="replace")
        known = cdre._load_known_hook_names()
        drifts = cdre.scan_for_drift(source, known)
        assert drifts == [], (
            f"Live dev-rules.md has {len(drifts)} drift(s):\n"
            + "\n".join(f"  - {d.render()}" for d in drifts)
        )
