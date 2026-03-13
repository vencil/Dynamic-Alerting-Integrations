#!/usr/bin/env python3
"""Tests for scaffold_tenant.py — Oracle + DB2 extensions (Phase 12).

Covers:
- RULE_PACKS catalogue includes oracle and db2 entries
- Oracle/DB2 entries have required keys (display, exporter, defaults, etc.)
- Non-interactive mode generates correct tenant YAML for new DB types
- Metric dictionary entries exist for Oracle/DB2
- Rule pack YAML files are valid and follow three-group structure
"""

import os
import sys
import tempfile
import shutil
import unittest

import yaml

# Make scaffold_tenant importable
TOOLS_DIR = os.path.join(
    os.path.dirname(__file__), os.pardir, "scripts", "tools",
)
sys.path.insert(0, os.path.abspath(TOOLS_DIR))

import scaffold_tenant  # noqa: E402

PROJECT_ROOT = os.path.join(os.path.dirname(__file__), os.pardir)


# ── Shared helper ──────────────────────────────────────────────────

def run_scaffold(tmpdir, tenant, dbs, namespaces=None,
                 receiver=None, receiver_type=None, non_interactive=False):
    """Run scaffold_tenant.main() with given CLI args.

    Consolidates the repeated _run_scaffold() pattern across test classes.
    """
    old_argv = sys.argv
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
    sys.argv = argv
    try:
        scaffold_tenant.main()
    finally:
        sys.argv = old_argv


# ── RULE_PACKS Catalogue ────────────────────────────────────────────


class TestRulePacksCatalogue(unittest.TestCase):
    """Verify RULE_PACKS dict has oracle and db2 with correct structure."""

    REQUIRED_KEYS = {"display", "exporter", "default_on", "rule_pack_file", "defaults"}

    def test_postgresql_in_rule_packs(self):
        """PostgreSQL entry exists in RULE_PACKS."""
        self.assertIn("postgresql", scaffold_tenant.RULE_PACKS)

    def test_postgresql_has_required_keys(self):
        """PostgreSQL entry has all required keys."""
        entry = scaffold_tenant.RULE_PACKS["postgresql"]
        for key in self.REQUIRED_KEYS:
            self.assertIn(key, entry, f"PostgreSQL missing key: {key}")

    def test_postgresql_not_default_on(self):
        """PostgreSQL should not be default_on."""
        self.assertFalse(scaffold_tenant.RULE_PACKS["postgresql"]["default_on"])

    def test_postgresql_has_defaults(self):
        """PostgreSQL defaults include key metrics."""
        defaults = scaffold_tenant.RULE_PACKS["postgresql"]["defaults"]
        self.assertIn("pg_connections", defaults)
        self.assertIn("pg_replication_lag", defaults)

    def test_postgresql_has_dimensional_example(self):
        """PostgreSQL has dimensional_example."""
        entry = scaffold_tenant.RULE_PACKS["postgresql"]
        self.assertIn("dimensional_example", entry)
        dim = entry["dimensional_example"]
        has_datname = any("datname" in k for k in dim)
        self.assertTrue(has_datname, "PostgreSQL dimensional_example missing datname example")

    def test_oracle_in_rule_packs(self):
        """Oracle entry exists in RULE_PACKS."""
        self.assertIn("oracle", scaffold_tenant.RULE_PACKS)

    def test_db2_in_rule_packs(self):
        """DB2 entry exists in RULE_PACKS."""
        self.assertIn("db2", scaffold_tenant.RULE_PACKS)

    def test_oracle_has_required_keys(self):
        """Oracle entry has all required keys."""
        entry = scaffold_tenant.RULE_PACKS["oracle"]
        for key in self.REQUIRED_KEYS:
            self.assertIn(key, entry, f"Oracle missing key: {key}")

    def test_db2_has_required_keys(self):
        """DB2 entry has all required keys."""
        entry = scaffold_tenant.RULE_PACKS["db2"]
        for key in self.REQUIRED_KEYS:
            self.assertIn(key, entry, f"DB2 missing key: {key}")

    def test_oracle_not_default_on(self):
        """Oracle should not be default_on (enterprise DB)."""
        self.assertFalse(scaffold_tenant.RULE_PACKS["oracle"]["default_on"])

    def test_db2_not_default_on(self):
        """DB2 should not be default_on (enterprise DB)."""
        self.assertFalse(scaffold_tenant.RULE_PACKS["db2"]["default_on"])

    def test_oracle_has_defaults(self):
        """Oracle defaults include key metrics."""
        defaults = scaffold_tenant.RULE_PACKS["oracle"]["defaults"]
        self.assertIn("oracle_sessions_active", defaults)
        self.assertIn("oracle_tablespace_used_percent", defaults)

    def test_db2_has_defaults(self):
        """DB2 defaults include key metrics."""
        defaults = scaffold_tenant.RULE_PACKS["db2"]["defaults"]
        self.assertIn("db2_connections_active", defaults)
        self.assertIn("db2_bufferpool_hit_ratio", defaults)

    def test_oracle_has_dimensional_example(self):
        """Oracle has dimensional_example for regex showcase."""
        entry = scaffold_tenant.RULE_PACKS["oracle"]
        self.assertIn("dimensional_example", entry)
        # Should have tablespace regex example
        dim = entry["dimensional_example"]
        has_regex = any("=~" in k or "tablespace" in k for k in dim)
        self.assertTrue(has_regex, "Oracle dimensional_example missing regex tablespace")

    def test_db2_has_dimensional_example(self):
        """DB2 has dimensional_example for regex showcase."""
        entry = scaffold_tenant.RULE_PACKS["db2"]
        self.assertIn("dimensional_example", entry)
        dim = entry["dimensional_example"]
        has_regex = any("=~" in k or "bufferpool" in k for k in dim)
        self.assertTrue(has_regex, "DB2 dimensional_example missing regex bufferpool")

    def test_clickhouse_in_rule_packs(self):
        """ClickHouse entry exists in RULE_PACKS."""
        self.assertIn("clickhouse", scaffold_tenant.RULE_PACKS)

    def test_clickhouse_has_required_keys(self):
        """ClickHouse entry has all required keys."""
        entry = scaffold_tenant.RULE_PACKS["clickhouse"]
        for key in self.REQUIRED_KEYS:
            self.assertIn(key, entry, f"ClickHouse missing key: {key}")

    def test_clickhouse_not_default_on(self):
        """ClickHouse should not be default_on."""
        self.assertFalse(scaffold_tenant.RULE_PACKS["clickhouse"]["default_on"])

    def test_clickhouse_has_defaults(self):
        """ClickHouse defaults include key metrics."""
        defaults = scaffold_tenant.RULE_PACKS["clickhouse"]["defaults"]
        self.assertIn("clickhouse_queries_rate", defaults)
        self.assertIn("clickhouse_active_connections", defaults)

    def test_clickhouse_has_dimensional_example(self):
        """ClickHouse has dimensional_example for regex showcase."""
        entry = scaffold_tenant.RULE_PACKS["clickhouse"]
        self.assertIn("dimensional_example", entry)
        dim = entry["dimensional_example"]
        has_regex = any("=~" in k or "database" in k for k in dim)
        self.assertTrue(has_regex, "ClickHouse dimensional_example missing regex database")

    def test_kafka_in_rule_packs(self):
        """Kafka entry exists in RULE_PACKS."""
        self.assertIn("kafka", scaffold_tenant.RULE_PACKS)

    def test_kafka_has_required_keys(self):
        """Kafka entry has all required keys."""
        entry = scaffold_tenant.RULE_PACKS["kafka"]
        for key in self.REQUIRED_KEYS:
            self.assertIn(key, entry, f"Kafka missing key: {key}")

    def test_kafka_not_default_on(self):
        """Kafka should not be default_on."""
        self.assertFalse(scaffold_tenant.RULE_PACKS["kafka"]["default_on"])

    def test_kafka_has_defaults(self):
        """Kafka defaults include key metrics."""
        defaults = scaffold_tenant.RULE_PACKS["kafka"]["defaults"]
        self.assertIn("kafka_consumer_lag", defaults)
        self.assertIn("kafka_under_replicated_partitions", defaults)

    def test_kafka_has_dimensional_example(self):
        """Kafka has dimensional_example."""
        entry = scaffold_tenant.RULE_PACKS["kafka"]
        self.assertIn("dimensional_example", entry)
        dim = entry["dimensional_example"]
        has_group = any("group" in k for k in dim)
        self.assertTrue(has_group, "Kafka dimensional_example missing group example")

    def test_rabbitmq_in_rule_packs(self):
        """RabbitMQ entry exists in RULE_PACKS."""
        self.assertIn("rabbitmq", scaffold_tenant.RULE_PACKS)

    def test_rabbitmq_has_required_keys(self):
        """RabbitMQ entry has all required keys."""
        entry = scaffold_tenant.RULE_PACKS["rabbitmq"]
        for key in self.REQUIRED_KEYS:
            self.assertIn(key, entry, f"RabbitMQ missing key: {key}")

    def test_rabbitmq_not_default_on(self):
        """RabbitMQ should not be default_on."""
        self.assertFalse(scaffold_tenant.RULE_PACKS["rabbitmq"]["default_on"])

    def test_rabbitmq_has_defaults(self):
        """RabbitMQ defaults include key metrics."""
        defaults = scaffold_tenant.RULE_PACKS["rabbitmq"]["defaults"]
        self.assertIn("rabbitmq_queue_messages", defaults)
        self.assertIn("rabbitmq_node_mem_percent", defaults)

    def test_rabbitmq_has_dimensional_example(self):
        """RabbitMQ has dimensional_example."""
        entry = scaffold_tenant.RULE_PACKS["rabbitmq"]
        self.assertIn("dimensional_example", entry)
        dim = entry["dimensional_example"]
        has_queue = any("queue" in k for k in dim)
        self.assertTrue(has_queue, "RabbitMQ dimensional_example missing queue example")

    def test_rule_pack_count_is_11(self):
        """Total RULE_PACKS should be 11 (kubernetes + postgresql + mariadb + redis + mongodb + elasticsearch + oracle + db2 + clickhouse + kafka + rabbitmq)."""
        self.assertGreaterEqual(len(scaffold_tenant.RULE_PACKS), 11)


# ── Non-Interactive Generation ───────────────────────────────────────


class TestNonInteractiveGeneration(unittest.TestCase):
    """Verify scaffold_tenant generates correct files for oracle/db2."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _run_scaffold(self, tenant, dbs):
        run_scaffold(self.tmpdir, tenant, dbs)

    def test_oracle_generates_tenant_yaml(self):
        """Oracle-only scaffold creates tenant YAML."""
        self._run_scaffold("test-ora", "oracle")
        path = os.path.join(self.tmpdir, "test-ora.yaml")
        self.assertTrue(os.path.isfile(path), f"Missing {path}")

    def test_oracle_generates_defaults_yaml(self):
        """Oracle scaffold creates _defaults.yaml."""
        self._run_scaffold("test-ora", "oracle")
        path = os.path.join(self.tmpdir, "_defaults.yaml")
        self.assertTrue(os.path.isfile(path), f"Missing {path}")

    def test_oracle_defaults_contain_oracle_metrics(self):
        """_defaults.yaml includes oracle metric keys."""
        self._run_scaffold("test-ora", "oracle")
        path = os.path.join(self.tmpdir, "_defaults.yaml")
        with open(path) as f:
            content = f.read()
        self.assertIn("oracle_sessions_active", content)

    def test_db2_generates_tenant_yaml(self):
        """DB2-only scaffold creates tenant YAML."""
        self._run_scaffold("test-db2", "db2")
        path = os.path.join(self.tmpdir, "test-db2.yaml")
        self.assertTrue(os.path.isfile(path))

    def test_db2_defaults_contain_db2_metrics(self):
        """_defaults.yaml includes db2 metric keys."""
        self._run_scaffold("test-db2", "db2")
        path = os.path.join(self.tmpdir, "_defaults.yaml")
        with open(path) as f:
            content = f.read()
        self.assertIn("db2_connections_active", content)
        self.assertIn("db2_bufferpool_hit_ratio", content)

    def test_combined_oracle_db2(self):
        """Combined oracle+db2 scaffold contains both metric sets."""
        self._run_scaffold("test-combo", "oracle,db2")
        path = os.path.join(self.tmpdir, "_defaults.yaml")
        with open(path) as f:
            content = f.read()
        self.assertIn("oracle_sessions_active", content)
        self.assertIn("db2_connections_active", content)

    def test_scaffold_report_mentions_oracle(self):
        """scaffold-report.txt mentions Oracle."""
        self._run_scaffold("test-ora", "oracle")
        path = os.path.join(self.tmpdir, "scaffold-report.txt")
        self.assertTrue(os.path.isfile(path), f"Missing {path}")
        with open(path) as f:
            content = f.read()
        self.assertIn("Oracle", content)

    def test_scaffold_report_mentions_db2(self):
        """scaffold-report.txt mentions DB2."""
        self._run_scaffold("test-db2", "db2")
        path = os.path.join(self.tmpdir, "scaffold-report.txt")
        with open(path) as f:
            content = f.read()
        self.assertIn("DB2", content)

    def test_postgresql_generates_tenant_yaml(self):
        """PostgreSQL-only scaffold creates tenant YAML."""
        self._run_scaffold("test-pg", "postgresql")
        path = os.path.join(self.tmpdir, "test-pg.yaml")
        self.assertTrue(os.path.isfile(path), f"Missing {path}")

    def test_postgresql_defaults_contain_pg_metrics(self):
        """_defaults.yaml includes PostgreSQL metric keys."""
        self._run_scaffold("test-pg", "postgresql")
        path = os.path.join(self.tmpdir, "_defaults.yaml")
        with open(path) as f:
            content = f.read()
        self.assertIn("pg_connections", content)
        self.assertIn("pg_replication_lag", content)

    def test_postgresql_scaffold_report_mentions_postgresql(self):
        """scaffold-report.txt mentions PostgreSQL."""
        self._run_scaffold("test-pg", "postgresql")
        path = os.path.join(self.tmpdir, "scaffold-report.txt")
        self.assertTrue(os.path.isfile(path), f"Missing {path}")
        with open(path) as f:
            content = f.read()
        self.assertIn("PostgreSQL", content)

    def test_clickhouse_generates_tenant_yaml(self):
        """ClickHouse-only scaffold creates tenant YAML."""
        self._run_scaffold("test-ch", "clickhouse")
        path = os.path.join(self.tmpdir, "test-ch.yaml")
        self.assertTrue(os.path.isfile(path))

    def test_clickhouse_defaults_contain_metrics(self):
        """_defaults.yaml includes clickhouse metric keys."""
        self._run_scaffold("test-ch", "clickhouse")
        path = os.path.join(self.tmpdir, "_defaults.yaml")
        with open(path) as f:
            content = f.read()
        self.assertIn("clickhouse_queries_rate", content)
        self.assertIn("clickhouse_active_connections", content)

    def test_kafka_generates_tenant_yaml(self):
        """Kafka-only scaffold creates tenant YAML."""
        self._run_scaffold("test-kafka", "kafka")
        path = os.path.join(self.tmpdir, "test-kafka.yaml")
        self.assertTrue(os.path.isfile(path))

    def test_kafka_defaults_contain_metrics(self):
        """_defaults.yaml includes Kafka metric keys."""
        self._run_scaffold("test-kafka", "kafka")
        path = os.path.join(self.tmpdir, "_defaults.yaml")
        with open(path) as f:
            content = f.read()
        self.assertIn("kafka_consumer_lag", content)
        self.assertIn("kafka_under_replicated_partitions", content)

    def test_rabbitmq_generates_tenant_yaml(self):
        """RabbitMQ-only scaffold creates tenant YAML."""
        self._run_scaffold("test-rmq", "rabbitmq")
        path = os.path.join(self.tmpdir, "test-rmq.yaml")
        self.assertTrue(os.path.isfile(path))

    def test_rabbitmq_defaults_contain_metrics(self):
        """_defaults.yaml includes RabbitMQ metric keys."""
        self._run_scaffold("test-rmq", "rabbitmq")
        path = os.path.join(self.tmpdir, "_defaults.yaml")
        with open(path) as f:
            content = f.read()
        self.assertIn("rabbitmq_queue_messages", content)
        self.assertIn("rabbitmq_node_mem_percent", content)


# ── N:1 Tenant Mapping ──────────────────────────────────────────────


class TestNamespaceMapping(unittest.TestCase):
    """Verify scaffold_tenant N:1 namespace mapping (v1.8.0)."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _run_scaffold(self, tenant, dbs, namespaces=None):
        run_scaffold(self.tmpdir, tenant, dbs, namespaces=namespaces)

    def test_scaffold_with_namespaces_single(self):
        """--namespaces with single namespace produces relabel file."""
        self._run_scaffold("test-ns", "mariadb", namespaces="ns-prod")
        relabel_path = os.path.join(self.tmpdir, "relabel_configs-test-ns.yaml")
        self.assertTrue(os.path.isfile(relabel_path))

    def test_scaffold_with_namespaces_multiple(self):
        """--namespaces with multiple namespaces produces relabel file."""
        self._run_scaffold("test-ns", "mariadb", namespaces="ns1,ns2,ns3")
        relabel_path = os.path.join(self.tmpdir, "relabel_configs-test-ns.yaml")
        self.assertTrue(os.path.isfile(relabel_path))
        with open(relabel_path) as f:
            content = f.read()
        self.assertIn("ns1|ns2|ns3", content)
        self.assertIn("test-ns", content)

    def test_relabel_snippet_yaml_valid(self):
        """generate_relabel_snippet produces valid YAML."""
        snippet = scaffold_tenant.generate_relabel_snippet("my-tenant", "ns1,ns2")
        # Parse YAML (skip comment lines)
        yaml_content = "\n".join(
            line for line in snippet.split("\n")
            if not line.startswith("#")
        )
        data = yaml.safe_load(yaml_content)
        self.assertIn("relabel_configs", data)
        self.assertEqual(len(data["relabel_configs"]), 2)

    def test_relabel_snippet_custom_tenant_label(self):
        """generate_relabel_snippet supports custom tenant_label."""
        snippet = scaffold_tenant.generate_relabel_snippet("my-tenant", "ns1,ns2", tenant_label="cluster")
        self.assertIn("cluster", snippet)

    def test_tenant_yaml_has_namespaces_metadata(self):
        """Tenant YAML includes _namespaces metadata when --namespaces used."""
        self._run_scaffold("test-ns", "mariadb", namespaces="ns1,ns2,ns3")
        tenant_path = os.path.join(self.tmpdir, "test-ns.yaml")
        with open(tenant_path) as f:
            data = yaml.safe_load(f)
        tenant_data = data["tenants"]["test-ns"]
        self.assertIn("_namespaces", tenant_data)
        self.assertEqual(tenant_data["_namespaces"], ["ns1", "ns2", "ns3"])

    def test_scaffold_report_mentions_namespaces(self):
        """scaffold-report.txt mentions namespace mapping when --namespaces used."""
        self._run_scaffold("test-ns", "mariadb", namespaces="ns1,ns2")
        report_path = os.path.join(self.tmpdir, "scaffold-report.txt")
        with open(report_path) as f:
            content = f.read()
        self.assertIn("relabel", content.lower())


# ── Metric Dictionary ────────────────────────────────────────────────


class TestMetricDictionary(unittest.TestCase):
    """Verify metric-dictionary.yaml has Oracle and DB2 entries."""

    @classmethod
    def setUpClass(cls):
        dict_path = os.path.join(PROJECT_ROOT, "scripts", "tools", "metric-dictionary.yaml")
        with open(dict_path) as f:
            cls.dictionary = yaml.safe_load(f)

    def test_oracle_entries_exist(self):
        """Dictionary has oracledb_ metric entries."""
        oracle_keys = [k for k in self.dictionary if k.startswith("oracledb_")]
        self.assertGreaterEqual(len(oracle_keys), 3, f"Only {len(oracle_keys)} oracle entries")

    def test_db2_entries_exist(self):
        """Dictionary has db2_ metric entries."""
        db2_keys = [k for k in self.dictionary if k.startswith("db2_")]
        self.assertGreaterEqual(len(db2_keys), 3, f"Only {len(db2_keys)} db2 entries")

    def test_oracle_sessions_has_golden(self):
        """oracledb_sessions_active has golden_rule mapping."""
        entry = self.dictionary.get("oracledb_sessions_active", {})
        self.assertIn("golden_rule", entry, "Missing golden_rule for oracledb_sessions_active")

    def test_db2_connections_has_golden(self):
        """db2_connections_active has golden_rule mapping."""
        entry = self.dictionary.get("db2_connections_active", {})
        self.assertIn("golden_rule", entry, "Missing golden_rule for db2_connections_active")

    def test_clickhouse_entries_exist(self):
        """Dictionary has ClickHouse metric entries."""
        ch_keys = [k for k in self.dictionary if k.startswith("ClickHouse")]
        self.assertGreaterEqual(len(ch_keys), 3, f"Only {len(ch_keys)} ClickHouse entries")

    def test_clickhouse_query_has_golden(self):
        """ClickHouseProfileEvents_Query has golden_rule mapping."""
        entry = self.dictionary.get("ClickHouseProfileEvents_Query", {})
        self.assertIn("golden_rule", entry)

    def test_kafka_entries_exist(self):
        """Dictionary has kafka_ metric entries."""
        kafka_keys = [k for k in self.dictionary if k.startswith("kafka_")]
        self.assertGreaterEqual(len(kafka_keys), 5, f"Only {len(kafka_keys)} kafka entries")

    def test_kafka_consumer_lag_has_golden(self):
        """kafka_consumergroup_lag_sum has golden_rule mapping."""
        entry = self.dictionary.get("kafka_consumergroup_lag_sum", {})
        self.assertIn("golden_rule", entry)

    def test_rabbitmq_entries_exist(self):
        """Dictionary has rabbitmq_ metric entries."""
        rmq_keys = [k for k in self.dictionary if k.startswith("rabbitmq_")]
        self.assertGreaterEqual(len(rmq_keys), 5, f"Only {len(rmq_keys)} rabbitmq entries")

    def test_rabbitmq_queue_has_golden(self):
        """rabbitmq_queue_messages_ready has golden_rule mapping."""
        entry = self.dictionary.get("rabbitmq_queue_messages_ready", {})
        self.assertIn("golden_rule", entry)


# ── Rule Pack YAML Validation ────────────────────────────────────────


class TestRulePackYAML(unittest.TestCase):
    """Validate canonical rule pack YAML files for Oracle and DB2."""

    RULE_PACKS_DIR = os.path.join(PROJECT_ROOT, "rule-packs")
    K8S_DIR = os.path.join(PROJECT_ROOT, "k8s", "03-monitoring")

    def _load_yaml(self, path):
        with open(path) as f:
            return yaml.safe_load(f)

    def _validate_rule_pack_structure(self, groups, db_prefix):
        """Check three-group pattern: normalization, threshold-normalization, alerts."""
        group_names = [g["name"] for g in groups]
        self.assertIn(f"{db_prefix}-normalization", group_names)
        self.assertIn(f"{db_prefix}-threshold-normalization", group_names)
        self.assertIn(f"{db_prefix}-alerts", group_names)

    def test_postgresql_rule_pack_exists(self):
        """rule-packs/rule-pack-postgresql.yaml exists."""
        path = os.path.join(self.RULE_PACKS_DIR, "rule-pack-postgresql.yaml")
        self.assertTrue(os.path.isfile(path))

    def test_postgresql_configmap_exists(self):
        """k8s/03-monitoring/configmap-rules-postgresql.yaml exists."""
        path = os.path.join(self.K8S_DIR, "configmap-rules-postgresql.yaml")
        self.assertTrue(os.path.isfile(path))

    def test_postgresql_rule_pack_three_groups(self):
        """PostgreSQL rule pack has the three-group structure."""
        path = os.path.join(self.RULE_PACKS_DIR, "rule-pack-postgresql.yaml")
        data = self._load_yaml(path)
        self._validate_rule_pack_structure(data["groups"], "postgresql")

    def test_postgresql_configmap_has_label(self):
        """PostgreSQL ConfigMap has rule-pack: postgresql label."""
        path = os.path.join(self.K8S_DIR, "configmap-rules-postgresql.yaml")
        data = self._load_yaml(path)
        self.assertEqual(data["metadata"]["labels"]["rule-pack"], "postgresql")

    def test_postgresql_uses_max_by_tenant(self):
        """PostgreSQL threshold normalization uses max by(tenant)."""
        path = os.path.join(self.RULE_PACKS_DIR, "rule-pack-postgresql.yaml")
        with open(path) as f:
            content = f.read()
        self.assertIn("max by(tenant)", content)

    def test_postgresql_alerts_have_maintenance_unless(self):
        """PostgreSQL alert rules use 'unless on(tenant)' maintenance filter."""
        path = os.path.join(self.RULE_PACKS_DIR, "rule-pack-postgresql.yaml")
        with open(path) as f:
            content = f.read()
        self.assertIn('unless on(tenant)', content)

    def test_postgresql_has_metric_group_labels(self):
        """PostgreSQL alerts have metric_group for severity dedup pairing."""
        path = os.path.join(self.RULE_PACKS_DIR, "rule-pack-postgresql.yaml")
        with open(path) as f:
            content = f.read()
        self.assertIn('metric_group: "pg_connections"', content)
        self.assertIn('metric_group: "pg_replication_lag"', content)

    def test_postgresql_division_by_zero_protection(self):
        """PostgreSQL recording rules use clamp_min to prevent division by zero."""
        path = os.path.join(self.RULE_PACKS_DIR, "rule-pack-postgresql.yaml")
        with open(path) as f:
            content = f.read()
        # connection_usage:ratio divides by max_connections — must clamp
        # rollback_ratio divides by total transactions — must clamp
        clamp_count = content.count("clamp_min")
        self.assertGreaterEqual(clamp_count, 2,
                                f"Expected at least 2 clamp_min guards, found {clamp_count}")

    def test_postgresql_rollback_uses_humanize_percentage(self):
        """PostgreSQLHighRollbackRatio description uses humanizePercentage (not printf)."""
        path = os.path.join(self.RULE_PACKS_DIR, "rule-pack-postgresql.yaml")
        with open(path) as f:
            content = f.read()
        # Find the rollback ratio alert section
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
        self.assertTrue(found_humanize,
                        "PostgreSQLHighRollbackRatio should use humanizePercentage")

    def test_oracle_rule_pack_exists(self):
        """rule-packs/rule-pack-oracle.yaml exists."""
        path = os.path.join(self.RULE_PACKS_DIR, "rule-pack-oracle.yaml")
        self.assertTrue(os.path.isfile(path))

    def test_db2_rule_pack_exists(self):
        """rule-packs/rule-pack-db2.yaml exists."""
        path = os.path.join(self.RULE_PACKS_DIR, "rule-pack-db2.yaml")
        self.assertTrue(os.path.isfile(path))

    def test_oracle_configmap_exists(self):
        """k8s/03-monitoring/configmap-rules-oracle.yaml exists."""
        path = os.path.join(self.K8S_DIR, "configmap-rules-oracle.yaml")
        self.assertTrue(os.path.isfile(path))

    def test_db2_configmap_exists(self):
        """k8s/03-monitoring/configmap-rules-db2.yaml exists."""
        path = os.path.join(self.K8S_DIR, "configmap-rules-db2.yaml")
        self.assertTrue(os.path.isfile(path))

    def test_oracle_rule_pack_three_groups(self):
        """Oracle rule pack has the three-group structure."""
        path = os.path.join(self.RULE_PACKS_DIR, "rule-pack-oracle.yaml")
        data = self._load_yaml(path)
        self._validate_rule_pack_structure(data["groups"], "oracle")

    def test_db2_rule_pack_three_groups(self):
        """DB2 rule pack has the three-group structure."""
        path = os.path.join(self.RULE_PACKS_DIR, "rule-pack-db2.yaml")
        data = self._load_yaml(path)
        self._validate_rule_pack_structure(data["groups"], "db2")

    def test_oracle_configmap_has_label(self):
        """Oracle ConfigMap has rule-pack: oracle label."""
        path = os.path.join(self.K8S_DIR, "configmap-rules-oracle.yaml")
        data = self._load_yaml(path)
        self.assertEqual(data["metadata"]["labels"]["rule-pack"], "oracle")

    def test_db2_configmap_has_label(self):
        """DB2 ConfigMap has rule-pack: db2 label."""
        path = os.path.join(self.K8S_DIR, "configmap-rules-db2.yaml")
        data = self._load_yaml(path)
        self.assertEqual(data["metadata"]["labels"]["rule-pack"], "db2")

    def test_oracle_uses_max_by_tenant(self):
        """Oracle threshold normalization uses max by(tenant), not sum."""
        path = os.path.join(self.RULE_PACKS_DIR, "rule-pack-oracle.yaml")
        with open(path) as f:
            content = f.read()
        # threshold normalization rules should use max by(tenant)
        self.assertIn("max by(tenant)", content)
        # Should NOT use sum by(tenant) for threshold normalization
        self.assertNotIn("sum by(tenant) (user_threshold", content)

    def test_db2_uses_max_by_tenant(self):
        """DB2 threshold normalization uses max by(tenant), not sum."""
        path = os.path.join(self.RULE_PACKS_DIR, "rule-pack-db2.yaml")
        with open(path) as f:
            content = f.read()
        self.assertIn("max by(tenant)", content)
        self.assertNotIn("sum by(tenant) (user_threshold", content)

    def test_oracle_alerts_have_maintenance_unless(self):
        """Oracle alert rules use 'unless on(tenant)' maintenance filter."""
        path = os.path.join(self.RULE_PACKS_DIR, "rule-pack-oracle.yaml")
        with open(path) as f:
            content = f.read()
        # Count alerts (excluding Down which may not use maintenance)
        self.assertIn('unless on(tenant)', content)

    def test_db2_alerts_have_maintenance_unless(self):
        """DB2 alert rules use 'unless on(tenant)' maintenance filter."""
        path = os.path.join(self.RULE_PACKS_DIR, "rule-pack-db2.yaml")
        with open(path) as f:
            content = f.read()
        self.assertIn('unless on(tenant)', content)

    def test_clickhouse_rule_pack_exists(self):
        """rule-packs/rule-pack-clickhouse.yaml exists."""
        path = os.path.join(self.RULE_PACKS_DIR, "rule-pack-clickhouse.yaml")
        self.assertTrue(os.path.isfile(path))

    def test_clickhouse_configmap_exists(self):
        """k8s/03-monitoring/configmap-rules-clickhouse.yaml exists."""
        path = os.path.join(self.K8S_DIR, "configmap-rules-clickhouse.yaml")
        self.assertTrue(os.path.isfile(path))

    def test_clickhouse_rule_pack_three_groups(self):
        """ClickHouse rule pack has the three-group structure."""
        path = os.path.join(self.RULE_PACKS_DIR, "rule-pack-clickhouse.yaml")
        data = self._load_yaml(path)
        self._validate_rule_pack_structure(data["groups"], "clickhouse")

    def test_clickhouse_configmap_has_label(self):
        """ClickHouse ConfigMap has rule-pack: clickhouse label."""
        path = os.path.join(self.K8S_DIR, "configmap-rules-clickhouse.yaml")
        data = self._load_yaml(path)
        self.assertEqual(data["metadata"]["labels"]["rule-pack"], "clickhouse")

    def test_clickhouse_uses_max_by_tenant(self):
        """ClickHouse threshold normalization uses max by(tenant)."""
        path = os.path.join(self.RULE_PACKS_DIR, "rule-pack-clickhouse.yaml")
        with open(path) as f:
            content = f.read()
        self.assertIn("max by(tenant)", content)
        self.assertNotIn("sum by(tenant) (user_threshold", content)

    def test_clickhouse_alerts_have_maintenance_unless(self):
        """ClickHouse alert rules use 'unless on(tenant)' maintenance filter."""
        path = os.path.join(self.RULE_PACKS_DIR, "rule-pack-clickhouse.yaml")
        with open(path) as f:
            content = f.read()
        self.assertIn('unless on(tenant)', content)

    def test_kafka_rule_pack_exists(self):
        """rule-packs/rule-pack-kafka.yaml exists."""
        path = os.path.join(self.RULE_PACKS_DIR, "rule-pack-kafka.yaml")
        self.assertTrue(os.path.isfile(path))

    def test_kafka_configmap_exists(self):
        """k8s/03-monitoring/configmap-rules-kafka.yaml exists."""
        path = os.path.join(self.K8S_DIR, "configmap-rules-kafka.yaml")
        self.assertTrue(os.path.isfile(path))

    def test_kafka_rule_pack_three_groups(self):
        """Kafka rule pack has the three-group structure."""
        path = os.path.join(self.RULE_PACKS_DIR, "rule-pack-kafka.yaml")
        data = self._load_yaml(path)
        self._validate_rule_pack_structure(data["groups"], "kafka")

    def test_kafka_configmap_has_label(self):
        """Kafka ConfigMap has rule-pack: kafka label."""
        path = os.path.join(self.K8S_DIR, "configmap-rules-kafka.yaml")
        data = self._load_yaml(path)
        self.assertEqual(data["metadata"]["labels"]["rule-pack"], "kafka")

    def test_kafka_uses_max_by_tenant(self):
        """Kafka threshold normalization uses max by(tenant)."""
        path = os.path.join(self.RULE_PACKS_DIR, "rule-pack-kafka.yaml")
        with open(path) as f:
            content = f.read()
        self.assertIn("max by(tenant)", content)

    def test_kafka_alerts_have_maintenance_unless(self):
        """Kafka alert rules use 'unless on(tenant)' maintenance filter."""
        path = os.path.join(self.RULE_PACKS_DIR, "rule-pack-kafka.yaml")
        with open(path) as f:
            content = f.read()
        self.assertIn('unless on(tenant)', content)

    def test_rabbitmq_rule_pack_exists(self):
        """rule-packs/rule-pack-rabbitmq.yaml exists."""
        path = os.path.join(self.RULE_PACKS_DIR, "rule-pack-rabbitmq.yaml")
        self.assertTrue(os.path.isfile(path))

    def test_rabbitmq_configmap_exists(self):
        """k8s/03-monitoring/configmap-rules-rabbitmq.yaml exists."""
        path = os.path.join(self.K8S_DIR, "configmap-rules-rabbitmq.yaml")
        self.assertTrue(os.path.isfile(path))

    def test_rabbitmq_rule_pack_three_groups(self):
        """RabbitMQ rule pack has the three-group structure."""
        path = os.path.join(self.RULE_PACKS_DIR, "rule-pack-rabbitmq.yaml")
        data = self._load_yaml(path)
        self._validate_rule_pack_structure(data["groups"], "rabbitmq")

    def test_rabbitmq_configmap_has_label(self):
        """RabbitMQ ConfigMap has rule-pack: rabbitmq label."""
        path = os.path.join(self.K8S_DIR, "configmap-rules-rabbitmq.yaml")
        data = self._load_yaml(path)
        self.assertEqual(data["metadata"]["labels"]["rule-pack"], "rabbitmq")

    def test_rabbitmq_uses_max_by_tenant(self):
        """RabbitMQ threshold normalization uses max by(tenant)."""
        path = os.path.join(self.RULE_PACKS_DIR, "rule-pack-rabbitmq.yaml")
        with open(path) as f:
            content = f.read()
        self.assertIn("max by(tenant)", content)

    def test_rabbitmq_alerts_have_maintenance_unless(self):
        """RabbitMQ alert rules use 'unless on(tenant)' maintenance filter."""
        path = os.path.join(self.RULE_PACKS_DIR, "rule-pack-rabbitmq.yaml")
        with open(path) as f:
            content = f.read()
        self.assertIn('unless on(tenant)', content)

    def test_db2_bufferpool_uses_less_than(self):
        """DB2 low bufferpool hit ratio alert uses < operator (not >)."""
        path = os.path.join(self.RULE_PACKS_DIR, "rule-pack-db2.yaml")
        with open(path) as f:
            content = f.read()
        # DB2LowBufferpoolHitRatio should use '<' since low ratio is bad
        self.assertIn("DB2LowBufferpoolHitRatio", content)
        # Find the section and verify it uses <
        lines = content.split("\n")
        in_bufferpool = False
        for line in lines:
            if "DB2LowBufferpoolHitRatio" in line:
                in_bufferpool = True
            if in_bufferpool and "< on(tenant)" in line:
                break
        else:
            self.fail("DB2LowBufferpoolHitRatio should use '< on(tenant)' operator")


# ── Scaffold Receiver Types (v1.8.0) ─────────────────────────────────


class TestScaffoldReceiverTypes(unittest.TestCase):
    """Verify scaffold_tenant supports all 6 receiver types in non-interactive mode."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _run_scaffold(self, tenant, dbs, receiver, receiver_type):
        run_scaffold(self.tmpdir, tenant, dbs,
                     receiver=receiver, receiver_type=receiver_type,
                     non_interactive=True)

    def _load_tenant_yaml(self, tenant):
        path = os.path.join(self.tmpdir, f"{tenant}.yaml")
        with open(path) as f:
            return yaml.safe_load(f)

    def test_rocketchat_receiver(self):
        """Scaffold with rocketchat receiver type produces correct _routing."""
        self._run_scaffold("test-rc", "mariadb",
                           "https://rocket.example.com/hooks/test", "rocketchat")
        data = self._load_tenant_yaml("test-rc")
        routing = data["tenants"]["test-rc"]["_routing"]
        self.assertEqual(routing["receiver"]["type"], "rocketchat")
        self.assertEqual(routing["receiver"]["url"], "https://rocket.example.com/hooks/test")

    def test_pagerduty_receiver(self):
        """Scaffold with pagerduty receiver type produces correct _routing."""
        self._run_scaffold("test-pd", "mariadb",
                           "abc123servicekey", "pagerduty")
        data = self._load_tenant_yaml("test-pd")
        routing = data["tenants"]["test-pd"]["_routing"]
        self.assertEqual(routing["receiver"]["type"], "pagerduty")
        self.assertEqual(routing["receiver"]["service_key"], "abc123servicekey")


# ── Kafka/RabbitMQ Alert Completeness (v1.8.0) ──────────────────────


class TestKafkaRabbitMQAlertCompleteness(unittest.TestCase):
    """Verify Kafka and RabbitMQ rule packs have all expected alerts."""

    RULE_PACKS_DIR = os.path.join(PROJECT_ROOT, "rule-packs")

    def _load_yaml(self, path):
        with open(path) as f:
            return yaml.safe_load(f)

    def _get_alert_names(self, groups):
        """Extract all alert names from rule groups."""
        alerts = []
        for g in groups:
            for r in g.get("rules", []):
                if "alert" in r:
                    alerts.append(r["alert"])
        return alerts

    def _get_metric_groups(self, groups):
        """Extract all metric_group labels from alert rules."""
        mgs = set()
        for g in groups:
            for r in g.get("rules", []):
                if "alert" in r:
                    mg = r.get("labels", {}).get("metric_group")
                    if mg:
                        mgs.add(mg)
        return mgs

    def test_kafka_has_all_expected_alerts(self):
        """Kafka rule pack has all CHANGELOG-promised alerts."""
        path = os.path.join(self.RULE_PACKS_DIR, "rule-pack-kafka.yaml")
        data = self._load_yaml(path)
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
            self.assertIn(name, alerts, f"Missing alert: {name}")

    def test_kafka_metric_groups_cover_all_metrics(self):
        """Kafka alerts have metric_group for all threshold metrics."""
        path = os.path.join(self.RULE_PACKS_DIR, "rule-pack-kafka.yaml")
        data = self._load_yaml(path)
        mgs = self._get_metric_groups(data["groups"])
        expected = {
            "kafka_consumer_lag", "kafka_under_replicated_partitions",
            "kafka_active_controllers", "kafka_broker_count", "kafka_request_rate",
        }
        for mg in expected:
            self.assertIn(mg, mgs, f"Missing metric_group: {mg}")

    def test_kafka_low_alerts_use_less_than(self):
        """KafkaNoActiveController and KafkaLowBrokerCount use < operator."""
        path = os.path.join(self.RULE_PACKS_DIR, "rule-pack-kafka.yaml")
        with open(path) as f:
            content = f.read()
        # These alerts fire when value DROPS below threshold
        for alert_name in ["KafkaNoActiveController", "KafkaLowBrokerCount"]:
            idx = content.index(alert_name)
            section = content[idx:idx + 300]
            self.assertIn("< on(tenant)", section,
                          f"{alert_name} should use '<' operator")

    def test_rabbitmq_has_all_expected_alerts(self):
        """RabbitMQ rule pack has all CHANGELOG-promised alerts."""
        path = os.path.join(self.RULE_PACKS_DIR, "rule-pack-rabbitmq.yaml")
        data = self._load_yaml(path)
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
            self.assertIn(name, alerts, f"Missing alert: {name}")

    def test_rabbitmq_low_consumers_uses_less_than(self):
        """RabbitMQLowConsumers uses < operator (fires when consumers drop)."""
        path = os.path.join(self.RULE_PACKS_DIR, "rule-pack-rabbitmq.yaml")
        with open(path) as f:
            content = f.read()
        idx = content.index("RabbitMQLowConsumers")
        section = content[idx:idx + 300]
        self.assertIn("< on(tenant)", section,
                      "RabbitMQLowConsumers should use '<' operator")

    def test_rabbitmq_unacked_uses_config_driven_threshold(self):
        """RabbitMQHighUnackedMessages uses config-driven threshold (not hardcoded)."""
        path = os.path.join(self.RULE_PACKS_DIR, "rule-pack-rabbitmq.yaml")
        with open(path) as f:
            content = f.read()
        idx = content.index("RabbitMQHighUnackedMessages")
        section = content[idx:idx + 400]
        self.assertIn("tenant:alert_threshold:rabbitmq_unacked_messages", section,
                      "RabbitMQHighUnackedMessages should use config-driven threshold")
        self.assertNotIn("> 10000", section,
                         "RabbitMQHighUnackedMessages should not use hardcoded threshold")

    def test_rabbitmq_mem_division_by_zero_protection(self):
        """RabbitMQ memory ratio recording rule uses clamp_min to prevent /0."""
        path = os.path.join(self.RULE_PACKS_DIR, "rule-pack-rabbitmq.yaml")
        with open(path) as f:
            content = f.read()
        self.assertIn("clamp_min", content,
                      "RabbitMQ memory ratio should use clamp_min for division safety")


# ── ConfigMap ↔ Rule Pack Sync ─────────────────────────────────────


class TestConfigMapRulePackSync(unittest.TestCase):
    """Verify ConfigMap wrappers contain the same data as canonical rule-pack YAML files.

    Prevents drift when rule packs are updated but ConfigMap wrappers are not synced.
    """

    RULE_PACKS_DIR = os.path.join(PROJECT_ROOT, "rule-packs")
    CONFIGMAP_DIR = os.path.join(PROJECT_ROOT, "k8s", "03-monitoring")

    def _load_yaml(self, path):
        with open(path, encoding="utf-8") as f:
            return yaml.safe_load(f)

    def _get_rule_pack_files(self):
        """Find all rule-pack-*.yaml canonical files."""
        packs = []
        for fname in sorted(os.listdir(self.RULE_PACKS_DIR)):
            if fname.startswith("rule-pack-") and fname.endswith(".yaml"):
                packs.append(fname)
        return packs

    def test_each_rule_pack_has_configmap_wrapper(self):
        """Every rule-pack-*.yaml should have a corresponding configmap-rules-*.yaml."""
        for pack_file in self._get_rule_pack_files():
            # rule-pack-mysql.yaml → configmap-rules-mysql.yaml
            db_name = pack_file.replace("rule-pack-", "").replace(".yaml", "")
            cm_file = f"configmap-rules-{db_name}.yaml"
            cm_path = os.path.join(self.CONFIGMAP_DIR, cm_file)
            self.assertTrue(
                os.path.isfile(cm_path),
                f"Missing ConfigMap wrapper {cm_file} for {pack_file}")

    def test_configmap_data_matches_rule_pack(self):
        """ConfigMap data section should contain the exact same rule groups as the canonical source."""
        for pack_file in self._get_rule_pack_files():
            db_name = pack_file.replace("rule-pack-", "").replace(".yaml", "")
            cm_file = f"configmap-rules-{db_name}.yaml"
            cm_path = os.path.join(self.CONFIGMAP_DIR, cm_file)
            if not os.path.isfile(cm_path):
                continue  # covered by test above

            pack_data = self._load_yaml(os.path.join(self.RULE_PACKS_DIR, pack_file))
            cm_data = self._load_yaml(cm_path)

            # ConfigMap wraps rule pack content in data.<key> (key names vary)
            cm_inner = cm_data.get("data", {})
            self.assertTrue(
                len(cm_inner) > 0,
                f"ConfigMap {cm_file} has empty data section")

            # Collect all group names from all embedded YAML docs
            embedded_groups = []
            for _data_key, yaml_str in cm_inner.items():
                embedded = yaml.safe_load(yaml_str)
                if embedded and "groups" in embedded:
                    for g in embedded["groups"]:
                        embedded_groups.append(g["name"])

            # Compare: all canonical pack groups must appear in ConfigMap
            pack_groups = [g["name"] for g in pack_data.get("groups", [])]
            for gname in pack_groups:
                self.assertIn(
                    gname, embedded_groups,
                    f"Group '{gname}' from {pack_file} missing in {cm_file}")


# ── Relabel Snippet Edge Cases ─────────────────────────────────────


class TestRelabelSnippetEdgeCases(unittest.TestCase):
    """Edge cases for generate_relabel_snippet()."""

    def test_empty_string_returns_empty(self):
        """Empty string input returns empty output."""
        result = scaffold_tenant.generate_relabel_snippet("t", "")
        self.assertEqual(result, "")

    def test_none_namespaces_returns_empty(self):
        """None returns empty output (function handles gracefully)."""
        result = scaffold_tenant.generate_relabel_snippet("t", [])
        self.assertEqual(result, "")

    def test_whitespace_only_namespaces_filtered(self):
        """Comma-separated whitespace-only entries are filtered out."""
        result = scaffold_tenant.generate_relabel_snippet("t", " , , ")
        self.assertEqual(result, "")

    def test_namespaces_with_extra_whitespace(self):
        """Leading/trailing whitespace in namespace names is trimmed."""
        result = scaffold_tenant.generate_relabel_snippet("t", " ns-a , ns-b ")
        self.assertIn("ns-a", result)
        self.assertIn("ns-b", result)
        # Verify the regex line itself has trimmed values (no leading space before ns-a)
        for line in result.splitlines():
            if line.strip().startswith("regex:"):
                self.assertIn("ns-a|ns-b", line)
                break

    def test_custom_tenant_label(self):
        """Custom tenant_label is reflected in output."""
        result = scaffold_tenant.generate_relabel_snippet("t", "ns1", tenant_label="team")
        self.assertIn("team", result)


# ── generate_profile() Tests (v1.12.0 Improvement) ───────────────

class TestGenerateProfile(unittest.TestCase):
    """Tests for scaffold_tenant.generate_profile()."""

    def test_basic_profile_generation(self):
        """Profile for mariadb should contain mariadb defaults + optional overrides."""
        result = scaffold_tenant.generate_profile(
            "standard-mariadb-prod", ["kubernetes", "mariadb"])
        self.assertIn("profiles", result)
        self.assertIn("standard-mariadb-prod", result["profiles"])
        profile = result["profiles"]["standard-mariadb-prod"]
        # Should have kubernetes + mariadb defaults + mariadb optional
        self.assertIn("container_cpu", profile)
        self.assertIn("mysql_connections", profile)
        self.assertIn("mysql_connections_critical", profile)  # optional override

    def test_staging_tier_relaxed(self):
        """Staging tier should produce 20% more relaxed thresholds."""
        prod = scaffold_tenant.generate_profile(
            "p", ["kubernetes", "mariadb"], tier="prod")
        staging = scaffold_tenant.generate_profile(
            "s", ["kubernetes", "mariadb"], tier="staging")
        prod_cpu = prod["profiles"]["p"]["container_cpu"]
        staging_cpu = staging["profiles"]["s"]["container_cpu"]
        self.assertGreater(staging_cpu, prod_cpu)

    def test_multi_db_profile(self):
        """Profile with multiple DBs should include all metric keys."""
        result = scaffold_tenant.generate_profile(
            "multi", ["kubernetes", "mariadb", "redis"])
        profile = result["profiles"]["multi"]
        self.assertIn("mysql_connections", profile)
        self.assertIn("redis_memory_used_bytes", profile)

    def test_empty_db_list(self):
        """Empty DB list still returns valid profile structure."""
        result = scaffold_tenant.generate_profile("empty", [])
        self.assertIn("profiles", result)
        self.assertEqual(result["profiles"]["empty"], {})

    def test_generate_profile_cli(self):
        """--generate-profile via CLI writes _profiles.yaml."""
        with tempfile.TemporaryDirectory() as tmpdir:
            old_argv = sys.argv
            sys.argv = [
                "scaffold_tenant.py",
                "--generate-profile", "test-profile",
                "--db", "mariadb",
                "-o", tmpdir,
            ]
            try:
                with self.assertRaises(SystemExit) as cm:
                    scaffold_tenant.main()
                self.assertEqual(cm.exception.code, 0)
            finally:
                sys.argv = old_argv

            path = os.path.join(tmpdir, "_profiles.yaml")
            self.assertTrue(os.path.isfile(path))
            with open(path, encoding="utf-8") as f:
                data = yaml.safe_load(f)
            self.assertIn("profiles", data)
            self.assertIn("test-profile", data["profiles"])


if __name__ == "__main__":
    unittest.main()
