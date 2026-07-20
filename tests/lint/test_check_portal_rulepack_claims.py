"""Tests for check_portal_rulepack_claims.py — portal rule-pack claim guard.

Pinned contracts
----------------
1. **Live dogfood**: the repo's real portal file has ZERO bogus claims. This is
   the regression lock for the 28 fabricated alert names + 2 wrong severities
   AND the 27 fabricated recording-rule names that shipped before the guard.
2. **Discriminating power (all three fault kinds)**: an alert name that exists in
   no rule tree is flagged `unknown-alert`; a severity that contradicts the
   shipped rule is flagged `severity-mismatch`; a recording-rule name that exists
   in no rule tree is flagged `unknown-recording-rule`. A guard that cannot go
   red is worthless, so these are asserted directly rather than trusted.
3. **Findings are actionable**: every finding carries BOTH the pack and the rule
   name — "55 claims are wrong" is not a fixable report.
4. **Fail loud, never fail open**: if the portal file's shape changes so the
   text parser stops matching, the guard raises (exit 2) instead of reporting a
   clean run over zero parsed claims.
5. **Platform-scope names resolve**: rules that live only in
   configmap-rules-platform.yaml (no rule-pack counterpart) are indexed.
6. **`recording: []` stays legal**: `operational` / `platform` genuinely ship no
   recording rules; an empty array must not be mistaken for a parser break.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import List

import pytest

_TOOLS_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "scripts", "tools", "lint")
sys.path.insert(0, _TOOLS_DIR)

import check_portal_rulepack_claims as guard  # noqa: E402


@pytest.fixture(scope="module")
def repo() -> Path:
    return guard._repo_root()


# ---------------------------------------------------------------- live dogfood
def test_repo_portal_claims_all_resolve(repo: Path):
    """The shipped portal file must advertise only rules we actually ship."""
    findings, stats = guard.check(repo)
    assert findings == [], (
        "portal advertises rules that do not match what we ship:\n"
        + "\n".join(f"  [{f['pack']}] {f['rule']}: {f['kind']} — {f['detail']}"
                    for f in findings)
    )
    # Sanity: the guard actually looked at both faces.
    assert stats["portal_alerts"] > 0
    assert stats["portal_recording"] > 0
    assert stats["shipped_alerts"] > 0
    assert stats["shipped_recording"] > 0


def test_shipped_index_includes_platform_only_rules(repo: Path):
    """configmap-rules-platform.yaml has no rule-pack counterpart — index it anyway."""
    shipped = guard.collect_shipped_alerts(repo)
    # These exist ONLY in the platform ConfigMap.
    assert "PrometheusRuleEvaluationFailing" in shipped
    assert "TenantMetricsOverLimit" in shipped
    assert shipped["PrometheusRuleEvaluationFailing"]["severity"] == "critical"


def test_shipped_index_covers_all_three_trees(repo: Path):
    shipped = guard.collect_shipped_alerts(repo)
    origins = {o for entry in shipped.values() for o in entry["origins"]}
    assert "kubernetes" in origins
    assert "platform" in origins
    # A rule-pack alert is seen in more than one copy (rule-pack + configmap + CRD).
    assert len(shipped["ContainerOOMKilled"]["origins"]) >= 2


def test_recording_index_is_populated(repo: Path):
    """Recording rules are indexed separately from alerts, and don't cross-contaminate."""
    alerts, records = guard.collect_shipped_rules(repo)
    assert "tenant:kafka_consumer_lag:max" in records
    assert "tenant:redis_evicted_keys:rate5m" in records
    # An alert name must never leak into the recording index (or vice versa).
    assert "ContainerOOMKilled" not in records
    assert "tenant:kafka_consumer_lag:max" not in alerts


# -------------------------------------------------------- discriminating power
_PORTAL_STUB = """\
const RULE_PACKS = {
  mariadb: {
    label: 'MariaDB/MySQL',
    recording: [
%s    ],
    alerts: [
      { name: '%s', severity: '%s', expr: 'x', desc: 'd', action: 'a' },
    ]
  },
};
"""

_GOOD_RECORD = "tenant:mysql_slow_queries:rate5m"


def _write_stub(
    tmp_path: Path,
    name: str = "MariaDBHighSlowQueries",
    severity: str = "warning",
    records: List[str] = None,
) -> Path:
    if records is None:
        records = [_GOOD_RECORD]
    rec_lines = "".join(
        f"      {{ name: '{r}', expr: 'e', desc: 'd' }},\n" for r in records
    )
    portal = tmp_path / guard.PORTAL_REL
    portal.parent.mkdir(parents=True, exist_ok=True)
    portal.write_text(_PORTAL_STUB % (rec_lines, name, severity), encoding="utf-8")
    return portal


@pytest.fixture
def fake_repo(tmp_path: Path, repo: Path, monkeypatch):
    """A tmp repo that reuses the REAL shipped index but a synthetic portal file.

    Rather than fabricate rule YAML, reuse the real index — the guard's
    tree-reading is covered by the dogfood tests above; here we vary only the
    portal side.
    """
    real = guard.collect_shipped_rules(repo)
    monkeypatch.setattr(guard, "collect_shipped_rules", lambda _r: real)
    return tmp_path


def test_unknown_alert_name_is_flagged(fake_repo: Path):
    _write_stub(fake_repo, name="MariaDBTotallyMadeUp")
    findings, _ = guard.check(fake_repo)
    assert len(findings) == 1
    assert findings[0]["kind"] == "unknown-alert"
    # Contract 3: actionable — names the pack AND the rule.
    assert findings[0]["pack"] == "mariadb"
    assert findings[0]["rule"] == "MariaDBTotallyMadeUp"


def test_severity_mismatch_is_flagged(fake_repo: Path):
    # MariaDBHighSlowQueries really ships as `warning`.
    _write_stub(fake_repo, severity="critical")
    findings, _ = guard.check(fake_repo)
    assert len(findings) == 1
    assert findings[0]["kind"] == "severity-mismatch"
    assert "critical" in findings[0]["detail"] and "warning" in findings[0]["detail"]


def test_unknown_recording_rule_is_flagged(fake_repo: Path):
    """Contract 2: the recording face must be able to go red on its own."""
    _write_stub(fake_repo, records=["tenant:mysql_totally_made_up:ratio"])
    findings, _ = guard.check(fake_repo)
    assert len(findings) == 1
    assert findings[0]["kind"] == "unknown-recording-rule"
    assert findings[0]["pack"] == "mariadb"
    assert findings[0]["rule"] == "tenant:mysql_totally_made_up:ratio"


def test_recording_drift_is_caught_even_when_alerts_are_clean(fake_repo: Path):
    """The exact regression this extension exists for: a green alert face must
    not mask a broken recording face."""
    _write_stub(fake_repo, records=[_GOOD_RECORD, "tenant:kafka_lag:max"])
    findings, _ = guard.check(fake_repo)
    assert [f["kind"] for f in findings] == ["unknown-recording-rule"]
    assert findings[0]["rule"] == "tenant:kafka_lag:max"


def test_correct_claim_passes(fake_repo: Path):
    _write_stub(fake_repo)
    findings, _ = guard.check(fake_repo)
    assert findings == []


def test_empty_recording_array_is_legal(fake_repo: Path):
    """Contract 6: operational / platform ship zero recording rules."""
    portal = fake_repo / guard.PORTAL_REL
    portal.parent.mkdir(parents=True, exist_ok=True)
    portal.write_text(
        "const RULE_PACKS = {\n"
        "  mariadb: {\n"
        "    label: 'x',\n"
        "    recording: [],\n"
        "    alerts: [\n"
        "      { name: 'MariaDBHighSlowQueries', severity: 'warning', expr: 'x', desc: 'd', action: 'a' },\n"
        "    ]\n"
        "  },\n"
        "};\n",
        encoding="utf-8",
    )
    findings, stats = guard.check(fake_repo)
    assert findings == []
    assert stats["portal_recording"] == 0


# ------------------------------------------------------------------ fail loud
def test_unparseable_portal_raises_not_silently_passes(tmp_path: Path):
    """Contract 4: a shape change must raise, not report a clean zero-claim run."""
    portal = tmp_path / guard.PORTAL_REL
    portal.parent.mkdir(parents=True, exist_ok=True)
    portal.write_text("const RULE_PACKS = buildPacks();\n", encoding="utf-8")
    with pytest.raises(ValueError, match="no rule-pack blocks matched"):
        guard.parse_portal_claims(portal)


def test_pack_without_parseable_entries_raises(tmp_path: Path):
    portal = tmp_path / guard.PORTAL_REL
    portal.parent.mkdir(parents=True, exist_ok=True)
    portal.write_text(
        "const RULE_PACKS = {\n"
        "  mariadb: {\n"
        "    label: 'x',\n"
        "    alerts: [\n"
        "    ]\n"
        "  },\n"
        "};\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="no parseable"):
        guard.parse_portal_claims(portal)


def test_pack_missing_recording_array_entirely_raises(tmp_path: Path):
    """A pack with neither `recording: [...]` nor `recording: [],` is a shape
    change the parser must not silently skip over."""
    portal = tmp_path / guard.PORTAL_REL
    portal.parent.mkdir(parents=True, exist_ok=True)
    portal.write_text(
        "const RULE_PACKS = {\n"
        "  mariadb: {\n"
        "    label: 'x',\n"
        "    alerts: [\n"
        "      { name: 'MariaDBHighSlowQueries', severity: 'warning', expr: 'x', desc: 'd', action: 'a' },\n"
        "    ]\n"
        "  },\n"
        "};\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="no parseable `recording"):
        guard.parse_portal_claims(portal)


def test_parser_reads_every_pack_in_the_real_file(repo: Path):
    claims = guard.parse_portal_claims(repo / guard.PORTAL_REL)
    # Guards against a regex that silently matches only the first pack.
    assert len(claims) >= 13
    assert "kubernetes" in claims and "platform" in claims
    assert all(entry["alerts"] for entry in claims.values())
    # The two packs that legitimately ship no recording rules.
    assert claims["platform"]["recording"] == []
    assert claims["operational"]["recording"] == []
    # ...and a pack that definitely has some.
    assert claims["mariadb"]["recording"]
