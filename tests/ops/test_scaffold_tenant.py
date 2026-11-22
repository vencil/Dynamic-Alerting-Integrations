"""Unit tests for scaffold_tenant.py.

涵蓋核心可測試函式（非互動模式路徑）：
- build_receiver_from_args: CLI 參數 → receiver dict 轉換
- generate_defaults: 平台預設值產生
- generate_tenant: Tenant YAML 產生（non-interactive）
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

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scaffold_tenant import (
    CONFD_DIR,
    relabel_snippet_name,
    build_receiver_from_args,
    counterexample_prompt_lines,
    prompt_value,
    generate_defaults,
    generate_tenant,
    generate_report,
    generate_relabel_snippet,
    print_catalog,
    run_non_interactive,
    run_from_onboard,
    saturation_critical_head,
    saturation_default_keys,
    write_outputs,
    RULE_PACKS,
    SATURATION_CRITICAL_COMMENT,
)
from _registry_lib import (  # noqa: E402
    is_shipped_optional_key,
)
from _lib_exitcodes import EXIT_CALLER_ERROR  # noqa: E402


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

    def test_kubernetes_values_are_built_in_so_no_defaults_section(self):
        """#3832 決策 7：kubernetes 的閾值全是讀取端內建，根檔不複製；
        `defaults:` 沒有任何鍵時整段不寫（沒寫與空的讀法相同）。"""
        result = generate_defaults(["kubernetes"])
        assert "defaults" not in result
        assert "state_filters" in result

    def test_includes_state_filters(self):
        """state_filters 從 kubernetes pack 提取。"""
        result = generate_defaults(["kubernetes"])
        assert "state_filters" in result
        assert "container_crashloop" in result["state_filters"]

    @pytest.mark.parametrize("dbs", [["kubernetes", "postgresql"], ["kubernetes", "redis"],
                                     ["kubernetes", "elasticsearch", "oracle", "rabbitmq"],
                                     list(RULE_PACKS)])
    def test_writes_no_threshold_key(self, dbs):
        """#3924：登錄表的鍵都內建在讀取端（平台值或 off），根檔一個閾值鍵都
        不寫——沒有 `defaults:`、沒有 `optional_overrides:`，不論選哪些 pack。"""
        result = generate_defaults(dbs)
        assert "defaults" not in result and "optional_overrides" not in result, result

    def test_unknown_db_ignored(self):
        """未知的 DB 類型不造成錯誤。"""
        assert generate_defaults(["kubernetes", "nonexistent_db"]) == generate_defaults(["kubernetes"])


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

            assert os.path.isfile(os.path.join(tmpdir, CONFD_DIR, "_defaults.yaml"))
            assert os.path.isfile(os.path.join(tmpdir, CONFD_DIR, "db-c.yaml"))
            assert os.path.isfile(os.path.join(tmpdir, "scaffold-report.txt"))

    @_skipif_unix_modes
    def test_file_permissions(self):
        """conf.d 檔由 `da-guard write` 寫出，新檔與被取代的檔一律 0o644（#3535：
        conf.d 屬 repo 生成物，不沿用原檔權限）；報告不是 conf.d 檔，仍由本工具以
        0o600 寫出。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            defaults = {"defaults": {}}
            tenant = {"tenants": {"db-c": {}}}
            write_outputs(tmpdir, "db-c", defaults, tenant, "report")
            for fn, want in [(f"{CONFD_DIR}/_defaults.yaml", 0o644),
                             (f"{CONFD_DIR}/db-c.yaml", 0o644),
                             ("scaffold-report.txt", 0o600)]:
                path = os.path.join(tmpdir, fn)
                mode = os.stat(path).st_mode & 0o777
                assert mode == want, f"{fn} permissions {oct(mode)} != {oct(want)}"
            # A replace does not keep the mode the file has (#3535). Another
            # text: the same text is `unchanged`, and nothing is written.
            os.chmod(os.path.join(tmpdir, CONFD_DIR, "db-c.yaml"), 0o640)
            write_outputs(tmpdir, "db-c", {"defaults": {"mysql_connection_usage_percent": 80}},
                          {"tenants": {"db-c": {"mysql_connection_usage_percent": "70"}}}, "report")
            assert os.stat(os.path.join(tmpdir, CONFD_DIR, "db-c.yaml")).st_mode & 0o777 == 0o644

    def test_creates_relabel_file(self):
        """提供 relabel_snippet 時建立 relabel 檔案。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            defaults = {"defaults": {}}
            tenant = {"tenants": {"db-c": {}}}
            snippet = "# relabel\nrelabel_configs: []"
            write_outputs(tmpdir, "db-c", defaults, tenant, "report",
                          relabel_snippet=snippet)
            relabel_path = os.path.join(tmpdir, "relabel_configs-db-c.txt")
            assert os.path.isfile(relabel_path)

    def test_yaml_content_valid(self):
        """輸出的 YAML 檔案內容正確。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            defaults = {"defaults": {"container_cpu": 80}}
            tenant = {"tenants": {"db-c": {"container_cpu": "70"}}}
            write_outputs(tmpdir, "db-c", defaults, tenant, "report")

            with open(os.path.join(tmpdir, CONFD_DIR, "db-c.yaml"), encoding="utf-8") as f:
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

    def test_sections_are_dimensions_and_state_filters_only(self):
        """#3924：只剩 `dimensions:`（#3475）與 `state_filters:`。"""
        result = generate_defaults(list(RULE_PACKS.keys()))
        assert set(result) <= {"dimensions", "state_filters"}, set(result)


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

        with open(os.path.join(config_dir, CONFD_DIR, "_defaults.yaml"), encoding="utf-8") as f:
            parsed = yaml.safe_load(f)
        # kubernetes + postgresql 的值全是內建：根檔沒有 `defaults:`。
        assert "defaults" not in parsed
        assert "state_filters" in parsed

    def test_tenant_yaml_no_reserved_leak(self, config_dir):
        """tenant YAML 不含 _reserved prefix keys（除 _routing 等預期 key）。"""
        defaults = generate_defaults(["kubernetes"])
        tenant = generate_tenant("db-z", ["kubernetes"], interactive=False)
        write_outputs(config_dir, "db-z", defaults, tenant, "report")

        with open(os.path.join(config_dir, CONFD_DIR, "db-z.yaml"), encoding="utf-8") as f:
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
            assert os.path.exists(os.path.join(d, CONFD_DIR, "db-x.yaml"))
            assert os.path.exists(os.path.join(d, CONFD_DIR, "_defaults.yaml"))

    def test_with_profile(self):
        with tempfile.TemporaryDirectory() as d:
            # The profile is defined in the tree first: a tenant naming no
            # profile is refused (#3591).
            # #4014: the draft conf.d is judged as a tree of its own, so the
            # profile goes there; a platform file is not another tenant.
            os.makedirs(os.path.join(d, CONFD_DIR))
            with open(os.path.join(d, CONFD_DIR, "_profiles.yaml"), "w", encoding="utf-8") as f:
                f.write("profiles:\n  high-load:\n    mysql_connection_usage_percent: 95\n")
            args = argparse.Namespace(
                tenant="db-x", db="mariadb", output_dir=d,
                profile="high-load", silent_mode=None,
                severity_dedup="enable", routing_receiver=None,
                namespaces=None,
            )
            run_non_interactive(args)
            with open(os.path.join(d, CONFD_DIR, "db-x.yaml"), encoding="utf-8") as f:
                data = yaml.safe_load(f)
            assert data["tenants"]["db-x"]["_profile"] == "high-load"

    def test_with_a_profile_the_tree_does_not_define_writes_nothing(self):
        """#3591: `_profile` naming a profile no root platform file defines
        is a value the exporter does not read (value_unread); da-guard write
        refuses it and nothing is written."""
        with tempfile.TemporaryDirectory() as d:
            args = argparse.Namespace(
                tenant="db-x", db="mariadb", output_dir=d,
                profile="high-load", silent_mode=None,
                severity_dedup="enable", routing_receiver=None,
                namespaces=None,
            )
            with pytest.raises(SystemExit):
                run_non_interactive(args)
            assert not os.path.exists(os.path.join(d, CONFD_DIR, "db-x.yaml"))

    def test_with_silent_mode(self):
        with tempfile.TemporaryDirectory() as d:
            args = argparse.Namespace(
                tenant="db-x", db="mariadb", output_dir=d,
                profile=None, silent_mode="warning",
                severity_dedup="enable", routing_receiver=None,
                namespaces=None,
            )
            run_non_interactive(args)
            with open(os.path.join(d, CONFD_DIR, "db-x.yaml"), encoding="utf-8") as f:
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
            with open(os.path.join(d, CONFD_DIR, "db-x.yaml"), encoding="utf-8") as f:
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
            with open(os.path.join(d, CONFD_DIR, "db-x.yaml"), encoding="utf-8") as f:
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
            relabel_file = os.path.join(d, "relabel_configs-db-x.txt")
            assert os.path.exists(relabel_file)

    def test_namespaces_leave_no_unread_top_level_key(self):
        """#3671: the N:1 relabel snippet sits beside the conf.d files; as a
        `.yaml` it was a tenant file whose `relabel_configs:` no reader reads,
        which the shared key verdict blocks the whole tree on."""
        import subprocess
        with tempfile.TemporaryDirectory() as d:
            args = argparse.Namespace(
                tenant="db-x", db="mariadb", output_dir=d,
                profile=None, silent_mode=None,
                severity_dedup="enable", routing_receiver=None,
                namespaces="ns1,ns2",
            )
            run_non_interactive(args)
            # #4014: the snippet stays at -o beside the draft conf.d; -o holds
            # no file a conf.d reader reads (the draft is a hidden directory),
            # so -o inside a served tree adds no tenant file there either.
            import _lib_parse_tree
            assert os.path.isfile(os.path.join(d, relabel_snippet_name("db-x")))
            assert [e.path for e in _lib_parse_tree.list_tree(d).config_files()] == []
            binary = os.environ.get("DA_GUARD_BINARY", "da-guard")
            out = subprocess.run([binary, "key-verdict", "--config-dir",
                                  os.path.join(d, CONFD_DIR)],
                                 capture_output=True, text=True, encoding="utf-8", timeout=60)
            assert out.returncode == 0, out.stderr
            verdict = json.loads(out.stdout)
            reasons = [f.get("reason") for f in verdict.get("findings", [])]
            assert "tenant_file_top_level" not in reasons, verdict["findings"]
            assert not verdict.get("blocks"), verdict

    def test_a_config_named_extra_is_refused(self, monkeypatch):
        """The guard: a file written beside the batch that a conf.d reader
        would scan is refused before anything is written (#3671)."""
        # Patch the namespace run_non_interactive resolves names in: another
        # test may have re-imported scaffold_tenant, and `sys.modules` would
        # then hold a different module object than the one bound here.
        monkeypatch.setitem(run_non_interactive.__globals__, "relabel_snippet_name",
                            lambda t: f"relabel_configs-{t}.yaml")
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "conf.d")
            args = argparse.Namespace(
                tenant="db-x", db="mariadb", output_dir=out,
                profile=None, silent_mode=None,
                severity_dedup="enable", routing_receiver=None,
                namespaces="ns1,ns2",
            )
            with pytest.raises(SystemExit) as exc:
                run_non_interactive(args)
            assert exc.value.code == EXIT_CALLER_ERROR
            assert not os.path.exists(out)

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
            with open(os.path.join(d, CONFD_DIR, "db-x.yaml"), encoding="utf-8") as f:
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
            with open(os.path.join(d, CONFD_DIR, "db-x.yaml"), encoding="utf-8") as f:
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
            with open(os.path.join(d, CONFD_DIR, "db-x.yaml"), encoding="utf-8") as f:
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
            mapping_path = os.path.join(d, CONFD_DIR, "_instance_mapping.yaml")
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
            mapping_path = os.path.join(d, CONFD_DIR, "_instance_mapping.yaml")
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
            relabel_file = os.path.join(d, "relabel_configs-db-x.txt")
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
            assert not os.path.exists(os.path.join(d, CONFD_DIR, "_instance_mapping.yaml"))
            assert not os.path.exists(os.path.join(d, "relabel_configs-db-x.txt"))

    @_skipif_unix_modes
    def test_mapping_file_permissions(self):
        """_instance_mapping.yaml is a conf.d file `da-guard write` creates:
        0o644, a repo artifact's mode (#3535)."""
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
            mapping_path = os.path.join(d, CONFD_DIR, "_instance_mapping.yaml")
            mode = os.stat(mapping_path).st_mode & 0o777
            assert mode == 0o644


# ---------------------------------------------------------------------------
# run_from_onboard
# ---------------------------------------------------------------------------

def _onboard_ns(from_onboard, **kw):
    """The namespace `--from-onboard <path>` parses to (#3445: the flag is a
    `_lib_input` file declaration), plus *kw*."""
    import _lib_input
    import scaffold_tenant
    parser = argparse.ArgumentParser()
    _lib_input.add_input(parser, "--from-onboard",
                         decode=scaffold_tenant.ONBOARD_HINTS_DOCUMENT)
    args = parser.parse_args(["--from-onboard", from_onboard])
    for key, value in kw.items():
        setattr(args, key, value)
    return args


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

            args = _onboard_ns(
                from_onboard=hints_path, output_dir=d,
            )
            run_from_onboard(args)
            assert os.path.exists(os.path.join(d, CONFD_DIR, "db-a.yaml"))
            assert os.path.exists(os.path.join(d, CONFD_DIR, "db-b.yaml"))

    def test_one_defaults_file_holds_every_tenants_packs(self):
        """⛔ 迴歸（#3206 10c-3）：每個租戶各算一份 `_defaults.yaml`、寫到同一個
        路徑，最後一個贏。現在只寫一份（所有租戶的 pack 取聯集）；#3924 起它
        不寫閾值鍵，兩個租戶檔都在。"""
        with tempfile.TemporaryDirectory() as d:
            hints = {
                "tenants": ["db-a", "db-b"],
                "db_types": {"db-a": ["mariadb"], "db-b": ["oracle"]},
                "routing_hints": {},
            }
            hints_path = os.path.join(d, "hints.json")
            with open(hints_path, "w", encoding="utf-8") as f:
                json.dump(hints, f)
            run_from_onboard(_onboard_ns(from_onboard=hints_path, output_dir=d))
            with open(os.path.join(d, CONFD_DIR, "_defaults.yaml"), encoding="utf-8") as f:
                written = yaml.safe_load(f)
            with open(os.path.join(d, CONFD_DIR, "db-a.yaml"), encoding="utf-8") as f:
                db_a = f.read()
            with open(os.path.join(d, CONFD_DIR, "db-b.yaml"), encoding="utf-8") as f:
                db_b = f.read()
        # #3924: one file for every tenant, and no threshold key in it (each
        # is built into the readers) — so neither tenant's packs can be lost.
        assert "defaults" not in written and "optional_overrides" not in written, written
        assert "db-a" in db_a and "db-b" in db_b

    def test_a_tenant_named_twice_is_scaffolded_once(self):
        """迴歸（#3206 10c-3）：hints 的 tenants 重複時，舊版 rc 0（同一檔寫兩次），
        改成一批之後兩個 `db-a.yaml` 會被 da-guard 以 bad_input 拒——進批次前
        保序去重。"""
        with tempfile.TemporaryDirectory() as d:
            hints = {"tenants": ["db-a", "db-b", "db-a"],
                     "db_types": {"db-a": ["mariadb"], "db-b": ["mariadb"]},
                     "routing_hints": {}}
            hints_path = os.path.join(d, "hints.json")
            with open(hints_path, "w", encoding="utf-8") as f:
                json.dump(hints, f)
            out = os.path.join(d, "out")
            run_from_onboard(_onboard_ns(from_onboard=hints_path, output_dir=out))
            assert sorted(os.listdir(out)) == [CONFD_DIR.split("/")[0], "scaffold-report.txt"]
            assert sorted(os.listdir(os.path.join(out, CONFD_DIR))) == [
                "_defaults.yaml", "db-a.yaml", "db-b.yaml"]

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

            args = _onboard_ns(
                from_onboard=hints_path, output_dir=d,
            )
            run_from_onboard(args)
            with open(os.path.join(d, CONFD_DIR, "db-a.yaml"), encoding="utf-8") as f:
                data = yaml.safe_load(f)
            routing = data["tenants"]["db-a"]["_routing"]
            assert routing["group_wait"] == "10s"

    def test_invalid_hints_exits(self):
        with tempfile.TemporaryDirectory() as d:
            args = _onboard_ns(
                from_onboard=os.path.join(d, "nonexistent.json"),
                output_dir=d,
            )
            # #3445: refused by the lib's reading, named (a CallerError
            # run() turns into rc 2).
            from _lib_exitcodes import CallerError
            with pytest.raises(CallerError) as exc:
                run_from_onboard(args)
            assert exc.value.reason == "file_not_found"

    # ADR-035 (blind review F1): every id in the hints is judged BEFORE the
    # first file is written — `Team_A` used to produce Team_A.yaml at rc 0,
    # a conf.d generate-routes then refuses in every mode.
    @pytest.mark.parametrize("tenants", [["Team_A"], ["db-a", "Team_A"], ["db-a", "x" * 64]])
    def test_invalid_tenant_id_refused_before_any_write(self, tenants, capsys):
        with tempfile.TemporaryDirectory() as d:
            hints_path = os.path.join(d, "hints.json")
            with open(hints_path, "w", encoding="utf-8") as f:
                json.dump({"tenants": tenants, "db_types": {}, "routing_hints": {}}, f)
            out = os.path.join(d, "out")
            args = _onboard_ns(from_onboard=hints_path, output_dir=out)
            with pytest.raises(SystemExit) as exc_info:
                run_from_onboard(args)
            assert exc_info.value.code == EXIT_CALLER_ERROR
            assert not os.path.exists(out), os.listdir(out)
        err = capsys.readouterr().err
        assert "DNS-1123" in err and repr(tenants[-1]) in err, err

    # An id that is no string (a list, a mapping) is named as an invalid id,
    # not a TypeError from de-duplicating it first (blind review, #3206 10c-3).
    @pytest.mark.parametrize("bad", [["a"], {"x": 1}])
    def test_unhashable_tenant_id_is_named_not_a_traceback(self, bad, capsys):
        with tempfile.TemporaryDirectory() as d:
            hints_path = os.path.join(d, "hints.json")
            with open(hints_path, "w", encoding="utf-8") as f:
                json.dump({"tenants": [bad, "db-b"], "db_types": {}, "routing_hints": {}}, f)
            out = os.path.join(d, "out")
            with pytest.raises(SystemExit) as exc_info:
                run_from_onboard(_onboard_ns(from_onboard=hints_path, output_dir=out))
            assert exc_info.value.code == EXIT_CALLER_ERROR
            assert not os.path.exists(out), os.listdir(out)
        err = capsys.readouterr().err
        assert "DNS-1123" in err and repr(bad) in err and "TypeError" not in err, err

    def test_no_tenants_exits(self):
        with tempfile.TemporaryDirectory() as d:
            hints_path = os.path.join(d, "empty.json")
            with open(hints_path, "w", encoding="utf-8") as f:
                json.dump({"tenants": []}, f)

            args = _onboard_ns(
                from_onboard=hints_path, output_dir=d,
            )
            with pytest.raises(SystemExit):
                run_from_onboard(args)


# ============================================================
# 飽和類指標 _critical 教育註解（metric_class == saturation）
# ============================================================

# 被標 saturation 的 defaults 鍵（真 N=21：#3778 把 mysql_threads_running 移出 defaults 層；同步 CHANGELOG 與 portal 測試常數）
EXPECTED_SATURATION_KEYS = {
    # kubernetes
    "container_cpu", "container_cpu_throttle", "container_memory",
    # mariadb（mysql_threads_running 自 #3778 在 optional_overrides 層，不在 defaults）
    "mysql_connection_usage_percent",
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


class TestSaturationCriticalHead:
    """saturation_critical_head()：哪個節點上方有教育註解（#3206 10c-3：註解是
    交給 da-guard write 的資料，不再是對 PyYAML 文字的行插入）。"""

    _TEXT = SATURATION_CRITICAL_COMMENT[len("# "):]

    def test_saturation_critical_key_gets_the_note(self):
        at = ("tenants", "db-x", "mysql_connection_usage_percent_critical")
        assert saturation_critical_head(at, "50") == self._TEXT

    def test_non_saturation_critical_untouched(self):
        """非飽和鍵（pg_replication_lag_critical）不插註解。"""
        assert saturation_critical_head(
            ("tenants", "db-x", "pg_replication_lag_critical"), "60") is None

    def test_base_key_without_critical_suffix_untouched(self):
        """飽和 base 鍵本身（無 _critical 後綴）不插註解。"""
        assert saturation_critical_head(
            ("tenants", "db-x", "mysql_connection_usage_percent"), "80") is None

    def test_a_sequence_item_is_not_a_key(self):
        assert saturation_critical_head(
            ("tenants", "db-x", "_namespaces", "[0]"),
            "mysql_connection_usage_percent_critical") is None

    def test_the_note_lands_on_the_key_in_the_value_sent(self):
        """data_value 把註解掛在那一個鍵上（其他鍵沒有）。"""
        import _lib_confedit
        v = _lib_confedit.data_value({"tenants": {"db-x": {
            "mysql_connection_usage_percent_critical": "90",
            "pg_replication_lag_critical": "60"}}}, head=saturation_critical_head)
        rows = v["map"][0][1]["map"][0][1]["map"]
        assert rows[0] == ["mysql_connection_usage_percent_critical", {"str": "90"},
                           {"head": self._TEXT}]
        assert rows[1] == ["pg_replication_lag_critical", {"str": "60"}]


class TestSaturationWriteOutputs:
    """write_outputs() 飽和註解整合測試。"""

    def test_tenant_yaml_has_comment_above_saturation_critical(self):
        """含 mysql_connection_usage_percent_critical 的 tenant YAML 註解在該鍵上一行。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            defaults = {"defaults": {"mysql_connection_usage_percent": 80}}
            tenant = {"tenants": {"db-x": {"mysql_connection_usage_percent_critical": "90"}}}
            write_outputs(tmpdir, "db-x", defaults, tenant, "report")
            with open(os.path.join(tmpdir, CONFD_DIR, "db-x.yaml"), encoding="utf-8") as f:
                lines = f.read().split("\n")
            idx = next(i for i, l in enumerate(lines)
                       if l.strip().startswith("mysql_connection_usage_percent_critical:"))
            assert lines[idx - 1].strip() == SATURATION_CRITICAL_COMMENT

    def test_empty_tenant_noop(self):
        """非互動空 tenant → 無任何飽和註解。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            defaults = generate_defaults(["kubernetes"])
            tenant = generate_tenant("db-x", ["kubernetes"], interactive=False)
            write_outputs(tmpdir, "db-x", defaults, tenant, "report")
            with open(os.path.join(tmpdir, CONFD_DIR, "db-x.yaml"), encoding="utf-8") as f:
                content = f.read()
            assert SATURATION_CRITICAL_COMMENT not in content


class TestSaturationGenerateReport:
    """generate_report() 飽和教育段測試。"""

    def test_mariadb_report_includes_education_section(self):
        report = generate_report("db-x", ["kubernetes", "mariadb"], "/tmp/out")
        assert "飽和類指標的 critical 層" in report
        # #3778: mysql_threads_running_critical is not offered — its base has
        # no defaults value, so the critical row cannot be served.
        assert "mysql_threads_running_critical" not in report
        assert "mysql_connection_usage_percent_critical" in report
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
        """D-A PR-3 起 RULE_PACKS 由 registry 生成，每個鍵都帶 metric_class，
        值域即 registry schema 的 enum。消費端（saturation 教育段、portal）
        只認 defaults tier 的 "saturation"，所以多出的類別不改變任何輸出。"""
        schema_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..",
                                   "docs", "schemas", "threshold-registry.schema.json")
        with open(schema_path, encoding="utf-8") as fh:
            schema = json.load(fh)
        (key_schema,) = schema["properties"]["keys"]["patternProperties"].values()
        enum = set(key_schema["properties"]["metric_class"]["enum"])
        for name, pack in RULE_PACKS.items():
            for section in ("defaults", "optional_overrides"):
                for key, info in pack.get(section, {}).items():
                    assert info.get("metric_class") in enum, f"{name}.{section}.{key}"

    def test_saturation_outside_defaults_is_only_a_critical_variant(self):
        """optional_overrides 裡的 saturation 只能是 saturation base 的
        `_critical`（繼承而來），或平台宣告、不給值的平鍵（#3778 的
        mysql_threads_running）與它的 `_critical`——不會長出別的 defaults 外飽和鍵。"""
        bases = saturation_default_keys()
        declared = {k for pack in RULE_PACKS.values()
                    for k, info in pack.get("optional_overrides", {}).items()
                    if is_shipped_optional_key(k) and info.get("metric_class") == "saturation"}
        assert declared == {"mysql_threads_running"}, declared
        for name, pack in RULE_PACKS.items():
            for key, info in pack.get("optional_overrides", {}).items():
                if info.get("metric_class") == "saturation" and key not in declared:
                    assert key.endswith("_critical") and \
                        key[:-len("_critical")] in bases | declared, \
                        f"{name}.optional_overrides.{key}"

    def test_saturation_set_matches_expected(self):
        """被標 set == 預期常數（真 N=21）。"""
        assert saturation_default_keys() == EXPECTED_SATURATION_KEYS
        assert len(EXPECTED_SATURATION_KEYS) == 21


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

    @pytest.mark.parametrize("argv", [
        ["--generate-profile", "p", "--db", "mariadb"],
        ["--tier", "staging", "--tenant", "db-x", "--db", "mariadb", "--non-interactive"],
    ])
    def test_profile_generator_flags_are_gone(self, argv, capsys, tmp_path):
        """#3691: a generated profile froze a copy of every value it wrote (a
        profile is expanded into each bound tenant's overrides), and staging's
        x1.2 inverted tiers. Both flags are refused as unknown (rc 2)."""
        import scaffold_tenant
        # -o: were the flags back, a run must not write into the cwd.
        with mock.patch("sys.argv", ["scaffold_tenant.py", *argv, "-o", str(tmp_path)]):
            with pytest.raises(SystemExit) as exc_info:
                scaffold_tenant.main()
        assert exc_info.value.code == EXIT_CALLER_ERROR
        assert "unrecognized arguments" in capsys.readouterr().err
        assert not hasattr(scaffold_tenant, "generate_profile")

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
            assert os.path.exists(os.path.join(d, CONFD_DIR, "db-test.yaml"))

    def test_non_interactive_missing_args_exits(self):
        import scaffold_tenant
        with mock.patch("sys.argv", [
            "scaffold_tenant.py", "--non-interactive",
        ]):
            with pytest.raises(SystemExit):
                scaffold_tenant.main()

    # ADR-035: `--tenant` becomes a conf.d key and a file name, and was not
    # checked at all — `Team_A.yaml` was written. Refused now, rc 2, nothing
    # written; the message cites the rule's own description.
    @pytest.mark.parametrize("tenant", ["Team_A", "UPPER", "bad tenant", "a" * 64, "x-"])
    def test_invalid_tenant_id_is_refused(self, tenant, capsys):
        import scaffold_tenant
        with tempfile.TemporaryDirectory() as d:
            with mock.patch("sys.argv", [
                "scaffold_tenant.py", "--tenant", tenant, "--db", "mariadb",
                "-o", d, "--non-interactive",
            ]):
                with pytest.raises(SystemExit) as exc_info:
                    scaffold_tenant.main()
            assert exc_info.value.code == EXIT_CALLER_ERROR
            assert os.listdir(d) == [], os.listdir(d)
        err = capsys.readouterr().err
        assert repr(tenant) in err and "DNS-1123" in err, err

    def test_interactive_tenant_id_is_refused(self, capsys):
        import scaffold_tenant
        with tempfile.TemporaryDirectory() as d:
            with mock.patch("builtins.input", side_effect=["Team_A"]):
                with pytest.raises(SystemExit) as exc_info:
                    scaffold_tenant.run_interactive(d)
            assert exc_info.value.code == EXIT_CALLER_ERROR
            assert os.listdir(d) == [], os.listdir(d)


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
        """空白→沿用平台值（不寫，#3924）、'disable'→保留、'skip'→省略。"""
        # kubernetes defaults 序：container_cpu(80), container_cpu_throttle(25),
        # container_memory(85)——三個都是平台內建值。
        res = _gen_tenant_interactive("db-c", ["kubernetes"], [
            "",         # container_cpu → 沿用平台值，不寫
            "disable",  # container_cpu_throttle → "disable"（保留）
            "skip",     # container_memory → None（省略）
            "skip", "skip", "skip",   # k8s optional_overrides(3: container_cpu_critical, container_cpu_throttle_critical, container_memory_critical)
            _NEUTRAL_MAINT, _NEUTRAL_SILENT, _NEUTRAL_DEDUP, *_NEUTRAL_ROUTING,
        ])
        assert res == {"tenants": {"db-c": {
            "container_cpu_throttle": "disable",
        }}}

    def test_custom_values_kept_as_strings(self):
        """自訂數值以字串保留。"""
        res = _gen_tenant_interactive("db-c", ["kubernetes"], [
            "75", "30", "90",
            "skip", "skip", "skip",   # k8s optional_overrides(3: container_cpu_critical, container_cpu_throttle_critical, container_memory_critical)
            _NEUTRAL_MAINT, _NEUTRAL_SILENT, _NEUTRAL_DEDUP, *_NEUTRAL_ROUTING,
        ])
        assert res == {"tenants": {"db-c": {
            "container_cpu": "75",
            "container_cpu_throttle": "30",
            "container_memory": "90",
        }}}

    def test_optional_overrides_prompted_after_defaults(self):
        """defaults 問完才問 optional_overrides（mariadb 的 _critical tiers）。"""
        # 次序：k8s defaults(3) → k8s optional_overrides(3: container_*_critical)
        #       → mariadb defaults(2: mysql_connection_usage_percent, mysql_replication_lag)
        #       → mariadb optional_overrides(4: mysql_threads_running,
        #         mysql_connection_usage_percent_critical, mysql_threads_running_critical,
        #         mysql_replication_lag_critical)。#3924：每個 base 都內建（平台值或
        #       off），所以 mysql_threads_running_critical 也問。
        res = _gen_tenant_interactive("db-c", ["kubernetes", "mariadb"], [
            "skip", "skip", "skip",   # k8s 3 defaults
            "skip", "skip", "skip",   # k8s 3 optional_overrides (container_*_critical)
            "skip", "skip",           # mariadb 2 defaults
            "skip",                   # mysql_threads_running → 省略
            "100",                    # mysql_connection_usage_percent_critical → 保留
            "skip",                   # mysql_threads_running_critical → 省略
            "skip",                   # mysql_replication_lag_critical → 省略
            _NEUTRAL_MAINT, _NEUTRAL_SILENT, _NEUTRAL_DEDUP, *_NEUTRAL_ROUTING,
        ])
        assert res == {"tenants": {"db-c": {"mysql_connection_usage_percent_critical": "100"}}}


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
            "",          # container_cpu_throttle → 沿用平台值，不寫（#3924）
            "skip",      # container_memory → 省略
            "skip", "skip", "skip",   # k8s optional_overrides(3: container_cpu_critical, container_cpu_throttle_critical, container_memory_critical)
            "y", "2026-06-01T00:00:00Z", "",   # 維護 enable+expires,noreason
            "2", "",     # 靜音 warning 純字串（空 expires）
            "2",         # dedup disable
            "webhook", "https://h/x", "", "", "", "",  # 路由
        ])
        assert res == {"tenants": {"db-c": {
            "container_cpu": "75",
            "_state_maintenance": {"target": "enable", "expires": "2026-06-01T00:00:00Z"},
            "_silent_mode": "warning",
            "_severity_dedup": "disable",
            "_routing": {
                "receiver": {"type": "webhook", "url": "https://h/x"},
                "group_by": ["alertname", "tenant"],
                "group_wait": "30s", "group_interval": "5m", "repeat_interval": "4h",
            },
        }}}


class TestPromptEnterWritesNothing:
    """互動 prompt 按 Enter 一律不寫值（#3924）。

    登錄表的鍵都內建在讀取端：平台出貨即啟用的鍵省略就是平台值，所以 Enter
    沿用平台值、不寫；其餘的鍵（operator 鍵、每一個 `<base>_critical`）沒有
    平台值，Enter 跳過——把登錄表的參考數字預設寫進租戶檔，等於替客戶武裝一個
    它沒校準過的數字（ADR-030 的盲寫參考庫對其中幾個有實測反例）。數字仍顯示，
    因為那是操作者唯一的起點；prompt 在每一類鍵上都保留，它是租戶唯一會被告知
    這些 key 存在的地方。
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

    @pytest.mark.parametrize("dbs", [["oracle"], ["postgresql"], ["postgresql", "oracle"]])
    def test_all_empty_input_writes_no_threshold(self, dbs):
        prompts, overrides = self._prompts_for(dbs)
        assert not [k for k in overrides if not k.startswith("_")], overrides
        keys = {k for db in dbs for tier in ("defaults", "optional_overrides")
                for k in RULE_PACKS[db].get(tier, {})}
        for key in keys:  # every key is still asked
            assert [p for p in prompts if p.strip().startswith(f"{key} [")], key

    def test_a_typed_value_is_written(self):
        """拿掉的是**預設**、不是這一格：租戶真的填了就照樣寫進去。"""
        answers = iter(["70"])

        def _fake_input(prompt=""):
            if prompt.strip().startswith("oracle_wait_time_rate ["):
                return next(answers)
            return ""

        with mock.patch("builtins.input", _fake_input):
            res = generate_tenant("db-c", ["oracle"], interactive=True)
        assert res["tenants"]["db-c"].get("oracle_wait_time_rate") == "70"

    def test_prompt_text_per_kind_of_key(self):
        """平台給值的鍵：寫出平台值、留空沿用；沒有平台值的鍵：數字標成參考值、留空跳過。"""
        from _rule_packs_generated import BUILTIN_DEFAULT_KEYS
        prompts, _ = self._prompts_for(["oracle"])
        oracle = {k for tier in ("defaults", "optional_overrides")
                  for k in RULE_PACKS["oracle"].get(tier, {})}
        armed = sorted(oracle & set(BUILTIN_DEFAULT_KEYS))
        unarmed = sorted(oracle - set(BUILTIN_DEFAULT_KEYS))
        assert armed and unarmed
        for key in armed:
            (p,) = [p for p in prompts if p.strip().startswith(f"{key} [")]
            assert "平台值" in p and "留空沿用平台值" in p, p
        for key in unarmed:
            (p,) = [p for p in prompts if p.strip().startswith(f"{key} [")]
            assert "無平台值" in p and "留空跳過" in p and "參考值" in p, p


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
        """The counter-example prints for a key with no platform value too, and
        Enter writes nothing (#3924: the registry number is a reference, never
        the Enter-default)."""
        monkeypatch.setattr("builtins.input", lambda _="": "")
        info = {"value": 200, "unit": "count", "desc": "d",
                "value_counterexample": dict(self._CE)}
        got = prompt_value("oracle_sessions_active", info)
        assert got is None, "Enter wrote the registry number"
        out = capsys.readouterr().out
        assert "#1176" in out and "peak reaches 240" in out


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


# ============================================================
# #3227: every _silent_mode value scaffold writes is one the tenant schema
# takes (its silentMode enums are pinned to the exporter's words by
# TestSilentModeSchemaEnumsMatchReader in pkg/config).
# ============================================================
def _silent_mode_schema_enums():
    with open(os.path.join(REPO_ROOT, "docs", "schemas", "tenant-config.schema.json"),
              encoding="utf-8") as f:
        branches = json.load(f)["definitions"]["silentMode"]["oneOf"]
    scalar = next(b["enum"] for b in branches if b.get("type") == "string")
    target = next(b["properties"]["target"]["enum"] for b in branches if b.get("type") == "object")
    return scalar, target


class TestSilentModeChoicesWithinSchema:
    def test_cli_choices_are_schema_words(self):
        from scaffold_tenant import SILENT_MODE_CHOICES
        scalar, _ = _silent_mode_schema_enums()
        assert SILENT_MODE_CHOICES and set(SILENT_MODE_CHOICES) <= set(scalar), \
            sorted(set(SILENT_MODE_CHOICES) - set(scalar))

    def test_cli_parser_offers_exactly_those_choices(self):
        # The constant is what --silent-mode takes, not a second copy.
        import subprocess
        from scaffold_tenant import SILENT_MODE_CHOICES
        tool = os.path.join(REPO_ROOT, "scripts", "tools", "ops", "scaffold_tenant.py")
        p = subprocess.run([sys.executable, tool, "--help"], capture_output=True, text=True,
                           encoding="utf-8", timeout=60, env={**os.environ, "DA_LANG": "en"})
        assert p.returncode == 0, p.stderr
        m = re.search(r"--silent-mode \{([^}]*)\}", p.stdout)
        assert m, p.stdout
        assert tuple(m.group(1).split(",")) == SILENT_MODE_CHOICES

    def test_interactive_targets_are_schema_words_in_both_forms(self):
        from scaffold_tenant import INTERACTIVE_SILENT_TARGETS
        scalar, target = _silent_mode_schema_enums()
        picks = set(INTERACTIVE_SILENT_TARGETS.values())
        assert picks and picks <= set(scalar) and picks <= set(target), picks


def test_a_file_written_again_is_replaced_not_appended(tmp_path):
    """#3107 ④ (`append=True` on the scaffold write survived the ② mutations):
    a non-conf.d file the scaffold writes over an existing one ends as the
    new content alone."""
    import scaffold_tenant as st
    # Not a config name: those go through `da-guard write`, not this write.
    path = tmp_path / "scaffold-report.md"
    path.write_text("an older and longer report\n" * 3, encoding="utf-8")
    st._perform_writes(tmp_path, [], [(str(path), "# new\n")])
    assert path.read_text(encoding="utf-8") == "# new\n"
