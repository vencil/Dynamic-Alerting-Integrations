"""Tests for scripts/tools/lint/check_engagement_disclosure.py.

Engagement-disclosure gate (dev-rules §E). The repo and its issues are PUBLIC and
a published line is irreversible, so this gate blocks the narrow conjunction
"<source platform> … <in-flight marker>" on one line. It is deliberately NOT a
keyword denylist — naming the platform is fine; asserting a live engagement is not.

Coverage (every case below was a real defect found by adversarial review, so these
are regression pins, not decoration):
  - conjunction detected / platform-alone and marker-alone NOT detected
  - scan globs cover every surface the pre-commit `files:` pattern triggers on —
    notably README.en.md and .yml (both were silently unscanned)
  - English in-flight phrasings with internal spaces ("in progress", "underway",
    "active X to Y migration") — the ZH markers were covered, the EN ones were not
  - the deid-ok opt-out requires a comment anchor AND a rationale, and a marker
    merely QUOTED in an inline code span must NOT exempt the line (prose that
    documents the marker previously self-exempted)
  - SELF_PATH exclusion does not create a bypass for other files
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = REPO_ROOT / "scripts" / "tools" / "lint" / "check_engagement_disclosure.py"

_spec = importlib.util.spec_from_file_location("check_engagement_disclosure", _SCRIPT)
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)

_PLATFORM_ONLY = "we also ship a Splunk HEC sink example"
_MARKER_ONLY = "the kubernetes pack is an active migration target"
_VIOLATION = "the Oracle pack is an active Splunk-to-VM migration target"  # deid-ok: lint 測試需以字面違規當 fixture


def _hits(line: str) -> bool:
    """True when the line trips the conjunction (mirrors scan()'s predicate)."""
    stripped = mod._CODE_SPAN_RE.sub("", line)
    if mod.DEID_OK_RE.search(stripped):
        return False
    return bool(mod._PLATFORM.search(line) and mod._IN_FLIGHT.search(line))


# ── the core conjunction ──────────────────────────────────────────────────

def test_conjunction_is_flagged():
    assert _hits(_VIOLATION)


def test_platform_alone_is_not_flagged():
    """10+ benign platform mentions exist (log sinks, token allowlists, examples).
    Flagging the word alone would be almost all false positives."""
    assert not _hits(_PLATFORM_ONLY)


def test_in_flight_marker_alone_is_not_flagged():
    assert not _hits(_MARKER_ONLY)


# ── scan surface must match the pre-commit trigger ────────────────────────

def test_scan_globs_cover_english_readme_and_yml():
    """A file that FIRES the hook but is never scanned is the worst silent failure
    for an irreversible-publication guard. Both of these were unscanned."""
    globs = set(mod.SCAN_GLOBS)
    assert any(g.startswith("README") and "*" in g for g in globs), \
        "README.en.md must be scanned, not just README.md"
    assert any(g.endswith("*.yml") for g in globs), ".yml surfaces must be scanned"


def test_scan_globs_reach_every_repo_surface_the_hook_triggers_on(tmp_path):
    """End-to-end: plant the same violation in each surface, all must be found."""
    for rel in ("README.en.md", "docs/x.md", "tests/rulepacks/a.yml",
                "scripts/tools/z.py", "CHANGELOG.md"):
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(f"# t\n\n{_VIOLATION}\n", encoding="utf-8")
    found = {f for f, _, _ in mod.scan(tmp_path)}
    assert found == {"README.en.md", "docs/x.md", "tests/rulepacks/a.yml",
                     "scripts/tools/z.py", "CHANGELOG.md"}, found


# ── English phrasing parity with the Chinese markers ──────────────────────

def test_english_in_flight_phrasings_with_spaces():
    for s in (
        "an active Splunk to VM migration is underway",  # deid-ok: lint 測試需以字面違規當 fixture
        "the Splunk migration is in progress",  # deid-ok: lint 測試需以字面違規當 fixture
        "we are migrating off Splunk this quarter",  # deid-ok: lint 測試需以字面違規當 fixture
        "the Oracle pack is an active Splunk-to-VM migration target",  # deid-ok: lint 測試需以字面違規當 fixture
    ):
        assert _hits(s), f"missed: {s}"


# ── the opt-out marker must not be fail-open ──────────────────────────────

def test_deid_ok_marker_exempts_when_comment_anchored_with_rationale():
    assert not _hits(f"{_VIOLATION} <!-- deid-ok: policy doc must show the anti-pattern -->")
    assert not _hits(f"# {_VIOLATION}  # deid-ok: fixture header")


def test_deid_ok_without_rationale_does_not_exempt():
    assert _hits(f"{_VIOLATION} <!-- deid-ok: -->")


def test_deid_ok_quoted_in_code_span_does_not_exempt():
    """A doc that TEACHES the marker must not exempt its own example line —
    this was the residual fail-open after the first hardening pass."""
    assert _hits(f"{_VIOLATION}, see the `<!-- deid-ok: reason -->` marker")
    assert _hits(f"{_VIOLATION}, use `# deid-ok: reason`")


def test_bare_marker_mention_does_not_exempt():
    assert _hits(f"{_VIOLATION} — the deid-ok marker is documented in dev-rules")


# ── self-exclusion is scoped ──────────────────────────────────────────────

def test_self_exclusion_does_not_leak_to_other_scripts(tmp_path):
    p = tmp_path / "scripts" / "tools" / "other.py"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(f"# {_VIOLATION}\n", encoding="utf-8")
    assert [f for f, _, _ in mod.scan(tmp_path)] == ["scripts/tools/other.py"]


# ── the live repo must stay clean (guards against a silent re-introduction) ─

def test_real_repo_is_clean():
    assert mod.scan(REPO_ROOT) == []
