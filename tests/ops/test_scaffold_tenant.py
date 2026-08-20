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
- print_catalog: Exporter 目錄輸出
- run_non_interactive / run_from_onboard / main: CLI 路徑
"""
import argparse
import json
import os
import re
import sys
import tempfile
from unittest import mock

import pytest

# Some tests assert exact Unix mode bits (0o600). Windows NTFS doesn't
# honor those — chmod() is a no-op and stat returns 0o666. Tests skip
# when sys.platform == "win32" so local Windows runs match CI Linux.
_skipif_unix_modes = pytest.mark.skipif(
    sys.platform == "win32",
    reason="Windows NTFS doesn't honor Unix mode bits; chmod is a no-op",
)

import pytest
import yaml

from scaffold_tenant import (
    annotate_defaults_counterexamples,
    annotate_saturation_criticals,
    build_receiver_from_args,
    counterexample_for_key,
    counterexample_prompt_lines,
    prompt_value,
    generate_defaults,
    generate_tenant,
    generate_profile,
    generate_report,
    generate_relabel_snippet,
    print_catalog,
    run_non_interactive,
    run_from_onboard,
    saturation_default_keys,
    write_outputs,
    RULE_PACKS,
    SATURATION_CRITICAL_COMMENT,
)
from _registry_lib import (  # noqa: E402
    counterexample_observed as registry_counterexample_observed,
)
from _lib_exitcodes import EXIT_CALLER_ERROR  # noqa: E402
# Imported, never restated: the renderer owns the wording, the gates only
# reference it (CodeRabbit, #1344 — three files had hand-copied a fragment).
from _registry_lib import COUNTEREXAMPLE_MARK as _CE_MARK  # noqa: E402


# ============================================================
# build_receiver_from_args
# ============================================================
class TestBuildReceiverFromArgs:
    """build_receiver_from_args() CLI 參數轉換測試。"""

    @pytest.mark.parametrize("rtype,value,expected_field,expected_value", [
        ("webhook", "https://hooks.example.com/alert", "url", "https://hooks.example.com/alert"),
        ("slack", "https://hooks.slack.com/T/B/X", "api_url", "https://hooks.slack.com/T/B/X"),
        ("teams", "https://outlook.office.com/webhook/test", "webhook_url", "https://outlook.office.com/webhook/test"),
        ("pagerduty", "abc123", "service_key", "abc123"),
        ("rocketchat", "https://chat.example.com/hooks/abc", "url", "https://chat.example.com/hooks/abc"),
    ], ids=["webhook", "slack", "teams", "pagerduty", "rocketchat"])
    def test_simple_receiver(self, rtype, value, expected_field, expected_value):
        """各類 receiver 正確建立（type + 對應欄位）。"""
        obj = build_receiver_from_args(rtype, value)
        assert obj["type"] == rtype
        assert obj[expected_field] == expected_value

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
# generate_defaults — 宣告 key 清單（#1310）
# ============================================================
class TestGenerateDefaultsDeclaredList:
    """⛔ 這一格是 tenant-api 寫入路徑唯一讀得到的宣告來源。

    threshold-exporter 讀 chart 渲染出來的 ConfigMap，但 tenant-api 的
    `--config-dir` 是 init container clone 下來的**客戶 GitOps repo**
    （`helm/tenant-api/templates/deployment.yaml`），而
    `config.mergeTenantConfig` 從那個目錄的 `_defaults.yaml` 讀
    `OptionalOverrides`。只出貨 chart 那一份，租戶透過 Portal / Tenant API 設這
    些 key 仍會被 `ValidateTenantKeys` 判 unknown → HTTP 400——`merge_tenant.go`
    的註解在事情發生前就逐字預言過這個失效模式。
    """

    def test_selected_pack_declares_its_flat_optional_keys(self):
        result = generate_defaults(["kubernetes", "oracle"])
        assert result["optional_overrides"] == [
            "oracle_wait_time_rate", "oracle_process_count",
            "oracle_pga_allocated_bytes",
        ]

    def test_declared_keys_carry_no_value(self):
        """是 list 不是 map：給了值就等於對每個租戶武裝一個平台選的數字，
        正好是這一格語意的反面（platform-defaults.schema.json: array of string）。"""
        result = generate_defaults(["kubernetes", "db2"])
        assert isinstance(result["optional_overrides"], list)
        assert all(isinstance(k, str) for k in result["optional_overrides"])
        # ...而且不得同時混進 defaults（那才是「主張值」）
        for key in result["optional_overrides"]:
            assert key not in result["defaults"]

    def test_no_critical_key_is_ever_declared(self):
        """#1311：`<base>_critical` 走 resolveCriticalRows 讀 defaults[base]，
        放進宣告清單只會是一格裝飾。"""
        result = generate_defaults(list(RULE_PACKS.keys()))
        assert not [k for k in result["optional_overrides"]
                    if k.endswith("_critical")]

    def test_unselected_pack_contributes_nothing(self):
        """只選 postgresql（optional tier 全是 `_critical`）⇒ 整格不出現，
        而不是出現一個空 list（空 list 讀起來像意外）。"""
        result = generate_defaults(["kubernetes", "postgresql"])
        assert "optional_overrides" not in result

    def test_supply_face_is_untouched(self):
        """可達性 gate 的供給面只讀 `defaults`；新增 sibling key 不得改變它。"""
        db_packs = [k for k in RULE_PACKS if k != "kubernetes"]
        assert len(generate_defaults(db_packs)["defaults"]) == 42

    def test_derivation_is_the_shared_predicate_not_a_fourth_copy(self):
        """清單內容必須逐字等於 chart 那一份的同 pack 子集——同一個 predicate、
        同一個走訪順序。手抄第四份正是 #1189 的病灶。"""
        import _registry_lib

        db_packs = [k for k in RULE_PACKS if k != "kubernetes"]
        generated = generate_defaults(db_packs)["optional_overrides"]
        assert generated == _registry_lib.optional_override_list_keys(
            _registry_lib.build_registry_doc())


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

    def test_optional_tier_split_critical_in_flat_out(self):
        """⛔ #1189 / TRK-337：optional_overrides 的兩半在 profile 裡待遇相反。

        `<base>_critical` 走 resolveCriticalRows，只要求 base 在 `defaults:`
        （pg_connections 等都在），所以 profile 供給它會產生真的 critical 層
        row——保留。**平鍵**則是「宣告但平台不主張值」那一格：profile 是平台
        授權的檔案，exporter 的 ApplyProfiles 現在會拒絕替它填值並記一行
        WARN，寫進去只會生出一份 dead-on-arrival 的檔案。

        逐 pack 對照該 pack 自己的 optional_overrides，不用字串後綴猜。
        """
        import scaffold_tenant as st

        problems = []
        for db, pack in st.RULE_PACKS.items():
            optional = pack.get("optional_overrides", {})
            if not optional:
                continue
            profile = generate_profile("std", [db])["profiles"]["std"]
            leaked = [k for k in optional if not k.endswith("_critical") and k in profile]
            missing = [k for k in optional if k.endswith("_critical") and k not in profile]
            if leaked or missing:
                problems.append(f"{db}: flat-leaked={leaked} critical-missing={missing}")
        assert not problems, "\n".join(problems)


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

    @_skipif_unix_modes
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
        allowed_reserved = {"_routing", "_routing_profile", "_severity_dedup",
                            "_metadata", "_silent_mode", "_state_maintenance"}
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


# ---------------------------------------------------------------------------
# print_catalog
# ---------------------------------------------------------------------------

class TestPrintCatalog:
    """print_catalog() 測試。"""

    def test_outputs_all_rule_packs(self, capsys):
        print_catalog()
        out = capsys.readouterr().out
        for key, pack in RULE_PACKS.items():
            assert pack["display"] in out
            assert pack["exporter"] in out


# ---------------------------------------------------------------------------
# run_non_interactive
# ---------------------------------------------------------------------------

class TestRunNonInteractive:
    """run_non_interactive() CLI 路徑測試。"""

    def test_basic(self, capsys):
        with tempfile.TemporaryDirectory() as d:
            args = argparse.Namespace(
                tenant="db-x", db="mariadb", output_dir=d,
                profile=None, silent_mode=None, severity_dedup="enable",
                routing_receiver=None, namespaces=None,
            )
            run_non_interactive(args)
            assert os.path.exists(os.path.join(d, "db-x.yaml"))
            assert os.path.exists(os.path.join(d, "_defaults.yaml"))

    def test_with_profile(self):
        with tempfile.TemporaryDirectory() as d:
            args = argparse.Namespace(
                tenant="db-x", db="mariadb", output_dir=d,
                profile="high-load", silent_mode=None,
                severity_dedup="enable", routing_receiver=None,
                namespaces=None,
            )
            run_non_interactive(args)
            with open(os.path.join(d, "db-x.yaml"), encoding="utf-8") as f:
                data = yaml.safe_load(f)
            assert data["tenants"]["db-x"]["_profile"] == "high-load"

    def test_with_silent_mode(self):
        with tempfile.TemporaryDirectory() as d:
            args = argparse.Namespace(
                tenant="db-x", db="mariadb", output_dir=d,
                profile=None, silent_mode="warning",
                severity_dedup="enable", routing_receiver=None,
                namespaces=None,
            )
            run_non_interactive(args)
            with open(os.path.join(d, "db-x.yaml"), encoding="utf-8") as f:
                data = yaml.safe_load(f)
            assert data["tenants"]["db-x"]["_silent_mode"] == "warning"

    def test_with_severity_dedup_disable(self):
        with tempfile.TemporaryDirectory() as d:
            args = argparse.Namespace(
                tenant="db-x", db="mariadb", output_dir=d,
                profile=None, silent_mode=None,
                severity_dedup="disable", routing_receiver=None,
                namespaces=None,
            )
            run_non_interactive(args)
            with open(os.path.join(d, "db-x.yaml"), encoding="utf-8") as f:
                data = yaml.safe_load(f)
            assert data["tenants"]["db-x"]["_severity_dedup"] == "disable"

    def test_with_routing(self):
        with tempfile.TemporaryDirectory() as d:
            args = argparse.Namespace(
                tenant="db-x", db="mariadb", output_dir=d,
                profile=None, silent_mode=None,
                severity_dedup="enable",
                routing_receiver="https://hooks.example.com/alert",
                routing_receiver_type="webhook",
                routing_smarthost=None,
                routing_group_by=None,
                routing_group_wait=None,
                routing_group_interval=None,
                routing_repeat_interval=None,
                namespaces=None,
            )
            run_non_interactive(args)
            with open(os.path.join(d, "db-x.yaml"), encoding="utf-8") as f:
                data = yaml.safe_load(f)
            routing = data["tenants"]["db-x"]["_routing"]
            assert routing["receiver"]["type"] == "webhook"
            assert routing["group_wait"] == "30s"

    def test_with_namespaces(self):
        with tempfile.TemporaryDirectory() as d:
            args = argparse.Namespace(
                tenant="db-x", db="mariadb", output_dir=d,
                profile=None, silent_mode=None,
                severity_dedup="enable", routing_receiver=None,
                namespaces="ns1,ns2",
            )
            run_non_interactive(args)
            relabel_file = os.path.join(d, "relabel_configs-db-x.yaml")
            assert os.path.exists(relabel_file)

    def test_invalid_db_exits(self):
        with tempfile.TemporaryDirectory() as d:
            args = argparse.Namespace(
                tenant="db-x", db="nonexistent_db", output_dir=d,
                profile=None, silent_mode=None,
                severity_dedup="enable", routing_receiver=None,
                namespaces=None,
            )
            with pytest.raises(SystemExit):
                run_non_interactive(args)

    # ── ADR-007: --routing-profile ────────────────────────────────────

    def test_with_routing_profile(self):
        """--routing-profile adds _routing_profile key to tenant YAML."""
        with tempfile.TemporaryDirectory() as d:
            args = argparse.Namespace(
                tenant="db-x", db="mariadb", output_dir=d,
                profile=None, silent_mode=None,
                severity_dedup="enable", routing_receiver=None,
                namespaces=None,
                routing_profile="team-sre-apac",
                topology="1:1",
                mapping_instance=None, mapping_filter=None,
            )
            run_non_interactive(args)
            with open(os.path.join(d, "db-x.yaml"), encoding="utf-8") as f:
                data = yaml.safe_load(f)
            assert data["tenants"]["db-x"]["_routing_profile"] == "team-sre-apac"

    def test_routing_profile_none_omitted(self):
        """Without --routing-profile, _routing_profile is absent."""
        with tempfile.TemporaryDirectory() as d:
            args = argparse.Namespace(
                tenant="db-x", db="mariadb", output_dir=d,
                profile=None, silent_mode=None,
                severity_dedup="enable", routing_receiver=None,
                namespaces=None,
                routing_profile=None,
                topology="1:1",
                mapping_instance=None, mapping_filter=None,
            )
            run_non_interactive(args)
            with open(os.path.join(d, "db-x.yaml"), encoding="utf-8") as f:
                data = yaml.safe_load(f)
            assert "_routing_profile" not in data["tenants"]["db-x"]

    def test_routing_profile_with_routing(self):
        """--routing-profile coexists with --routing-receiver."""
        with tempfile.TemporaryDirectory() as d:
            args = argparse.Namespace(
                tenant="db-x", db="mariadb", output_dir=d,
                profile=None, silent_mode=None,
                severity_dedup="enable",
                routing_receiver="https://hooks.example.com/alert",
                routing_receiver_type="webhook",
                routing_smarthost=None,
                routing_group_by=None,
                routing_group_wait=None,
                routing_group_interval=None,
                routing_repeat_interval=None,
                namespaces=None,
                routing_profile="team-dba-global",
                topology="1:1",
                mapping_instance=None, mapping_filter=None,
            )
            run_non_interactive(args)
            with open(os.path.join(d, "db-x.yaml"), encoding="utf-8") as f:
                data = yaml.safe_load(f)
            t = data["tenants"]["db-x"]
            assert t["_routing_profile"] == "team-dba-global"
            assert t["_routing"]["receiver"]["type"] == "webhook"

    # ── ADR-006: --topology ───────────────────────────────────────────

    def test_topology_1n_generates_mapping(self):
        """--topology=1:N with --mapping-instance and --mapping-filter produces _instance_mapping.yaml."""
        with tempfile.TemporaryDirectory() as d:
            args = argparse.Namespace(
                tenant="db-x", db="mariadb", output_dir=d,
                profile=None, silent_mode=None,
                severity_dedup="enable", routing_receiver=None,
                namespaces=None,
                routing_profile=None,
                topology="1:N",
                mapping_instance="oracle-prod-01",
                mapping_filter='schema=~"app_a_.*"',
            )
            run_non_interactive(args)
            mapping_path = os.path.join(d, "_instance_mapping.yaml")
            assert os.path.exists(mapping_path)
            with open(mapping_path, encoding="utf-8") as f:
                content = f.read()
            data = yaml.safe_load(content.split("---")[-1] if "---" in content else content)
            # May be None if comment-only; parse lines after comments
            if data is None:
                lines = [l for l in content.splitlines() if not l.startswith("#")]
                data = yaml.safe_load("\n".join(lines))
            assert "instance_tenant_mapping" in data
            assert "oracle-prod-01" in data["instance_tenant_mapping"]
            mapping = data["instance_tenant_mapping"]["oracle-prod-01"]
            assert mapping[0]["tenant"] == "db-x"
            assert mapping[0]["filter"] == 'schema=~"app_a_.*"'

    def test_topology_1n_missing_args_warns(self, capsys):
        """--topology=1:N without --mapping-instance warns and skips mapping file."""
        with tempfile.TemporaryDirectory() as d:
            args = argparse.Namespace(
                tenant="db-x", db="mariadb", output_dir=d,
                profile=None, silent_mode=None,
                severity_dedup="enable", routing_receiver=None,
                namespaces=None,
                routing_profile=None,
                topology="1:N",
                mapping_instance=None, mapping_filter=None,
            )
            run_non_interactive(args)
            mapping_path = os.path.join(d, "_instance_mapping.yaml")
            assert not os.path.exists(mapping_path)
            captured = capsys.readouterr()
            assert "WARN" in captured.err

    def test_topology_n1_with_namespaces(self):
        """--topology=N:1 with --namespaces generates relabel_configs."""
        with tempfile.TemporaryDirectory() as d:
            args = argparse.Namespace(
                tenant="db-x", db="mariadb", output_dir=d,
                profile=None, silent_mode=None,
                severity_dedup="enable", routing_receiver=None,
                namespaces="ns-a,ns-b",
                routing_profile=None,
                topology="N:1",
                mapping_instance=None, mapping_filter=None,
            )
            run_non_interactive(args)
            relabel_file = os.path.join(d, "relabel_configs-db-x.yaml")
            assert os.path.exists(relabel_file)

    def test_topology_n1_without_namespaces_warns(self, capsys):
        """--topology=N:1 without --namespaces warns."""
        with tempfile.TemporaryDirectory() as d:
            args = argparse.Namespace(
                tenant="db-x", db="mariadb", output_dir=d,
                profile=None, silent_mode=None,
                severity_dedup="enable", routing_receiver=None,
                namespaces=None,
                routing_profile=None,
                topology="N:1",
                mapping_instance=None, mapping_filter=None,
            )
            run_non_interactive(args)
            captured = capsys.readouterr()
            assert "WARN" in captured.err

    def test_topology_default_no_extra_files(self):
        """Default topology (1:1) produces no mapping or relabel files."""
        with tempfile.TemporaryDirectory() as d:
            args = argparse.Namespace(
                tenant="db-x", db="mariadb", output_dir=d,
                profile=None, silent_mode=None,
                severity_dedup="enable", routing_receiver=None,
                namespaces=None,
                routing_profile=None,
                topology="1:1",
                mapping_instance=None, mapping_filter=None,
            )
            run_non_interactive(args)
            assert not os.path.exists(os.path.join(d, "_instance_mapping.yaml"))
            assert not os.path.exists(os.path.join(d, "relabel_configs-db-x.yaml"))

    @_skipif_unix_modes
    def test_mapping_file_secure_permissions(self):
        """_instance_mapping.yaml has secure 0o600 permissions."""
        with tempfile.TemporaryDirectory() as d:
            args = argparse.Namespace(
                tenant="db-x", db="mariadb", output_dir=d,
                profile=None, silent_mode=None,
                severity_dedup="enable", routing_receiver=None,
                namespaces=None,
                routing_profile=None,
                topology="1:N",
                mapping_instance="inst-01",
                mapping_filter='schema=~"test"',
            )
            run_non_interactive(args)
            mapping_path = os.path.join(d, "_instance_mapping.yaml")
            mode = os.stat(mapping_path).st_mode & 0o777
            assert mode == 0o600


# ---------------------------------------------------------------------------
# run_from_onboard
# ---------------------------------------------------------------------------

class TestRunFromOnboard:
    """run_from_onboard() 測試。"""

    def test_basic(self):
        with tempfile.TemporaryDirectory() as d:
            hints = {
                "tenants": ["db-a", "db-b"],
                "db_types": {
                    "db-a": ["mariadb"],
                    "db-b": ["mariadb"],
                },
                "routing_hints": {},
            }
            hints_path = os.path.join(d, "hints.json")
            with open(hints_path, "w", encoding="utf-8") as f:
                json.dump(hints, f)

            args = argparse.Namespace(
                from_onboard=hints_path, output_dir=d,
            )
            run_from_onboard(args)
            assert os.path.exists(os.path.join(d, "db-a.yaml"))
            assert os.path.exists(os.path.join(d, "db-b.yaml"))

    def test_with_routing_hints(self):
        with tempfile.TemporaryDirectory() as d:
            hints = {
                "tenants": ["db-a"],
                "db_types": {"db-a": ["mariadb"]},
                "routing_hints": {
                    "db-a": {
                        "receiver_type": "webhook",
                        "group_wait": "10s",
                        "group_interval": "1m",
                        "repeat_interval": "2h",
                    }
                },
            }
            hints_path = os.path.join(d, "hints.json")
            with open(hints_path, "w", encoding="utf-8") as f:
                json.dump(hints, f)

            args = argparse.Namespace(
                from_onboard=hints_path, output_dir=d,
            )
            run_from_onboard(args)
            with open(os.path.join(d, "db-a.yaml"), encoding="utf-8") as f:
                data = yaml.safe_load(f)
            routing = data["tenants"]["db-a"]["_routing"]
            assert routing["group_wait"] == "10s"

    def test_invalid_hints_exits(self):
        with tempfile.TemporaryDirectory() as d:
            args = argparse.Namespace(
                from_onboard=os.path.join(d, "nonexistent.json"),
                output_dir=d,
            )
            with pytest.raises(SystemExit):
                run_from_onboard(args)

    def test_no_tenants_exits(self):
        with tempfile.TemporaryDirectory() as d:
            hints_path = os.path.join(d, "empty.json")
            with open(hints_path, "w", encoding="utf-8") as f:
                json.dump({"tenants": []}, f)

            args = argparse.Namespace(
                from_onboard=hints_path, output_dir=d,
            )
            with pytest.raises(SystemExit):
                run_from_onboard(args)


# ============================================================
# 飽和類指標 _critical 教育註解（metric_class == saturation）
# ============================================================

# 被標 saturation 的 defaults 鍵（真 N=22；同步 CHANGELOG 與 portal 測試常數）
EXPECTED_SATURATION_KEYS = {
    # kubernetes
    "container_cpu", "container_cpu_throttle", "container_memory",
    # mariadb
    "mysql_connections", "mysql_threads_running",
    # postgresql
    "pg_connections",
    # redis
    "redis_memory_used_bytes", "redis_connected_clients",
    # mongodb
    "mongodb_connections_current",
    # elasticsearch
    "es_heap_usage_percent",  # renamed from es_jvm_memory_used_percent (#1196 C / #1231)
    # oracle
    "oracle_sessions_active",
    # db2
    "db2_connections_active",
    # clickhouse
    "clickhouse_active_connections",
    # kafka
    "kafka_consumer_lag",
    # rabbitmq
    "rabbitmq_node_mem_percent", "rabbitmq_connections",
    "rabbitmq_queue_messages", "rabbitmq_unacked_messages",
    # jvm
    "jvm_memory", "jvm_threads",
    # nginx
    "nginx_connections", "nginx_waiting",
}


class TestAnnotateSaturationCriticals:
    """annotate_saturation_criticals() 行插入測試。"""

    def test_inserts_comment_above_saturation_critical(self):
        """飽和 _critical 鍵上方插入註解，縮排保留。"""
        text = 'tenants:\n  db-x:\n    mysql_threads_running_critical: "50"\n'
        out = annotate_saturation_criticals(text)
        lines = out.split("\n")
        idx = next(i for i, l in enumerate(lines)
                   if l.strip().startswith("mysql_threads_running_critical:"))
        assert lines[idx - 1] == f"    {SATURATION_CRITICAL_COMMENT}"

    def test_non_saturation_critical_untouched(self):
        """非飽和鍵（pg_replication_lag_critical）不插註解。"""
        text = 'tenants:\n  db-x:\n    pg_replication_lag_critical: "60"\n'
        out = annotate_saturation_criticals(text)
        assert out == text

    def test_commented_line_not_annotated(self):
        """已註解行（# 開頭）不誤觸。"""
        text = 'tenants:\n  db-x:\n    # mysql_threads_running_critical: "50"\n'
        out = annotate_saturation_criticals(text)
        assert out == text

    def test_idempotent(self):
        """重複套用不重複插入。"""
        text = 'tenants:\n  db-x:\n    mysql_threads_running_critical: "50"\n'
        once = annotate_saturation_criticals(text)
        assert annotate_saturation_criticals(once) == once

    def test_yaml_roundtrip_data_unchanged(self):
        """插入註解後 safe_load 資料等值（純顯示、不改語義）。"""
        text = yaml.safe_dump(
            {"tenants": {"db-x": {
                "mysql_threads_running_critical": "50",
                "container_memory_critical": "95",
                "pg_replication_lag_critical": "60",
            }}},
            default_flow_style=False, sort_keys=False)
        out = annotate_saturation_criticals(text)
        assert yaml.safe_load(out) == yaml.safe_load(text)

    def test_base_key_without_critical_suffix_untouched(self):
        """飽和 base 鍵本身（無 _critical 後綴）不插註解。"""
        text = 'tenants:\n  db-x:\n    mysql_threads_running: "30"\n'
        assert annotate_saturation_criticals(text) == text


class TestSaturationWriteOutputs:
    """write_outputs() 飽和註解整合測試。"""

    def test_tenant_yaml_has_comment_above_saturation_critical(self):
        """含 mysql_threads_running_critical 的 tenant YAML 註解在該鍵上一行。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            defaults = {"defaults": {"mysql_threads_running": 30}}
            tenant = {"tenants": {"db-x": {"mysql_threads_running_critical": "50"}}}
            write_outputs(tmpdir, "db-x", defaults, tenant, "report")
            with open(os.path.join(tmpdir, "db-x.yaml"), encoding="utf-8") as f:
                lines = f.read().split("\n")
            idx = next(i for i, l in enumerate(lines)
                       if l.strip().startswith("mysql_threads_running_critical:"))
            assert lines[idx - 1].strip() == SATURATION_CRITICAL_COMMENT

    def test_empty_tenant_noop(self):
        """非互動空 tenant → 無任何飽和註解。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            defaults = generate_defaults(["kubernetes"])
            tenant = generate_tenant("db-x", ["kubernetes"], interactive=False)
            write_outputs(tmpdir, "db-x", defaults, tenant, "report")
            with open(os.path.join(tmpdir, "db-x.yaml"), encoding="utf-8") as f:
                content = f.read()
            assert SATURATION_CRITICAL_COMMENT not in content


class TestSaturationGenerateProfile:
    """--generate-profile 的 _profiles.yaml 飽和註解測試。"""

    def test_profiles_yaml_annotates_every_saturation_critical(self):
        import scaffold_tenant
        saturation = saturation_default_keys()
        with tempfile.TemporaryDirectory() as d:
            with mock.patch("sys.argv", [
                "scaffold_tenant.py",
                "--generate-profile", "standard-prod",
                "--db", "mariadb,nginx",
                "-o", d,
            ]):
                with pytest.raises(SystemExit) as exc_info:
                    scaffold_tenant.main()
                assert exc_info.value.code == 0
            with open(os.path.join(d, "_profiles.yaml"), encoding="utf-8") as f:
                lines = f.read().split("\n")
            import re
            found = 0
            for i, line in enumerate(lines):
                m = re.match(r"^(\s*)([A-Za-z0-9_]+)_critical:", line)
                if m and m.group(2) in saturation:
                    found += 1
                    assert lines[i - 1] == f"{m.group(1)}{SATURATION_CRITICAL_COMMENT}", \
                        f"line {i}: {line!r} 缺教育註解"
            # mariadb 的 mysql_connections/mysql_threads_running + nginx 的
            # nginx_connections/nginx_waiting critical tiers 至少各一
            assert found >= 4


class TestSaturationGenerateReport:
    """generate_report() 飽和教育段測試。"""

    def test_mariadb_report_includes_education_section(self):
        report = generate_report("db-x", ["kubernetes", "mariadb"], "/tmp/out")
        assert "飽和類指標的 critical 層" in report
        assert "mysql_threads_running_critical" in report
        assert "mysql_connections_critical" in report
        assert "container_cpu_critical" in report
        # #1447: this asserted the repo-relative `docs/…md` path. The report
        # is handed to a tenant whose repository has no `docs/` tree, so the
        # pointer named a file they cannot open; it is the published URL now.
        #
        # ⛔ Assert the WHOLE URL, not the trailing path segment: a substring
        # match is satisfied by a relative path or the wrong host, which is
        # exactly the drift this replaced.
        from _lib_python import DOCS_SITE_BASE
        assert DOCS_SITE_BASE + "alerting-design-fundamentals/" in report
        assert "docs/alerting-design-fundamentals.md" not in report, (
            "the repo-relative form is unreachable for the report's reader")

    def test_no_saturation_packs_no_section(self):
        """無飽和鍵組合（空 selected_dbs）→ 無教育段。"""
        report = generate_report("db-x", [], "/tmp/out")
        assert "飽和類指標的 critical 層" not in report
        assert "alerting-design-fundamentals" not in report


class TestSaturationRulePacksIntegrity:
    """RULE_PACKS metric_class 完整性。"""

    def test_metric_class_value_domain(self):
        """metric_class 值域僅 {"saturation"}。"""
        values = {
            info["metric_class"]
            for pack in RULE_PACKS.values()
            for section in ("defaults", "optional_overrides")
            for info in pack.get(section, {}).values()
            if "metric_class" in info
        }
        assert values == {"saturation"}

    def test_metric_class_only_in_defaults(self):
        """metric_class 只出現在 defaults，不出現在 optional_overrides。"""
        for name, pack in RULE_PACKS.items():
            for key, info in pack.get("optional_overrides", {}).items():
                assert "metric_class" not in info, \
                    f"{name}.optional_overrides.{key} 不應有 metric_class"

    def test_saturation_set_matches_expected(self):
        """被標 set == 預期常數（真 N=22）。"""
        assert saturation_default_keys() == EXPECTED_SATURATION_KEYS
        assert len(EXPECTED_SATURATION_KEYS) == 22


# ---------------------------------------------------------------------------
# main — CLI entry points
# ---------------------------------------------------------------------------

class TestMainCLI:
    """main() CLI 路徑測試。"""

    def test_catalog_mode(self, capsys):
        import scaffold_tenant
        with mock.patch("sys.argv", ["scaffold_tenant.py", "--catalog"]):
            with pytest.raises(SystemExit) as exc_info:
                scaffold_tenant.main()
            assert exc_info.value.code == 0
        out = capsys.readouterr().out
        assert "Supported Exporters" in out

    def test_generate_profile_mode(self):
        import scaffold_tenant
        with tempfile.TemporaryDirectory() as d:
            with mock.patch("sys.argv", [
                "scaffold_tenant.py",
                "--generate-profile", "standard-prod",
                "--db", "mariadb",
                "-o", d,
            ]):
                with pytest.raises(SystemExit) as exc_info:
                    scaffold_tenant.main()
                assert exc_info.value.code == 0
            assert os.path.exists(os.path.join(d, "_profiles.yaml"))

    def test_generate_profile_no_db_exits(self):
        import scaffold_tenant
        with mock.patch("sys.argv", [
            "scaffold_tenant.py", "--generate-profile", "test",
        ]):
            with pytest.raises(SystemExit) as exc_info:
                scaffold_tenant.main()
            assert exc_info.value.code == EXIT_CALLER_ERROR

    def test_generate_profile_invalid_db_exits(self):
        import scaffold_tenant
        with mock.patch("sys.argv", [
            "scaffold_tenant.py",
            "--generate-profile", "test",
            "--db", "invalid_db_type",
        ]):
            with pytest.raises(SystemExit) as exc_info:
                scaffold_tenant.main()
            assert exc_info.value.code == EXIT_CALLER_ERROR

    def test_non_interactive_mode(self):
        import scaffold_tenant
        with tempfile.TemporaryDirectory() as d:
            with mock.patch("sys.argv", [
                "scaffold_tenant.py",
                "--tenant", "db-test",
                "--db", "mariadb",
                "-o", d,
                "--non-interactive",
            ]):
                scaffold_tenant.main()
            assert os.path.exists(os.path.join(d, "db-test.yaml"))

    def test_non_interactive_missing_args_exits(self):
        import scaffold_tenant
        with mock.patch("sys.argv", [
            "scaffold_tenant.py", "--non-interactive",
        ]):
            with pytest.raises(SystemExit):
                scaffold_tenant.main()


# ============================================================
# generate_tenant — 互動路徑 characterization（da-tools ROI 第六波）
#
# Pin 當前互動 prompt 行為，作為後續重構的安全網。generate_tenant 的
# 詢問次序（一律 sequential）：
#   metric 覆寫 → 維護模式 → 靜音模式 → 嚴重度去重 → 告警路由
# 每個分支以 mock 的 input() 序列驅動，斷言回傳 dict 逐鍵相同（golden）。
#
# 分支耦合（某些 input 的答案決定後續問幾個 prompt）：
#   - 維護：expires 空白 → 不再問 reason（純字串 "enable"）
#   - 靜音：choice∈{2,3,4} 才問 expires；expires 空白 → 不問 reason
#   - 路由：receiver_type 決定走哪個 receiver 分支（各吃不同數量 input）；
#           receiver 必填欄位齊備（receiver_obj 為真）才問 4 個 timing prompt；
#           slack 額外在 api_url 有值時才問 channel；未知型別只吃 receiver_type
# ============================================================


def _gen_tenant_interactive(tenant_name, selected_dbs, inputs):
    """以 mock 的 input() 序列跑 generate_tenant(interactive=True)。

    ``inputs`` 依 prompt 次序排列：metric 覆寫 → 維護 → 靜音 → dedup → 路由。
    序列長度必須與實際消耗的 input() 次數相符（多給會忽略、少給會 StopIteration）。
    """
    with mock.patch("builtins.input", side_effect=list(inputs)):
        return generate_tenant(tenant_name, selected_dbs, interactive=True)


# Neutral（no-op）答案，讓「非受測」段落產生空覆寫，隔離出受測分支：
_NEUTRAL_MAINT = "N"          # 維護：不啟用（1 個 input）
_NEUTRAL_SILENT = "1"         # 靜音：Normal（1 個 input）
_NEUTRAL_DEDUP = "1"          # dedup：Enable 預設不寫（1 個 input）
_NEUTRAL_ROUTING = ["", ""]   # 路由：receiver_type 空→webhook，url 空→無 receiver（2 個 input）


class TestGenerateTenantNonInteractiveGolden:
    """non-interactive 路徑 golden dict：一律回傳空 tenant 覆寫。"""

    @pytest.mark.parametrize("dbs", [
        ["kubernetes"],
        ["kubernetes", "mariadb"],
        ["kubernetes", "postgresql", "redis"],
        ["nonexistent"],
        [],
    ], ids=["single", "k8s+mariadb", "multi", "unknown", "empty"])
    def test_returns_empty_tenant_dict(self, dbs):
        """non-interactive 不論選哪些 DB 都回傳 {"tenants": {name: {}}}。"""
        assert generate_tenant("db-c", dbs, interactive=False) == {"tenants": {"db-c": {}}}

    def test_tenant_name_preserved_verbatim(self):
        """Tenant 名稱逐字保留於 golden dict。"""
        assert generate_tenant("prod-mysql-01", ["kubernetes"], interactive=False) == \
            {"tenants": {"prod-mysql-01": {}}}


class TestGenerateTenantInteractiveMetrics:
    """互動 metric 覆寫 prompt（每個 metric 一個 input，順序 = pack 定義序）。"""

    def test_value_variants_empty_disable_skip(self):
        """空白→預設值字串保留、'disable'→保留、'skip'→省略。"""
        # kubernetes defaults 序：container_cpu(80), container_cpu_throttle(25),
        # container_memory(85)。
        res = _gen_tenant_interactive("db-c", ["kubernetes"], [
            "",         # container_cpu → str(80)
            "disable",  # container_cpu_throttle → "disable"（保留）
            "skip",     # container_memory → None（省略）
            _NEUTRAL_MAINT, _NEUTRAL_SILENT, _NEUTRAL_DEDUP, *_NEUTRAL_ROUTING,
        ])
        assert res == {"tenants": {"db-c": {
            "container_cpu": "80",
            "container_cpu_throttle": "disable",
        }}}

    def test_custom_values_kept_as_strings(self):
        """自訂數值以字串保留。"""
        res = _gen_tenant_interactive("db-c", ["kubernetes"], [
            "75", "30", "90",
            _NEUTRAL_MAINT, _NEUTRAL_SILENT, _NEUTRAL_DEDUP, *_NEUTRAL_ROUTING,
        ])
        assert res == {"tenants": {"db-c": {
            "container_cpu": "75",
            "container_cpu_throttle": "30",
            "container_memory": "90",
        }}}

    def test_optional_overrides_prompted_after_defaults(self):
        """defaults 問完才問 optional_overrides（mariadb 的 _critical tiers）。"""
        # 次序：k8s defaults(3) → mariadb defaults(3: mysql_connections, mysql_threads_running,
        #         mysql_replication_lag)
        #       → mariadb optional_overrides(3: mysql_connections_critical,
        #         mysql_threads_running_critical, mysql_replication_lag_critical)。
        res = _gen_tenant_interactive("db-c", ["kubernetes", "mariadb"], [
            "skip", "skip", "skip",   # k8s 3 defaults
            "skip", "skip", "skip",   # mariadb 3 defaults
            "100",                    # mysql_connections_critical → 保留
            "skip",                   # mysql_threads_running_critical → 省略
            "skip",                   # mysql_replication_lag_critical → 省略
            _NEUTRAL_MAINT, _NEUTRAL_SILENT, _NEUTRAL_DEDUP, *_NEUTRAL_ROUTING,
        ])
        assert res == {"tenants": {"db-c": {"mysql_connections_critical": "100"}}}


class TestGenerateTenantInteractiveMaintenance:
    """互動維護模式（維護 → 靜音 → dedup → 路由 的第一段）。"""

    def test_enable_with_expires_and_reason(self):
        res = _gen_tenant_interactive("db-c", [], [
            "y", "2026-04-01T00:00:00Z", "quarterly patch",
            _NEUTRAL_SILENT, _NEUTRAL_DEDUP, *_NEUTRAL_ROUTING,
        ])
        assert res["tenants"]["db-c"] == {"_state_maintenance": {
            "target": "enable", "expires": "2026-04-01T00:00:00Z",
            "reason": "quarterly patch"}}

    def test_enable_with_expires_no_reason(self):
        res = _gen_tenant_interactive("db-c", [], [
            "y", "2026-04-01T00:00:00Z", "",
            _NEUTRAL_SILENT, _NEUTRAL_DEDUP, *_NEUTRAL_ROUTING,
        ])
        assert res["tenants"]["db-c"] == {"_state_maintenance": {
            "target": "enable", "expires": "2026-04-01T00:00:00Z"}}

    def test_enable_no_expires_is_plain_string(self):
        """空 expires → 純字串 "enable"，且【不】再問 reason（分支耦合）。"""
        res = _gen_tenant_interactive("db-c", [], [
            "y", "",  # 空 expires：此後不會有 reason prompt
            _NEUTRAL_SILENT, _NEUTRAL_DEDUP, *_NEUTRAL_ROUTING,
        ])
        assert res["tenants"]["db-c"] == {"_state_maintenance": "enable"}

    @pytest.mark.parametrize("answer", ["N", "n", "", "no"])
    def test_not_y_omits_maintenance(self, answer):
        """非 'y'（strip+lower 後）→ 不寫 _state_maintenance，不問 expires。"""
        res = _gen_tenant_interactive("db-c", [], [
            answer, _NEUTRAL_SILENT, _NEUTRAL_DEDUP, *_NEUTRAL_ROUTING,
        ])
        assert res == {"tenants": {"db-c": {}}}

    @pytest.mark.parametrize("answer", ["y", "Y", "Y ", " y "])
    def test_y_after_strip_lower_enables(self, answer):
        """答案經 strip().lower()：'Y'/'Y '/' y ' 皆等同 'y' → 啟用維護。"""
        res = _gen_tenant_interactive("db-c", [], [
            answer, "",  # 啟用後空 expires → 純字串 "enable"
            _NEUTRAL_SILENT, _NEUTRAL_DEDUP, *_NEUTRAL_ROUTING,
        ])
        assert res["tenants"]["db-c"] == {"_state_maintenance": "enable"}


class TestGenerateTenantInteractiveSilent:
    """互動靜音模式（4 選項 + expires/reason 耦合）。"""

    @pytest.mark.parametrize("choice,target", [
        ("2", "warning"), ("3", "critical"), ("4", "all")])
    def test_structured_with_expires_and_reason(self, choice, target):
        res = _gen_tenant_interactive("db-c", [], [
            _NEUTRAL_MAINT, choice, "2026-05-01T00:00:00Z", "load test",
            _NEUTRAL_DEDUP, *_NEUTRAL_ROUTING,
        ])
        assert res["tenants"]["db-c"] == {"_silent_mode": {
            "target": target, "expires": "2026-05-01T00:00:00Z", "reason": "load test"}}

    @pytest.mark.parametrize("choice,target", [
        ("2", "warning"), ("3", "critical"), ("4", "all")])
    def test_plain_string_when_no_expires(self, choice, target):
        """空 expires → 純字串 target，且【不】問 reason。"""
        res = _gen_tenant_interactive("db-c", [], [
            _NEUTRAL_MAINT, choice, "",
            _NEUTRAL_DEDUP, *_NEUTRAL_ROUTING,
        ])
        assert res["tenants"]["db-c"] == {"_silent_mode": target}

    def test_expires_no_reason(self):
        res = _gen_tenant_interactive("db-c", [], [
            _NEUTRAL_MAINT, "2", "2026-05-01T00:00:00Z", "",
            _NEUTRAL_DEDUP, *_NEUTRAL_ROUTING,
        ])
        assert res["tenants"]["db-c"] == {"_silent_mode": {
            "target": "warning", "expires": "2026-05-01T00:00:00Z"}}

    @pytest.mark.parametrize("choice", ["1", "", "9"])
    def test_normal_or_invalid_omits_and_skips_expires(self, choice):
        """choice∉{2,3,4} → 無 _silent_mode，且【不】問 expires（分支耦合）。"""
        res = _gen_tenant_interactive("db-c", [], [
            _NEUTRAL_MAINT, choice, _NEUTRAL_DEDUP, *_NEUTRAL_ROUTING,
        ])
        assert res == {"tenants": {"db-c": {}}}


class TestGenerateTenantInteractiveDedup:
    """互動嚴重度去重（單一 input，無子分支）。"""

    def test_choice_2_writes_disable(self):
        res = _gen_tenant_interactive("db-c", [], [
            _NEUTRAL_MAINT, _NEUTRAL_SILENT, "2", *_NEUTRAL_ROUTING,
        ])
        assert res["tenants"]["db-c"] == {"_severity_dedup": "disable"}

    @pytest.mark.parametrize("choice", ["1", "", "x"])
    def test_non_2_omits(self, choice):
        """僅 '2' 觸發 disable；其餘（含預設 1）省略。"""
        res = _gen_tenant_interactive("db-c", [], [
            _NEUTRAL_MAINT, _NEUTRAL_SILENT, choice, *_NEUTRAL_ROUTING,
        ])
        assert res == {"tenants": {"db-c": {}}}


class TestGenerateTenantInteractiveRouting:
    """互動告警路由（6 receiver 型別 + receiver_obj 真偽門控 timing prompts）。"""

    # 路由前置：維護 N、靜音 1、dedup 1（3 個 no-op input）
    _PRE = [_NEUTRAL_MAINT, _NEUTRAL_SILENT, _NEUTRAL_DEDUP]

    def test_webhook_default_timing(self):
        res = _gen_tenant_interactive("db-c", [], self._PRE + [
            "webhook", "https://hooks.example.com/x",
            "", "", "", "",  # group_by, group_wait, group_interval, repeat_interval
        ])
        assert res["tenants"]["db-c"]["_routing"] == {
            "receiver": {"type": "webhook", "url": "https://hooks.example.com/x"},
            "group_by": ["alertname", "tenant"],
            "group_wait": "30s", "group_interval": "5m", "repeat_interval": "4h",
        }

    def test_empty_type_defaults_to_webhook(self):
        """receiver_type 空白 → "webhook"。"""
        res = _gen_tenant_interactive("db-c", [], self._PRE + [
            "", "https://h/x", "", "", "", "",
        ])
        assert res["tenants"]["db-c"]["_routing"]["receiver"] == {
            "type": "webhook", "url": "https://h/x"}

    def test_email_custom_timing(self):
        res = _gen_tenant_interactive("db-c", [], self._PRE + [
            "email", "a@x.com,b@x.com", "smtp.x.com:587",
            "severity,tenant", "10s", "2m", "1h",
        ])
        assert res["tenants"]["db-c"]["_routing"] == {
            "receiver": {"type": "email", "to": ["a@x.com", "b@x.com"],
                         "smarthost": "smtp.x.com:587"},
            "group_by": ["severity", "tenant"],
            "group_wait": "10s", "group_interval": "2m", "repeat_interval": "1h",
        }

    def test_slack_with_channel(self):
        res = _gen_tenant_interactive("db-c", [], self._PRE + [
            "slack", "https://hooks.slack.com/T/B/X", "#alerts",
            "", "", "", "",
        ])
        assert res["tenants"]["db-c"]["_routing"]["receiver"] == {
            "type": "slack", "api_url": "https://hooks.slack.com/T/B/X",
            "channel": "#alerts"}

    def test_slack_without_channel(self):
        """空 channel → 不寫 channel 鍵（channel prompt 在 api_url 有值時才問）。"""
        res = _gen_tenant_interactive("db-c", [], self._PRE + [
            "slack", "https://hooks.slack.com/T/B/X", "",
            "", "", "", "",
        ])
        assert res["tenants"]["db-c"]["_routing"]["receiver"] == {
            "type": "slack", "api_url": "https://hooks.slack.com/T/B/X"}

    def test_teams(self):
        res = _gen_tenant_interactive("db-c", [], self._PRE + [
            "teams", "https://outlook.office.com/webhook/x",
            "", "", "", "",
        ])
        assert res["tenants"]["db-c"]["_routing"]["receiver"] == {
            "type": "teams", "webhook_url": "https://outlook.office.com/webhook/x"}

    def test_rocketchat(self):
        res = _gen_tenant_interactive("db-c", [], self._PRE + [
            "rocketchat", "https://chat.x.com/hooks/x",
            "", "", "", "",
        ])
        assert res["tenants"]["db-c"]["_routing"]["receiver"] == {
            "type": "rocketchat", "url": "https://chat.x.com/hooks/x"}

    def test_pagerduty(self):
        res = _gen_tenant_interactive("db-c", [], self._PRE + [
            "pagerduty", "abc123",
            "", "", "", "",
        ])
        assert res["tenants"]["db-c"]["_routing"]["receiver"] == {
            "type": "pagerduty", "service_key": "abc123"}

    def test_type_lowercased(self):
        """receiver_type 經 .lower() → "SLACK" 視為 slack。"""
        res = _gen_tenant_interactive("db-c", [], self._PRE + [
            "SLACK", "https://hooks.slack.com/T/B/X", "",
            "", "", "", "",
        ])
        assert res["tenants"]["db-c"]["_routing"]["receiver"]["type"] == "slack"

    def test_unknown_type_no_routing(self):
        """未知型別 → 只吃 receiver_type，不問後續，無 _routing。"""
        res = _gen_tenant_interactive("db-c", [], self._PRE + ["carrierpigeon"])
        assert res == {"tenants": {"db-c": {}}}

    def test_webhook_empty_url_no_routing(self):
        """webhook 缺 URL → receiver_obj 為空 → 不問 timing，無 _routing。"""
        res = _gen_tenant_interactive("db-c", [], self._PRE + ["webhook", ""])
        assert res == {"tenants": {"db-c": {}}}

    def test_email_missing_smarthost_no_routing(self):
        """email 一律吃 to+smarthost 兩個 input；缺 smarthost → 無 receiver。"""
        res = _gen_tenant_interactive("db-c", [], self._PRE + ["email", "a@x.com", ""])
        assert res == {"tenants": {"db-c": {}}}

    def test_custom_group_by_split(self):
        res = _gen_tenant_interactive("db-c", [], self._PRE + [
            "webhook", "https://h/x",
            "alertname,severity,tenant", "", "", "",
        ])
        assert res["tenants"]["db-c"]["_routing"]["group_by"] == \
            ["alertname", "severity", "tenant"]


class TestGenerateTenantInteractiveOrder:
    """一次跑滿全部段落，pin 詢問次序 + 各段落 golden 整合。"""

    def test_full_sequence_pins_prompt_order(self):
        # 次序：metric → 維護 → 靜音 → dedup → 路由
        res = _gen_tenant_interactive("db-c", ["kubernetes"], [
            "75",        # container_cpu
            "",          # container_cpu_throttle → "25"
            "skip",      # container_memory → 省略
            "y", "2026-06-01T00:00:00Z", "",   # 維護 enable+expires,noreason
            "2", "",     # 靜音 warning 純字串（空 expires）
            "2",         # dedup disable
            "webhook", "https://h/x", "", "", "", "",  # 路由
        ])
        assert res == {"tenants": {"db-c": {
            "container_cpu": "75",
            "container_cpu_throttle": "25",
            "_state_maintenance": {"target": "enable", "expires": "2026-06-01T00:00:00Z"},
            "_silent_mode": "warning",
            "_severity_dedup": "disable",
            "_routing": {
                "receiver": {"type": "webhook", "url": "https://h/x"},
                "group_by": ["alertname", "tenant"],
                "group_wait": "30s", "group_interval": "5m", "repeat_interval": "4h",
            },
        }}}


# ============================================================
# #1310 — Enter 預設在三種 key 上的差別待遇
# ============================================================
class TestPromptDefaultsSplitByKeyClass:
    """互動 prompt 的 Enter 預設，只有**平鍵**被拿掉（#1310 / #1311）。

    ⛔ 這是本次接線引入的 regression 的擋板，不是美化：在 `optional_overrides:`
    清單出貨**之前**，租戶按 Enter 寫下的 `oracle_process_count: 300`
    會被 `ValidateTenantKeys` 當 unknown key 擋掉，所以那個鍵盤動作是無害的。
    清單一旦出貨，同一個 Enter 就真的武裝一條閾值——而 ADR-030 的盲寫參考庫
    對這幾個數字有實測反例（備份批次 560 支 process 誤觸建議值 300）。

    另外兩類**不得**受影響：
      * `defaults` tier —— 平台本來就對每個租戶主張這個值；
      * `<base>_critical` —— 走 `resolveCriticalRows`，base 在 `defaults:` 裡
        有值，所以那個數字是平台真的持有的立場，可以按 Enter 採用。

    prompt 本身在三類上都保留：它是租戶唯一會被告知這些 key 存在的地方。
    """

    _NEUTRAL_TAIL = [_NEUTRAL_MAINT, _NEUTRAL_SILENT, _NEUTRAL_DEDUP,
                     *_NEUTRAL_ROUTING]

    @staticmethod
    def _prompts_for(selected_dbs):
        """跑一次全空輸入，回傳 (每個 prompt 字串, 產生的 tenant 覆寫)。"""
        seen = []

        def _fake_input(prompt=""):
            seen.append(prompt)
            return ""

        with mock.patch("builtins.input", _fake_input):
            res = generate_tenant("db-c", selected_dbs, interactive=True)
        return seen, res["tenants"]["db-c"]

    def test_flat_optional_key_empty_input_writes_nothing(self):
        """oracle：2 個 defaults 全部按 Enter 保留，3 個平鍵一個都不落地。"""
        res = _gen_tenant_interactive("db-c", ["oracle"], [
            "", "",              # defaults: sessions_active / tablespace
            "", "", "",          # 平鍵 ×3 —— 留空即跳過
            *self._NEUTRAL_TAIL,
        ])
        assert res == {"tenants": {"db-c": {
            "oracle_sessions_active": "200",
            "oracle_tablespace_used_percent": "85",
        }}}

    def test_flat_optional_key_typed_value_is_still_honoured(self):
        """拿掉的是**預設**、不是這一格：租戶真的填了就照樣寫進去。"""
        res = _gen_tenant_interactive("db-c", ["oracle"], [
            "", "",
            "60", "", "8589934592",   # wait_time_rate 填 / process_count 跳過 / pga 填
            *self._NEUTRAL_TAIL,
        ])
        assert res == {"tenants": {"db-c": {
            "oracle_sessions_active": "200",
            "oracle_tablespace_used_percent": "85",
            "oracle_wait_time_rate": "60",
            "oracle_pga_allocated_bytes": "8589934592",
        }}}

    def test_critical_optional_key_keeps_its_enter_default(self):
        """postgresql 的 optional tier 全是 `_critical`：行為必須逐字不變。"""
        res = _gen_tenant_interactive("db-c", ["postgresql"], [
            "", "",              # defaults
            "", "",              # _critical ×2 —— Enter 仍採用平台值
            *self._NEUTRAL_TAIL,
        ])
        assert res == {"tenants": {"db-c": {
            "pg_connections": "80",
            "pg_replication_lag": "30",
            "pg_connections_critical": "90",
            "pg_replication_lag_critical": "60",
        }}}

    def test_mixed_packs_split_on_the_shipped_list_predicate(self):
        """同一次執行內兩類並存，判準是 `is_shipped_optional_key`，不是 pack。"""
        prompts, overrides = self._prompts_for(["postgresql", "oracle"])
        assert set(overrides) == {
            "pg_connections", "pg_replication_lag",
            "pg_connections_critical", "pg_replication_lag_critical",
            "oracle_sessions_active", "oracle_tablespace_used_percent",
        }, overrides
        for key in ("oracle_wait_time_rate", "oracle_process_count",
                    "oracle_pga_allocated_bytes"):
            assert key not in overrides

    def test_prompt_text_shows_the_number_as_a_reference_not_a_default(self):
        """數字仍要看得見（它是操作者唯一的起點），但不能長得像 Enter 預設。"""
        prompts, _ = self._prompts_for(["oracle"])
        flat = [p for p in prompts if "oracle_process_count" in p]
        assert len(flat) == 1, prompts
        assert "無平台預設" in flat[0] and "留空跳過" in flat[0], flat[0]
        assert "300" in flat[0], "the reference number must still be shown"
        assert "(300 count)" not in flat[0], (
            "still rendered in the Enter-default shape: " + flat[0])

        valued = [p for p in prompts if "oracle_sessions_active" in p]
        assert len(valued) == 1, prompts
        assert "(200 count)" in valued[0], (
            "the defaults tier prompt is a UX contract and must not change: "
            + valued[0])
        assert "無平台預設" not in valued[0]

    def test_predicate_is_the_one_the_shipped_list_is_built_from(self):
        """判準不得再手抄第四份：prompt 與清單生成必須是同一個函式。"""
        import scaffold_tenant as st
        import _registry_lib

        assert st.is_shipped_optional_key is _registry_lib.is_shipped_optional_key


# ============================================================
# 租戶 stub 的宣告 key 區塊（#1321）
# ============================================================

# ⛔ stub 格式的期望值只有一份，與 `test_init_project.py` 共用
#（`tests/ops/_stub_declared_shared.py`）。兩包釘的是**同一個** renderer 產出的
# 同一段格式；各留一份手抄的結果是：格式一改、只更新其中一份，另一份會繼續綠著
# 而其實已經不再量任何東西。
from _stub_declared_shared import (  # noqa: E402
    REPO_ROOT,
    STUB_PLACEHOLDER,
    STUB_PLACEHOLDER_VALUE,
    paste_declared_lines_under_tenant as _paste_declared_lines_under_tenant,
    shipped_chart_declared_keys,
    stub_key_lines as _stub_key_lines,
)

# 參數化掉的差異：這一支生成器渲染的是中文 stub。
_LANG = "zh"
_STUB_PLACEHOLDER = STUB_PLACEHOLDER[_LANG]

# ⚠️ 這個 `os.path.join` 刻意留在**本檔**而不是搬進共用 helper：
# `verify_diff.build_map` 只掃 `test_*.py` 取路徑引用，搬走等於把
# `helm/threshold-exporter/values.yaml` → 本模組這條 text_map 拿掉——chart 一改
# 就不會再選中下面那條讀 chart 的測試。
_SHIPPED_CHART_VALUES = os.path.join(
    REPO_ROOT, "helm", "threshold-exporter", "values.yaml")


def _shipped_chart_declared_keys():
    return shipped_chart_declared_keys(_SHIPPED_CHART_VALUES)


def _tenant_yaml_for(tmpdir, dbs, tenant="db-c", overrides=None):
    """實跑 generate_defaults + generate_tenant + write_outputs，回傳檔案全文。

    ``overrides`` 給的是「該租戶已經有自己的 key」那個形狀——非互動預設產出的是
    `db-c: {}`（flow mapping），而 flow mapping 底下的縮排不受既有 key 約束，
    量不出 stub 縮排漂移。
    """
    packs = ["kubernetes", *dbs]
    defaults = generate_defaults(packs)
    tenant_data = generate_tenant(tenant, packs, interactive=False)
    if overrides:
        tenant_data["tenants"][tenant] = dict(overrides)
    write_outputs(tmpdir, tenant, defaults, tenant_data, "report")
    with open(os.path.join(tmpdir, f"{tenant}.yaml"), encoding="utf-8") as f:
        return f.read()


class TestTenantStubDeclaredBlock:
    """⛔ 租戶唯一會打開的檔案是 `<tenant>.yaml`——不是 `_defaults.yaml`、不是
    rule-pack header、也不是 Portal。宣告層要讓租戶知道，就得寫在這裡。

    而原本的標頭「三態: 數值=Custom, 省略=Default」對宣告層是反的：那一格沒有值
    可繼承，省略＝沒有值＝靜默。
    """

    def _declared(self, dbs):
        from _registry_lib import shipped_optional_keys_for_packs
        return shipped_optional_keys_for_packs(["kubernetes", *dbs], RULE_PACKS)

    def test_header_no_longer_claims_omission_means_default_for_every_key(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            content = _tenant_yaml_for(tmpdir, ["oracle"])
        assert '# 三態: 數值=Custom, 省略=Default, "disable"=停用' not in content
        assert "省略＝沒有值＝靜默" in content
        assert "沒有值可繼承" in content

    def test_header_also_names_the_critical_tier(self):
        """兩類還不是完整分類：`<base>_critical` 不在宣告清單裡，卻只要 `<base>`
        在 `defaults:` 有值就會產出一條真的 critical row。停在兩類會讀成「已窮盡」
        而其實沒有——判準與 rule-pack header 一分為三那條相同。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            content = _tenant_yaml_for(tmpdir, ["oracle"])
        assert "第三類 <base>_critical" in content
        assert "真的 critical 閾值" in content

    def test_critical_paragraph_matches_the_sibling_defaults_file(self):
        """⛔ 第三類敘述不能是寫死的句子——它只對**本生成器**的產物為真。

        `scaffold_tenant.RULE_PACKS` 的 defaults tier 一個 `_critical` 都沒有，
        `init_project.RULE_PACK_CATALOG` 則**曾經**直接放了 16 個進 `defaults:`；
        同一句「兩個區塊都不列」對當時的它是假的（#1218 已把那 16 個搬走，兩者
        今天同 regime——但推導保留，寫死任一 regime 會在下次移動時靜默變假）。
        所以這條**讀兄弟檔**（同一次 `write_outputs` 寫出去的 `_defaults.yaml`），
        拿它推導出期望值再比對——原本那條只斷言字串存在，而一個假字串同樣會存在。
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            content = _tenant_yaml_for(tmpdir, ["oracle", "postgresql"])
            with open(os.path.join(tmpdir, "_defaults.yaml"), encoding="utf-8") as f:
                sibling = yaml.safe_load(f)
        defaults = sibling["defaults"]
        crit = [k for k in defaults if k.endswith("_critical")]
        assert not crit, (
            "fixture 假設變了：這一支本來是 defaults tier 不含 _critical 的那個"
            "生成器；若真的要改，header 也必須跟著改（這正是本條在守的東西）")

        from _registry_lib import render_tenant_critical_note_lines
        expected = "\n".join(
            render_tenant_critical_note_lines(defaults, lang="zh"))
        assert expected in content

        # 另加一條不經過 renderer 的斷言：header 的用詞必須與產物一致。
        header = content.split("\ntenants:")[0]
        assert ("一個都沒有列" in header) == (not crit)

    def test_block_members_equal_the_derived_set(self):
        """⛔ 這條釘的是**接線**、不是推導——名字讀起來像後者，所以明寫。

        等號右邊是 `shipped_optional_keys_for_packs`，而生成器呼叫的正是同一支：
        推導壞掉時兩邊會一起動，這條照樣綠（實測：推導反轉與 pack 過濾失效都不會
        點亮它，是本 class 其他測試抓到的）。它真正擋得住的是整段消失、少一個 key、
        或同一個 key 出現兩次——值得釘，因為租戶被告知可以把這段當成「我這些 pack
        的完整清單」，而「集合相等（非子字串）」正是讓這句話讀得下去的前提。

        最後一條斷言的來源是獨立的：列出的每個 key 都必須出現在**出貨的** chart
        values 上，讀產物而非重算（與可達性 gate 同一條紀律），它不會跟著生成器動。
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            content = _tenant_yaml_for(tmpdir, ["oracle", "db2", "clickhouse"])
        listed = [k for k, _rhs in _stub_key_lines(content)]
        assert set(listed) == set(self._declared(["oracle", "db2", "clickhouse"]))
        assert len(listed) == len(set(listed)), "重複的 key 行"
        assert set(listed) <= _shipped_chart_declared_keys(), (
            "stub 列出了出貨 chart 沒有宣告的 key——租戶照著填會被唯一受支援的"
            "寫入者判 unknown key（400）")

    def test_block_is_filtered_by_the_selected_packs(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            content = _tenant_yaml_for(tmpdir, ["oracle"])
        listed = {k for k, _ in _stub_key_lines(content)}
        assert listed == set(self._declared(["oracle"]))
        assert not [k for k in listed if k.startswith("db2_")]

    def test_no_declared_key_is_ever_given_a_value(self):
        """⛔ 這條是「好心幫租戶填預設」的擋板。#1310 PR-C 才剛為此拿掉互動 prompt
        的 Enter 預設（ADR-030 對這幾個數字有實測反例：備份批次 560 誤觸建議的
        300、stats-gather 22GB 誤觸建議的 4GiB）；在生成物上填回去是同一個錯。"""
        dbs = ["oracle", "db2", "clickhouse"]
        with tempfile.TemporaryDirectory() as tmpdir:
            content = _tenant_yaml_for(tmpdir, dbs)
        declared = set(self._declared(dbs))

        # (a) 解析後的文件裡不得出現任何宣告 key……
        parsed = yaml.safe_load(content)
        assert not declared & set(parsed["tenants"]["db-c"] or {})

        # (b) ……key 行上也不得出現數字（即使是註解掉的）。
        for key, rhs in _stub_key_lines(content):
            value = rhs.split("#")[0].strip()
            assert value == _STUB_PLACEHOLDER, f"{key} 被填了值: {rhs!r}"

    def test_reference_numbers_are_labelled_as_such(self):
        """數字仍要看得見（它是唯一的起點），但必須標成參考起點而非背書。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            content = _tenant_yaml_for(tmpdir, ["oracle"])
        assert "參考起點 300 count" in content
        assert "非背書" in content

    def test_pack_without_declared_keys_gets_no_block(self):
        """只選 postgresql（optional tier 全是 `_critical`）⇒ 整段不出現。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            content = _tenant_yaml_for(tmpdir, ["postgresql"])
        assert not _stub_key_lines(content)
        assert "平台已宣告、但不主張值的 key" not in content

    def test_stub_is_still_valid_yaml_and_block_is_all_comments(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            content = _tenant_yaml_for(tmpdir, ["oracle", "db2"])
        parsed = yaml.safe_load(content)
        assert set(parsed) == {"tenants"}
        lines = content.split("\n")
        start = next(i for i, l in enumerate(lines)
                     if l.startswith("# ── 平台已宣告"))
        assert all(l.startswith("#") or not l.strip() for l in lines[start:])

    def test_header_pointer_agrees_with_whether_the_block_exists(self):
        """⛔ 標頭那句「清單見檔尾」是這裡最後一句寫死的話。

        檔尾那一段只在宣告清單非空時才附上，而**多數** pack 的 optional tier 全是
        `_critical`（實測 postgresql / mariadb / redis / mongodb / elasticsearch /
        kafka / rabbitmq / jvm / nginx / kubernetes 一律推導成 []）。只選這些
        pack 的租戶，拿到的標頭會指向一個它檔案裡不存在的段落。

        ⛔ 斷言寫成與產物的**等價**、而不是「片語有出現」：改成條件句（「若有宣告
        key 則見檔尾」）同樣能通過存在性檢查，卻把生成器自己做得到的判斷丟回給
        租戶。
        """
        from _registry_lib import TENANT_STUB_DECLARED_HEADING
        heading = TENANT_STUB_DECLARED_HEADING[_LANG]
        shapes = [
            ["postgresql"],            # optional tier 全 _critical
            ["mariadb"],               # 同上，第二個
            ["oracle"],                # 有平鍵
            ["postgresql", "oracle"],  # 混合
        ]
        for dbs in shapes:
            declared = self._declared(dbs)
            with tempfile.TemporaryDirectory() as tmpdir:
                content = _tenant_yaml_for(tmpdir, dbs)
            header = content.split("\ntenants:")[0]

            assert (heading in content) == bool(declared), dbs
            assert ("清單見檔尾" in header) == bool(declared), dbs
            assert ("所以本檔沒有那一段可看" in header) == (not declared), dbs
            if declared:
                assert f"本次選的 pack 有 {len(declared)} 個這種 key" in header, (
                    dbs, header)
            # ……而指路句所承諾的那一段，就是這份清單本身。
            assert [k for k, _ in _stub_key_lines(content)] == declared, dbs

    def test_block_matches_what_the_sibling_defaults_file_declares(self):
        """⛔ 同一次 write_outputs 寫出兩個檔。stub 列出 `_defaults.yaml` 沒宣告的
        key，等於請租戶去踩 tenant-api 的 unknown-key 400。"""
        dbs = ["oracle", "db2", "clickhouse"]
        with tempfile.TemporaryDirectory() as tmpdir:
            content = _tenant_yaml_for(tmpdir, dbs)
            with open(os.path.join(tmpdir, "_defaults.yaml"), encoding="utf-8") as f:
                declared_in_defaults = yaml.safe_load(f)["optional_overrides"]
        assert [k for k, _ in _stub_key_lines(content)] == declared_in_defaults

    def test_declared_lines_survive_the_copy_paste_the_prose_prescribes(self):
        """⛔ stub 對租戶承諾「縮排已對齊」，而在此之前零 gate。

        stub 的縮排是 `_registry_lib.render_tenant_declared_stub_lines` 的常數
        `indent=4`，租戶 key 的縮排是這裡 `yaml.safe_dump` 產生的——兩份各自為政
        的事實，其中一份被寫成對租戶的承諾。實測把那個常數 +4：**324 passed、零
        測試轉紅**，而漂移後照做貼上，最常見情境（租戶已有 4-space key）會得到
        `YAMLError: while parsing a block mapping`。

        所以真的做一次往返，並且**同時**斷言「parse 得過」與「key 落在這個租戶
        底下」：只驗 parse 不夠——縮排過深的一行可能仍是合法 YAML，只是悄悄掛進
        前一個巢狀 mapping。
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            content = _tenant_yaml_for(
                tmpdir, ["oracle", "db2"],
                overrides={"container_cpu": "70", "container_memory": "85"})
        keys = [k for k, _ in _stub_key_lines(content)]
        assert keys, "沒有宣告 key 可往返"

        parsed = yaml.safe_load(_paste_declared_lines_under_tenant(content, "db-c"))
        section = parsed["tenants"]["db-c"]
        for key in keys:
            assert key in section, (
                f"{key} 沒有落在該租戶底下——stub 的縮排已與生成器 dump 出來的不符")
            assert section[key] == STUB_PLACEHOLDER_VALUE[_LANG]
        # 租戶原本的 key 仍在原處
        assert section["container_cpu"] == "70"

    def test_declared_lines_paste_into_an_empty_tenant_as_documented(self):
        """空租戶（`db-c: {}`）走說明裡那條前置步驟：先拆掉 ` {}` 再貼。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            content = _tenant_yaml_for(tmpdir, ["oracle"])
        assert "db-c: {}" in content, "fixture 假設變了：非互動產出應為空 flow mapping"
        keys = [k for k, _ in _stub_key_lines(content)]
        parsed = yaml.safe_load(_paste_declared_lines_under_tenant(content, "db-c"))
        assert set(keys) <= set(parsed["tenants"]["db-c"])

    def test_appending_twice_is_a_no_op(self):
        """共用 appender 宣稱冪等（比照 annotate_saturation_criticals）——釘住它，
        免得日後有人把它接到「重寫既有租戶檔」的路徑上時堆出第二段。"""
        from _registry_lib import append_tenant_declared_stub

        keys = self._declared(["oracle"])
        once = append_tenant_declared_stub("tenants:\n  db-c: {}\n", keys, lang="zh")
        assert append_tenant_declared_stub(once, keys, lang="zh") == once

    def test_cli_path_carries_the_block_end_to_end(self):
        """接線也要釘住：run_non_interactive 這條真正的出貨路徑要有這一段。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            args = argparse.Namespace(
                tenant="db-c", db="oracle", output_dir=tmpdir)
            run_non_interactive(args)
            with open(os.path.join(tmpdir, "db-c.yaml"), encoding="utf-8") as f:
                content = f.read()
        assert {k for k, _ in _stub_key_lines(content)} == set(
            self._declared(["oracle"]))


# ============================================================
# value_counterexample at the interactive prompt (#1176)
# ============================================================
class TestCounterexamplePrompt:
    """The prompt is where a human types the number in, so it is the last
    place the platform can say what it knows about that number."""

    _CE = {"issue": 1176, "direction": "over_fire", "observed": "peak reaches 240"}

    def test_absent_field_prints_nothing(self):
        """Silence must read as "nothing measured", never as "validated"."""
        assert counterexample_prompt_lines({"value": 1}) == []

    def test_directions_do_not_share_wording(self):
        over = counterexample_prompt_lines({"value_counterexample": dict(self._CE)})
        under = counterexample_prompt_lines(
            {"value_counterexample": {**self._CE, "direction": "under_fire"}})
        assert over and under and over != under

    def test_warning_reaches_the_defaults_tier_branch(self, capsys, monkeypatch):
        """The regression this pins: the declared tier is NOT the only one with
        counter-examples. Two defaults-tier keys have them too, and those take
        the branch where the registry number IS the Enter-default — pressing
        Enter arms a value the reference library already contradicted."""
        monkeypatch.setattr("builtins.input", lambda _="": "")
        info = {"value": 200, "unit": "count", "desc": "d",
                "value_counterexample": dict(self._CE)}
        got = prompt_value("oracle_sessions_active", info)   # no_platform_default=False
        assert got == "200", "Enter still adopts the platform value here"
        out = capsys.readouterr().out
        assert "#1176" in out and "peak reaches 240" in out

    def test_non_interactive_defaults_file_carries_the_warning(self):
        """⛔ The path that never asks anyone is the one that needed this most.

        `--non-interactive`, `--from-onboard` batch onboarding, and the
        interactive run whose operator declines to customise all go
        generate_defaults → write_outputs, writing the platform-asserted
        numbers straight into `_defaults.yaml` with no prompt in sight. Three
        of those numbers have measured counter-examples.
        """
        defaults = generate_defaults(["oracle", "db2"])
        dumped = yaml.safe_dump(defaults, default_flow_style=False,
                                allow_unicode=True, sort_keys=False)
        annotated = annotate_defaults_counterexamples(dumped)
        lines = annotated.split("\n")

        marked = 0
        for key, info in (
            (k, i)
            for pack in RULE_PACKS.values()
            for k, i in (pack.get("defaults") or {}).items()
        ):
            if "value_counterexample" not in info or key not in defaults["defaults"]:
                continue
            idx = next(i for i, l in enumerate(lines)
                       if l.strip().startswith(f"{key}:"))
            # Adjacency over the CONTIGUOUS comment block directly above the
            # key, not just `lines[idx - 1]`: the clause wraps at 100 columns,
            # so for a long `observed` the line touching the key is the
            # continuation. Still adjacency — the block must end at this key,
            # so a caveat parked elsewhere in the file does not count.
            block, j = [], idx - 1
            while j >= 0 and lines[j].strip().startswith("#"):
                block.append(lines[j])
                j -= 1
            head = " ".join(" ".join(reversed(block)).replace("#", " ").split())
            assert _CE_MARK in head, f"{key} written with no caveat"
            # ⛔ The WHOLE clause, not its last token. `observed.split()[-1]`
            # is a one-word substring test: two keys whose clauses end in the
            # same word (`… reaches 360 connections` / `… active connections`)
            # could swap caveats undetected — a comparison looser than the
            # thing it compares (blind review, #1344).
            # The ZH clause, because this writer emits Chinese comments. Using
            # `observed` (English) would pass only on the fallback path that
            # `observed_zh` exists to retire (#1344).
            observed = " ".join(registry_counterexample_observed(
                info["value_counterexample"], "zh").split())
            assert observed in head, f"{key} carries a caveat that is not its own"
            marked += 1
        assert marked >= 2, "fewer than two keys exercised — assertion is weak"

        # Counter-case: a key with nothing measured must NOT gain a line, or
        # "silent" would stop meaning "nothing measured".
        clean = next(k for k in defaults["defaults"]
                     if counterexample_for_key(k) is None)
        idx = next(i for i, l in enumerate(lines)
                   if l.strip().startswith(f"{clean}:"))
        assert _CE_MARK not in lines[idx - 1], clean

        # ...and the result is still loadable YAML (comments, not corruption).
        assert yaml.safe_load(annotated) == defaults

    def test_every_registry_counterexample_is_reachable_from_a_prompt(self):
        """Non-vacuity: each key carrying the field must live in a tier the
        interactive flow actually prompts for, or the line renders for nobody."""
        found = 0
        for pack in RULE_PACKS.values():
            for tier in ("defaults", "optional_overrides"):
                for key, info in (pack.get(tier) or {}).items():
                    if "value_counterexample" in info:
                        assert counterexample_prompt_lines(info), key
                        found += 1
        assert found >= 2, "fewer than two keys carry the field"
