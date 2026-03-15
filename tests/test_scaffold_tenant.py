"""Unit tests for scaffold_tenant.py.

涵蓋核心可測試函式（非互動模式路徑）：
- build_receiver_from_args: CLI 參數 → receiver dict 轉換
- generate_defaults: 平台預設值產生
- generate_tenant: Tenant YAML 產生（non-interactive）
- generate_profile: Profile 框架產生
- generate_report: Scaffold report 產生
- generate_relabel_snippet: Prometheus relabel_configs snippet 產生
- write_outputs: 檔案輸出驗證
- RULE_PACKS 常數完整性
"""
import os
import tempfile

import pytest
import yaml

from scaffold_tenant import (
    build_receiver_from_args,
    generate_defaults,
    generate_tenant,
    generate_profile,
    generate_report,
    generate_relabel_snippet,
    write_outputs,
    RULE_PACKS,
)


# ============================================================
# build_receiver_from_args
# ============================================================
class TestBuildReceiverFromArgs:
    """build_receiver_from_args() CLI 參數轉換測試。"""

    def test_webhook(self):
        """Webhook receiver 正確建立。"""
        obj = build_receiver_from_args("webhook", "https://hooks.example.com/alert")
        assert obj == {"type": "webhook", "url": "https://hooks.example.com/alert"}

    def test_slack(self):
        """Slack receiver 使用 api_url 欄位。"""
        obj = build_receiver_from_args("slack", "https://hooks.slack.com/T/B/X")
        assert obj["type"] == "slack"
        assert obj["api_url"] == "https://hooks.slack.com/T/B/X"

    def test_email_with_smarthost(self):
        """Email receiver 包含 to 清單和 smarthost。"""
        obj = build_receiver_from_args(
            "email", "admin@example.com,ops@example.com",
            smarthost="smtp.example.com:587")
        assert obj["type"] == "email"
        assert obj["to"] == ["admin@example.com", "ops@example.com"]
        assert obj["smarthost"] == "smtp.example.com:587"

    def test_email_default_smarthost(self):
        """Email receiver 缺少 smarthost 時使用預設值。"""
        obj = build_receiver_from_args("email", "admin@example.com")
        assert obj["smarthost"] == "localhost:25"

    def test_teams(self):
        """Teams receiver 使用 webhook_url 欄位。"""
        obj = build_receiver_from_args("teams", "https://outlook.office.com/webhook/test")
        assert obj["type"] == "teams"
        assert obj["webhook_url"] == "https://outlook.office.com/webhook/test"

    def test_pagerduty(self):
        """PagerDuty receiver 使用 service_key 欄位。"""
        obj = build_receiver_from_args("pagerduty", "abc123")
        assert obj["type"] == "pagerduty"
        assert obj["service_key"] == "abc123"

    def test_rocketchat(self):
        """Rocket.Chat receiver 使用 url 欄位。"""
        obj = build_receiver_from_args("rocketchat", "https://chat.example.com/hooks/abc")
        assert obj["type"] == "rocketchat"
        assert obj["url"] == "https://chat.example.com/hooks/abc"


# ============================================================
# generate_defaults
# ============================================================
class TestGenerateDefaults:
    """generate_defaults() 測試。"""

    def test_includes_kubernetes(self):
        """kubernetes defaults 永遠包含。"""
        result = generate_defaults(["kubernetes"])
        assert "defaults" in result
        assert "container_cpu" in result["defaults"]
        assert "container_memory" in result["defaults"]

    def test_includes_state_filters(self):
        """state_filters 從 kubernetes pack 提取。"""
        result = generate_defaults(["kubernetes"])
        assert "state_filters" in result
        assert "container_crashloop" in result["state_filters"]

    def test_adds_db_defaults(self):
        """指定的 DB pack defaults 合併到結果中。"""
        result = generate_defaults(["kubernetes", "postgresql"])
        assert "pg_connections" in result["defaults"]
        assert "pg_replication_lag" in result["defaults"]
        # kubernetes defaults 也在
        assert "container_cpu" in result["defaults"]

    def test_unknown_db_ignored(self):
        """未知的 DB 類型不造成錯誤。"""
        result = generate_defaults(["kubernetes", "nonexistent_db"])
        assert "container_cpu" in result["defaults"]


# ============================================================
# generate_tenant
# ============================================================
class TestGenerateTenant:
    """generate_tenant() non-interactive 模式測試。"""

    def test_basic_structure(self):
        """產生正確的 tenants 結構。"""
        result = generate_tenant("db-c", ["kubernetes", "mariadb"], interactive=False)
        assert "tenants" in result
        assert "db-c" in result["tenants"]

    def test_non_interactive_empty_overrides(self):
        """Non-interactive 模式不產生 metric overrides（繼承 defaults）。"""
        result = generate_tenant("db-c", ["kubernetes"], interactive=False)
        tenant = result["tenants"]["db-c"]
        # Non-interactive 不產生 metric key overrides
        assert "container_cpu" not in tenant

    def test_unknown_db_skipped(self):
        """未知 DB 類型不影響產生。"""
        result = generate_tenant("db-c", ["nonexistent"], interactive=False)
        assert "tenants" in result


# ============================================================
# generate_profile
# ============================================================
class TestGenerateProfile:
    """generate_profile() 測試。"""

    def test_basic_profile(self):
        """產生正確的 profiles 結構。"""
        result = generate_profile("std-pg-prod", ["postgresql"])
        assert "profiles" in result
        assert "std-pg-prod" in result["profiles"]
        profile = result["profiles"]["std-pg-prod"]
        assert "pg_connections" in profile

    def test_prod_tier(self):
        """prod tier 使用原始閾值。"""
        result = generate_profile("std-pg-prod", ["postgresql"], tier="prod")
        profile = result["profiles"]["std-pg-prod"]
        # pg_connections 預設 80，prod 不調整
        assert profile["pg_connections"] == 80

    def test_staging_tier_relaxed(self):
        """staging tier 放寬 20%。"""
        result = generate_profile("std-pg-staging", ["postgresql"], tier="staging")
        profile = result["profiles"]["std-pg-staging"]
        # pg_connections 預設 80，staging → 80 * 1.2 = 96
        assert profile["pg_connections"] == 96

    def test_includes_optional_overrides(self):
        """包含 optional_overrides 的欄位（critical tiers）。"""
        result = generate_profile("std-pg-prod", ["postgresql"])
        profile = result["profiles"]["std-pg-prod"]
        assert "pg_connections_critical" in profile


# ============================================================
# generate_report
# ============================================================
class TestGenerateReport:
    """generate_report() 測試。"""

    def test_basic_report(self):
        """Report 包含基本結構。"""
        report = generate_report("db-c", ["kubernetes", "mariadb"], "/tmp/out")
        assert "db-c" in report
        assert "scaffold-report" not in report  # 不包含自身檔名
        assert "部署指令" in report
        assert "驗證" in report

    def test_includes_rule_packs(self):
        """Report 列出已選擇的 Rule Packs。"""
        report = generate_report("db-c", ["kubernetes", "mariadb"], "/tmp/out")
        assert "已預載" in report

    def test_includes_namespaces(self):
        """指定 namespaces 時包含 relabel 段落。"""
        report = generate_report("db-c", ["kubernetes"], "/tmp/out", namespaces="ns1,ns2")
        assert "N:1 Tenant Mapping" in report
        assert "relabel" in report.lower()

    def test_no_namespaces(self):
        """未指定 namespaces 時不包含 relabel 段落。"""
        report = generate_report("db-c", ["kubernetes"], "/tmp/out")
        assert "N:1 Tenant Mapping" not in report


# ============================================================
# generate_relabel_snippet
# ============================================================
class TestGenerateRelabelSnippet:
    """generate_relabel_snippet() 測試。"""

    def test_basic_snippet(self):
        """產生包含 keep + replacement 的 relabel_configs。"""
        snippet = generate_relabel_snippet("db-c", "ns1,ns2")
        assert "relabel_configs" in snippet
        assert "ns1|ns2" in snippet
        assert "db-c" in snippet

    def test_list_input(self):
        """接受 list 格式的 namespaces。"""
        snippet = generate_relabel_snippet("db-c", ["ns1", "ns2", "ns3"])
        assert "ns1|ns2|ns3" in snippet

    def test_empty_namespaces(self):
        """空 namespaces 回傳空字串。"""
        assert generate_relabel_snippet("db-c", "") == ""
        assert generate_relabel_snippet("db-c", []) == ""

    def test_custom_tenant_label(self):
        """自訂 tenant_label。"""
        snippet = generate_relabel_snippet("db-c", "ns1", tenant_label="instance")
        assert "instance" in snippet

    def test_valid_yaml(self):
        """產生的 snippet 是合法 YAML。"""
        snippet = generate_relabel_snippet("db-c", "ns1,ns2")
        # 移除 comment 行後解析
        yaml_content = "\n".join(
            line for line in snippet.split("\n") if not line.startswith("#"))
        parsed = yaml.safe_load(yaml_content)
        assert "relabel_configs" in parsed
        assert len(parsed["relabel_configs"]) == 2


# ============================================================
# write_outputs
# ============================================================
class TestWriteOutputs:
    """write_outputs() 檔案輸出測試。"""

    def test_creates_files(self):
        """正確建立所有輸出檔案。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            defaults = {"defaults": {"container_cpu": 80}}
            tenant = {"tenants": {"db-c": {"container_cpu": "70"}}}
            report = "# Test Report"
            write_outputs(tmpdir, "db-c", defaults, tenant, report)

            assert os.path.isfile(os.path.join(tmpdir, "_defaults.yaml"))
            assert os.path.isfile(os.path.join(tmpdir, "db-c.yaml"))
            assert os.path.isfile(os.path.join(tmpdir, "scaffold-report.txt"))

    def test_secure_permissions(self):
        """輸出檔案權限為 0o600。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            defaults = {"defaults": {}}
            tenant = {"tenants": {"db-c": {}}}
            write_outputs(tmpdir, "db-c", defaults, tenant, "report")
            for fn in ["_defaults.yaml", "db-c.yaml", "scaffold-report.txt"]:
                path = os.path.join(tmpdir, fn)
                mode = os.stat(path).st_mode & 0o777
                assert mode == 0o600, f"{fn} permissions {oct(mode)} != 0o600"

    def test_creates_relabel_file(self):
        """提供 relabel_snippet 時建立 relabel 檔案。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            defaults = {"defaults": {}}
            tenant = {"tenants": {"db-c": {}}}
            snippet = "# relabel\nrelabel_configs: []"
            write_outputs(tmpdir, "db-c", defaults, tenant, "report",
                          relabel_snippet=snippet)
            relabel_path = os.path.join(tmpdir, "relabel_configs-db-c.yaml")
            assert os.path.isfile(relabel_path)

    def test_yaml_content_valid(self):
        """輸出的 YAML 檔案內容正確。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            defaults = {"defaults": {"container_cpu": 80}}
            tenant = {"tenants": {"db-c": {"container_cpu": "70"}}}
            write_outputs(tmpdir, "db-c", defaults, tenant, "report")

            with open(os.path.join(tmpdir, "db-c.yaml"), encoding="utf-8") as f:
                parsed = yaml.safe_load(f)
            assert parsed["tenants"]["db-c"]["container_cpu"] == "70"


# ============================================================
# RULE_PACKS 常數完整性
# ============================================================
class TestRulePacksIntegrity:
    """RULE_PACKS 常數結構驗證。"""

    def test_kubernetes_always_present(self):
        """kubernetes pack 永遠存在。"""
        assert "kubernetes" in RULE_PACKS

    def test_all_packs_have_required_keys(self):
        """每個 pack 包含必要欄位。"""
        required = {"display", "exporter", "defaults", "rule_pack_file"}
        for name, pack in RULE_PACKS.items():
            missing = required - set(pack.keys())
            assert not missing, f"RULE_PACKS['{name}'] 缺少: {missing}"

    def test_defaults_have_value_and_unit(self):
        """每個 defaults entry 包含 value 和 unit。"""
        for name, pack in RULE_PACKS.items():
            for key, info in pack.get("defaults", {}).items():
                assert "value" in info, f"{name}.defaults.{key} 缺少 value"
                assert "unit" in info, f"{name}.defaults.{key} 缺少 unit"

    @pytest.mark.parametrize("db", [
        k for k in RULE_PACKS
        if k not in ("kubernetes", "mariadb")  # mariadb 因 MySQL 共用而 default_on
    ])
    def test_db_packs_not_default_on(self, db):
        """大多數 DB packs 預設不啟用（需顯式選擇）。"""
        assert RULE_PACKS[db].get("default_on") is not True


# ============================================================
# generate_defaults 進階測試
# ============================================================
class TestGenerateDefaultsAdvanced:
    """generate_defaults() 進階場景。"""

    def test_multiple_db_packs_merged(self):
        """多 DB pack defaults 全部合併。"""
        result = generate_defaults(["kubernetes", "postgresql", "mariadb"])
        d = result["defaults"]
        assert "container_cpu" in d      # kubernetes
        assert "pg_connections" in d     # postgresql
        assert "mysql_connections" in d  # mariadb (MySQL/MariaDB combo pack)

    def test_empty_db_list_only_kubernetes(self):
        """空 DB list 只產生 kubernetes defaults。"""
        result = generate_defaults(["kubernetes"])
        d = result["defaults"]
        assert "container_cpu" in d
        # 不該有任何 DB 特有 key
        db_keys = {"pg_connections", "mysql_connections", "redis_memory",
                    "mongo_connections", "mssql_cpu"}
        assert not db_keys.intersection(d.keys())

    def test_defaults_values_are_numeric(self):
        """所有 defaults 值為數值型別。"""
        result = generate_defaults(list(RULE_PACKS.keys()))
        for key, val in result["defaults"].items():
            assert isinstance(val, (int, float)), f"defaults['{key}'] = {val!r} 非數值"


# ============================================================
# generate_tenant 進階測試
# ============================================================
class TestGenerateTenantAdvanced:
    """generate_tenant() 進階場景。"""

    def test_multiple_dbs_structure(self):
        """多 DB 選擇時 tenant 結構正確。"""
        result = generate_tenant("prod-db", ["kubernetes", "postgresql", "mysql"],
                                 interactive=False)
        assert "prod-db" in result["tenants"]

    def test_tenant_yaml_roundtrip(self):
        """generate_tenant 產出可正確序列化/反序列化。"""
        result = generate_tenant("db-x", ["kubernetes"], interactive=False)
        dumped = yaml.dump(result, default_flow_style=False)
        reloaded = yaml.safe_load(dumped)
        assert reloaded["tenants"]["db-x"] == result["tenants"]["db-x"]

    def test_tenant_name_preserved_exactly(self):
        """Tenant 名稱（含 hyphen/underscore）完全保留。"""
        for name in ["db-a", "db_b", "prod-mysql-01"]:
            result = generate_tenant(name, ["kubernetes"], interactive=False)
            assert name in result["tenants"]


# ============================================================
# write_outputs config_dir fixture 版本
# ============================================================
class TestWriteOutputsFixture:
    """write_outputs() 使用 config_dir fixture 的測試。"""

    def test_defaults_yaml_structure(self, config_dir):
        """_defaults.yaml 包含正確的 defaults 和 state_filters。"""
        defaults = generate_defaults(["kubernetes", "postgresql"])
        tenant = generate_tenant("db-z", ["kubernetes"], interactive=False)
        report = "# test"
        write_outputs(config_dir, "db-z", defaults, tenant, report)

        with open(os.path.join(config_dir, "_defaults.yaml"), encoding="utf-8") as f:
            parsed = yaml.safe_load(f)
        assert "defaults" in parsed
        assert "container_cpu" in parsed["defaults"]
        assert "pg_connections" in parsed["defaults"]

    def test_tenant_yaml_no_reserved_leak(self, config_dir):
        """tenant YAML 不含 _reserved prefix keys（除 _routing 等預期 key）。"""
        defaults = generate_defaults(["kubernetes"])
        tenant = generate_tenant("db-z", ["kubernetes"], interactive=False)
        write_outputs(config_dir, "db-z", defaults, tenant, "report")

        with open(os.path.join(config_dir, "db-z.yaml"), encoding="utf-8") as f:
            parsed = yaml.safe_load(f)
        t = parsed["tenants"]["db-z"]
        allowed_reserved = {"_routing", "_severity_dedup", "_metadata",
                            "_silent_mode", "_state_maintenance"}
        for key in t:
            if key.startswith("_"):
                assert key in allowed_reserved, f"非預期 reserved key: {key}"

    def test_report_file_content(self, config_dir):
        """scaffold-report.txt 寫入完整報告。"""
        defaults = generate_defaults(["kubernetes"])
        tenant = generate_tenant("db-z", ["kubernetes"], interactive=False)
        report = generate_report("db-z", ["kubernetes"], config_dir)
        write_outputs(config_dir, "db-z", defaults, tenant, report)

        with open(os.path.join(config_dir, "scaffold-report.txt"),
                  encoding="utf-8") as f:
            content = f.read()
        assert "db-z" in content
        assert len(content) > 50  # 非空報告
