#!/usr/bin/env python3
"""pytest style tests for scaffold_tenant.py — Rule Pack catalogue, scaffold generation, YAML validation。

涵蓋：
- RULE_PACKS 目錄：所有 DB 類型具備必要鍵和結構
- 非互動模式為各 DB 類型生成正確的租戶 YAML
- metric dictionary 條目存在於所有 DB 類型
- Rule pack YAML 檔案遵循三層組結構
- ConfigMap ↔ Rule Pack 同步漂移偵測
- N:1 命名空間映射 (relabel snippets)
- generate_profile() 以供層級式閾值設定檔
"""

import os
import sys
import tempfile
import shutil

import pytest
import yaml

import scaffold_tenant  # noqa: E402

PROJECT_ROOT = os.path.join(os.path.dirname(__file__), os.pardir)
RULE_PACKS_DIR = os.path.join(PROJECT_ROOT, "rule-packs")
K8S_DIR = os.path.join(PROJECT_ROOT, "k8s", "03-monitoring")


# ── Shared helper ──────────────────────────────────────────────────

def run_scaffold(monkeypatch, tmpdir, tenant, dbs, namespaces=None,
                 receiver=None, receiver_type=None, non_interactive=False):
    """Run scaffold_tenant.main() with given CLI args (monkeypatch 版)."""
    argv = [
        "scaffold_tenant.py",
        "--tenant", tenant,
        "--db", dbs,
        "-o", tmpdir,
    ]
    if namespaces:
        argv.extend(["--namespaces", namespaces])
    if non_interactive:
        argv.append("--non-interactive")
    if receiver:
        argv.extend(["--routing-receiver", receiver])
    if receiver_type:
        argv.extend(["--routing-receiver-type", receiver_type])
    monkeypatch.setattr(sys, "argv", argv)
    scaffold_tenant.main()


def _load_yaml(path):
    """安全載入 YAML 檔案。"""
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


# ── RULE_PACKS catalogue data ─────────────────────────────────────
# (db_key, default_on, expected_metrics, dim_keyword)
# 每筆代表一個 DB type 的驗證資料

RULE_PACK_CATALOGUE = [
    ("postgresql", False,
     ["pg_connections", "pg_replication_lag"], "datname"),
    ("oracle", False,
     ["oracle_sessions_active", "oracle_tablespace_used_percent"], "tablespace"),
    ("db2", False,
     ["db2_connections_active", "db2_bufferpool_hit_ratio"], "bufferpool"),
    ("clickhouse", False,
     ["clickhouse_queries_rate", "clickhouse_active_connections"], "database"),
    ("kafka", False,
     ["kafka_consumer_lag", "kafka_under_replicated_partitions"], "group"),
    ("rabbitmq", False,
     ["rabbitmq_queue_messages", "rabbitmq_node_mem_percent"], "queue"),
]

REQUIRED_KEYS = {"display", "exporter", "default_on", "rule_pack_file", "defaults"}


# ── RULE_PACKS Catalogue ────────────────────────────────────────────


class TestRulePacksCatalogue:
    """Verify RULE_PACKS dict has all DB types with correct structure."""

    @pytest.mark.parametrize("db_key,default_on,metrics,dim_kw", RULE_PACK_CATALOGUE,
                             ids=[r[0] for r in RULE_PACK_CATALOGUE])
    def test_entry_exists(self, db_key, default_on, metrics, dim_kw):
        """DB type 存在於 RULE_PACKS。"""
        assert db_key in scaffold_tenant.RULE_PACKS

    @pytest.mark.parametrize("db_key,default_on,metrics,dim_kw", RULE_PACK_CATALOGUE,
                             ids=[r[0] for r in RULE_PACK_CATALOGUE])
    def test_has_required_keys(self, db_key, default_on, metrics, dim_kw):
        """DB type 具備所有必要 key。"""
        entry = scaffold_tenant.RULE_PACKS[db_key]
        for key in REQUIRED_KEYS:
            assert key in entry, f"{db_key} missing key: {key}"

    @pytest.mark.parametrize("db_key,default_on,metrics,dim_kw", RULE_PACK_CATALOGUE,
                             ids=[r[0] for r in RULE_PACK_CATALOGUE])
    def test_default_on_flag(self, db_key, default_on, metrics, dim_kw):
        """DB type 的 default_on 旗標正確。"""
        assert scaffold_tenant.RULE_PACKS[db_key]["default_on"] == default_on

    @pytest.mark.parametrize("db_key,default_on,metrics,dim_kw", RULE_PACK_CATALOGUE,
                             ids=[r[0] for r in RULE_PACK_CATALOGUE])
    def test_has_expected_defaults(self, db_key, default_on, metrics, dim_kw):
        """DB type defaults 包含預期 metric key。"""
        defaults = scaffold_tenant.RULE_PACKS[db_key]["defaults"]
        for m in metrics:
            assert m in defaults, f"{db_key} defaults missing: {m}"

    @pytest.mark.parametrize("db_key,default_on,metrics,dim_kw", RULE_PACK_CATALOGUE,
                             ids=[r[0] for r in RULE_PACK_CATALOGUE])
    def test_has_dimensional_example(self, db_key, default_on, metrics, dim_kw):
        """DB type 有 dimensional_example 且包含預期關鍵字。"""
        entry = scaffold_tenant.RULE_PACKS[db_key]
        assert "dimensional_example" in entry
        dim = entry["dimensional_example"]
        has_kw = any(dim_kw in k for k in dim)
        assert has_kw, f"{db_key} dimensional_example missing '{dim_kw}' keyword"

    def test_rule_pack_count_is_11(self):
        """Total RULE_PACKS >= 11（含 kubernetes + 所有 DB types）。"""
        assert len(scaffold_tenant.RULE_PACKS) >= 11


# ── Non-Interactive Generation ───────────────────────────────────────
# (tenant, dbs, expected_metric_in_defaults, report_keyword)

SCAFFOLD_CASES = [
    ("test-pg", "postgresql", ["pg_connections", "pg_replication_lag"], "PostgreSQL"),
    ("test-ora", "oracle", ["oracle_sessions_active"], "Oracle"),
    ("test-db2", "db2", ["db2_connections_active", "db2_bufferpool_hit_ratio"], "DB2"),
    ("test-ch", "clickhouse", ["clickhouse_queries_rate", "clickhouse_active_connections"], "ClickHouse"),
    ("test-kafka", "kafka", ["kafka_consumer_lag", "kafka_under_replicated_partitions"], "Kafka"),
    ("test-rmq", "rabbitmq", ["rabbitmq_queue_messages", "rabbitmq_node_mem_percent"], "RabbitMQ"),
]


class TestNonInteractiveGeneration:
    """Verify scaffold_tenant generates correct files for each DB type."""

    @pytest.mark.parametrize("tenant,dbs,metrics,report_kw", SCAFFOLD_CASES,
                             ids=[c[0] for c in SCAFFOLD_CASES])
    def test_generates_tenant_yaml(self, monkeypatch, tenant, dbs, metrics, report_kw):
        """Scaffold 產生 tenant YAML 檔案。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            run_scaffold(monkeypatch, tmpdir, tenant, dbs)
            path = os.path.join(tmpdir, f"{tenant}.yaml")
            assert os.path.isfile(path), f"Missing {path}"

    @pytest.mark.parametrize("tenant,dbs,metrics,report_kw", SCAFFOLD_CASES,
                             ids=[c[0] for c in SCAFFOLD_CASES])
    def test_defaults_contain_expected_metrics(self, monkeypatch, tenant, dbs, metrics, report_kw):
        """_defaults.yaml 包含預期 metric key。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            run_scaffold(monkeypatch, tmpdir, tenant, dbs)
            path = os.path.join(tmpdir, "_defaults.yaml")
            with open(path, encoding="utf-8") as f:
                content = f.read()
            for m in metrics:
                assert m in content, f"{dbs} defaults missing {m}"

    @pytest.mark.parametrize("tenant,dbs,metrics,report_kw", SCAFFOLD_CASES,
                             ids=[c[0] for c in SCAFFOLD_CASES])
    def test_scaffold_report_mentions_db(self, monkeypatch, tenant, dbs, metrics, report_kw):
        """scaffold-report.txt 提及 DB 類型名稱。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            run_scaffold(monkeypatch, tmpdir, tenant, dbs)
            path = os.path.join(tmpdir, "scaffold-report.txt")
            assert os.path.isfile(path), f"Missing {path}"
            with open(path, encoding="utf-8") as f:
                content = f.read()
            assert report_kw in content

    def test_generates_defaults_yaml(self, monkeypatch):
        """Oracle scaffold 產生 _defaults.yaml。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            run_scaffold(monkeypatch, tmpdir, "test-ora", "oracle")
            assert os.path.isfile(os.path.join(tmpdir, "_defaults.yaml"))

    def test_combined_oracle_db2(self, monkeypatch):
        """Combined oracle+db2 scaffold 包含兩組 metric。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            run_scaffold(monkeypatch, tmpdir, "test-combo", "oracle,db2")
            with open(os.path.join(tmpdir, "_defaults.yaml"), encoding="utf-8") as f:
                content = f.read()
            assert "oracle_sessions_active" in content
            assert "db2_connections_active" in content


# ── N:1 Tenant Mapping ──────────────────────────────────────────────


class TestNamespaceMapping:
    """Verify scaffold_tenant N:1 namespace mapping (v1.8.0)."""

    def test_scaffold_with_namespaces_single(self, monkeypatch):
        """--namespaces with single namespace produces relabel file."""
        with tempfile.TemporaryDirectory() as tmpdir:
            run_scaffold(monkeypatch, tmpdir, "test-ns", "mariadb", namespaces="ns-prod")
            relabel_path = os.path.join(tmpdir, "relabel_configs-test-ns.yaml")
            assert os.path.isfile(relabel_path)

    def test_scaffold_with_namespaces_multiple(self, monkeypatch):
        """--namespaces with multiple namespaces produces relabel file."""
        with tempfile.TemporaryDirectory() as tmpdir:
            run_scaffold(monkeypatch, tmpdir, "test-ns", "mariadb", namespaces="ns1,ns2,ns3")
            relabel_path = os.path.join(tmpdir, "relabel_configs-test-ns.yaml")
            assert os.path.isfile(relabel_path)
            with open(relabel_path, encoding="utf-8") as f:
                content = f.read()
            assert "ns1|ns2|ns3" in content
            assert "test-ns" in content

    def test_relabel_snippet_yaml_valid(self):
        """generate_relabel_snippet produces valid YAML."""
        snippet = scaffold_tenant.generate_relabel_snippet("my-tenant", "ns1,ns2")
        yaml_content = "\n".join(
            line for line in snippet.split("\n")
            if not line.startswith("#")
        )
        data = yaml.safe_load(yaml_content)
        assert "relabel_configs" in data
        assert len(data["relabel_configs"]) == 2

    def test_relabel_snippet_custom_tenant_label(self):
        """generate_relabel_snippet supports custom tenant_label."""
        snippet = scaffold_tenant.generate_relabel_snippet(
            "my-tenant", "ns1,ns2", tenant_label="cluster")
        assert "cluster" in snippet

    def test_tenant_yaml_has_namespaces_metadata(self, monkeypatch):
        """Tenant YAML includes _namespaces metadata when --namespaces used."""
        with tempfile.TemporaryDirectory() as tmpdir:
            run_scaffold(monkeypatch, tmpdir, "test-ns", "mariadb", namespaces="ns1,ns2,ns3")
            data = _load_yaml(os.path.join(tmpdir, "test-ns.yaml"))
            tenant_data = data["tenants"]["test-ns"]
            assert "_namespaces" in tenant_data
            assert tenant_data["_namespaces"] == ["ns1", "ns2", "ns3"]

    def test_scaffold_report_mentions_namespaces(self, monkeypatch):
        """scaffold-report.txt mentions namespace mapping when --namespaces used."""
        with tempfile.TemporaryDirectory() as tmpdir:
            run_scaffold(monkeypatch, tmpdir, "test-ns", "mariadb", namespaces="ns1,ns2")
            with open(os.path.join(tmpdir, "scaffold-report.txt"), encoding="utf-8") as f:
                content = f.read()
            assert "relabel" in content.lower()


# ── Metric Dictionary ────────────────────────────────────────────────
# (prefix, min_count, golden_metric)

METRIC_DICT_CASES = [
    ("oracledb_", 3, "oracledb_sessions_active"),
    ("db2_", 3, "db2_connections_active"),
    ("ClickHouse", 3, "ClickHouseProfileEvents_Query"),
    ("kafka_", 5, "kafka_consumergroup_lag_sum"),
    ("rabbitmq_", 5, "rabbitmq_queue_messages_ready"),
]


class TestMetricDictionary:
    """Verify metric-dictionary.yaml has entries for all DB types."""

    @pytest.mark.parametrize("prefix,min_count,golden_metric", METRIC_DICT_CASES,
                             ids=[c[0].rstrip("_") for c in METRIC_DICT_CASES])
    def test_entries_exist(self, metric_dictionary, prefix, min_count, golden_metric):
        """Dictionary 至少有 min_count 筆 prefix 開頭的項目。"""
        keys = [k for k in metric_dictionary if k.startswith(prefix)]
        assert len(keys) >= min_count, f"Only {len(keys)} {prefix}* entries"

    @pytest.mark.parametrize("prefix,min_count,golden_metric", METRIC_DICT_CASES,
                             ids=[c[0].rstrip("_") for c in METRIC_DICT_CASES])
    def test_golden_metric_has_golden_rule(self, metric_dictionary, prefix, min_count, golden_metric):
        """Golden metric 有 golden_rule mapping。"""
        entry = metric_dictionary.get(golden_metric, {})
        assert "golden_rule" in entry, f"Missing golden_rule for {golden_metric}"


# ── Rule Pack YAML Validation ────────────────────────────────────────
# (db_prefix, rule_pack_file, configmap_file)

RULE_PACK_YAML_CASES = [
    ("postgresql", "rule-pack-postgresql.yaml", "configmap-rules-postgresql.yaml"),
    ("oracle", "rule-pack-oracle.yaml", "configmap-rules-oracle.yaml"),
    ("db2", "rule-pack-db2.yaml", "configmap-rules-db2.yaml"),
    ("clickhouse", "rule-pack-clickhouse.yaml", "configmap-rules-clickhouse.yaml"),
    ("kafka", "rule-pack-kafka.yaml", "configmap-rules-kafka.yaml"),
    ("rabbitmq", "rule-pack-rabbitmq.yaml", "configmap-rules-rabbitmq.yaml"),
]


class TestRulePackYAML:
    """Validate canonical rule pack YAML files for all DB types."""

    @pytest.mark.parametrize("db_prefix,pack_file,cm_file", RULE_PACK_YAML_CASES,
                             ids=[c[0] for c in RULE_PACK_YAML_CASES])
    def test_rule_pack_exists(self, db_prefix, pack_file, cm_file):
        """rule-packs/<file> 存在。"""
        assert os.path.isfile(os.path.join(RULE_PACKS_DIR, pack_file))

    @pytest.mark.parametrize("db_prefix,pack_file,cm_file", RULE_PACK_YAML_CASES,
                             ids=[c[0] for c in RULE_PACK_YAML_CASES])
    def test_configmap_exists(self, db_prefix, pack_file, cm_file):
        """k8s/03-monitoring/<configmap> 存在。"""
        assert os.path.isfile(os.path.join(K8S_DIR, cm_file))

    @pytest.mark.parametrize("db_prefix,pack_file,cm_file", RULE_PACK_YAML_CASES,
                             ids=[c[0] for c in RULE_PACK_YAML_CASES])
    def test_rule_pack_three_groups(self, db_prefix, pack_file, cm_file):
        """Rule pack 符合 three-group structure（normalization, threshold-normalization, alerts）。"""
        data = _load_yaml(os.path.join(RULE_PACKS_DIR, pack_file))
        group_names = [g["name"] for g in data["groups"]]
        assert f"{db_prefix}-normalization" in group_names
        assert f"{db_prefix}-threshold-normalization" in group_names
        assert f"{db_prefix}-alerts" in group_names

    @pytest.mark.parametrize("db_prefix,pack_file,cm_file", RULE_PACK_YAML_CASES,
                             ids=[c[0] for c in RULE_PACK_YAML_CASES])
    def test_configmap_has_label(self, db_prefix, pack_file, cm_file):
        """ConfigMap 有正確的 rule-pack label。"""
        data = _load_yaml(os.path.join(K8S_DIR, cm_file))
        assert data["metadata"]["labels"]["rule-pack"] == db_prefix

    @pytest.mark.parametrize("db_prefix,pack_file,cm_file", RULE_PACK_YAML_CASES,
                             ids=[c[0] for c in RULE_PACK_YAML_CASES])
    def test_uses_max_by_tenant(self, db_prefix, pack_file, cm_file):
        """Threshold normalization 使用 max by(tenant)。"""
        with open(os.path.join(RULE_PACKS_DIR, pack_file), encoding="utf-8") as f:
            content = f.read()
        assert "max by(tenant)" in content
        assert "sum by(tenant) (user_threshold" not in content

    @pytest.mark.parametrize("db_prefix,pack_file,cm_file", RULE_PACK_YAML_CASES,
                             ids=[c[0] for c in RULE_PACK_YAML_CASES])
    def test_alerts_have_maintenance_unless(self, db_prefix, pack_file, cm_file):
        """Alert rules 使用 'unless on(tenant)' maintenance filter。"""
        with open(os.path.join(RULE_PACKS_DIR, pack_file), encoding="utf-8") as f:
            content = f.read()
        assert "unless on(tenant)" in content

    # ── PostgreSQL-specific tests ──

    def test_postgresql_has_metric_group_labels(self):
        """PostgreSQL alerts 有 metric_group for severity dedup。"""
        with open(os.path.join(RULE_PACKS_DIR, "rule-pack-postgresql.yaml"), encoding="utf-8") as f:
            content = f.read()
        assert 'metric_group: "pg_connections"' in content
        assert 'metric_group: "pg_replication_lag"' in content

    def test_postgresql_division_by_zero_protection(self):
        """PostgreSQL recording rules 使用 clamp_min 防止除以零。"""
        with open(os.path.join(RULE_PACKS_DIR, "rule-pack-postgresql.yaml"), encoding="utf-8") as f:
            content = f.read()
        clamp_count = content.count("clamp_min")
        assert clamp_count >= 2, f"Expected at least 2 clamp_min guards, found {clamp_count}"

    def test_postgresql_rollback_uses_humanize_percentage(self):
        """PostgreSQLHighRollbackRatio description 使用 humanizePercentage。"""
        with open(os.path.join(RULE_PACKS_DIR, "rule-pack-postgresql.yaml"), encoding="utf-8") as f:
            content = f.read()
        lines = content.split("\n")
        in_rollback = False
        found_humanize = False
        for line in lines:
            if "PostgreSQLHighRollbackRatio" in line:
                in_rollback = True
            if in_rollback and "humanizePercentage" in line:
                found_humanize = True
                break
            if in_rollback and line.strip().startswith("- alert:") and "RollbackRatio" not in line:
                break
        assert found_humanize, "PostgreSQLHighRollbackRatio should use humanizePercentage"

    # ── DB2-specific test ──

    def test_db2_bufferpool_uses_less_than(self):
        """DB2 low bufferpool hit ratio alert 使用 < operator。"""
        with open(os.path.join(RULE_PACKS_DIR, "rule-pack-db2.yaml"), encoding="utf-8") as f:
            content = f.read()
        assert "DB2LowBufferpoolHitRatio" in content
        lines = content.split("\n")
        in_bufferpool = False
        for line in lines:
            if "DB2LowBufferpoolHitRatio" in line:
                in_bufferpool = True
            if in_bufferpool and "< on(tenant)" in line:
                break
        else:
            pytest.fail("DB2LowBufferpoolHitRatio should use '< on(tenant)' operator")


# ── Scaffold Receiver Types (v1.8.0) ─────────────────────────────────


class TestScaffoldReceiverTypes:
    """Verify scaffold_tenant supports receiver types in non-interactive mode."""

    @pytest.mark.parametrize("tenant,receiver,rtype,expected_key,expected_value", [
        ("test-rc", "https://rocket.example.com/hooks/test", "rocketchat",
         "url", "https://rocket.example.com/hooks/test"),
        ("test-pd", "abc123servicekey", "pagerduty",
         "service_key", "abc123servicekey"),
    ], ids=["rocketchat", "pagerduty"])
    def test_receiver_type_scaffold(self, monkeypatch, tenant, receiver, rtype,
                                    expected_key, expected_value):
        """Scaffold with receiver type produces correct _routing。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            run_scaffold(monkeypatch, tmpdir, tenant, "mariadb",
                         receiver=receiver, receiver_type=rtype,
                         non_interactive=True)
            data = _load_yaml(os.path.join(tmpdir, f"{tenant}.yaml"))
            routing = data["tenants"][tenant]["_routing"]
            assert routing["receiver"]["type"] == rtype
            assert routing["receiver"][expected_key] == expected_value


# ── Kafka/RabbitMQ Alert Completeness (v1.8.0) ──────────────────────


class TestKafkaRabbitMQAlertCompleteness:
    """Verify Kafka and RabbitMQ rule packs have all expected alerts."""

    @staticmethod
    def _get_alert_names(groups):
        """Extract all alert names from rule groups."""
        return [r["alert"] for g in groups for r in g.get("rules", []) if "alert" in r]

    @staticmethod
    def _get_metric_groups(groups):
        """Extract all metric_group labels from alert rules."""
        return {
            r.get("labels", {}).get("metric_group")
            for g in groups for r in g.get("rules", [])
            if "alert" in r and r.get("labels", {}).get("metric_group")
        }

    def test_kafka_has_all_expected_alerts(self):
        """Kafka rule pack 包含所有 CHANGELOG 承諾的 alerts。"""
        data = _load_yaml(os.path.join(RULE_PACKS_DIR, "rule-pack-kafka.yaml"))
        alerts = self._get_alert_names(data["groups"])
        expected = [
            "KafkaExporterAbsent",
            "KafkaHighConsumerLag", "KafkaHighConsumerLagCritical",
            "KafkaUnderReplicatedPartitions", "KafkaUnderReplicatedPartitionsCritical",
            "KafkaNoActiveController",
            "KafkaLowBrokerCount",
            "KafkaHighRequestRate", "KafkaHighRequestRateCritical",
        ]
        for name in expected:
            assert name in alerts, f"Missing alert: {name}"

    def test_kafka_metric_groups_cover_all_metrics(self):
        """Kafka alerts 有完整的 metric_group 覆蓋。"""
        data = _load_yaml(os.path.join(RULE_PACKS_DIR, "rule-pack-kafka.yaml"))
        mgs = self._get_metric_groups(data["groups"])
        expected = {
            "kafka_consumer_lag", "kafka_under_replicated_partitions",
            "kafka_active_controllers", "kafka_broker_count", "kafka_request_rate",
        }
        for mg in expected:
            assert mg in mgs, f"Missing metric_group: {mg}"

    @pytest.mark.parametrize("alert_name", [
        "KafkaNoActiveController", "KafkaLowBrokerCount",
    ])
    def test_kafka_low_alerts_use_less_than(self, alert_name):
        """低值觸發的 Kafka alert 使用 < operator。"""
        with open(os.path.join(RULE_PACKS_DIR, "rule-pack-kafka.yaml"), encoding="utf-8") as f:
            content = f.read()
        idx = content.index(alert_name)
        section = content[idx:idx + 300]
        assert "< on(tenant)" in section, f"{alert_name} should use '<' operator"

    def test_rabbitmq_has_all_expected_alerts(self):
        """RabbitMQ rule pack 包含所有 CHANGELOG 承諾的 alerts。"""
        data = _load_yaml(os.path.join(RULE_PACKS_DIR, "rule-pack-rabbitmq.yaml"))
        alerts = self._get_alert_names(data["groups"])
        expected = [
            "RabbitMQExporterAbsent",
            "RabbitMQHighQueueDepth", "RabbitMQHighQueueDepthCritical",
            "RabbitMQHighMemory", "RabbitMQHighMemoryCritical",
            "RabbitMQHighConnections",
            "RabbitMQLowConsumers",
            "RabbitMQHighUnackedMessages",
        ]
        for name in expected:
            assert name in alerts, f"Missing alert: {name}"

    def test_rabbitmq_low_consumers_uses_less_than(self):
        """RabbitMQLowConsumers 使用 < operator。"""
        with open(os.path.join(RULE_PACKS_DIR, "rule-pack-rabbitmq.yaml"), encoding="utf-8") as f:
            content = f.read()
        idx = content.index("RabbitMQLowConsumers")
        section = content[idx:idx + 300]
        assert "< on(tenant)" in section

    def test_rabbitmq_unacked_uses_config_driven_threshold(self):
        """RabbitMQHighUnackedMessages 使用 config-driven threshold。"""
        with open(os.path.join(RULE_PACKS_DIR, "rule-pack-rabbitmq.yaml"), encoding="utf-8") as f:
            content = f.read()
        idx = content.index("RabbitMQHighUnackedMessages")
        section = content[idx:idx + 400]
        assert "tenant:alert_threshold:rabbitmq_unacked_messages" in section
        assert "> 10000" not in section

    def test_rabbitmq_mem_division_by_zero_protection(self):
        """RabbitMQ memory ratio recording rule 使用 clamp_min 防止除以零。"""
        with open(os.path.join(RULE_PACKS_DIR, "rule-pack-rabbitmq.yaml"), encoding="utf-8") as f:
            content = f.read()
        assert "clamp_min" in content


# ── ConfigMap ↔ Rule Pack Sync ─────────────────────────────────────


class TestConfigMapRulePackSync:
    """Verify ConfigMap wrappers contain the same data as canonical rule-pack YAML files."""

    @staticmethod
    def _get_rule_pack_files():
        """Find all rule-pack-*.yaml canonical files."""
        return sorted(
            f for f in os.listdir(RULE_PACKS_DIR)
            if f.startswith("rule-pack-") and f.endswith(".yaml")
        )

    def test_each_rule_pack_has_configmap_wrapper(self):
        """Every rule-pack-*.yaml 有對應的 configmap-rules-*.yaml。"""
        for pack_file in self._get_rule_pack_files():
            db_name = pack_file.replace("rule-pack-", "").replace(".yaml", "")
            cm_path = os.path.join(K8S_DIR, f"configmap-rules-{db_name}.yaml")
            assert os.path.isfile(cm_path), f"Missing ConfigMap wrapper for {pack_file}"

    def test_configmap_data_matches_rule_pack(self):
        """ConfigMap data section 包含與 canonical source 相同的 rule groups。"""
        for pack_file in self._get_rule_pack_files():
            db_name = pack_file.replace("rule-pack-", "").replace(".yaml", "")
            cm_path = os.path.join(K8S_DIR, f"configmap-rules-{db_name}.yaml")
            if not os.path.isfile(cm_path):
                continue

            pack_data = _load_yaml(os.path.join(RULE_PACKS_DIR, pack_file))
            cm_data = _load_yaml(cm_path)

            cm_inner = cm_data.get("data", {})
            assert len(cm_inner) > 0, f"ConfigMap {db_name} has empty data section"

            embedded_groups = []
            for _data_key, yaml_str in cm_inner.items():
                embedded = yaml.safe_load(yaml_str)
                if embedded and "groups" in embedded:
                    for g in embedded["groups"]:
                        embedded_groups.append(g["name"])

            pack_groups = [g["name"] for g in pack_data.get("groups", [])]
            for gname in pack_groups:
                assert gname in embedded_groups, \
                    f"Group '{gname}' from {pack_file} missing in configmap-rules-{db_name}.yaml"


# ── Relabel Snippet Edge Cases ─────────────────────────────────────


class TestRelabelSnippetEdgeCases:
    """Edge cases for generate_relabel_snippet()."""

    def test_empty_string_returns_empty(self):
        assert scaffold_tenant.generate_relabel_snippet("t", "") == ""

    def test_none_namespaces_returns_empty(self):
        assert scaffold_tenant.generate_relabel_snippet("t", []) == ""

    def test_whitespace_only_namespaces_filtered(self):
        assert scaffold_tenant.generate_relabel_snippet("t", " , , ") == ""

    def test_namespaces_with_extra_whitespace(self):
        result = scaffold_tenant.generate_relabel_snippet("t", " ns-a , ns-b ")
        assert "ns-a" in result
        assert "ns-b" in result
        for line in result.splitlines():
            if line.strip().startswith("regex:"):
                assert "ns-a|ns-b" in line
                break

    def test_custom_tenant_label(self):
        result = scaffold_tenant.generate_relabel_snippet("t", "ns1", tenant_label="team")
        assert "team" in result


# ── generate_profile() Tests (v1.12.0) ───────────────────────────


class TestGenerateProfile:
    """Tests for scaffold_tenant.generate_profile()."""

    def test_basic_profile_generation(self):
        """Profile for mariadb 包含 defaults + optional overrides。"""
        result = scaffold_tenant.generate_profile(
            "standard-mariadb-prod", ["kubernetes", "mariadb"])
        assert "profiles" in result
        profile = result["profiles"]["standard-mariadb-prod"]
        assert "container_cpu" in profile
        assert "mysql_connections" in profile
        assert "mysql_connections_critical" in profile

    def test_staging_tier_relaxed(self):
        """Staging tier 產生比 prod 寬鬆 20% 的閾值。"""
        prod = scaffold_tenant.generate_profile("p", ["kubernetes", "mariadb"], tier="prod")
        staging = scaffold_tenant.generate_profile("s", ["kubernetes", "mariadb"], tier="staging")
        assert staging["profiles"]["s"]["container_cpu"] > prod["profiles"]["p"]["container_cpu"]

    def test_multi_db_profile(self):
        """Multiple DBs profile 包含所有 metric key。"""
        result = scaffold_tenant.generate_profile("multi", ["kubernetes", "mariadb", "redis"])
        profile = result["profiles"]["multi"]
        assert "mysql_connections" in profile
        assert "redis_memory_used_bytes" in profile

    def test_empty_db_list(self):
        """Empty DB list 仍回傳有效 profile 結構。"""
        result = scaffold_tenant.generate_profile("empty", [])
        assert "profiles" in result
        assert result["profiles"]["empty"] == {}

    def test_generate_profile_cli(self, monkeypatch):
        """--generate-profile via CLI writes _profiles.yaml."""
        with tempfile.TemporaryDirectory() as tmpdir:
            monkeypatch.setattr(sys, "argv", [
                "scaffold_tenant.py",
                "--generate-profile", "test-profile",
                "--db", "mariadb",
                "-o", tmpdir,
            ])
            with pytest.raises(SystemExit) as exc_info:
                scaffold_tenant.main()
            assert exc_info.value.code == 0

            path = os.path.join(tmpdir, "_profiles.yaml")
            assert os.path.isfile(path)
            data = _load_yaml(path)
            assert "profiles" in data
            assert "test-profile" in data["profiles"]
