"""
tests/test_offboard_deprecate.py — pytest style unit tests for offboard_tenant.py,
deprecate_rule.py, and validate_migration.py pure logic.
Tests the filesystem-based lifecycle tools and vector comparison logic
introduced in v0.6.0.
"""

import os
import tempfile
from pathlib import Path

import pytest
from _platform_fs import symlink_or_skip  # noqa: E402
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

def _configs(tmp_path, files):
    """`load_all_configs` over a real conf.d holding *files* (#3206: which file
    is a tenant's own is Go's `tenant_named`, read from the tree)."""
    make_confdir(str(tmp_path), files)
    return offboard_tenant.load_all_configs(str(tmp_path))


def test_check_cross_references_no_cross_ref(tmp_path):
    """測試無交叉引用。"""
    configs = _configs(tmp_path, {"db-a.yaml": {"tenants": {"db-a": {"m": 1}}},
                                  "db-b.yaml": {"tenants": {"db-b": {"m": 2}}}})
    refs = offboard_tenant.check_cross_references("db-a", configs)
    assert refs == []

def test_check_cross_references_found_cross_ref(tmp_path):
    """測試發現交叉引用。"""
    configs = _configs(tmp_path, {"db-a.yaml": {"tenants": {"db-a": {"m": 1}}},
                                  "db-b.yaml": {"note": "depends on db-a"}})
    refs = offboard_tenant.check_cross_references("db-a", configs)
    assert "db-b.yaml" in refs

def test_a_file_whose_name_only_starts_with_the_tenant_is_not_its_own(tmp_path):
    """#3206: the tenant's own file is the one whose NAME carries its id (Go's
    `tenant_named`), not any file starting with `<tenant>.` — `db-a.bak.yaml`
    carries `db-a.bak`, so its mention of db-a is a reference."""
    configs = _configs(tmp_path, {"db-a.yaml": {"tenants": {"db-a": {"m": 1}}},
                                  "db-a.bak.yaml": {"tenants": {"db-a": {"m": 9}}}})
    assert offboard_tenant.check_cross_references("db-a", configs) == ["db-a.bak.yaml"]
    assert offboard_tenant.get_tenant_metrics("db-a", configs) == {"m": 1}


# ===================================================================
# 3. offboard_tenant — get_tenant_metrics
# ===================================================================

def test_get_tenant_metrics_found(tmp_path):
    """測試租戶 metrics 已找到。"""
    configs = _configs(tmp_path, {"db-a.yaml": {"tenants": {"db-a": {
        "mysql_connections": 70, "mysql_threads_running": 80}}}})
    metrics = offboard_tenant.get_tenant_metrics("db-a", configs)
    assert len(metrics) == 2
    assert metrics["mysql_connections"] == 70

def test_get_tenant_metrics_empty(tmp_path):
    """測試空租戶 metrics。"""
    configs = _configs(tmp_path, {"db-a.yaml": {"tenants": {"db-a": {}}}})
    metrics = offboard_tenant.get_tenant_metrics("db-a", configs)
    assert metrics == {}

def test_get_tenant_metrics_missing_tenant(tmp_path):
    """測試缺失租戶。"""
    configs = _configs(tmp_path, {"db-b.yaml": {"tenants": {"db-b": {"m": 1}}}})
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
# 6. deprecate_rule — remove_from_all_defaults (#1787: 下架＝刪 key)
# ===================================================================

def _only_carrier(results):
    """`remove_from_all_defaults` 對單一載體回一筆 `(path, ok, msg, removed)`。"""
    assert len(results) == 1, results
    _path, ok, msg, _removed = results[0]
    return ok, msg


def test_remove_from_defaults_preview_mode():
    """測試預覽模式。"""
    with tempfile.TemporaryDirectory() as d:
        make_confdir(d, {
            "_defaults.yaml": {"defaults": {"mysql_connections": 70}},
        })
        ok, msg = _only_carrier(deprecate_rule.remove_from_all_defaults(
            "mysql_connections", d, execute=False))
        assert ok is True
        assert "將從" in msg and "移除" in msg
        # File should NOT be modified
        data = deprecate_rule.load_yaml_file(os.path.join(d, "_defaults.yaml"))
        assert data["defaults"]["mysql_connections"] == 70

def test_remove_from_defaults_execute_mode():
    """執行模式：key 被刪掉，而不是被寫成 "disable"（`defaults:` 是
    `map[string]float64`，字串會讓整份檔被 `parsePartialConfig` 丟掉）。"""
    with tempfile.TemporaryDirectory() as d:
        make_confdir(d, {
            "_defaults.yaml": {"defaults": {"mysql_connections": 70,
                                            "mem_usage": 90}},
        })
        ok, msg = _only_carrier(deprecate_rule.remove_from_all_defaults(
            "mysql_connections", d, execute=True))
        assert ok is True
        data = deprecate_rule.load_yaml_file(os.path.join(d, "_defaults.yaml"))
        assert "mysql_connections" not in data["defaults"]
        # 只刪目標，鄰居留著。
        assert data["defaults"]["mem_usage"] == 90

def test_a_carrier_already_written_as_disable_refuses_the_run(capsys):
    """舊行為留下的 `defaults: <m>: disable`：exporter 不服務這份載體（da-guard
    驗證點名它），所以本工具在寫入之前就拒絕整次執行、點名它、rc 2（#3206），
    不再替它修——要先手動修好。"""
    with tempfile.TemporaryDirectory() as d:
        make_confdir(d, {
            "_defaults.yaml": {"defaults": {"mysql_connections": "disable",
                                            "mem_usage": 90}},
        })
        before = open(os.path.join(d, "_defaults.yaml"), "rb").read()
        with pytest.raises(SystemExit) as ei:
            deprecate_rule.remove_from_all_defaults("mysql_connections", d, execute=True)
        assert ei.value.code == 2
        assert "ERROR: _defaults.yaml:" in capsys.readouterr().err
        assert open(os.path.join(d, "_defaults.yaml"), "rb").read() == before

def test_remove_from_defaults_no_related_key_is_a_named_noop():
    """載體沒有任何相關 key ⇒ 具名 no-op，一個位元組都不寫。"""
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "_defaults.yaml")
        make_confdir(d, {"_defaults.yaml": {"defaults": {"mem_usage": 90}}})
        before = open(path, "rb").read()
        ok, msg = _only_carrier(deprecate_rule.remove_from_all_defaults(
            "mysql_connections", d, execute=True))
        assert ok is True
        assert "沒有 mysql_connections 相關 key" in msg and "略過" in msg
        assert open(path, "rb").read() == before

def test_remove_from_defaults_missing_defaults_file():
    """沒有載體 ⇒ 回正典路徑那一筆，ok=False。"""
    with tempfile.TemporaryDirectory() as d:
        ok, msg = _only_carrier(deprecate_rule.remove_from_all_defaults(
            "m", d, execute=False))
        assert ok is False
        assert "_defaults.yaml 不存在" in msg


# ===================================================================
# 6b. deprecate_rule — 租戶平面述詞（#1787）
# ===================================================================

def test_tenant_key_belongs_to_metric_accepts_exactly_the_metrics_own_shapes():
    """接受集合**恰為**「四個確切名字 + 它們的維度形狀」。

    只列舉被拒絕的 key 測不出述詞太寬，所以這條釘的是被接受的集合。
    """
    belongs = deprecate_rule.tenant_key_belongs_to_metric
    for key in ("container_cpu", "container_cpu_critical",
                "custom_container_cpu", "custom_container_cpu_critical",
                'container_cpu{pod="x"}',
                'custom_container_cpu_critical{pod="x",ns="y"}'):
        assert belongs(key, "container_cpu"), key


def test_tenant_key_belongs_to_metric_rejects_a_neighbour_that_merely_shares_the_prefix():
    """`container_cpu_throttle*` 是**另一個指標**，不是 `container_cpu` 的。

    子字串比對會把它報成引用（永久 rc 1），移除端更會刪到它的維度鍵。
    """
    belongs = deprecate_rule.tenant_key_belongs_to_metric
    for key in ("container_cpu_throttle", "container_cpu_throttle_critical",
                'container_cpu_throttle{pod="x"}',
                "custom_container_cpu_throttle_critical",
                "mem_usage", "cpu_usage_backup"):
        assert not belongs(key, "container_cpu"), key


def test_carrier_health_separates_the_three_ways_a_carrier_goes_wrong():
    """產線的載體體檢回三類 kind，因為下場是三件不同的事。

    完整真值表在 `tests/golden/fixtures/defaults-carrier-oracle.json`（Go 側
    `TestDefaultsCarrierOracle` 裁判）；這裡只釘形狀與三個 kind 常數。
    """
    fn = deprecate_rule.carrier_health
    UNP, NULL = deprecate_rule.UNPARSEABLE, deprecate_rule.NULL_NOT_DECLARED

    assert fn(b"defaults:\n  a: 80\n  b: 1.5\n  c: -3\n") == []
    # 整份載體被丟的那一類，key 具名。
    assert fn(b"defaults:\n  a: 80\n  old: disable\n") == [
        ("old", "disable", UNP)]
    assert fn(b"defaults:\n  m:\n    x: 1\n") == [("m", "<mapping>", UNP)]
    assert fn(b"defaults:\n  l:\n  - 1\n") == [("l", "<list>", UNP)]
    # 寫成 null、exporter 當作沒宣告的那一類（#2518；之前是 0 閾值）。
    assert fn(b"defaults:\n  k:\n") == [("k", "(空)", NULL)]
    # 文件層級的那一類，key 是 None。
    assert fn(b"defaults:\n- a\n") == [(None, "defaults 不是 mapping（list）", UNP)]
    got = fn(b"defaults: [\n")
    assert len(got) == 1 and got[0][0] is None and got[0][2] == UNP, got
    # 沒有 `defaults:` 不是本述詞的事。
    assert fn(b"tenants:\n  a: {}\n") == []
    # 讀不到的檔是第三類。
    got = deprecate_rule.carrier_health_at("/nonexistent/_defaults.yaml")
    assert len(got) == 1 and got[0][0] is None and got[0][2] == deprecate_rule.UNREADABLE, got


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
                "mysql_threads_running": 80,
            }}},
        })
        removed = deprecate_rule.remove_from_tenants(
            "mysql_connections", d, execute=True)
        assert len(removed) == 1
        data = deprecate_rule.load_yaml_file(os.path.join(d, "db-a.yaml"))
        assert "mysql_connections" not in data["tenants"]["db-a"]
        assert "mysql_threads_running" in data["tenants"]["db-a"]

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

def test_a_null_sample_is_refused_upstream():
    """#3133：樣本值是 null 時，查詢在上游就被拒（rc 2），不會以「缺值」
    進到 extract_value_map（以前讀成 None，報成單邊缺值、rc 1）。"""
    import _lib_prometheus
    body = {"status": "success", "data": {"resultType": "vector", "result": [
        {"metric": {"tenant": "db-a"}, "value": [0, None]}]}}
    with pytest.raises(_lib_prometheus.QueryError, match=r"result\[0\]\.value"):
        _lib_prometheus._vector_rows(body)


def test_extract_value_map_histogram_row_is_none():
    """只有 histogram 的列沒有浮點樣本，記為 None。"""
    vmap = validate_migration.extract_value_map(
        [{"metric": {"tenant": "db-a"}, "histogram": [0, {}]}])
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


# ---------------------------------------------------------------------------
# #1607 — deprecate_rule's unusable-entry reporting, called IN-PROCESS
# ---------------------------------------------------------------------------
#
# ⛔ The cross-tool harness drives this tool through `subprocess`, which is
# right for asserting operator-visible output but leaves these branches
# unmeasured: `[tool.coverage.run]` sets no `concurrency` /
# `COVERAGE_PROCESS_START`, so a child process is not counted. Measured on the
# PR that added the harness: this file went -2.6%, the largest of the three.


def _confd_with_unusable(tmp_path):
    root = tmp_path / "conf.d"
    root.mkdir()
    (root / "_defaults.yaml").write_text(
        "defaults:\n  pg_connections: 80\n", encoding="utf-8")
    (root / "alpha.yaml").write_text(
        "tenants:\n  alpha:\n    pg_connections: 90\n", encoding="utf-8")
    (root / "notes.yaml").mkdir()
    symlink_or_skip(root / "gone.yaml", (root / "broken.yaml"))
    return root


def _run_main(monkeypatch, argv):
    monkeypatch.setattr(deprecate_rule.sys, "argv", ["deprecate_rule", *argv])
    return deprecate_rule.main()


def test_main_refuses_unusable_entries_once_per_invocation(
        tmp_path, capsys, monkeypatch):
    """⛔ ONCE per run, not once per metric, and before any metric is
    processed: the refusal is the run's (#3206), not a warn-and-continue."""
    root = _confd_with_unusable(tmp_path)
    with pytest.raises(SystemExit) as exc:
        _run_main(monkeypatch,
                  ["pg_connections", "mysql_connections", "redis_memory",
                   "--config-dir", str(root)])
    assert exc.value.code == 2
    cap = capsys.readouterr()
    text = cap.out + cap.err
    for name in ("notes.yaml", "broken.yaml"):
        assert text.count(f"ERROR: {name}:") == 1, text
    assert "(not_regular_file: a directory with a config file name" in text
    assert "Processing:" not in text, text


def test_scan_for_metric_refuses_unusable_entries(tmp_path, capsys):
    """The scan itself must not try to read a directory as a tenant file:
    it refuses the tree (#3206), naming both."""
    root = _confd_with_unusable(tmp_path)
    with pytest.raises(SystemExit) as exc:
        deprecate_rule.scan_for_metric("pg_connections", str(root))
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "ERROR: notes.yaml:" in err and "ERROR: broken.yaml:" in err, err
