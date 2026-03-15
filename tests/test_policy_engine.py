"""
test_policy_engine.py — Policy-as-Code 引擎測試。

覆蓋範圍：
- DSL 運算子（required/forbidden/equals/gte/lte/matches/one_of/contains）
- 條件式規則（when 子句）
- 萬用字元目標
- Tenant 配置載入
- 策略載入（_defaults.yaml / standalone）
- 報告生成（text/JSON）
- CLI 整合
- 邊界情況
"""
import json
import os
import sys
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts", "tools"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts", "tools", "ops"))

import policy_engine as pe


# ═══════════════════════════════════════════════════════════════════
# Fixtures
# ═══════════════════════════════════════════════════════════════════

@pytest.fixture
def sample_config():
    """典型 tenant 配置。"""
    return {
        "mysql_connections": "80",
        "mysql_cpu": "70",
        "container_memory": "85",
        "_routing": {
            "receiver": {
                "type": "webhook",
                "url": "https://hooks.example.com/alerts",
            },
            "group_wait": "30s",
            "repeat_interval": "4h",
        },
        "_metadata": {
            "runbook_url": "https://runbooks.example.com/db-a",
            "owner": "platform-team",
            "tier": "production",
        },
        "_severity_dedup": "enable",
    }


@pytest.fixture
def make_rule():
    """快速建立 PolicyRule 的 factory。"""
    def _make(**kwargs):
        defaults = {
            "name": "test-rule",
            "description": "測試規則",
            "target": "_routing",
            "operator": "required",
        }
        defaults.update(kwargs)
        return pe.PolicyRule(**defaults)
    return _make


# ═══════════════════════════════════════════════════════════════════
# TestPolicyRule
# ═══════════════════════════════════════════════════════════════════

class TestPolicyRule:
    """PolicyRule 資料模型驗證。"""

    def test_valid_rule_creation(self, make_rule):
        """合法規則能正常建立。"""
        rule = make_rule()
        assert rule.name == "test-rule"
        assert rule.severity == "error"

    def test_invalid_operator_raises(self):
        """無效運算子應拋出 ValueError。"""
        with pytest.raises(ValueError, match="unknown operator"):
            pe.PolicyRule(
                name="bad", description="", target="x",
                operator="invalid_op"
            )

    def test_invalid_severity_raises(self):
        """無效嚴重度應拋出 ValueError。"""
        with pytest.raises(ValueError, match="unknown severity"):
            pe.PolicyRule(
                name="bad", description="", target="x",
                operator="required", severity="critical"
            )

    def test_default_values(self, make_rule):
        """預設值正確設定。"""
        rule = make_rule()
        assert rule.value is None
        assert rule.when is None
        assert rule.exclude_tenants == []


# ═══════════════════════════════════════════════════════════════════
# TestResolveTarget
# ═══════════════════════════════════════════════════════════════════

class TestResolveTarget:
    """目標路徑解析測試。"""

    def test_simple_key(self, sample_config):
        """簡單 key 查找成功。"""
        found, val = pe._resolve_target(sample_config, "mysql_connections")
        assert found is True
        assert val == "80"

    def test_dot_path(self, sample_config):
        """dot-path 嵌套查找成功。"""
        found, val = pe._resolve_target(sample_config, "_routing.receiver.type")
        assert found is True
        assert val == "webhook"

    def test_missing_key(self, sample_config):
        """不存在的 key 回傳 (False, None)。"""
        found, val = pe._resolve_target(sample_config, "nonexistent")
        assert found is False
        assert val is None

    def test_missing_nested_key(self, sample_config):
        """不存在的嵌套 key 回傳 (False, None)。"""
        found, val = pe._resolve_target(sample_config, "_routing.nonexistent.deep")
        assert found is False

    def test_wildcard_match(self, sample_config):
        """萬用字元匹配多個 key。"""
        found, val = pe._resolve_target(sample_config, "mysql_*")
        assert found is True
        assert isinstance(val, dict)
        assert "mysql_connections" in val
        assert "mysql_cpu" in val

    def test_wildcard_no_match(self, sample_config):
        """萬用字元無匹配回傳 (False, None)。"""
        found, val = pe._resolve_target(sample_config, "redis_*")
        assert found is False


# ═══════════════════════════════════════════════════════════════════
# TestEvaluateOperator — parametrized
# ═══════════════════════════════════════════════════════════════════

class TestEvaluateOperator:
    """運算子評估測試。"""

    @pytest.mark.parametrize("actual,expected_result", [
        ("hello", True),
        ("", False),
        (None, False),
        ({"a": 1}, True),
        ({}, False),
        ([], False),
        ([1], True),
    ], ids=["string", "empty-string", "none", "dict", "empty-dict", "empty-list", "list"])
    def test_required(self, actual, expected_result):
        """required 運算子正確判斷非空值。"""
        assert pe._evaluate_operator("required", actual, None) == expected_result

    @pytest.mark.parametrize("actual,expected_result", [
        (None, True),
        ("value", False),
        (0, False),
    ], ids=["none", "has-value", "zero"])
    def test_forbidden(self, actual, expected_result):
        """forbidden 運算子正確判斷不存在。"""
        assert pe._evaluate_operator("forbidden", actual, None) == expected_result

    @pytest.mark.parametrize("actual,expected_val,expected_result", [
        ("webhook", "webhook", True),
        ("webhook", "email", False),
        (80, "80", True),
    ], ids=["match", "mismatch", "numeric-string"])
    def test_equals(self, actual, expected_val, expected_result):
        """equals 運算子以字串比較。"""
        assert pe._evaluate_operator("equals", actual, expected_val) == expected_result

    def test_not_equals(self):
        """not_equals 運算子。"""
        assert pe._evaluate_operator("not_equals", "a", "b") is True
        assert pe._evaluate_operator("not_equals", "a", "a") is False

    @pytest.mark.parametrize("op,actual,expected_val,expected_result", [
        ("gte", "80", "70", True),
        ("gte", "70", "70", True),
        ("gte", "60", "70", False),
        ("lte", "60", "70", True),
        ("lte", "70", "70", True),
        ("lte", "80", "70", False),
        ("gt", "80", "70", True),
        ("gt", "70", "70", False),
        ("lt", "60", "70", True),
        ("lt", "70", "70", False),
    ], ids=[
        "gte-above", "gte-equal", "gte-below",
        "lte-below", "lte-equal", "lte-above",
        "gt-above", "gt-equal",
        "lt-below", "lt-equal",
    ])
    def test_comparison_numeric(self, op, actual, expected_val, expected_result):
        """數值比較運算子。"""
        assert pe._evaluate_operator(op, actual, expected_val) == expected_result

    @pytest.mark.parametrize("actual,expected_val,expected_result", [
        ("5m", "4m", True),    # 300 >= 240
        ("30s", "1m", False),  # 30 < 60
        ("1h", "30m", True),   # 3600 >= 1800
    ], ids=["5m>=4m", "30s<1m", "1h>=30m"])
    def test_comparison_duration(self, actual, expected_val, expected_result):
        """Duration 比較運算子。"""
        assert pe._evaluate_operator("gte", actual, expected_val) == expected_result

    def test_matches(self):
        """matches 正則匹配。"""
        assert pe._evaluate_operator("matches", "https://hooks.example.com", r"^https://") is True
        assert pe._evaluate_operator("matches", "http://unsafe.com", r"^https://") is False

    def test_matches_invalid_regex(self):
        """無效正則不崩潰，回傳 False。"""
        assert pe._evaluate_operator("matches", "test", r"[invalid") is False

    def test_one_of(self):
        """one_of 清單匹配。"""
        assert pe._evaluate_operator("one_of", "webhook", ["webhook", "email", "slack"]) is True
        assert pe._evaluate_operator("one_of", "teams", ["webhook", "email"]) is False

    def test_one_of_non_list(self):
        """one_of 非清單參數回傳 False。"""
        assert pe._evaluate_operator("one_of", "a", "a") is False

    def test_contains(self):
        """contains 字串包含。"""
        assert pe._evaluate_operator("contains", "hello world", "world") is True
        assert pe._evaluate_operator("contains", "hello", "world") is False

    def test_non_numeric_comparison(self):
        """非數值比較回傳 False。"""
        assert pe._evaluate_operator("gte", "abc", "def") is False


# ═══════════════════════════════════════════════════════════════════
# TestEvaluateWhen
# ═══════════════════════════════════════════════════════════════════

class TestEvaluateWhen:
    """when 條件子句測試。"""

    def test_when_required_true(self, sample_config):
        """when required 條件成立。"""
        when = {"target": "_routing", "operator": "required"}
        assert pe._evaluate_when(sample_config, when) is True

    def test_when_required_false(self, sample_config):
        """when required 條件不成立。"""
        when = {"target": "_nonexistent", "operator": "required"}
        assert pe._evaluate_when(sample_config, when) is False

    def test_when_equals(self, sample_config):
        """when equals 條件判斷。"""
        when = {"target": "_severity_dedup", "operator": "equals", "value": "enable"}
        assert pe._evaluate_when(sample_config, when) is True

    def test_when_empty_target(self, sample_config):
        """when 空 target 預設通過。"""
        when = {"target": "", "operator": "required"}
        assert pe._evaluate_when(sample_config, when) is True

    def test_when_forbidden(self, sample_config):
        """when forbidden 條件。"""
        when = {"target": "_routing", "operator": "forbidden"}
        assert pe._evaluate_when(sample_config, when) is False


# ═══════════════════════════════════════════════════════════════════
# TestEvaluateRule
# ═══════════════════════════════════════════════════════════════════

class TestEvaluateRule:
    """單一規則評估測試。"""

    def test_required_pass(self, make_rule, sample_config):
        """必填欄位存在 → 通過。"""
        rule = make_rule(target="_routing", operator="required")
        violations = pe.evaluate_rule(rule, "db-a", sample_config)
        assert len(violations) == 0

    def test_required_fail(self, make_rule, sample_config):
        """必填欄位不存在 → 違規。"""
        rule = make_rule(target="_nonexistent", operator="required")
        violations = pe.evaluate_rule(rule, "db-a", sample_config)
        assert len(violations) == 1
        assert violations[0].severity == "error"

    def test_dot_path_equals(self, make_rule, sample_config):
        """嵌套路徑 equals 檢查。"""
        rule = make_rule(
            target="_routing.receiver.type",
            operator="equals", value="webhook"
        )
        violations = pe.evaluate_rule(rule, "db-a", sample_config)
        assert len(violations) == 0

    def test_dot_path_equals_fail(self, make_rule, sample_config):
        """嵌套路徑 equals 不匹配 → 違規。"""
        rule = make_rule(
            target="_routing.receiver.type",
            operator="equals", value="pagerduty"
        )
        violations = pe.evaluate_rule(rule, "db-a", sample_config)
        assert len(violations) == 1

    def test_when_condition_skips(self, make_rule, sample_config):
        """when 條件不成立 → 跳過規則。"""
        rule = make_rule(
            target="_routing.receiver.type",
            operator="equals", value="pagerduty",
            when={"target": "_nonexistent", "operator": "required"},
        )
        violations = pe.evaluate_rule(rule, "db-a", sample_config)
        assert len(violations) == 0

    def test_when_condition_evaluates(self, make_rule, sample_config):
        """when 條件成立 → 評估規則。"""
        rule = make_rule(
            target="_routing.receiver.type",
            operator="equals", value="pagerduty",
            when={"target": "_severity_dedup", "operator": "equals", "value": "enable"},
        )
        violations = pe.evaluate_rule(rule, "db-a", sample_config)
        assert len(violations) == 1

    def test_exclude_tenant(self, make_rule, sample_config):
        """排除的 tenant 不評估。"""
        rule = make_rule(
            target="_nonexistent", operator="required",
            exclude_tenants=["db-a"],
        )
        violations = pe.evaluate_rule(rule, "db-a", sample_config)
        assert len(violations) == 0

    def test_wildcard_target_pass(self, make_rule, sample_config):
        """萬用字元匹配全部通過。"""
        rule = make_rule(target="mysql_*", operator="lte", value="100")
        violations = pe.evaluate_rule(rule, "db-a", sample_config)
        assert len(violations) == 0

    def test_wildcard_target_fail(self, make_rule, sample_config):
        """萬用字元匹配部分違規。"""
        rule = make_rule(target="mysql_*", operator="lte", value="50")
        violations = pe.evaluate_rule(rule, "db-a", sample_config)
        assert len(violations) == 2  # mysql_connections=80, mysql_cpu=70

    def test_wildcard_required_no_match(self, make_rule, sample_config):
        """萬用字元 required 無匹配 → 違規。"""
        rule = make_rule(target="redis_*", operator="required")
        violations = pe.evaluate_rule(rule, "db-a", sample_config)
        assert len(violations) == 1

    def test_forbidden_pass(self, make_rule):
        """forbidden 欄位不存在 → 通過。"""
        rule = make_rule(target="_dangerous", operator="forbidden")
        violations = pe.evaluate_rule(rule, "db-a", {"mysql_cpu": "80"})
        assert len(violations) == 0

    def test_forbidden_fail(self, make_rule, sample_config):
        """forbidden 欄位存在 → 違規。"""
        rule = make_rule(target="_routing", operator="forbidden")
        violations = pe.evaluate_rule(rule, "db-a", sample_config)
        assert len(violations) == 1

    def test_duration_gte(self, make_rule, sample_config):
        """Duration gte 比較。"""
        rule = make_rule(
            target="_routing.repeat_interval",
            operator="gte", value="1h",
        )
        violations = pe.evaluate_rule(rule, "db-a", sample_config)
        assert len(violations) == 0  # 4h >= 1h

    def test_matches_url_pattern(self, make_rule, sample_config):
        """matches regex 驗證 HTTPS URL。"""
        rule = make_rule(
            target="_routing.receiver.url",
            operator="matches", value=r"^https://",
        )
        violations = pe.evaluate_rule(rule, "db-a", sample_config)
        assert len(violations) == 0

    def test_one_of_receiver_type(self, make_rule, sample_config):
        """one_of 驗證 receiver type。"""
        rule = make_rule(
            target="_routing.receiver.type",
            operator="one_of",
            value=["webhook", "pagerduty", "slack"],
        )
        violations = pe.evaluate_rule(rule, "db-a", sample_config)
        assert len(violations) == 0

    def test_nonexistent_target_non_required(self, make_rule):
        """不存在的目標 + 非 required/forbidden 運算子 → 靜默跳過。"""
        rule = make_rule(
            target="_routing.receiver.type",
            operator="equals", value="webhook",
        )
        violations = pe.evaluate_rule(rule, "db-a", {"mysql_cpu": "80"})
        assert len(violations) == 0  # No _routing → skip


# ═══════════════════════════════════════════════════════════════════
# TestEvaluatePolicies
# ═══════════════════════════════════════════════════════════════════

class TestEvaluatePolicies:
    """多 tenant 多規則評估測試。"""

    def test_all_pass(self, make_rule, sample_config):
        """所有 tenant 通過所有規則。"""
        rules = [
            make_rule(name="r1", target="_routing", operator="required"),
            make_rule(name="r2", target="_metadata", operator="required"),
        ]
        result = pe.evaluate_policies(rules, {"db-a": sample_config})
        assert result.passed is True
        assert result.error_count == 0
        assert result.tenants_evaluated == 1
        assert result.rules_evaluated == 2

    def test_mixed_violations(self, make_rule, sample_config):
        """混合違規結果。"""
        rules = [
            make_rule(name="r1", target="_routing", operator="required"),
            make_rule(name="r2", target="_nonexistent", operator="required", severity="warning"),
        ]
        configs = {
            "db-a": sample_config,
            "db-b": {"mysql_cpu": "80"},  # 缺少 _routing 和 _nonexistent
        }
        result = pe.evaluate_policies(rules, configs)
        assert result.tenants_evaluated == 2
        # db-a: r1 pass, r2 warning | db-b: r1 error, r2 warning
        assert result.error_count == 1
        assert result.warning_count == 2

    def test_empty_tenants(self, make_rule):
        """空 tenant 清單。"""
        rules = [make_rule()]
        result = pe.evaluate_policies(rules, {})
        assert result.passed is True
        assert result.tenants_evaluated == 0


# ═══════════════════════════════════════════════════════════════════
# TestLoadPolicies
# ═══════════════════════════════════════════════════════════════════

class TestLoadPolicies:
    """策略載入測試。"""

    def test_load_from_defaults(self, tmp_path):
        """從 _defaults.yaml 的 _policies section 載入。"""
        policy_file = tmp_path / "_defaults.yaml"
        policy_file.write_text("""
defaults:
  mysql_cpu: 80

_policies:
  - name: routing-required
    description: "所有租戶必須配置路由"
    target: _routing
    operator: required
    severity: error
  - name: metadata-recommended
    description: "建議配置 metadata"
    target: _metadata
    operator: required
    severity: warning
""", encoding="utf-8")

        rules = pe.load_policies(str(policy_file))
        assert len(rules) == 2
        assert rules[0].name == "routing-required"
        assert rules[0].severity == "error"
        assert rules[1].severity == "warning"

    def test_load_from_standalone(self, tmp_path):
        """從獨立 policy 檔案載入。"""
        policy_file = tmp_path / "policies.yaml"
        policy_file.write_text("""
policies:
  - name: https-only
    description: "Webhook must use HTTPS"
    target: _routing.receiver.url
    operator: matches
    value: "^https://"
""", encoding="utf-8")

        rules = pe.load_policies(str(policy_file))
        assert len(rules) == 1
        assert rules[0].name == "https-only"

    def test_load_nonexistent_file(self):
        """不存在的檔案回傳空清單。"""
        rules = pe.load_policies("/nonexistent/file.yaml")
        assert rules == []

    def test_load_no_policies_key(self, tmp_path):
        """沒有 policies 或 _policies key 回傳空清單。"""
        f = tmp_path / "empty.yaml"
        f.write_text("defaults:\n  mysql_cpu: 80\n", encoding="utf-8")
        rules = pe.load_policies(str(f))
        assert rules == []

    def test_load_invalid_rule_skipped(self, tmp_path):
        """無效規則被跳過，有效規則正常載入。"""
        f = tmp_path / "mixed.yaml"
        f.write_text("""
policies:
  - name: valid-rule
    description: test
    target: _routing
    operator: required
  - name: bad-rule
    description: test
    target: x
    operator: INVALID_OP
""", encoding="utf-8")
        rules = pe.load_policies(str(f))
        assert len(rules) == 1
        assert rules[0].name == "valid-rule"

    def test_load_with_when_clause(self, tmp_path):
        """載入含 when 子句的規則。"""
        f = tmp_path / "conditional.yaml"
        f.write_text("""
policies:
  - name: critical-pagerduty
    description: "Critical needs PagerDuty"
    target: _routing.receiver.type
    operator: equals
    value: pagerduty
    when:
      target: _metadata.tier
      operator: equals
      value: production
""", encoding="utf-8")
        rules = pe.load_policies(str(f))
        assert len(rules) == 1
        assert rules[0].when is not None
        assert rules[0].when["target"] == "_metadata.tier"


# ═══════════════════════════════════════════════════════════════════
# TestLoadTenantConfigs
# ═══════════════════════════════════════════════════════════════════

class TestLoadTenantConfigs:
    """Tenant 配置載入測試。"""

    def test_flat_format(self, tmp_path):
        """Flat 格式 YAML — 檔名即 tenant。"""
        (tmp_path / "db-a.yaml").write_text(
            "mysql_cpu: '80'\n_routing:\n  receiver:\n    type: webhook\n",
            encoding="utf-8",
        )
        configs = pe.load_tenant_configs(str(tmp_path))
        assert "db-a" in configs
        assert configs["db-a"]["mysql_cpu"] == "80"

    def test_multi_tenant_wrapper(self, tmp_path):
        """Multi-tenant wrapper 格式。"""
        (tmp_path / "cluster.yaml").write_text("""
tenants:
  prod-db:
    mysql_cpu: '90'
  prod-cache:
    redis_memory: '80'
""", encoding="utf-8")
        configs = pe.load_tenant_configs(str(tmp_path))
        assert "prod-db" in configs
        assert "prod-cache" in configs

    def test_skip_defaults(self, tmp_path):
        """跳過 _ 開頭的檔案。"""
        (tmp_path / "_defaults.yaml").write_text(
            "defaults:\n  mysql_cpu: 80\n", encoding="utf-8"
        )
        (tmp_path / "db-a.yaml").write_text(
            "mysql_cpu: '70'\n", encoding="utf-8"
        )
        configs = pe.load_tenant_configs(str(tmp_path))
        assert "db-a" in configs
        assert "_defaults" not in configs

    def test_nonexistent_dir(self):
        """不存在的目錄回傳空 dict。"""
        configs = pe.load_tenant_configs("/nonexistent/dir")
        assert configs == {}


# ═══════════════════════════════════════════════════════════════════
# TestReports
# ═══════════════════════════════════════════════════════════════════

class TestReports:
    """報告生成測試。"""

    def test_text_report_pass(self):
        """通過時的文字報告。"""
        result = pe.PolicyResult(tenants_evaluated=2, rules_evaluated=3)
        text = pe.generate_text_report(result, "en")
        assert "All policies passed" in text
        assert "Tenants: 2" in text

    def test_text_report_fail(self):
        """違規時的文字報告。"""
        result = pe.PolicyResult(
            tenants_evaluated=1, rules_evaluated=1,
            violations=[pe.Violation(
                tenant="db-a", rule_name="test", description="desc",
                severity="error", target="_routing", message="missing",
            )],
        )
        text = pe.generate_text_report(result, "en")
        assert "db-a" in text
        assert "✗" in text
        assert "FAIL" in text

    def test_text_report_zh(self):
        """中文報告。"""
        result = pe.PolicyResult(tenants_evaluated=1, rules_evaluated=1)
        text = pe.generate_text_report(result, "zh")
        assert "策略評估報告" in text
        assert "所有策略均通過" in text

    def test_text_report_warning_icon(self):
        """警告用 ⚠ 圖示。"""
        result = pe.PolicyResult(
            tenants_evaluated=1, rules_evaluated=1,
            violations=[pe.Violation(
                tenant="db-a", rule_name="test", description="desc",
                severity="warning", target="_metadata", message="missing",
            )],
        )
        text = pe.generate_text_report(result, "en")
        assert "⚠" in text

    def test_json_report(self):
        """JSON 報告結構正確。"""
        result = pe.PolicyResult(
            tenants_evaluated=2, rules_evaluated=3,
            violations=[pe.Violation(
                tenant="db-a", rule_name="r1", description="d",
                severity="error", target="t", message="m",
            )],
        )
        report = pe.generate_json_report(result)
        assert report["tenants_evaluated"] == 2
        assert report["passed"] is False
        assert len(report["violations"]) == 1
        assert report["violations"][0]["tenant"] == "db-a"

    def test_json_report_passed(self):
        """JSON 報告 — 全通過。"""
        result = pe.PolicyResult(tenants_evaluated=1, rules_evaluated=1)
        report = pe.generate_json_report(result)
        assert report["passed"] is True
        assert report["violations"] == []


# ═══════════════════════════════════════════════════════════════════
# TestCLI
# ═══════════════════════════════════════════════════════════════════

class TestCLI:
    """CLI 整合測試。"""

    def test_main_no_policies(self, tmp_path, capsys):
        """無策略規則 → 正常退出。"""
        (tmp_path / "db-a.yaml").write_text("mysql_cpu: '80'\n", encoding="utf-8")
        exit_code = pe.main(["--config-dir", str(tmp_path)])
        assert exit_code == 0
        assert "No policy rules found" in capsys.readouterr().out

    def test_main_all_pass(self, tmp_path, capsys):
        """所有策略通過 → exit 0。"""
        (tmp_path / "_defaults.yaml").write_text("""
_policies:
  - name: cpu-exists
    description: "CPU threshold must exist"
    target: mysql_cpu
    operator: required
""", encoding="utf-8")
        (tmp_path / "db-a.yaml").write_text("mysql_cpu: '80'\n", encoding="utf-8")
        exit_code = pe.main(["--config-dir", str(tmp_path)])
        assert exit_code == 0

    def test_main_ci_fail(self, tmp_path, capsys):
        """CI 模式下有 error → exit 1。"""
        (tmp_path / "_defaults.yaml").write_text("""
_policies:
  - name: routing-required
    description: "Routing required"
    target: _routing
    operator: required
    severity: error
""", encoding="utf-8")
        (tmp_path / "db-a.yaml").write_text("mysql_cpu: '80'\n", encoding="utf-8")
        exit_code = pe.main(["--config-dir", str(tmp_path), "--ci"])
        assert exit_code == 1

    def test_main_ci_warning_only_pass(self, tmp_path, capsys):
        """CI 模式下只有 warning → exit 0。"""
        (tmp_path / "_defaults.yaml").write_text("""
_policies:
  - name: metadata-recommended
    description: "Metadata recommended"
    target: _metadata
    operator: required
    severity: warning
""", encoding="utf-8")
        (tmp_path / "db-a.yaml").write_text("mysql_cpu: '80'\n", encoding="utf-8")
        exit_code = pe.main(["--config-dir", str(tmp_path), "--ci"])
        assert exit_code == 0

    def test_main_json_output(self, tmp_path, capsys):
        """JSON 輸出格式。"""
        (tmp_path / "_defaults.yaml").write_text("""
_policies:
  - name: test
    description: test
    target: mysql_cpu
    operator: required
""", encoding="utf-8")
        (tmp_path / "db-a.yaml").write_text("mysql_cpu: '80'\n", encoding="utf-8")
        exit_code = pe.main(["--config-dir", str(tmp_path), "--json"])
        assert exit_code == 0
        output = capsys.readouterr().out
        data = json.loads(output)
        assert data["passed"] is True

    def test_main_standalone_policy(self, tmp_path, capsys):
        """獨立 policy 檔案載入。"""
        policy = tmp_path / "policy.yaml"
        policy.write_text("""
policies:
  - name: https-only
    description: "HTTPS only"
    target: _routing.receiver.url
    operator: matches
    value: "^https://"
""", encoding="utf-8")
        (tmp_path / "db-a.yaml").write_text(
            "_routing:\n  receiver:\n    url: https://safe.com\n",
            encoding="utf-8",
        )
        exit_code = pe.main([
            "--config-dir", str(tmp_path),
            "--policy", str(policy),
        ])
        assert exit_code == 0

    def test_main_no_tenants(self, tmp_path, capsys):
        """無 tenant 配置 → 正常退出。"""
        (tmp_path / "_defaults.yaml").write_text("""
_policies:
  - name: test
    description: test
    target: x
    operator: required
""", encoding="utf-8")
        exit_code = pe.main(["--config-dir", str(tmp_path)])
        assert exit_code == 0


# ═══════════════════════════════════════════════════════════════════
# TestFormatViolationMsg
# ═══════════════════════════════════════════════════════════════════

class TestFormatViolationMsg:
    """違規訊息格式化測試。"""

    @pytest.mark.parametrize("operator,target,actual,expected,substring", [
        ("required", "_routing", None, None, "必填"),
        ("forbidden", "_dangerous", "yes", None, "禁止"),
        ("equals", "type", "email", "webhook", "期望等於"),
        ("not_equals", "type", "email", "email", "不得等於"),
        ("gte", "cpu", "50", "80", "≥"),
        ("lte", "cpu", "90", "80", "≤"),
        ("matches", "url", "http://x", "^https://", "匹配"),
        ("one_of", "type", "teams", ["webhook", "email"], "之一"),
        ("contains", "url", "foo.com", "example", "包含"),
    ], ids=[
        "required", "forbidden", "equals", "not_equals",
        "gte", "lte", "matches", "one_of", "contains",
    ])
    def test_message_format(self, operator, target, actual, expected, substring):
        """各運算子訊息格式正確包含關鍵字。"""
        msg = pe._format_violation_msg(operator, target, actual, expected)
        assert substring in msg


# ═══════════════════════════════════════════════════════════════════
# TestToComparable
# ═══════════════════════════════════════════════════════════════════

class TestToComparable:
    """值轉換測試。"""

    @pytest.mark.parametrize("value,expected_type", [
        (42, float),
        (3.14, float),
        ("100", float),
        ("5m", float),
        ("1h", float),
        ("abc", str),
    ], ids=["int", "float", "numeric-str", "duration-5m", "duration-1h", "non-numeric"])
    def test_type_conversion(self, value, expected_type):
        """正確轉換為可比較型別。"""
        result = pe._to_comparable(value)
        assert isinstance(result, expected_type)

    def test_duration_value(self):
        """Duration 轉換為正確秒數。"""
        assert pe._to_comparable("5m") == 300.0
        assert pe._to_comparable("1h") == 3600.0
