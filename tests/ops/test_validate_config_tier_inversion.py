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


def test_a_dimensional_warning_counts_in_the_tenants_largest_warning(tmp_path):
    """The shipped threshold rule is `max by(tenant)` over the tier's series,
    dimensional ones included: a dimensional warning (97) above the plain
    critical (95) makes the alert's warning 97."""
    r = vc.check_tier_inversion(_tree(tmp_path, {
        "_defaults.yaml": "defaults:\n  mysql_connections: 80\ndimensions:\n  mysql_connections: [db]\n",
        "tx.yaml": "tenants:\n  tx:\n    mysql_connections_critical: \"95\"\n"
                   "    'mysql_connections{db=\"a\"}': \"97\"\n"}))
    assert r["status"] == vc.FAIL, r
    assert "critical 95 is not above warning 97" in r["details"][0], r


def test_a_dimensional_critical_under_the_plain_warning_fails(tmp_path):
    """Review F1: the critical is dimensional (60), the warning the root's
    default (80): the alert's critical fires at 60, before its warning."""
    r = vc.check_tier_inversion(_tree(tmp_path, {
        "_defaults.yaml": "defaults:\n  mysql_connections: 80\ndimensions:\n  mysql_connections: [db]\n",
        "tx.yaml": "tenants:\n  tx:\n    'mysql_connections{db=\"a\"}': \"60:critical\"\n"}))
    assert r["status"] == vc.FAIL, r
    assert "critical 60 is not above warning 80" in r["details"][0], r


def test_a_larger_dimensional_critical_lifts_the_tenants_critical(tmp_path):
    """Review F2: the alert's critical is max(70, 99) = 99, above the warning
    (80): no inversion, though the plain critical alone is below it."""
    r = vc.check_tier_inversion(_tree(tmp_path, {
        "_defaults.yaml": "defaults:\n  mysql_connections: 80\ndimensions:\n  mysql_connections: [db]\n",
        "tx.yaml": "tenants:\n  tx:\n    mysql_connections_critical: \"70\"\n"
                   "    'mysql_connections{db=\"a\"}': \"99:critical\"\n"}))
    assert r["status"] == vc.PASS, r


def test_stricter_is_the_shipped_alerts_direction(tmp_path, monkeypatch):
    """For an alert that fires on a low value, critical must be BELOW warning."""
    import _observed_map_lib
    real = _observed_map_lib.load_observed_map

    def flipped(*a, **kw):
        m = dict(real(*a, **kw))
        for k in ("mysql_connections", "mysql_connections_critical"):
            m[k] = {**m[k], "direction": "<"}
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
    # Not the advice to edit thresholds: nothing was judged.
    assert r["hint"] == vc._TIER_INVERSION_NOT_CHECKED_HINT, r


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


def test_a_critical_no_shipped_alert_compares_is_not_judged(tmp_path):
    """kafka_broker_count's pack normalises only the warning tier: the map has
    no `kafka_broker_count_critical` entry, so no critical alert fires and
    there is nothing to invert — whatever the critical value."""
    import _observed_map_lib
    observed = _observed_map_lib.load_observed_map()
    assert _observed_map_lib.lookup(observed, "kafka_broker_count")[0] \
        is _observed_map_lib.LookupKind.COMPARED
    assert _observed_map_lib.lookup(observed, "kafka_broker_count_critical")[0] \
        is not _observed_map_lib.LookupKind.COMPARED
    r = vc.check_tier_inversion(_tree(tmp_path, {
        "_defaults.yaml": "defaults:\n  kafka_broker_count: 3\n",
        "tx.yaml": "tenants:\n  tx:\n    kafka_broker_count_critical: \"5\"\n"}))
    assert r["status"] == vc.PASS and "nothing to compare" in r["details"][0], r


def test_tiers_whose_directions_disagree_are_not_judged(tmp_path, monkeypatch):
    import _observed_map_lib
    real = _observed_map_lib.load_observed_map

    def split(*a, **kw):
        m = dict(real(*a, **kw))
        m["mysql_connections"] = {**m["mysql_connections"], "direction": "<"}
        return m
    monkeypatch.setattr(_observed_map_lib, "load_observed_map", split)
    r = vc.check_tier_inversion(_tree(tmp_path, {
        "_defaults.yaml": "defaults:\n  mysql_connections: 80\n",
        "tx.yaml": "tenants:\n  tx:\n    mysql_connections_critical: \"70\"\n"}))
    assert r["status"] == vc.PASS and "nothing to compare" in r["details"][0], r


def test_a_file_the_exporter_drops_is_not_checked(tmp_path):
    """The dropped file's tenant is not served, so a PASS would claim a
    tenant it never judged (#3691)."""
    # A syntax error: validate_config's yaml_syntax row names it and the
    # other rows still run on the rest of the tree (the CLI: called alone, the
    # served reading refuses such a tree outright).
    conf_d = _tree(tmp_path, {
        "_defaults.yaml": "defaults:\n  mysql_connections: 80\n",
        "tx.yaml": "tenants:\n  tx:\n    mysql_connections_critical: \"60\"\n   bad: [\n",
        "ty.yaml": "tenants:\n  ty:\n    mysql_connections_critical: \"95\"\n"})
    p = subprocess.run([sys.executable, str(_REPO / "scripts/tools/ops/validate_config.py"),
                        "--config-dir", conf_d, "--json"],
                       capture_output=True, text=True, encoding="utf-8", timeout=300)
    r = {row["check"]: row for row in json.loads(p.stdout)}["tier_inversion"]
    assert r["status"] == vc.WARN, r
    assert r["details"][0].startswith("not checked:") and "tx.yaml" in r["details"][0], r


def test_a_part_of_the_day_metrics_cannot_be_gathered_in_is_named(tmp_path):
    """01:00-03:00 the two keys collide into one series: /metrics fails then
    and serves nothing; the row says that part was not judged."""
    r = vc.check_tier_inversion(_tree(tmp_path, {
        "_defaults.yaml": "defaults:\n  mysql_connections: 80\n",
        "tx.yaml": "tenants:\n  tx:\n    mysql_connections_critical: \"95\"\n"
                   "    mysql_connections:\n      default: \"70\"\n      overrides:\n"
                   "        - window: \"01:00-03:00\"\n          value: \"50:critical\"\n"}))
    assert r["status"] == vc.PASS, r
    assert any(d.startswith("not judged where /metrics cannot be gathered")
               and "'tx' 01:00-03:00" in d for d in r["details"]), r

