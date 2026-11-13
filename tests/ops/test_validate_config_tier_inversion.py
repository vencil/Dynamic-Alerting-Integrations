"""validate-config `tier_inversion`（#3691）：租戶實際拿到的 critical 不比 warning 嚴。

判的是 /metrics 給每個租戶的值、逐時段（da-guard served-values `--schedules`），
不是單一檔案：profile 的 `<base>_critical` 對平台檔的 `<base>`、時段對另一層的
預設值，這類跨檔倒置 check_threshold_unit_sanity 的 TIER-INVERSION 看不到。
「嚴」的方向取自出貨告警的比較（`metric_observed_map.yaml` 的 `direction`）。
租戶 id 是 fixture 名（CLAUDE.md #9）。
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

import validate_config as vc

pytestmark = pytest.mark.usefixtures("da_guard_env")

_REPO = Path(__file__).resolve().parents[2]


def _tree(root: Path, files: dict[str, str]) -> str:
    for rel, text in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text, encoding="utf-8")
    return str(root)


def test_a_profile_critical_under_a_platform_warning_fails(tmp_path):
    """The case no single file shows: the profile's critical (70) and the
    root default's warning (80) live in two files."""
    r = vc.check_tier_inversion(_tree(tmp_path, {
        "_defaults.yaml": "defaults:\n  mysql_connections: 80\n"
                          "profiles:\n  gold:\n    mysql_connections_critical: \"70\"\n",
        "tx.yaml": "tenants:\n  tx:\n    _profile: gold\n"}))
    assert r["status"] == vc.FAIL, r
    assert len(r["details"]) == 1, r
    line = r["details"][0]
    assert "'tx'" in line and "`mysql_connections`" in line and "00:00-24:00" in line, line
    assert "critical 70 is not above warning 80" in line, line


def test_a_stricter_critical_passes(tmp_path):
    r = vc.check_tier_inversion(_tree(tmp_path, {
        "_defaults.yaml": "defaults:\n  mysql_connections: 80\n",
        "tx.yaml": "tenants:\n  tx:\n    mysql_connections_critical: \"95\"\n"}))
    assert r["status"] == vc.PASS, r


def test_only_the_window_that_inverts_is_named(tmp_path):
    r = vc.check_tier_inversion(_tree(tmp_path, {
        "_defaults.yaml": "defaults:\n  mysql_connections: 80\n",
        "tx.yaml": "tenants:\n  tx:\n    mysql_connections_critical: \"95\"\n"
                   "    mysql_connections:\n      default: \"70\"\n      overrides:\n"
                   "        - window: \"01:00-03:00\"\n          value: \"99\"\n"}))
    assert r["status"] == vc.FAIL, r
    assert len(r["details"]) == 1 and "01:00-03:00" in r["details"][0], r


def test_a_dimensional_warning_is_paired_with_the_plain_critical(tmp_path):
    """The exporter serves the plain critical for every selector of the base."""
    r = vc.check_tier_inversion(_tree(tmp_path, {
        "_defaults.yaml": "defaults:\n  mysql_connections: 80\ndimensions:\n  mysql_connections: [db]\n",
        "tx.yaml": "tenants:\n  tx:\n    mysql_connections_critical: \"95\"\n"
                   "    'mysql_connections{db=\"a\"}': \"97\"\n"}))
    assert r["status"] == vc.FAIL, r
    assert len(r["details"]) == 1 and "db=a" in r["details"][0], r


def test_stricter_is_the_shipped_alerts_direction(tmp_path, monkeypatch):
    """For an alert that fires on a low value, critical must be BELOW warning."""
    import _observed_map_lib
    real = _observed_map_lib.load_observed_map

    def flipped(*a, **kw):
        m = dict(real(*a, **kw))
        m["mysql_connections"] = {**m["mysql_connections"], "direction": "<"}
        return m
    monkeypatch.setattr(_observed_map_lib, "load_observed_map", flipped)
    files = {"_defaults.yaml": "defaults:\n  mysql_connections: 80\n"}
    below = vc.check_tier_inversion(_tree(tmp_path / "below", {
        **files, "tx.yaml": "tenants:\n  tx:\n    mysql_connections_critical: \"70\"\n"}))
    above = vc.check_tier_inversion(_tree(tmp_path / "above", {
        **files, "tx.yaml": "tenants:\n  tx:\n    mysql_connections_critical: \"95\"\n"}))
    assert below["status"] == vc.PASS, below
    assert above["status"] == vc.FAIL and "is not below warning 80" in above["details"][0], above


def test_a_key_the_map_defers_is_not_judged(tmp_path):
    """container_cpu's alerts compare a version-aware recording rule the map
    does not record (KNOWN_DEFERRED): it has no direction, so no verdict."""
    r = vc.check_tier_inversion(_tree(tmp_path, {
        "_defaults.yaml": "defaults:\n  container_cpu: 80\n",
        "tx.yaml": "tenants:\n  tx:\n    container_cpu_critical: \"60\"\n"}))
    assert r["status"] == vc.PASS, r


def test_unreadable_reading_is_not_checked(tmp_path, monkeypatch):
    def boom(*a, **kw):
        raise vc.DaGuardNotFoundError("no da-guard")
    monkeypatch.setattr(vc, "load_served_tree", boom)
    r = vc.check_tier_inversion(str(tmp_path))
    assert r["status"] == vc.WARN and r["details"][0].startswith("not checked:"), r


def test_the_shipped_example_tree_passes():
    r = vc.check_tier_inversion(str(_REPO / "components/threshold-exporter/config/conf.d"))
    assert r["status"] == vc.PASS, r


def test_the_row_fails_validate_config(tmp_path):
    conf_d = _tree(tmp_path, {
        "_defaults.yaml": "defaults:\n  mysql_connections: 80\n",
        "tx.yaml": "tenants:\n  tx:\n    mysql_connections_critical: \"60\"\n"})
    p = subprocess.run([sys.executable, str(_REPO / "scripts/tools/ops/validate_config.py"),
                        "--config-dir", conf_d, "--json"],
                       capture_output=True, text=True, encoding="utf-8", timeout=300)
    rows = {r["check"]: r for r in json.loads(p.stdout)}
    assert rows["tier_inversion"]["status"] == vc.FAIL, rows["tier_inversion"]
    assert p.returncode == 1, p.returncode


def test_an_equal_critical_is_inverted(tmp_path):
    """Equal fires both at once, and dedup then mutes the warning: no stricter."""
    r = vc.check_tier_inversion(_tree(tmp_path, {
        "_defaults.yaml": "defaults:\n  mysql_connections: 80\n",
        "tx.yaml": "tenants:\n  tx:\n    mysql_connections_critical: \"80\"\n"}))
    assert r["status"] == vc.FAIL and "critical 80 is not above warning 80" in r["details"][0], r


def test_windows_that_do_not_meet_are_not_compared(tmp_path):
    """Both tiers raised in the same window: in no minute is the critical
    below the warning, though the window's warning (99) is above the
    default's critical (95)."""
    r = vc.check_tier_inversion(_tree(tmp_path, {
        "_defaults.yaml": "defaults:\n  mysql_connections: 80\n",
        "tx.yaml": "tenants:\n  tx:\n"
                   "    mysql_connections:\n      default: \"70\"\n      overrides:\n"
                   "        - window: \"01:00-03:00\"\n          value: \"99\"\n"
                   "    mysql_connections_critical:\n      default: \"95\"\n      overrides:\n"
                   "        - window: \"01:00-03:00\"\n          value: \"120\"\n"}))
    assert r["status"] == vc.PASS, r


def test_nothing_to_compare_says_so(tmp_path):
    r = vc.check_tier_inversion(_tree(tmp_path, {
        "_defaults.yaml": "defaults:\n  mysql_connections: 80\n",
        "tx.yaml": "tenants:\n  tx:\n    mysql_connections: \"70\"\n"}))
    assert r["status"] == vc.PASS and "nothing to compare" in r["details"][0], r
