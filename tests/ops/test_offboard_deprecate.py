"""
tests/test_offboard_deprecate.py — pytest style unit tests for offboard_tenant.py,
deprecate_rule.py, and validate_migration.py pure logic.
Tests the filesystem-based lifecycle tools and vector comparison logic
introduced in v0.6.0.
"""

import os
import tempfile

import pytest
import yaml

# ---------------------------------------------------------------------------
# Import tools
# ---------------------------------------------------------------------------

import offboard_tenant  # noqa: E402
import deprecate_rule  # noqa: E402
import validate_migration  # noqa: E402


# ===================================================================
# Helper: create temp conf.d directory with YAML files
# ===================================================================
def make_confdir(tmpdir, files):
    """Create YAML files in tmpdir. files = {filename: dict_content}."""
    for filename, content in files.items():
        path = os.path.join(tmpdir, filename)
        with open(path, 'w', encoding='utf-8') as f:
            yaml.safe_dump(content, f, default_flow_style=False, allow_unicode=True)
        os.chmod(path, 0o600)


# ===================================================================
# 1. offboard_tenant — find_config_file
# ===================================================================

def test_find_config_file_yaml_extension():
    """測試 YAML 副檔名。"""
    with tempfile.TemporaryDirectory() as d:
        make_confdir(d, {"db-a.yaml": {"tenants": {"db-a": {}}}})
        path = offboard_tenant.find_config_file("db-a", d)
        assert path is not None
        assert path.endswith("db-a.yaml")

def test_find_config_file_yml_extension():
    """測試 YML 副檔名。"""
    with tempfile.TemporaryDirectory() as d:
        make_confdir(d, {"db-b.yml": {"tenants": {"db-b": {}}}})
        # rename to .yml
        src = os.path.join(d, "db-b.yml")
        assert os.path.exists(src)
        path = offboard_tenant.find_config_file("db-b", d)
        assert path is not None

def test_find_config_file_not_found():
    """測試找不到檔案。"""
    with tempfile.TemporaryDirectory() as d:
        path = offboard_tenant.find_config_file("nonexistent", d)
        assert path is None


# ===================================================================
# 2. offboard_tenant — check_cross_references
# ===================================================================

def test_check_cross_references_no_cross_ref():
    """測試無交叉引用。"""
    configs = {
        "db-a.yaml": {"path": "/x/db-a.yaml", "data": {"tenants": {"db-a": {"m": 1}}}},
        "db-b.yaml": {"path": "/x/db-b.yaml", "data": {"tenants": {"db-b": {"m": 2}}}},
    }
    refs = offboard_tenant.check_cross_references("db-a", configs)
    assert refs == []

def test_check_cross_references_found_cross_ref():
    """測試發現交叉引用。"""
    configs = {
        "db-a.yaml": {"path": "/x/db-a.yaml", "data": {"tenants": {"db-a": {"m": 1}}}},
        "db-b.yaml": {"path": "/x/db-b.yaml", "data": {"note": "depends on db-a"}},
    }
    refs = offboard_tenant.check_cross_references("db-a", configs)
    assert "db-b.yaml" in refs


# ===================================================================
# 3. offboard_tenant — get_tenant_metrics
# ===================================================================

def test_get_tenant_metrics_found():
    """測試租戶 metrics 已找到。"""
    configs = {
        "db-a.yaml": {
            "path": "/x/db-a.yaml",
            "data": {"tenants": {"db-a": {"mysql_connections": 70, "mysql_cpu": 80}}},
        },
    }
    metrics = offboard_tenant.get_tenant_metrics("db-a", configs)
    assert len(metrics) == 2
    assert metrics["mysql_connections"] == 70

def test_get_tenant_metrics_empty():
    """測試空租戶 metrics。"""
    configs = {
        "db-a.yaml": {
            "path": "/x/db-a.yaml",
            "data": {"tenants": {"db-a": {}}},
        },
    }
    metrics = offboard_tenant.get_tenant_metrics("db-a", configs)
    assert metrics == {}

def test_get_tenant_metrics_missing_tenant():
    """測試缺失租戶。"""
    configs = {
        "db-b.yaml": {
            "path": "/x/db-b.yaml",
            "data": {"tenants": {"db-b": {"m": 1}}},
        },
    }
    metrics = offboard_tenant.get_tenant_metrics("db-a", configs)
    assert metrics == {}


# ===================================================================
# 4. offboard_tenant — run_precheck
# ===================================================================

def test_run_precheck_pass():
    """測試預檢查通過。"""
    with tempfile.TemporaryDirectory() as d:
        make_confdir(d, {
            "db-a.yaml": {"tenants": {"db-a": {"mysql_connections": 70}}},
            "db-b.yaml": {"tenants": {"db-b": {"mysql_connections": 80}}},
        })
        can_proceed, report = offboard_tenant.run_precheck("db-a", d)
        assert can_proceed is True
        report_text = "\n".join(report)
        assert "Pre-check" in report_text

def test_run_precheck_fail_no_config():
    """測試無配置預檢查失敗。"""
    with tempfile.TemporaryDirectory() as d:
        can_proceed, report = offboard_tenant.run_precheck("nonexistent", d)
        report_text = "\n".join(report)
        assert "找不到" in report_text

def test_run_precheck_warning_cross_ref():
    """測試交叉引用警告。"""
    with tempfile.TemporaryDirectory() as d:
        make_confdir(d, {
            "db-a.yaml": {"tenants": {"db-a": {"m": 1}}},
            "db-b.yaml": {"tenants": {"db-b": {"note": "db-a related"}}},
        })
        can_proceed, report = offboard_tenant.run_precheck("db-a", d)
        # Cross-ref is a warning, can still proceed
        assert can_proceed is True


# ===================================================================
# 5. deprecate_rule — scan_for_metric
# ===================================================================

def test_scan_for_metric_found_in_defaults():
    """測試在預設值中找到 metric。"""
    with tempfile.TemporaryDirectory() as d:
        make_confdir(d, {
            "_defaults.yaml": {"defaults": {"mysql_connections": 70}},
        })
        findings = deprecate_rule.scan_for_metric("mysql_connections", d)
        assert len(findings) == 1
        assert findings[0]["filename"] == "_defaults.yaml"

def test_scan_for_metric_found_variants():
    """測試找到 metric 的變體。"""
    with tempfile.TemporaryDirectory() as d:
        make_confdir(d, {
            "db-a.yaml": {"tenants": {"db-a": {
                "mysql_connections": 70,
                "custom_mysql_connections": 80,
                "mysql_connections_critical": 90,
            }}},
        })
        findings = deprecate_rule.scan_for_metric("mysql_connections", d)
        assert len(findings) == 1
        total_occ = sum(len(f["occurrences"]) for f in findings)
        assert total_occ == 3

def test_scan_for_metric_not_found():
    """測試 metric 未找到。"""
    with tempfile.TemporaryDirectory() as d:
        make_confdir(d, {
            "_defaults.yaml": {"defaults": {"other_metric": 50}},
        })
        findings = deprecate_rule.scan_for_metric("mysql_connections", d)
        assert len(findings) == 0

def test_scan_for_metric_dimensional_key():
    """測試維度鍵 metric。"""
    with tempfile.TemporaryDirectory() as d:
        make_confdir(d, {
            "db-a.yaml": {"tenants": {"db-a": {
                'mysql_connections{db="orders"}': 100,
            }}},
        })
        findings = deprecate_rule.scan_for_metric("mysql_connections", d)
        assert len(findings) == 1


# ===================================================================
# 6. deprecate_rule — disable_in_defaults
# ===================================================================

def test_disable_in_defaults_preview_mode():
    """測試預覽模式。"""
    with tempfile.TemporaryDirectory() as d:
        make_confdir(d, {
            "_defaults.yaml": {"defaults": {"mysql_connections": 70}},
        })
        ok, msg = deprecate_rule.disable_in_defaults(
            "mysql_connections", d, execute=False)
        assert ok is True
        assert "disable" in msg
        # File should NOT be modified
        data = deprecate_rule.load_yaml_file(os.path.join(d, "_defaults.yaml"))
        assert data["defaults"]["mysql_connections"] == 70

def test_disable_in_defaults_execute_mode():
    """測試執行模式。"""
    with tempfile.TemporaryDirectory() as d:
        make_confdir(d, {
            "_defaults.yaml": {"defaults": {"mysql_connections": 70}},
        })
        ok, msg = deprecate_rule.disable_in_defaults(
            "mysql_connections", d, execute=True)
        assert ok is True
        data = deprecate_rule.load_yaml_file(os.path.join(d, "_defaults.yaml"))
        assert data["defaults"]["mysql_connections"] == "disable"

def test_disable_in_defaults_already_disabled():
    """測試已停用。"""
    with tempfile.TemporaryDirectory() as d:
        make_confdir(d, {
            "_defaults.yaml": {"defaults": {"mysql_connections": "disable"}},
        })
        ok, msg = deprecate_rule.disable_in_defaults(
            "mysql_connections", d, execute=True)
        assert ok is True
        assert "已經是" in msg

def test_disable_in_defaults_missing_defaults_file():
    """測試缺失預設值檔案。"""
    with tempfile.TemporaryDirectory() as d:
        ok, msg = deprecate_rule.disable_in_defaults("m", d, execute=False)
        assert ok is False


# ===================================================================
# 7. deprecate_rule — remove_from_tenants
# ===================================================================

def test_remove_from_tenants_preview():
    """測試預覽模式。"""
    with tempfile.TemporaryDirectory() as d:
        make_confdir(d, {
            "db-a.yaml": {"tenants": {"db-a": {"mysql_connections": 70}}},
        })
        removed = deprecate_rule.remove_from_tenants(
            "mysql_connections", d, execute=False)
        assert len(removed) == 1
        # File should NOT be modified
        data = deprecate_rule.load_yaml_file(os.path.join(d, "db-a.yaml"))
        assert "mysql_connections" in data["tenants"]["db-a"]

def test_remove_from_tenants_execute():
    """測試執行模式。"""
    with tempfile.TemporaryDirectory() as d:
        make_confdir(d, {
            "db-a.yaml": {"tenants": {"db-a": {
                "mysql_connections": 70,
                "mysql_cpu": 80,
            }}},
        })
        removed = deprecate_rule.remove_from_tenants(
            "mysql_connections", d, execute=True)
        assert len(removed) == 1
        data = deprecate_rule.load_yaml_file(os.path.join(d, "db-a.yaml"))
        assert "mysql_connections" not in data["tenants"]["db-a"]
        assert "mysql_cpu" in data["tenants"]["db-a"]

def test_remove_from_tenants_skips_defaults():
    """測試跳過預設值檔案。"""
    with tempfile.TemporaryDirectory() as d:
        make_confdir(d, {
            "_defaults.yaml": {"defaults": {"mysql_connections": 70}},
        })
        removed = deprecate_rule.remove_from_tenants(
            "mysql_connections", d, execute=True)
        assert len(removed) == 0


# ===================================================================
# 8. validate_migration — extract_value_map
# ===================================================================

def test_extract_value_map_normal():
    """測試正常提取值對映。"""
    results = [
        {"metric": {"tenant": "db-a"}, "value": [1234567890, "42"]},
        {"metric": {"tenant": "db-b"}, "value": [1234567890, "99"]},
    ]
    vmap = validate_migration.extract_value_map(results)
    assert vmap["db-a"] == 42.0
    assert vmap["db-b"] == 99.0

def test_extract_value_map_no_tenant_label():
    """測試無租戶標籤。"""
    results = [
        {"metric": {}, "value": [0, "10"]},
    ]
    vmap = validate_migration.extract_value_map(results)
    assert "__no_label__" in vmap

def test_extract_value_map_null_value():
    """測試空值。"""
    results = [
        {"metric": {"tenant": "db-a"}, "value": [0, None]},
    ]
    vmap = validate_migration.extract_value_map(results)
    assert vmap["db-a"] is None

def test_extract_value_map_empty_results():
    """測試空結果。"""
    vmap = validate_migration.extract_value_map([])
    assert vmap == {}


# ===================================================================
# 9. validate_migration — compare_vectors
# ===================================================================

def test_compare_vectors_match():
    """測試向量匹配。"""
    old = {"db-a": 100.0}
    new = {"db-a": 100.0}
    diffs = validate_migration.compare_vectors(old, new)
    assert len(diffs) == 1
    assert diffs[0]["status"] == "match"

def test_compare_vectors_within_tolerance():
    """測試容差內的匹配。"""
    old = {"db-a": 100.0}
    new = {"db-a": 100.05}
    diffs = validate_migration.compare_vectors(old, new, tolerance=0.001)
    assert diffs[0]["status"] == "match"

def test_compare_vectors_mismatch():
    """測試不匹配。"""
    old = {"db-a": 100.0}
    new = {"db-a": 200.0}
    diffs = validate_migration.compare_vectors(old, new)
    assert diffs[0]["status"] == "mismatch"
    assert diffs[0]["delta"] == 100.0

def test_compare_vectors_old_missing():
    """測試舊值缺失。"""
    old = {}
    new = {"db-a": 50.0}
    diffs = validate_migration.compare_vectors(old, new)
    assert diffs[0]["status"] == "old_missing"

def test_compare_vectors_new_missing():
    """測試新值缺失。"""
    old = {"db-a": 50.0}
    new = {}
    diffs = validate_migration.compare_vectors(old, new)
    assert diffs[0]["status"] == "new_missing"

def test_compare_vectors_both_empty():
    """測試兩邊都空。"""
    old = {"db-a": None}
    new = {"db-a": None}
    diffs = validate_migration.compare_vectors(old, new)
    assert diffs[0]["status"] == "both_empty"

def test_compare_vectors_zero_values_match():
    """測試零值匹配。"""
    old = {"db-a": 0.0}
    new = {"db-a": 0.0}
    diffs = validate_migration.compare_vectors(old, new)
    assert diffs[0]["status"] == "match"

def test_compare_vectors_multi_tenant():
    """測試多租戶向量。"""
    old = {"db-a": 10.0, "db-b": 20.0}
    new = {"db-a": 10.0, "db-b": 25.0}
    diffs = validate_migration.compare_vectors(old, new)
    statuses = {d["tenant"]: d["status"] for d in diffs}
    assert statuses["db-a"] == "match"
    assert statuses["db-b"] == "mismatch"
